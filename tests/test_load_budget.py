"""Unit tests for the shared load-budget calculations (HEO-31 Phase 3 PR 3)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from heo2.load_budget import evening_demand_kwh, evening_floor_soc
from heo2.models import ProgrammeInputs


def _inputs(*, load_per_hour=1.0, capacity=20.0, min_soc=10.0) -> ProgrammeInputs:
    return ProgrammeInputs(
        now=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
        current_soc=50.0,
        battery_capacity_kwh=capacity,
        min_soc=min_soc,
        import_rates=[],
        export_rates=[],
        solar_forecast_kwh=[0.0] * 24,
        load_forecast_kwh=[load_per_hour] * 24,
        igo_dispatching=False,
        saving_session=False,
        saving_session_start=None,
        saving_session_end=None,
        ev_charging=False,
        grid_connected=True,
        active_appliances=[],
        appliance_expected_kwh=0.0,
    )


class TestEveningDemand:
    def test_default_window_is_18_to_24(self):
        # 6h * 1.0 kWh/h = 6.0 kWh
        assert evening_demand_kwh(_inputs(load_per_hour=1.0)) == 6.0

    def test_custom_window(self):
        # 5h * 2.0 kWh/h = 10.0 kWh
        assert evening_demand_kwh(_inputs(load_per_hour=2.0), start_hour=17, end_hour=22) == 10.0

    def test_zero_load(self):
        assert evening_demand_kwh(_inputs(load_per_hour=0.0)) == 0.0


class TestEveningFloorSoc:
    def test_min_soc_plus_demand_percentage(self):
        # 6.0 kWh / 20.0 kWh = 30% + min_soc 10 = 40%
        assert evening_floor_soc(_inputs(load_per_hour=1.0, capacity=20.0, min_soc=10.0)) == 40

    def test_clamped_to_100(self):
        # 24h * 5.0 kWh = 120 kWh demand, way over capacity. With
        # default 18..24 window (6h), 30 kWh / 20 kWh = 150% + min_soc.
        # Clamped to 100.
        assert evening_floor_soc(_inputs(load_per_hour=5.0, capacity=20.0, min_soc=10.0)) == 100

    def test_clamped_to_min_soc_when_demand_zero(self):
        # No demand -> floor is just min_soc.
        assert evening_floor_soc(_inputs(load_per_hour=0.0, min_soc=15.0)) == 15

    def test_zero_capacity_returns_min_soc(self):
        # Degenerate: battery has no capacity. Don't divide.
        assert evening_floor_soc(_inputs(load_per_hour=2.0, capacity=0.0, min_soc=20.0)) == 20

    def test_int_truncation_matches_legacy(self):
        """Legacy code did `int(min_soc + pct)` (truncation toward
        zero). We preserve that so floor values match historical
        behaviour exactly."""
        # 0.5 kWh/h * 6h = 3.0 kWh; 3.0/20.0 = 15.0%; min_soc=10 -> 25.
        assert evening_floor_soc(_inputs(load_per_hour=0.5, capacity=20.0, min_soc=10.0)) == 25

        # 0.55 * 6 = 3.3; 3.3/20 = 16.5%; min_soc 10 + 16.5 = 26.5 -> int -> 26
        assert evening_floor_soc(_inputs(load_per_hour=0.55, capacity=20.0, min_soc=10.0)) == 26

    def test_custom_window_passes_through(self):
        # 17..22 = 5h * 1.0 = 5 kWh / 20 kWh = 25% + 10 = 35
        assert evening_floor_soc(_inputs(load_per_hour=1.0), start_hour=17, end_hour=22) == 35
