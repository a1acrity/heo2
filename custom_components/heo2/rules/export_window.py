"""ExportWindowRule -- drain battery during top-ranked export windows."""

from __future__ import annotations

from ..const import (
    DEFAULT_HIGH_SOC_THRESHOLD,
    DEFAULT_IGO_OFF_PEAK_PENCE,
    DEFAULT_LOW_SOC_THRESHOLD,
    DEFAULT_SELL_TOP_PCT,
    DEFAULT_SELL_TOP_PCT_HIGH_SOC,
    DEFAULT_SELL_TOP_PCT_LOW_SOC,
    ROUND_TRIP_EFFICIENCY,
)
from ..models import ProgrammeInputs
from ..rank_pricing import (
    filter_today,
    hours_covered_by,
    select_export_top_pct,
    select_worth_selling_windows,
)
from ..rule_engine import PRIO_EXPORT_WINDOW, Rule


def _slot_view_covers_rate(slot_view, rate, tz):
    """True iff the programme `slot_view` (local time-of-day) covers
    the `rate.start` (UTC absolute datetime) in local time."""
    if tz is not None and rate.start.tzinfo is not None:
        local_t = rate.start.astimezone(tz).time()
    else:
        local_t = rate.start.time()
    return slot_view.contains_time(local_t)


class ExportWindowRule(Rule):
    """Drain the battery during the top-ranked export windows of today.

    Implements SPEC §5a:
      - Pick top-N% of today's export rates by p/kWh, where N depends on
        SOC + tomorrow forecast (15 / 30 / 50).
      - Filter to windows that are *worth* selling in: rate * round-trip
        efficiency > replacement cost (next IGO off-peak, ~5p).
      - Drain to `min_soc` during those windows. EveningProtect raises
        the floor if evening demand requires it; SafetyRule clamps to
        min_soc finally.

    Replaces the legacy fixed-threshold version (>7.86p effective stored
    cost) which broke whenever Agile distributions shifted seasonally.
    """

    name = "export_window"
    description = "Drain battery during top-ranked Agile Outgoing windows"
    priority_class = PRIO_EXPORT_WINDOW

    def __init__(
        self,
        *,
        replacement_cost_pence: float = DEFAULT_IGO_OFF_PEAK_PENCE,
        round_trip_efficiency: float = ROUND_TRIP_EFFICIENCY,
        low_soc_threshold: float = DEFAULT_LOW_SOC_THRESHOLD,
        high_soc_threshold: float = DEFAULT_HIGH_SOC_THRESHOLD,
        n_low: int = DEFAULT_SELL_TOP_PCT_LOW_SOC,
        n_med: int = DEFAULT_SELL_TOP_PCT,
        n_high: int = DEFAULT_SELL_TOP_PCT_HIGH_SOC,
    ):
        self.replacement_cost_pence = replacement_cost_pence
        self.round_trip_efficiency = round_trip_efficiency
        self.low_soc_threshold = low_soc_threshold
        self.high_soc_threshold = high_soc_threshold
        self.n_low = n_low
        self.n_med = n_med
        self.n_high = n_high

    def propose(self, view, inputs: ProgrammeInputs) -> None:
        if not inputs.export_rates:
            return

        today_rates = filter_today(inputs.export_rates, inputs.now)
        if not today_rates:
            return

        daily_load_kwh = sum(inputs.load_forecast_kwh) or 1.0
        tomorrow_solar_kwh = sum(inputs.solar_forecast_kwh_tomorrow) if (
            inputs.solar_forecast_kwh_tomorrow
        ) else sum(inputs.solar_forecast_kwh)

        n_pct, n_reason = select_export_top_pct(
            current_soc=inputs.current_soc,
            tomorrow_solar_kwh=tomorrow_solar_kwh,
            daily_load_kwh=daily_load_kwh,
            low_soc_threshold=self.low_soc_threshold,
            high_soc_threshold=self.high_soc_threshold,
            n_low=self.n_low,
            n_med=self.n_med,
            n_high=self.n_high,
        )

        worth_windows = select_worth_selling_windows(
            today_rates,
            n_pct=n_pct,
            replacement_cost_pence=self.replacement_cost_pence,
            round_trip_efficiency=self.round_trip_efficiency,
        )

        if not worth_windows:
            view.log(
                f"ExportWindow: nothing worth selling in {n_reason} "
                f"(replacement cost {self.replacement_cost_pence:.2f}p)"
            )
            return

        tz = inputs.local_tz or inputs.now.tzinfo
        worth_hours = hours_covered_by(worth_windows, tz=tz)

        # Per SPEC §5 priority 1 (avoid peak import) DOMINATES priority 3
        # (sell during top windows). A naive drain-to-min_soc could leave
        # the battery empty going into the 18:30-23:30 evening window
        # and force grid imports at peak rates. Floor the drain target
        # at min_soc + (evening_demand / capacity).
        evening_demand_kwh = inputs.load_kwh_between(18, 24)
        if inputs.battery_capacity_kwh > 0:
            evening_floor_soc = int(
                inputs.min_soc
                + (evening_demand_kwh / inputs.battery_capacity_kwh * 100)
            )
        else:
            evening_floor_soc = int(inputs.min_soc)
        evening_floor_soc = min(evening_floor_soc, 100)
        drain_target = max(evening_floor_soc, int(inputs.min_soc))

        modified = False
        for slot in view.slots:
            if slot.grid_charge:
                continue
            covers_any = any(
                _slot_view_covers_rate(slot, r, tz) for r in worth_windows
            )
            if covers_any and slot.capacity_soc > drain_target:
                view.claim_slot(
                    slot.index, "capacity_soc", drain_target,
                    reason=f"drain during worth-selling slot",
                )
                modified = True

        # NOTE: setting slot.capacity_soc=N alone authorises the
        # battery to drain to N - it doesn't command export to grid.
        # work_mode + max_discharge_a are managed by
        # `PeakExportArbitrageRule` further down the chain, sized to
        # actual spare and only during the day's TOP-priced window(s).

        sorted_hours = sorted(worth_hours)
        rate_summary = (
            f"{worth_windows[0].rate_pence:.2f}-"
            f"{worth_windows[-1].rate_pence:.2f}p"
            if len(worth_windows) > 1
            else f"{worth_windows[0].rate_pence:.2f}p"
        )

        if modified:
            view.log(
                f"ExportWindow: drain to {drain_target}% in {n_reason}, "
                f"{len(worth_windows)} slots @ {rate_summary} "
                f"covering hours {sorted_hours} "
                f"(evening floor from {evening_demand_kwh:.1f} kWh demand)"
            )
        else:
            view.log(
                f"ExportWindow: {n_reason}, {len(worth_windows)} worth-selling "
                f"slots @ {rate_summary} but no slot SOC needed lowering"
            )
