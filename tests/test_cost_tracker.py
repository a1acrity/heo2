"""Tests for CostTracker energy cost accumulator."""

import pytest
from datetime import datetime, timezone, timedelta

from heo2.cost_tracker import CostAccumulator


@pytest.fixture
def acc() -> CostAccumulator:
    return CostAccumulator()


@pytest.fixture
def t0() -> datetime:
    return datetime(2026, 4, 15, 12, 0, 0, tzinfo=timezone.utc)


class TestCostAccumulator:
    def test_initial_values_are_zero(self, acc):
        assert acc.daily_import_cost == 0.0
        assert acc.daily_export_revenue == 0.0
        assert acc.daily_solar_value == 0.0
        assert acc.weekly_net_cost == 0.0
        assert acc.weekly_savings_vs_flat == 0.0

    def test_load_update_accumulates_import_cost(self, acc, t0):
        acc.update_load(watts=1000.0, now=t0, import_rate_pence=27.88)
        assert acc.daily_import_cost == 0.0
        t1 = t0 + timedelta(hours=1)
        acc.update_load(watts=1000.0, now=t1, import_rate_pence=27.88)
        assert acc.daily_import_cost == pytest.approx(0.2788, abs=0.001)

    def test_load_tracks_weekly_net_cost(self, acc, t0):
        acc.update_load(watts=2000.0, now=t0, import_rate_pence=27.88)
        t1 = t0 + timedelta(hours=1)
        acc.update_load(watts=2000.0, now=t1, import_rate_pence=27.88)
        assert acc.weekly_net_cost == pytest.approx(0.5576, abs=0.001)

    def test_load_tracks_weekly_imported_kwh(self, acc, t0):
        acc.update_load(watts=2000.0, now=t0, import_rate_pence=27.88)
        t1 = t0 + timedelta(hours=1)
        acc.update_load(watts=2000.0, now=t1, import_rate_pence=27.88)
        assert acc.weekly_imported_kwh == pytest.approx(2.0, abs=0.01)

    def test_pv_update_accumulates_solar_value(self, acc, t0):
        acc.update_pv(watts=3000.0, now=t0, import_rate_pence=27.88, export_rate_pence=15.0)
        t1 = t0 + timedelta(hours=1)
        acc.update_pv(watts=3000.0, now=t1, import_rate_pence=27.88, export_rate_pence=15.0)
        assert acc.daily_solar_value == pytest.approx(0.8364, abs=0.001)

    def test_pv_update_accumulates_export_revenue(self, acc, t0):
        acc.update_pv(watts=3000.0, now=t0, import_rate_pence=27.88, export_rate_pence=15.0)
        t1 = t0 + timedelta(hours=1)
        acc.update_pv(watts=3000.0, now=t1, import_rate_pence=27.88, export_rate_pence=15.0)
        assert acc.daily_export_revenue == pytest.approx(0.45, abs=0.001)

    def test_pv_reduces_weekly_net_cost(self, acc, t0):
        acc.update_pv(watts=3000.0, now=t0, import_rate_pence=27.88, export_rate_pence=15.0)
        t1 = t0 + timedelta(hours=1)
        acc.update_pv(watts=3000.0, now=t1, import_rate_pence=27.88, export_rate_pence=15.0)
        assert acc.weekly_net_cost == pytest.approx(-0.45, abs=0.001)

    def test_daily_reset_zeros_daily_values(self, acc, t0):
        acc.update_load(watts=1000.0, now=t0, import_rate_pence=27.88)
        acc.update_load(watts=1000.0, now=t0 + timedelta(hours=1), import_rate_pence=27.88)
        acc.reset_daily(t0 + timedelta(days=1))
        assert acc.daily_import_cost == 0.0
        assert acc.daily_export_revenue == 0.0
        assert acc.daily_solar_value == 0.0
        assert acc.weekly_net_cost != 0.0

    def test_weekly_reset_zeros_weekly_values(self, acc, t0):
        acc.update_load(watts=1000.0, now=t0, import_rate_pence=27.88)
        acc.update_load(watts=1000.0, now=t0 + timedelta(hours=1), import_rate_pence=27.88)
        acc.reset_weekly(t0 + timedelta(days=7))
        assert acc.weekly_net_cost == 0.0
        assert acc.weekly_savings_vs_flat == 0.0
        assert acc.weekly_imported_kwh == 0.0

    def test_savings_vs_flat(self, acc, t0):
        flat_rate_pence = 24.5
        acc.update_load(watts=2000.0, now=t0, import_rate_pence=7.0)
        acc.update_load(watts=2000.0, now=t0 + timedelta(hours=1), import_rate_pence=7.0)
        acc.update_pv(watts=1000.0, now=t0, import_rate_pence=7.0, export_rate_pence=15.0)
        acc.update_pv(watts=1000.0, now=t0 + timedelta(hours=1), import_rate_pence=7.0, export_rate_pence=15.0)
        acc.calculate_savings_vs_flat(flat_rate_pence)
        assert acc.weekly_savings_vs_flat == pytest.approx(0.50, abs=0.01)

    def test_last_daily_reset_recorded(self, acc, t0):
        reset_time = t0 + timedelta(days=1)
        acc.reset_daily(reset_time)
        assert acc.last_daily_reset == reset_time

    def test_last_weekly_reset_recorded(self, acc, t0):
        reset_time = t0 + timedelta(days=7)
        acc.reset_weekly(reset_time)
        assert acc.last_weekly_reset == reset_time
