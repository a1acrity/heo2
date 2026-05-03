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
from ..models import ProgrammeInputs, ProgrammeState
from ..rank_pricing import (
    filter_today,
    hours_covered_by,
    select_export_top_pct,
    select_worth_selling_windows,
)
from ..rule_engine import Rule


def _slot_covers_rate(slot, rate, tz):
    """True iff the programme `slot` (local time-of-day) covers the
    `rate.start` (UTC absolute datetime) in local time. HEO-7 fix:
    pre-2026-05 ExportWindowRule built `slot_hours = set(range(start.hour,
    end.hour))` and matched against `rate.start.hour`. That dropped
    half-hour Agile slots crossing the hour and entirely missed slots
    like 16:30-17:00 against a programme slot of 17:00-19:00. Working
    in absolute time (rate.start in local tz, against slot.start_time
    / slot.end_time) catches the overlap without rounding.
    """
    if tz is not None and rate.start.tzinfo is not None:
        local_t = rate.start.astimezone(tz).time()
    else:
        local_t = rate.start.time()
    return slot.contains_time(local_t)


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

    def apply(self, state: ProgrammeState, inputs: ProgrammeInputs) -> ProgrammeState:
        if not inputs.export_rates:
            return state

        # Today only. Tomorrow's rates are out of scope for the drain
        # decision in the current programme.
        today_rates = filter_today(inputs.export_rates, inputs.now)
        if not today_rates:
            return state

        daily_load_kwh = sum(inputs.load_forecast_kwh) or 1.0
        # Tomorrow's solar feeds the rank decision (high-SOC + high-solar
        # -> sell aggressively; low-solar -> conservative). Falls back to
        # today's forecast when tomorrow isn't available so the rule
        # still produces a reasonable answer.
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
            state.reason_log.append(
                f"ExportWindow: nothing worth selling in {n_reason} "
                f"(replacement cost {self.replacement_cost_pence:.2f}p)"
            )
            return state

        # HEO-7: local-tz aware slot matching. Use inputs.local_tz
        # (set by the coordinator post-PR2) so DST is respected;
        # fall back to inputs.now.tzinfo for tests that don't set it.
        tz = inputs.local_tz or inputs.now.tzinfo
        # Kept for the diagnostic log line below; the actual slot
        # selection uses _slot_covers_rate which is sub-hour accurate.
        worth_hours = hours_covered_by(worth_windows, tz=tz)

        # Per SPEC §5 priority 1 (avoid peak import) DOMINATES priority 3
        # (sell during top windows). A naive drain-to-min_soc could leave
        # the battery empty going into the 18:30-23:30 evening window
        # and force grid imports at peak rates. So the drain target is
        # floored at min_soc + (evening_demand / capacity), matching the
        # legacy rule's behaviour. EveningProtectRule (next in the chain)
        # only protects pre-evening slots; this floor handles overlap
        # cases like a 12:00-18:30 day slot that EveningProtect skips.
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
        for slot in state.slots:
            if slot.grid_charge:
                continue
            # Slot drains only if at least one worth-selling rate slot
            # falls within its time window. Sub-hour accurate via
            # `_slot_covers_rate`, unlike the pre-HEO-7 hour-set
            # intersection.
            covers_any = any(
                _slot_covers_rate(slot, r, tz) for r in worth_windows
            )
            if covers_any and slot.capacity_soc > drain_target:
                slot.capacity_soc = drain_target
                modified = True

        sorted_hours = sorted(worth_hours)
        rate_summary = (
            f"{worth_windows[0].rate_pence:.2f}-"
            f"{worth_windows[-1].rate_pence:.2f}p"
            if len(worth_windows) > 1
            else f"{worth_windows[0].rate_pence:.2f}p"
        )

        if modified:
            state.reason_log.append(
                f"ExportWindow: drain to {drain_target}% in {n_reason}, "
                f"{len(worth_windows)} slots @ {rate_summary} "
                f"covering hours {sorted_hours} "
                f"(evening floor from {evening_demand_kwh:.1f} kWh demand)"
            )
        else:
            state.reason_log.append(
                f"ExportWindow: {n_reason}, {len(worth_windows)} worth-selling "
                f"slots @ {rate_summary} but no slot SOC needed lowering"
            )
        return state
