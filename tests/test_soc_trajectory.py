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
    def test_returns_24_floats(self, flat_load, default_slots):
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
            current_hour=12,
        )
        assert len(result) == 24
        assert all(isinstance(v, float) for v in result)

    def test_first_value_is_current_soc(self, flat_load, default_slots):
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
        assert result[0] == 65.0

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
            current_hour=12,
        )
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
            current_hour=12,
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
            current_hour=12,
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
        assert result[6] > result[0]
