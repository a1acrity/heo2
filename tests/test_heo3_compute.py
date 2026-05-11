"""Compute library — 5 families per §12 of the design."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from heo3.compute import Compute, RateBand
from heo3.types import PlannedAction, RatePeriod, SystemConfig, TimeRange

from .heo3_fixtures import cheap_then_peak_rates, make_snapshot


# ── 12a Energy / SOC / kWh ─────────────────────────────────────────


class TestEnergySOC:
    def test_kwh_for_soc(self):
        c = Compute()
        snap = make_snapshot()
        # Default capacity 20.48 kWh × 50% = 10.24
        assert c.kwh_for_soc(50, snap) == pytest.approx(10.24)

    def test_soc_for_kwh_inverse(self):
        c = Compute()
        snap = make_snapshot()
        assert c.soc_for_kwh(c.kwh_for_soc(75, snap), snap) == pytest.approx(75.0)

    def test_usable_kwh_above_floor(self):
        c = Compute()
        snap = make_snapshot(soc_pct=80, config=SystemConfig(min_soc=20))
        # Usable = 60% × 20.48 = 12.288
        assert c.usable_kwh(snap) == pytest.approx(12.288)

    def test_usable_kwh_at_floor_is_zero(self):
        c = Compute()
        snap = make_snapshot(soc_pct=10, config=SystemConfig(min_soc=10))
        assert c.usable_kwh(snap) == 0.0

    def test_usable_kwh_below_floor_clamps_to_zero(self):
        c = Compute()
        snap = make_snapshot(soc_pct=5, config=SystemConfig(min_soc=10))
        assert c.usable_kwh(snap) == 0.0

    def test_headroom_kwh(self):
        c = Compute()
        snap = make_snapshot(soc_pct=80)
        # Room from 80% to 100% = 20% × 20.48 = 4.096
        assert c.headroom_kwh(snap) == pytest.approx(4.096)

    def test_headroom_full_battery_zero(self):
        c = Compute()
        snap = make_snapshot(soc_pct=100)
        assert c.headroom_kwh(snap) == 0.0

    def test_round_trip_efficiency(self):
        c = Compute()
        snap = make_snapshot()
        assert c.round_trip_efficiency(snap) == pytest.approx(0.9025)


# ── 12b Time / rate windows ────────────────────────────────────────


class TestRateWindows:
    def test_next_cheap_window(self):
        c = Compute()
        captured = datetime(2026, 5, 10, 6, 0, tzinfo=timezone.utc)
        snap = make_snapshot(captured_at=captured)
        # Cheap window ended at 05:00 today; next cheap window starts 00:00 tomorrow.
        cheap = c.next_cheap_window(snap)
        assert cheap is not None
        # Tomorrow's cheap window starts at midnight UTC of tomorrow.
        assert cheap.start.day == 11

    def test_next_peak_window(self):
        c = Compute()
        captured = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
        snap = make_snapshot(captured_at=captured)
        peak = c.next_peak_window(snap)
        assert peak is not None
        # Peak runs 16:00-19:00 today.
        assert peak.start.hour == 16
        assert peak.end.hour == 19

    def test_no_rates_returns_none(self):
        c = Compute()
        snap = make_snapshot(rates=((), ()))
        assert c.next_cheap_window(snap) is None
        assert c.next_peak_window(snap) is None

    def test_top_export_windows(self):
        c = Compute()
        captured = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
        # Build export rates that ramp up through the day.
        export = tuple(
            RatePeriod(
                start=captured + timedelta(hours=h),
                end=captured + timedelta(hours=h + 1),
                rate_pence=10.0 + h,
            )
            for h in range(6)  # 12:00-18:00
        )
        snap = make_snapshot(captured_at=captured, export_today=export)
        # cap by no cheap-window (future-only export rates)
        top3 = c.top_export_windows(snap, n=3)
        assert len(top3) == 3
        # Highest first
        assert top3[0].rate_pence == 15.0
        assert top3[1].rate_pence == 14.0
        assert top3[2].rate_pence == 13.0

    def test_time_until(self):
        c = Compute()
        captured = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
        snap = make_snapshot(captured_at=captured)
        target = captured + timedelta(hours=3)
        assert c.time_until(target, snap) == timedelta(hours=3)


# ── 12c Forecast aggregation ───────────────────────────────────────


class TestForecastAggregation:
    def test_total_load_full_day(self):
        c = Compute()
        captured = datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc)
        snap = make_snapshot(
            captured_at=captured,
            today_load_kwh=tuple([1.0] * 24),
            tomorrow_load_kwh=tuple([1.0] * 24),
        )
        window = TimeRange(start=captured, end=captured + timedelta(hours=24))
        assert c.total_load(snap, window) == pytest.approx(24.0, abs=0.5)

    def test_total_load_partial_hour_prorated(self):
        c = Compute()
        # Captured at 12:00 UTC = 13:00 BST
        captured = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
        snap = make_snapshot(
            captured_at=captured, today_load_kwh=tuple([2.0] * 24)
        )
        window = TimeRange(
            start=captured, end=captured + timedelta(minutes=30)
        )
        # Half an hour of 2 kWh/h = 1.0
        assert c.total_load(snap, window) == pytest.approx(1.0, abs=0.01)

    def test_net_load_signed(self):
        c = Compute()
        captured = datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc)
        snap = make_snapshot(
            captured_at=captured,
            today_load_kwh=tuple([1.0] * 24),
            today_solar_kwh=tuple([2.0] * 24),
        )
        window = TimeRange(start=captured, end=captured + timedelta(hours=2))
        # Load 2 kWh - Solar 4 kWh = -2 (surplus)
        assert c.net_load(snap, window) == pytest.approx(-2.0)

    def test_bridge_kwh_floors_at_zero(self):
        c = Compute()
        captured = datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc)
        snap = make_snapshot(
            captured_at=captured,
            today_load_kwh=tuple([1.0] * 24),
            today_solar_kwh=tuple([5.0] * 24),  # massive surplus
        )
        # bridge = max(0, load - solar)
        until = captured + timedelta(hours=4)
        assert c.bridge_kwh(snap, until=until) == 0.0

    def test_bridge_kwh_positive(self):
        c = Compute()
        captured = datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc)
        snap = make_snapshot(
            captured_at=captured,
            today_load_kwh=tuple([2.0] * 24),
            today_solar_kwh=tuple([0.5] * 24),
        )
        until = captured + timedelta(hours=4)
        # Load 8 - Solar 2 = 6
        assert c.bridge_kwh(snap, until=until) == pytest.approx(6.0)

    def test_pv_takeover_hour(self):
        c = Compute()
        load = [2.0] * 24
        solar = [0.0] * 9 + [3.0] * 7 + [0.0] * 8  # solar overtakes at 9
        snap = make_snapshot(
            tomorrow_load_kwh=tuple(load),
            tomorrow_solar_kwh=tuple(solar),
        )
        assert c.pv_takeover_hour(snap) == 9

    def test_pv_takeover_hour_none_in_winter(self):
        c = Compute()
        snap = make_snapshot(
            tomorrow_load_kwh=tuple([2.0] * 24),
            tomorrow_solar_kwh=tuple([0.5] * 24),
        )
        assert c.pv_takeover_hour(snap) is None


# ── 12d Counterfactual ─────────────────────────────────────────────


class TestCounterfactual:
    def test_usage_at_rate_band_distributes(self):
        c = Compute()
        captured = datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc)
        snap = make_snapshot(
            captured_at=captured, today_load_kwh=tuple([1.0] * 24)
        )
        window = TimeRange(start=captured, end=captured + timedelta(hours=24))
        bands = c.usage_at_rate_band(snap, window)
        assert RateBand.PEAK in bands
        assert RateBand.OFF_PEAK in bands
        assert RateBand.CHEAP_WINDOW in bands
        # Total should approximate full day's load (24 kWh).
        total = sum(bands.values())
        assert total == pytest.approx(24.0, abs=0.5)

    def test_cost_breakdown_import_only(self):
        c = Compute()
        captured = datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc)
        snap = make_snapshot(
            captured_at=captured, today_load_kwh=tuple([1.0] * 24)
        )
        window = TimeRange(start=captured, end=captured + timedelta(hours=2))
        # First 2h are cheap (5p) — 2 kWh × 5p = 10p
        cb = c.cost_breakdown(snap, window)
        assert cb["import_cost_pence"] == pytest.approx(10.0, abs=0.5)
        assert cb["export_revenue_pence"] == 0.0


# ── 12e Physics ────────────────────────────────────────────────────


class TestPhysics:
    def test_time_to_charge_zero_when_already_above(self):
        c = Compute()
        snap = make_snapshot(soc_pct=85)
        assert c.time_to_charge(
            target_soc_pct=80, charge_rate_kw=5, snap=snap
        ) == timedelta(0)

    def test_time_to_charge_efficiency_applied(self):
        c = Compute()
        snap = make_snapshot(soc_pct=50)
        # 50→80 = 30% × 20.48 = 6.144 kWh delivered
        # / 0.95 efficiency = 6.467 grid kWh
        # / 5 kW rate = 1.293 hours
        td = c.time_to_charge(
            target_soc_pct=80, charge_rate_kw=5, snap=snap
        )
        assert td.total_seconds() == pytest.approx(1.293 * 3600, rel=0.01)

    def test_kwh_deliverable_uses_live_voltage(self):
        c = Compute()
        snap = make_snapshot(battery_voltage_v=51.2)
        # 100A × 51.2V × 1h = 5120 W·h = 5.12 kWh
        result = c.kwh_deliverable_in(
            duration=timedelta(hours=1), throttle_a=100, snap=snap
        )
        assert result == pytest.approx(5.12)

    def test_discharge_throttle_for_inverse(self):
        c = Compute()
        snap = make_snapshot(battery_voltage_v=51.2)
        # Want to deliver 2.56 kWh in 30 min @ 51.2V
        # → 5120 W → 100 A
        amps = c.discharge_throttle_for(
            kwh=2.56, duration=timedelta(minutes=30), snap=snap
        )
        assert amps == pytest.approx(100.0)

    def test_discharge_throttle_clamps_to_350(self):
        c = Compute()
        snap = make_snapshot()
        amps = c.discharge_throttle_for(
            kwh=1000.0, duration=timedelta(minutes=1), snap=snap
        )
        assert amps == 350.0  # hardware ceiling

    def test_discharge_throttle_zero_duration(self):
        c = Compute()
        snap = make_snapshot()
        assert c.discharge_throttle_for(
            kwh=1.0, duration=timedelta(0), snap=snap
        ) == 0.0
