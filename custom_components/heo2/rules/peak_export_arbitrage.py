# custom_components/heo2/rules/peak_export_arbitrage.py
"""PeakExportArbitrageRule -- sell our genuine spare at the day's
best-priced export slots, throttled to match the spare amount.

This is the *arbitrage* rule. Buy at IGO off-peak (~7p), sell at
Octopus Outgoing peak (~20p), pocket the spread on every kWh we
genuinely don't need ourselves.

Driven by the user's spec (verbatim, 2026-05-04):

  * "Literally the best price for export that day - as set by
    Octopus" -> we rank today's remaining export slots by
    rate_pence DESC and allocate from the top down.
  * "spare_kwh = current_soc - cumulative_load_now_to_2330
    + remaining_pv_forecast_today" -> the kWh we can lose without
    needing to import at peak before the cheap window starts.
  * "the amount and rate of sale can be managed using max discharge
    current along with selling first" -> we set both
    `work_mode = Selling first` AND `max_discharge_a` for the
    active slot so the inverter sells at exactly the rate we want.
    Outside the allocated slot the BaselineRule default
    ("Zero export to CT") suppresses any further export.

Allocation:
  * For each ranked slot, allocate up to
    `max_discharge_kw * slot_duration_hours` (=2.5 kWh per 30-min
    slot at 5 kW). Continue down the rank until `spare_kwh` is fully
    allocated or no more remaining slots today.
  * If `spare_kwh < per_slot_max`, the FIRST slot's amp rate is
    throttled so we deliver exactly `spare_kwh` over the slot
    duration - no need to switch off mid-slot.

Outside any allocated slot, the rule does nothing - BaselineRule's
`Zero export to CT` default suppresses export and the battery
covers house load only (which is what the user wanted: "must make
sure not to use peak rate electric").
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..models import ProgrammeInputs, RateSlot
from ..rank_pricing import next_cheap_window_start_local
from ..rule_engine import PRIO_PEAK_EXPORT_ARBITRAGE, Rule


def _local(dt: datetime, tz: ZoneInfo | None) -> datetime:
    if tz is not None and dt.tzinfo is not None:
        return dt.astimezone(tz)
    return dt


class PeakExportArbitrageRule(Rule):
    """Sell genuine spare battery at the day's best export prices."""

    name = "peak_export_arbitrage"
    description = (
        "Sell battery surplus at top-priced export slots, sized to "
        "available spare, throttled via discharge rate"
    )
    priority_class = PRIO_PEAK_EXPORT_ARBITRAGE

    def __init__(
        self,
        *,
        cheap_window_start_fallback: time = time(23, 30),
        max_discharge_kw: float = 5.0,
        battery_voltage_nominal: float = 51.2,
        max_discharge_a_default: float = 100.0,
    ):
        self.cheap_window_start_fallback = cheap_window_start_fallback
        self.max_discharge_kw = max_discharge_kw
        self.battery_voltage_nominal = battery_voltage_nominal
        self.max_discharge_a_default = max_discharge_a_default

    def _resolve_cheap_window_start(
        self,
        now_local: datetime,
        import_rates: list[RateSlot],
        tz: ZoneInfo | None,
    ) -> datetime:
        from_rates = next_cheap_window_start_local(
            import_rates, now_local, tz,
        )
        if from_rates is not None:
            return from_rates
        cheap_start = now_local.replace(
            hour=self.cheap_window_start_fallback.hour,
            minute=self.cheap_window_start_fallback.minute,
            second=0, microsecond=0,
        )
        if cheap_start <= now_local:
            cheap_start += timedelta(days=1)
        return cheap_start

    def _sum_forecast_until(
        self,
        forecast_hourly: list[float],
        now_local: datetime,
        until_local: datetime,
    ) -> float:
        if not forecast_hourly or len(forecast_hourly) < 24:
            return 0.0
        total = 0.0
        cursor = now_local
        while cursor < until_local:
            hour_idx = cursor.hour
            next_boundary = (cursor + timedelta(hours=1)).replace(
                minute=0, second=0, microsecond=0,
            )
            slice_end = min(next_boundary, until_local)
            fraction = (slice_end - cursor).total_seconds() / 3600.0
            total += forecast_hourly[hour_idx] * fraction
            cursor = slice_end
        return total

    def _sum_pv_remaining_today(
        self,
        forecast_hourly: list[float],
        now_local: datetime,
    ) -> float:
        end_of_day = now_local.replace(
            hour=23, minute=59, second=59, microsecond=0,
        )
        return self._sum_forecast_until(
            forecast_hourly, now_local, end_of_day,
        )

    def _today_remaining_export_slots(
        self,
        export_rates: list[RateSlot],
        now_local: datetime,
        cheap_start: datetime,
        tz: ZoneInfo | None,
    ) -> list[RateSlot]:
        out = []
        for r in export_rates:
            start_local = _local(r.start, tz)
            if start_local < now_local or start_local >= cheap_start:
                continue
            out.append(r)
        return out

    def propose(self, view, inputs: ProgrammeInputs) -> None:
        if not inputs.export_rates:
            return

        tz = inputs.local_tz
        now_local = inputs.now_local()
        cheap_start = self._resolve_cheap_window_start(
            now_local, inputs.import_rates, tz,
        )

        future_slots = self._today_remaining_export_slots(
            inputs.export_rates, now_local, cheap_start, tz,
        )
        if not future_slots:
            return

        floor_kwh = inputs.min_soc / 100.0 * inputs.battery_capacity_kwh
        current_kwh = inputs.current_soc / 100.0 * inputs.battery_capacity_kwh
        usable_kwh = max(0.0, current_kwh - floor_kwh)

        load_to_cheap = self._sum_forecast_until(
            inputs.load_forecast_kwh, now_local, cheap_start,
        )
        pv_remaining = self._sum_pv_remaining_today(
            inputs.solar_forecast_kwh, now_local,
        )

        spare_kwh = usable_kwh - load_to_cheap + pv_remaining
        cheap_str = cheap_start.strftime("%H:%M")
        if spare_kwh <= 0.05:
            view.log(
                f"PeakArbitrage: no spare to sell "
                f"(usable {usable_kwh:.2f} kWh - load_to_{cheap_str} "
                f"{load_to_cheap:.2f} + pv_remaining "
                f"{pv_remaining:.2f} = {spare_kwh:.2f} kWh)"
            )
            return

        sorted_slots = sorted(
            future_slots,
            key=lambda r: (-r.rate_pence, _local(r.start, tz)),
        )

        per_slot_max = self.max_discharge_kw * 0.5
        allocations: list[tuple[RateSlot, float]] = []
        remaining = spare_kwh
        for slot in sorted_slots:
            if remaining <= 0.001:
                break
            slot_duration_h = (slot.end - slot.start).total_seconds() / 3600.0
            slot_max = self.max_discharge_kw * slot_duration_h
            kwh = min(remaining, slot_max)
            allocations.append((slot, kwh))
            remaining -= kwh

        active_slot: RateSlot | None = None
        active_kwh: float = 0.0
        for slot, kwh in allocations:
            slot_start = _local(slot.start, tz)
            slot_end = _local(slot.end, tz)
            if slot_start <= now_local < slot_end:
                active_slot = slot
                active_kwh = kwh
                break

        if active_slot is None:
            future_str = ", ".join(
                f"{_local(s.start, tz).strftime('%H:%M')}@{s.rate_pence:.2f}p "
                f"({k:.2f} kWh)"
                for s, k in allocations[:3]
            )
            view.log(
                f"PeakArbitrage: spare {spare_kwh:.2f} kWh; "
                f"sell scheduled in {future_str or 'no future slots'}"
            )
            return

        slot_duration_h = (
            active_slot.end - active_slot.start
        ).total_seconds() / 3600.0
        kw_for_slot = active_kwh / slot_duration_h
        amps_target = kw_for_slot * 1000.0 / self.battery_voltage_nominal
        amps = max(1.0, min(amps_target, self.max_discharge_a_default))

        view.claim_global("work_mode", "Selling first", reason="active arbitrage slot")
        view.claim_global("max_discharge_a", round(amps, 1), reason="throttle to spare amount")

        slot_local_start = _local(active_slot.start, tz).strftime("%H:%M")
        view.log(
            f"PeakArbitrage: ACTIVE - selling {active_kwh:.2f} kWh "
            f"in slot {slot_local_start} "
            f"@ {active_slot.rate_pence:.2f}p "
            f"(spare {spare_kwh:.2f} kWh, throttle {amps:.0f}A); "
            f"work_mode -> Selling first"
        )
