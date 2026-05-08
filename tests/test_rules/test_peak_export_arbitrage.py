# tests/test_rules/test_peak_export_arbitrage.py
"""Tests for PeakExportArbitrageRule (the day's-best-price arbitrage)."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from heo2.models import ProgrammeInputs, ProgrammeState, RateSlot, SlotConfig
from heo2.rules.peak_export_arbitrage import PeakExportArbitrageRule


_LON = ZoneInfo("Europe/London")


def _slot(start_h, start_m, end_h, end_m, soc=50, gc=False):
    return SlotConfig(
        start_time=time(start_h, start_m),
        end_time=time(end_h, end_m),
        capacity_soc=soc,
        grid_charge=gc,
    )


def _empty_programme():
    return ProgrammeState(slots=[
        _slot(0, 0, 5, 30, 50, True),
        _slot(5, 30, 18, 30, 50, False),
        _slot(18, 30, 23, 30, 10, False),
        _slot(23, 30, 23, 55, 50, True),
        _slot(23, 55, 23, 55, 10, False),
        _slot(23, 55, 0, 0, 10, False),
    ], reason_log=[])


def _half_hour_rate(date: datetime, hour: int, minute: int, p: float) -> RateSlot:
    """Helper: a 30-min export rate slot at a specific local time."""
    start_local = date.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return RateSlot(
        start=start_local.astimezone(timezone.utc),
        end=(start_local + timedelta(minutes=30)).astimezone(timezone.utc),
        rate_pence=p,
    )


def _inputs(
    *,
    now_local: datetime,
    current_soc: float = 80.0,
    capacity: float = 20.0,
    min_soc: float = 10.0,
    export_rates: list[RateSlot] | None = None,
    import_rates: list[RateSlot] | None = None,
    load_24: list[float] | None = None,
    solar_24: list[float] | None = None,
):
    """Build ProgrammeInputs with `now` derived from a London-local
    datetime. Coordinator does the same in production."""
    return ProgrammeInputs(
        now=now_local.astimezone(timezone.utc),
        current_soc=current_soc,
        battery_capacity_kwh=capacity,
        min_soc=min_soc,
        import_rates=import_rates or [],
        export_rates=export_rates or [],
        solar_forecast_kwh=solar_24 or [0.0] * 24,
        load_forecast_kwh=load_24 or [0.5] * 24,
        igo_dispatching=False,
        saving_session=False,
        saving_session_start=None,
        saving_session_end=None,
        ev_charging=False,
        grid_connected=True,
        active_appliances=[],
        appliance_expected_kwh=0.0,
        local_tz=_LON,
    )


class TestPeakExportArbitrageRule:
    def test_no_export_rates_is_noop(self):
        now = datetime(2026, 5, 4, 17, 0, tzinfo=_LON)
        inputs = _inputs(now_local=now, export_rates=[])
        result = PeakExportArbitrageRule().apply(_empty_programme(), inputs)
        assert result.work_mode is None

    def test_no_spare_when_load_consumes_battery(self):
        """Battery 50%, load until 23:30 needs 8 kWh = 40% drain.
        Above min_soc we have only 8 kWh -> spare is 0 -> rule is no-op."""
        today = datetime(2026, 5, 4, 17, 0, tzinfo=_LON)
        rates = [_half_hour_rate(today, 18, 0, 25.0)]
        # 6.5h * 1.23 kWh/h ≈ 8 kWh load before 23:30
        load = [1.23] * 24
        inputs = _inputs(
            now_local=today, current_soc=50.0, capacity=20.0, min_soc=10.0,
            export_rates=rates, load_24=load,
        )
        result = PeakExportArbitrageRule().apply(_empty_programme(), inputs)
        assert result.work_mode is None
        assert any("no spare" in r for r in result.reason_log)

    def test_active_when_in_top_priced_slot_with_spare(self):
        """Battery full, currently inside the top-priced 30-min slot,
        light load -> rule fires Selling first + sized discharge."""
        today = datetime(2026, 5, 4, 18, 0, tzinfo=_LON)
        rates = [
            _half_hour_rate(today, 18, 0, 25.0),  # active now (best)
            _half_hour_rate(today, 17, 30, 22.0),  # in past
            _half_hour_rate(today, 18, 30, 21.0),  # later, lower
        ]
        inputs = _inputs(
            now_local=today, current_soc=100.0, capacity=20.0, min_soc=10.0,
            export_rates=rates,
            load_24=[0.3] * 24,  # very light
        )
        result = PeakExportArbitrageRule().apply(_empty_programme(), inputs)
        assert result.work_mode == "Selling first"
        assert result.max_discharge_a is not None
        assert result.max_discharge_a > 0
        assert any("ACTIVE" in r for r in result.reason_log)

    def test_active_mid_slot_when_tick_fires_after_slot_start(self):
        """Real production miss 2026-05-08: tick fires at 19:12 BST,
        inside an in-progress 19:00-19:30 export slot. Pre-fix the
        `_today_remaining_export_slots` filter excluded any slot whose
        start was already past, so 19:00 wasn't even in allocations
        and the active-slot detection failed. Rule must fire ACTIVE
        for in-progress slots, not just slots whose start instant
        coincides with `now_local`.
        """
        today = datetime(2026, 5, 8, 19, 12, tzinfo=_LON)  # 12 min into the slot
        rates = [
            _half_hour_rate(today, 19, 0, 13.10),   # active mid-slot
            _half_hour_rate(today, 19, 30, 13.28),  # next, slightly higher
            _half_hour_rate(today, 20, 0, 13.06),
            _half_hour_rate(today, 18, 30, 12.50),  # in past - should be ignored
        ]
        # IGO off-peak start at 23:30 - 6:30 forward = end of cheap window day
        # before. Set import rates with a clear cheap-window boundary so
        # `_resolve_cheap_window_start` lands at 23:30 BST tonight.
        cheap_start_local = today.replace(hour=23, minute=30)
        import_rates = [
            RateSlot(
                start=cheap_start_local.astimezone(timezone.utc),
                end=(cheap_start_local + timedelta(hours=6)).astimezone(timezone.utc),
                rate_pence=7.0,
            ),
            RateSlot(
                start=(cheap_start_local - timedelta(hours=18)).astimezone(timezone.utc),
                end=cheap_start_local.astimezone(timezone.utc),
                rate_pence=27.88,
            ),
        ]
        inputs = _inputs(
            now_local=today, current_soc=80.0, capacity=20.0, min_soc=10.0,
            export_rates=rates, import_rates=import_rates,
            load_24=[0.3] * 24,
        )
        result = PeakExportArbitrageRule().apply(_empty_programme(), inputs)
        assert result.work_mode == "Selling first", \
            f"Mid-slot activation failed: reason_log={result.reason_log}"
        assert result.max_discharge_a is not None
        assert any("ACTIVE" in r for r in result.reason_log), \
            f"Expected ACTIVE log, got: {result.reason_log}"

    def test_past_slots_excluded_from_allocation(self):
        """Slots whose end is in the past should not appear in
        allocations - we can't sell into yesterday."""
        today = datetime(2026, 5, 8, 20, 0, tzinfo=_LON)
        rates = [
            _half_hour_rate(today, 18, 0, 25.0),   # ended at 18:30, in past
            _half_hour_rate(today, 20, 0, 13.0),   # active now
        ]
        inputs = _inputs(
            now_local=today, current_soc=100.0, capacity=20.0, min_soc=10.0,
            export_rates=rates, load_24=[0.3] * 24,
        )
        result = PeakExportArbitrageRule().apply(_empty_programme(), inputs)
        # Active should be the 20:00 slot; the past 18:00 slot should
        # not be in allocations or accidentally chosen as active.
        if result.work_mode == "Selling first":
            assert any("20:00" in r for r in result.reason_log)
            assert not any("ACTIVE - selling" in r and "18:00" in r
                           for r in result.reason_log)

    def test_inactive_outside_allocated_slot(self):
        """Battery full, top-priced slot is 18:00-18:30, but we're at
        17:00 (between rates with no allocation) -> work_mode left
        alone."""
        today = datetime(2026, 5, 4, 17, 0, tzinfo=_LON)
        rates = [
            _half_hour_rate(today, 18, 0, 25.0),  # later, best
            _half_hour_rate(today, 17, 0, 5.0),   # active now, low rate
        ]
        inputs = _inputs(
            now_local=today, current_soc=100.0, capacity=20.0, min_soc=10.0,
            export_rates=rates, load_24=[0.3] * 24,
        )
        result = PeakExportArbitrageRule().apply(_empty_programme(), inputs)
        # Even though spare exists, we're not in the BEST slot - 5p
        # might still be allocated if spare > 5kW*0.5h, otherwise
        # no allocation reaches 17:00. With ~17 kWh spare and only
        # 2 worth-sell slots, allocation: best=2.5, second=2.5; 17:00
        # might end up allocated if spare > 2.5. Let's check: spare
        # ~= (100-10)/100*20 - 6.5*0.3 + 0 ≈ 18 - 1.95 ≈ 16 kWh.
        # First slot gets 2.5, second gets 2.5; total 5 kWh. So 17:00
        # IS allocated (it ranks 2nd by rate). work_mode WOULD be
        # set. To assert the "outside allocation" path we need a
        # scenario where the active slot got 0.
        # Refine: drop the 17:00 slot from rates so 17:00 has no rate.
        inputs2 = _inputs(
            now_local=today, current_soc=100.0, capacity=20.0, min_soc=10.0,
            export_rates=[_half_hour_rate(today, 18, 0, 25.0)],
            load_24=[0.3] * 24,
        )
        result2 = PeakExportArbitrageRule().apply(
            _empty_programme(), inputs2,
        )
        assert result2.work_mode is None
        assert any("scheduled" in r for r in result2.reason_log)

    def test_throttle_amps_when_spare_smaller_than_full_slot(self):
        """Spare ~1 kWh and slot is 30 min: full rate = 5 kW would
        empty spare in 12 min. Throttle to deliver 1 kWh over 30 min
        = 2 kW = ~39A."""
        today = datetime(2026, 5, 4, 18, 0, tzinfo=_LON)
        rates = [_half_hour_rate(today, 18, 0, 25.0)]
        # Compute conditions that yield spare ~= 1 kWh
        # current_above_floor = (50-10)/100 * 20 = 8 kWh
        # load_to_2330 = 5.5h * 1.27 = ~7 kWh => spare = 8 - 7 + 0 = 1 kWh
        inputs = _inputs(
            now_local=today, current_soc=50.0, capacity=20.0, min_soc=10.0,
            export_rates=rates, load_24=[1.27] * 24,
        )
        result = PeakExportArbitrageRule().apply(_empty_programme(), inputs)
        assert result.work_mode == "Selling first"
        # 1 kWh / 0.5h = 2 kW => 2000 / 51.2 ≈ 39A
        assert 30 < result.max_discharge_a < 50

    def test_pv_remaining_added_to_spare(self):
        """PV remaining today INCREASES spare - if there's still solar
        coming, we have more to sell."""
        today = datetime(2026, 5, 4, 16, 0, tzinfo=_LON)
        rates = [_half_hour_rate(today, 16, 0, 25.0)]  # active now
        # No PV: spare = 8 kWh - 7.5 kWh (load) = 0.5 kWh
        # With PV: spare gets +5 kWh of late-day solar -> 5.5 kWh
        no_pv = _inputs(
            now_local=today, current_soc=50.0, capacity=20.0, min_soc=10.0,
            export_rates=rates, load_24=[1.0] * 24,
            solar_24=[0.0] * 24,
        )
        with_pv = _inputs(
            now_local=today, current_soc=50.0, capacity=20.0, min_soc=10.0,
            export_rates=rates, load_24=[1.0] * 24,
            solar_24=[0.0] * 16 + [2.5, 2.0, 0.5] + [0.0] * 5,
        )
        r_no = PeakExportArbitrageRule().apply(
            _empty_programme(), no_pv,
        )
        r_yes = PeakExportArbitrageRule().apply(
            _empty_programme(), with_pv,
        )
        # With PV the throttle amp should be higher (more to sell)
        assert (r_yes.max_discharge_a or 0) > (r_no.max_discharge_a or 0)

    def test_cheap_window_horizon_uses_import_rates(self):
        """When import_rates show a cheap block starting at 22:00 (e.g.
        a Saving Session shifts the cheap window earlier), spare maths
        should use 22:00 - not the hardcoded 23:30 - as the horizon.

        Setup chosen so both spare values fall under the per-slot cap
        (2.5 kWh = max_discharge_kw * 0.5h), so the difference in
        horizon is visible in the throttle amps. capacity=10 kWh,
        current 40%, min_soc 10%, usable=3 kWh; load=0.4 kWh/h.
          - cheap=22:00: load_to_cheap = 4h * 0.4 = 1.6, spare = 1.4
          - cheap=23:30: load_to_cheap = 5.5h * 0.4 = 2.2, spare = 0.8
        Both fire; amps differ (cheap-22 is higher).
        """
        today = datetime(2026, 5, 4, 18, 0, tzinfo=_LON)
        export_rates = [_half_hour_rate(today, 18, 0, 25.0)]
        # Import rates: peak 16-22 expensive, 22-04 cheap (shifted from
        # standard IGO 23:30-05:30 by say a Saving Session arrangement).
        cheap_p, peak_p = 5.0, 30.0
        import_rates = []
        for h in [16, 17, 18, 19, 20, 21]:
            import_rates.append(_half_hour_rate(today, h, 0, peak_p))
            import_rates.append(_half_hour_rate(today, h, 30, peak_p))
        for h in [22, 23]:
            import_rates.append(_half_hour_rate(today, h, 0, cheap_p))
            import_rates.append(_half_hour_rate(today, h, 30, cheap_p))

        with_imports = _inputs(
            now_local=today, current_soc=40.0, capacity=10.0, min_soc=10.0,
            export_rates=export_rates, import_rates=import_rates,
            load_24=[0.4] * 24,
        )
        without_imports = _inputs(
            now_local=today, current_soc=40.0, capacity=10.0, min_soc=10.0,
            export_rates=export_rates, load_24=[0.4] * 24,
        )
        r_with = PeakExportArbitrageRule().apply(
            _empty_programme(), with_imports,
        )
        r_without = PeakExportArbitrageRule().apply(
            _empty_programme(), without_imports,
        )
        # Both should fire (active in 18:00 slot, spare available)
        assert r_with.work_mode == "Selling first"
        assert r_without.work_mode == "Selling first"
        # With import_rates the cheap horizon is 22:00 (4h away) not
        # 23:30 (5.5h away), so less load to cover, more spare, higher
        # discharge rate.
        assert r_with.max_discharge_a > r_without.max_discharge_a
