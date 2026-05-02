# custom_components/heo2/projection.py
"""Day-ahead financial projection of a programme. No Home Assistant imports.

Forward-simulates SOC and money flow over the next 24 hours given the
6-slot programme and the inputs HEO II already has (rates, solar/load
forecasts). Produces a one-line summary in the form required by SPEC §6:

    Expected return today: +£X.YZ - sells N kWh @ avg Mp,
    grid imports P kWh @ avg Qp, ZERO peak-rate import

The simulation operates at 30-minute resolution (matching Octopus Agile
slot granularity) and uses the active programme slot at each step to
decide:

  * `grid_charge=True`: import at the slot's import rate to push SOC
    toward `capacity_soc`, capped by `max_charge_kw`.
  * `grid_charge=False` and SOC > `capacity_soc` and slot rate is in the
    top-N% export window: discharge to the grid, capped by
    `max_discharge_kw`.
  * Otherwise: cover load from solar + battery, importing only when the
    battery hits `min_soc`. Import that hits a peak-rate slot is logged
    as `peak_import_kwh` so SPEC H1 violations are visible even though
    H1 itself permits "reality wins" (a reject would be inappropriate).

Pure data; the validator and dashboard sensor consume it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

from .models import ProgrammeInputs, ProgrammeState, RateSlot, SlotConfig


_SLOT_MINUTES = 30
_SLOT_HOURS = _SLOT_MINUTES / 60.0


@dataclass
class Projection:
    """Forward-simulated financial outcome of a programme over 24h."""

    expected_return_pence: float = 0.0
    sells_kwh: float = 0.0
    sells_pence: float = 0.0
    imports_kwh: float = 0.0
    imports_pence: float = 0.0
    peak_import_kwh: float = 0.0
    peak_import_pence: float = 0.0

    @property
    def sells_avg_pence(self) -> float | None:
        if self.sells_kwh <= 0:
            return None
        return self.sells_pence / self.sells_kwh

    @property
    def imports_avg_pence(self) -> float | None:
        if self.imports_kwh <= 0:
            return None
        return self.imports_pence / self.imports_kwh

    def summary(self) -> str:
        """One-line projection per SPEC §6.

        Format:
            Expected return today: +£X.YZ - sells N kWh @ avg Mp,
            grid imports P kWh @ avg Qp, ZERO peak-rate import

        The `+£X.YZ` carries a sign so a net-cost day reads `-£0.45`.
        Average prices are omitted when the corresponding kWh is zero.
        """
        sign = "+" if self.expected_return_pence >= 0 else "-"
        pounds = abs(self.expected_return_pence) / 100.0

        sells_avg = self.sells_avg_pence
        imp_avg = self.imports_avg_pence
        sells_part = (
            f"sells {self.sells_kwh:.1f} kWh @ avg {sells_avg:.1f}p"
            if sells_avg is not None
            else f"sells 0 kWh"
        )
        imp_part = (
            f"grid imports {self.imports_kwh:.1f} kWh @ avg {imp_avg:.1f}p"
            if imp_avg is not None
            else f"grid imports 0 kWh"
        )
        peak_part = (
            "ZERO peak-rate import"
            if self.peak_import_kwh <= 0.001
            else f"{self.peak_import_kwh:.2f} kWh peak-rate import"
        )
        return (
            f"Expected return today: {sign}£{pounds:.2f} - "
            f"{sells_part}, {imp_part}, {peak_part}"
        )


def _slot_at(slots: list[SlotConfig], local_clock: time) -> SlotConfig:
    """Return the programme slot whose time window contains `local_clock`."""
    for slot in slots:
        if slot.contains_time(local_clock):
            return slot
    # Fallback: shouldn't happen for a contiguous 6-slot programme that
    # covers 00:00-00:00, but pick the first slot defensively.
    return slots[0]


def _rate_at(rates: list[RateSlot], at: datetime) -> RateSlot | None:
    for r in rates:
        if r.start <= at < r.end:
            return r
    return None


def _top_export_window_starts(
    today_export_rates: list[RateSlot], top_n_pct: int,
) -> set[datetime]:
    """Precompute the set of `start` datetimes for the top-N% export
    rate slots.

    Tie handling: a ranked slice keeps the first N after sorting by
    rate desc - so two 25p slots in a sea of 5p both qualify, but a
    sea of 5p with `top_n_pct=10` does NOT then qualify any extra
    5p slots beyond the cut. This matches `rank_pricing.top_n_pct`.
    """
    if not today_export_rates or top_n_pct <= 0:
        return set()
    import math
    count = max(1, math.ceil(len(today_export_rates) * top_n_pct / 100))
    ordered = sorted(
        today_export_rates, key=lambda r: r.rate_pence, reverse=True,
    )
    return {r.start for r in ordered[:count]}


def project_day(
    programme: ProgrammeState,
    inputs: ProgrammeInputs,
    *,
    battery_capacity_kwh: float,
    max_charge_kw: float = 5.0,
    max_discharge_kw: float = 5.0,
    charge_efficiency: float = 0.95,
    discharge_efficiency: float = 0.95,
    peak_threshold_p: float = 24.0,
    export_top_pct: int = 30,
    replacement_cost_p: float = 4.95,
    horizon_hours: int = 24,
    tz: ZoneInfo | None = None,
) -> Projection:
    """Forward-simulate the next `horizon_hours` and return a Projection.

    Uses 30-min steps. Each step:
      1. Resolve the programme slot active at the step's start time.
      2. Look up import + export rates at that step.
      3. If `grid_charge=True`: charge from the grid toward `capacity_soc`
         (clamped by `max_charge_kw * step_hours`).
      4. Else if export rate is in the top-N% of today's export rates and
         SOC > capacity_soc: sell up to `max_discharge_kw * step_hours`
         from battery, no battery use beyond `min_soc`.
      5. Else: cover (load - solar) from battery; if battery hits
         `min_soc`, import from grid for the shortfall.

    Returns financial metrics plus peak_import_kwh (any forced import
    that landed on a slot with rate >= peak_threshold_p).
    """
    soc = inputs.current_soc
    min_soc = inputs.min_soc
    p = Projection()

    today_export_rates = inputs.export_rates  # already filtered to "now+24h"
    top_export_starts = _top_export_window_starts(
        today_export_rates, export_top_pct,
    )

    now = inputs.now
    step_count = int(horizon_hours * 60 / _SLOT_MINUTES)

    for step in range(step_count):
        step_start = now + timedelta(minutes=_SLOT_MINUTES * step)
        step_end = step_start + timedelta(minutes=_SLOT_MINUTES)

        # Programme slots are local time-of-day; solar/load forecasts
        # are 24-element arrays indexed by LOCAL hour. Project the UTC
        # step_start onto local time before looking either up.
        if tz is not None and step_start.tzinfo is not None:
            step_local = step_start.astimezone(tz)
        else:
            step_local = step_start
        local_clock = step_local.time()
        slot = _slot_at(programme.slots, local_clock)

        # Import / export rates at this 30-min step. Either may be missing
        # past BD's horizon; treat None as zero contribution to revenue
        # (we still simulate behaviour, just don't book money). Rate
        # lookup uses the absolute UTC datetime, since RateSlots are
        # tz-aware.
        imp = _rate_at(inputs.import_rates, step_start)
        exp = _rate_at(inputs.export_rates, step_start)
        imp_p = imp.rate_pence if imp is not None else 0.0
        exp_p = exp.rate_pence if exp is not None else 0.0

        # Solar / load at this step. Forecasts are hourly; pro-rate to 30 min.
        hour_idx = step_local.hour
        solar_kwh = inputs.solar_forecast_kwh[hour_idx] * _SLOT_HOURS
        load_kwh = inputs.load_forecast_kwh[hour_idx] * _SLOT_HOURS

        net_solar = solar_kwh - load_kwh

        if slot.grid_charge:
            # Charge from grid + use solar surplus toward capacity_soc.
            target_kwh = (slot.capacity_soc - soc) / 100.0 * battery_capacity_kwh
            grid_kwh = max(
                0.0, min(target_kwh, max_charge_kw * _SLOT_HOURS),
            )
            soc += (grid_kwh * charge_efficiency) / battery_capacity_kwh * 100.0

            p.imports_kwh += grid_kwh
            p.imports_pence += grid_kwh * imp_p
            if imp_p >= peak_threshold_p:
                p.peak_import_kwh += grid_kwh
                p.peak_import_pence += grid_kwh * imp_p

            # Solar surplus also charges battery (free) up to 100%
            if net_solar > 0:
                soc += min(
                    net_solar * charge_efficiency, max_charge_kw * _SLOT_HOURS,
                ) / battery_capacity_kwh * 100.0

        else:
            # Sell only when the slot is in the top-N% AND the rate
            # actually pays back the replacement cost (matches the rule
            # engine's `is_worth_selling`). Without this floor, top-N%
            # in a flat-rate day would erroneously sell at a loss.
            in_top_export = (
                exp is not None
                and exp.start in top_export_starts
                and exp_p * charge_efficiency * discharge_efficiency
                > replacement_cost_p
            )
            headroom_kwh = (soc - max(min_soc, slot.capacity_soc)) / 100.0 * battery_capacity_kwh

            if in_top_export and headroom_kwh > 0:
                # Sell battery into top-N% export window.
                sell_kwh = min(headroom_kwh, max_discharge_kw * _SLOT_HOURS)
                soc -= (sell_kwh / discharge_efficiency) / battery_capacity_kwh * 100.0
                p.sells_kwh += sell_kwh
                p.sells_pence += sell_kwh * exp_p
            else:
                # Hold/cover-load mode. Solar covers load first; any
                # shortfall comes from battery; if battery at min_soc,
                # import from grid.
                if net_solar >= 0:
                    # Surplus solar charges battery
                    soc += min(
                        net_solar * charge_efficiency,
                        max_charge_kw * _SLOT_HOURS,
                    ) / battery_capacity_kwh * 100.0
                else:
                    deficit_kwh = -net_solar
                    available_kwh = (soc - min_soc) / 100.0 * battery_capacity_kwh
                    from_battery = min(deficit_kwh, available_kwh)
                    from_grid = deficit_kwh - from_battery
                    soc -= (from_battery / discharge_efficiency) / battery_capacity_kwh * 100.0
                    if from_grid > 0:
                        p.imports_kwh += from_grid
                        p.imports_pence += from_grid * imp_p
                        if imp_p >= peak_threshold_p:
                            p.peak_import_kwh += from_grid
                            p.peak_import_pence += from_grid * imp_p

        soc = max(min_soc, min(100.0, soc))

    p.expected_return_pence = p.sells_pence - p.imports_pence
    return p
