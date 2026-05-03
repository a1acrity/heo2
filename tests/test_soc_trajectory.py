"""Tests for SOC trajectory forward simulation."""

import pytest
from datetime import time

from heo2.soc_trajectory import calculate_soc_trajectory
from heo2.models import SlotConfig


@pytest.fixture
def flat_load() -> list[float]:
    """1.9 kWh per hour for 24 hours."""
    return [1.9] * 24


@pytest.fixture
def midday_solar() -> list[float]:
    """Solar peak around midday: ~20 kWh total."""
    solar = [0.0] * 24
    for h in range(6, 18):
        solar[h] = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.2, 0.8, 1.5, 2.5, 3.0, 3.5, 3.5, 3.0, 2.0, 1.0, 0.3, 0.0][h]
    return solar


@pytest.fixture
def default_slots() -> list[SlotConfig]:
    """Default 6-slot programme at 20% min SOC, no grid charge."""
    from heo2.models import ProgrammeState
    return ProgrammeState.default(min_soc=20).slots


class TestSOCTrajectory:
    def test_returns_horizon_floats(self, flat_load, default_slots):
        """Default horizon is 30 hours (today + tomorrow morning)."""
        result = calculate_soc_trajectory(
            current_soc=50.0,
            solar_forecast_kwh=[0.0] * 24,
            load_forecast_kwh=flat_load,
            programme_slots=default_slots,
            battery_capacity_kwh=20.0,
            max_charge_kw=5.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            min_soc=20.0,
            max_soc=100.0,
            current_hour=0,
        )
        assert len(result) == 30
        assert all(isinstance(v, float) for v in result)

    def test_horizon_extends_into_tomorrow_with_separate_solar(
        self, flat_load, default_slots,
    ):
        """When solar_forecast_kwh_tomorrow is supplied, indices 24+
        use tomorrow's solar values - lets the chart show overnight
        recharge plus tomorrow's morning ramp."""
        # Today: zero solar; tomorrow: 5 kWh per hour for first 6
        solar_today = [0.0] * 24
        solar_tomorrow = [5.0] * 24
        slots = [
            SlotConfig(time(0, 0), time(5, 30), capacity_soc=80, grid_charge=True),
            SlotConfig(time(5, 30), time(8, 0), capacity_soc=20, grid_charge=False),
            SlotConfig(time(8, 0), time(12, 0), capacity_soc=20, grid_charge=False),
            SlotConfig(time(12, 0), time(16, 0), capacity_soc=20, grid_charge=False),
            SlotConfig(time(16, 0), time(23, 59), capacity_soc=20, grid_charge=False),
            SlotConfig(time(23, 59), time(0, 0), capacity_soc=20, grid_charge=False),
        ]
        result = calculate_soc_trajectory(
            current_soc=50.0,
            solar_forecast_kwh=solar_today,
            load_forecast_kwh=flat_load,
            programme_slots=slots,
            battery_capacity_kwh=20.0,
            max_charge_kw=5.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            min_soc=20.0,
            max_soc=100.0,
            current_hour=20,  # late evening
            solar_forecast_kwh_tomorrow=solar_tomorrow,
        )
        assert len(result) == 30
        # Tomorrow morning hours (24-29) should reflect the recharge
        # from solar_tomorrow + slot 1's GC=True target (=80%)
        # During hour 26 = 02:00 tomorrow we are in slot 1 with cap=80
        # and tomorrow_solar[2]=5 - SOC should climb past current_soc.
        assert result[26] > result[20], (
            f"hour 26 ({result[26]}) expected > hour 20 ({result[20]})"
        )

    def test_value_at_current_hour_is_current_soc(
        self, flat_load, default_slots,
    ):
        """trajectory[current_hour] holds the starting SOC before any
        simulation step; later hours integrate net load/solar."""
        result = calculate_soc_trajectory(
            current_soc=65.0,
            solar_forecast_kwh=[0.0] * 24,
            load_forecast_kwh=flat_load,
            programme_slots=default_slots,
            battery_capacity_kwh=20.0,
            max_charge_kw=5.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            min_soc=20.0,
            max_soc=100.0,
            current_hour=12,
        )
        assert result[12] == 65.0

    def test_past_hours_filled_with_current_soc(
        self, flat_load, default_slots,
    ):
        """Hours BEFORE current_hour have no actuals; we fill them
        with current_soc so the chart shows a flat back-fill rather
        than a gap or arbitrary number."""
        result = calculate_soc_trajectory(
            current_soc=42.0,
            solar_forecast_kwh=[0.0] * 24,
            load_forecast_kwh=flat_load,
            programme_slots=default_slots,
            battery_capacity_kwh=20.0,
            max_charge_kw=5.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            min_soc=20.0,
            max_soc=100.0,
            current_hour=15,
        )
        # Hours 0-14 are past, should all equal current_soc.
        for h in range(15):
            assert result[h] == 42.0, (
                f"hour {h} expected 42 (past fill), got {result[h]}"
            )

    def test_soc_decreases_with_load_no_solar(self, flat_load, default_slots):
        result = calculate_soc_trajectory(
            current_soc=80.0,
            solar_forecast_kwh=[0.0] * 24,
            load_forecast_kwh=flat_load,
            programme_slots=default_slots,
            battery_capacity_kwh=20.0,
            max_charge_kw=5.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            min_soc=20.0,
            max_soc=100.0,
            current_hour=0,
        )
        # Later simulated hour < earlier simulated hour
        assert result[5] < result[0]

    def test_soc_clamped_to_min(self, flat_load, default_slots):
        result = calculate_soc_trajectory(
            current_soc=25.0,
            solar_forecast_kwh=[0.0] * 24,
            load_forecast_kwh=flat_load,
            programme_slots=default_slots,
            battery_capacity_kwh=20.0,
            max_charge_kw=5.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            min_soc=20.0,
            max_soc=100.0,
            current_hour=0,
        )
        assert all(v >= 20.0 for v in result)

    def test_soc_clamped_to_max(self, default_slots):
        result = calculate_soc_trajectory(
            current_soc=95.0,
            solar_forecast_kwh=[5.0] * 24,
            load_forecast_kwh=[0.1] * 24,
            programme_slots=default_slots,
            battery_capacity_kwh=20.0,
            max_charge_kw=5.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            min_soc=20.0,
            max_soc=100.0,
            current_hour=0,
        )
        assert all(v <= 100.0 for v in result)

    def test_grid_charge_increases_soc(self):
        slots = [
            SlotConfig(time(0, 0), time(4, 0), capacity_soc=80, grid_charge=True),
            SlotConfig(time(4, 0), time(8, 0), capacity_soc=20, grid_charge=False),
            SlotConfig(time(8, 0), time(12, 0), capacity_soc=20, grid_charge=False),
            SlotConfig(time(12, 0), time(16, 0), capacity_soc=20, grid_charge=False),
            SlotConfig(time(16, 0), time(23, 59), capacity_soc=20, grid_charge=False),
            SlotConfig(time(23, 59), time(0, 0), capacity_soc=20, grid_charge=False),
        ]
        result = calculate_soc_trajectory(
            current_soc=30.0,
            solar_forecast_kwh=[0.0] * 24,
            load_forecast_kwh=[0.5] * 24,
            programme_slots=slots,
            battery_capacity_kwh=20.0,
            max_charge_kw=5.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            min_soc=20.0,
            max_soc=100.0,
            current_hour=0,
        )
        assert result[2] > result[0]

    def test_solar_increases_soc(self, midday_solar, default_slots):
        result = calculate_soc_trajectory(
            current_soc=40.0,
            solar_forecast_kwh=midday_solar,
            load_forecast_kwh=[0.5] * 24,
            programme_slots=default_slots,
            battery_capacity_kwh=20.0,
            max_charge_kw=5.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            min_soc=20.0,
            max_soc=100.0,
            current_hour=6,
        )
        # After several solar hours past current_hour=6, SOC should
        # have climbed above the starting value (40%).
        assert result[15] > result[6]
