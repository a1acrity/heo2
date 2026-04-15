# tests/test_dashboard_sensors.py
"""Tests for dashboard sensor entities."""

import sys
import types

# ---------------------------------------------------------------------------
# Stub out homeassistant before any heo2.sensor / heo2.number imports
# ---------------------------------------------------------------------------
def _ensure_ha_stubs() -> None:
    """Register minimal homeassistant module stubs into sys.modules.

    Uses force-registration (not setdefault) for submodules that may be
    missing when another test file has already registered a partial stub.
    """

    # --- homeassistant root ---
    if "homeassistant" not in sys.modules:
        sys.modules["homeassistant"] = types.ModuleType("homeassistant")

    # --- homeassistant.core ---
    if "homeassistant.core" not in sys.modules:
        _ha_core = types.ModuleType("homeassistant.core")
        _ha_core.HomeAssistant = type("HomeAssistant", (), {})
        sys.modules["homeassistant.core"] = _ha_core

    # --- homeassistant.config_entries ---
    if "homeassistant.config_entries" not in sys.modules:
        sys.modules["homeassistant.config_entries"] = types.ModuleType("homeassistant.config_entries")
    _ha_ce = sys.modules["homeassistant.config_entries"]
    if not hasattr(_ha_ce, "ConfigEntry"):
        _ha_ce.ConfigEntry = type("ConfigEntry", (), {})

    # --- homeassistant.helpers ---
    if "homeassistant.helpers" not in sys.modules:
        sys.modules["homeassistant.helpers"] = types.ModuleType("homeassistant.helpers")

    # --- homeassistant.helpers.entity_platform ---
    if "homeassistant.helpers.entity_platform" not in sys.modules:
        _ha_hep = types.ModuleType("homeassistant.helpers.entity_platform")
        _ha_hep.AddEntitiesCallback = object
        sys.modules["homeassistant.helpers.entity_platform"] = _ha_hep

    # --- homeassistant.helpers.update_coordinator ---
    _ha_uc = sys.modules.get("homeassistant.helpers.update_coordinator")
    if _ha_uc is None:
        _ha_uc = types.ModuleType("homeassistant.helpers.update_coordinator")
        sys.modules["homeassistant.helpers.update_coordinator"] = _ha_uc

    if not hasattr(_ha_uc, "CoordinatorEntity"):
        class CoordinatorEntity:
            def __init__(self, coordinator, *args, **kwargs):
                self.coordinator = coordinator
        _ha_uc.CoordinatorEntity = CoordinatorEntity

    if not hasattr(_ha_uc, "DataUpdateCoordinator"):
        class DataUpdateCoordinator:
            pass
        _ha_uc.DataUpdateCoordinator = DataUpdateCoordinator

    # --- homeassistant.components ---
    if "homeassistant.components" not in sys.modules:
        sys.modules["homeassistant.components"] = types.ModuleType("homeassistant.components")

    # --- homeassistant.components.sensor ---
    if "homeassistant.components.sensor" not in sys.modules:
        _ha_cs = types.ModuleType("homeassistant.components.sensor")

        class SensorEntity:
            pass

        class SensorStateClass:
            MEASUREMENT = "measurement"
            TOTAL = "total"

        class SensorDeviceClass:
            ENERGY = "energy"
            POWER = "power"
            MONETARY = "monetary"

        _ha_cs.SensorEntity = SensorEntity
        _ha_cs.SensorStateClass = SensorStateClass
        _ha_cs.SensorDeviceClass = SensorDeviceClass
        sys.modules["homeassistant.components.sensor"] = _ha_cs

    # --- homeassistant.components.number ---
    if "homeassistant.components.number" not in sys.modules:
        _ha_cn = types.ModuleType("homeassistant.components.number")

        class NumberEntity:
            pass

        class NumberMode:
            SLIDER = "slider"
            BOX = "box"

        _ha_cn.NumberEntity = NumberEntity
        _ha_cn.NumberMode = NumberMode
        sys.modules["homeassistant.components.number"] = _ha_cn


_ensure_ha_stubs()

import pytest
from datetime import datetime, timezone, time
from unittest.mock import MagicMock, PropertyMock

from heo2.models import RateSlot, ProgrammeState, ProgrammeInputs, SlotConfig
from heo2.cost_tracker import CostAccumulator


def _make_coordinator(
    inputs=None,
    programme=None,
    soc_trajectory=None,
    active_rule_names=None,
):
    """Create a mock coordinator with dashboard state."""
    coord = MagicMock()
    coord.last_inputs = inputs
    coord.current_programme = programme
    coord.soc_trajectory = soc_trajectory or [0.0] * 24
    type(coord).active_rule_names = PropertyMock(return_value=active_rule_names or [])
    return coord


def _make_entry(entry_id="test_entry"):
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


@pytest.fixture
def sample_inputs(now, igo_night_rates):
    return ProgrammeInputs(
        now=now,
        current_soc=50.0,
        battery_capacity_kwh=20.0,
        min_soc=20.0,
        import_rates=igo_night_rates,
        export_rates=[
            RateSlot(
                start=datetime(2026, 4, 13, 0, 0, tzinfo=timezone.utc),
                end=datetime(2026, 4, 13, 23, 59, tzinfo=timezone.utc),
                rate_pence=15.0,
            )
        ],
        solar_forecast_kwh=[0.0]*6 + [0.2, 0.8, 1.5, 2.5, 3.0, 3.5, 3.5, 3.0, 2.0, 1.0, 0.3, 0.0] + [0.0]*6,
        load_forecast_kwh=[1.9] * 24,
        igo_dispatching=False,
        saving_session=False,
        saving_session_start=None,
        saving_session_end=None,
        ev_charging=False,
        grid_connected=True,
        active_appliances=[],
        appliance_expected_kwh=0.0,
    )


class TestSolarForecastTodaySensor:
    def test_native_value_is_total_kwh(self, sample_inputs):
        from heo2.sensor import SolarForecastTodaySensor
        coord = _make_coordinator(inputs=sample_inputs)
        sensor = SolarForecastTodaySensor(coord, _make_entry())
        assert sensor.native_value == pytest.approx(21.3, abs=0.1)

    def test_hourly_attribute(self, sample_inputs):
        from heo2.sensor import SolarForecastTodaySensor
        coord = _make_coordinator(inputs=sample_inputs)
        sensor = SolarForecastTodaySensor(coord, _make_entry())
        attrs = sensor.extra_state_attributes
        assert "hourly" in attrs
        assert len(attrs["hourly"]) == 24

    def test_returns_none_when_no_inputs(self):
        from heo2.sensor import SolarForecastTodaySensor
        coord = _make_coordinator(inputs=None)
        sensor = SolarForecastTodaySensor(coord, _make_entry())
        assert sensor.native_value is None


class TestSolarForecastHourlySensor:
    def test_native_value_is_current_hour(self, sample_inputs):
        from heo2.sensor import SolarForecastHourlySensor
        coord = _make_coordinator(inputs=sample_inputs)
        sensor = SolarForecastHourlySensor(coord, _make_entry())
        # now fixture is hour 12, solar[12] = 3.5
        assert sensor.native_value == 3.5

    def test_forecast_attribute(self, sample_inputs):
        from heo2.sensor import SolarForecastHourlySensor
        coord = _make_coordinator(inputs=sample_inputs)
        sensor = SolarForecastHourlySensor(coord, _make_entry())
        attrs = sensor.extra_state_attributes
        assert "forecast" in attrs
        assert len(attrs["forecast"]) == 24


class TestCurrentImportRateSensor:
    def test_native_value(self, sample_inputs):
        from heo2.sensor import CurrentImportRateSensor
        coord = _make_coordinator(inputs=sample_inputs)
        sensor = CurrentImportRateSensor(coord, _make_entry())
        # At midday, day rate = 27.88
        assert sensor.native_value == 27.88


class TestSOCTrajectorySensor:
    def test_native_value_is_current_soc(self, sample_inputs):
        from heo2.sensor import SOCTrajectorySensor
        trajectory = [50.0] + [45.0] * 23
        coord = _make_coordinator(inputs=sample_inputs, soc_trajectory=trajectory)
        sensor = SOCTrajectorySensor(coord, _make_entry())
        assert sensor.native_value == 50.0

    def test_trajectory_attribute(self, sample_inputs):
        from heo2.sensor import SOCTrajectorySensor
        trajectory = [50.0 - i for i in range(24)]
        coord = _make_coordinator(inputs=sample_inputs, soc_trajectory=trajectory)
        sensor = SOCTrajectorySensor(coord, _make_entry())
        attrs = sensor.extra_state_attributes
        assert "trajectory" in attrs
        assert len(attrs["trajectory"]) == 24


class TestProgrammeSlotsSensor:
    def test_native_value_summary(self, default_programme):
        from heo2.sensor import ProgrammeSlotsSensor
        coord = _make_coordinator(programme=default_programme)
        sensor = ProgrammeSlotsSensor(coord, _make_entry())
        assert "6 slots" in sensor.native_value

    def test_slots_attribute(self, default_programme):
        from heo2.sensor import ProgrammeSlotsSensor
        coord = _make_coordinator(programme=default_programme)
        sensor = ProgrammeSlotsSensor(coord, _make_entry())
        attrs = sensor.extra_state_attributes
        assert "slots" in attrs
        assert len(attrs["slots"]) == 6
        assert "start" in attrs["slots"][0]
        assert "end" in attrs["slots"][0]
        assert "soc" in attrs["slots"][0]
        assert "grid_charge" in attrs["slots"][0]


class TestProgrammeReasonSensor:
    def test_native_value(self, default_programme):
        from heo2.sensor import ProgrammeReasonSensor
        default_programme.reason_log = ["CheapRate: target 80%", "Solar: hold"]
        coord = _make_coordinator(programme=default_programme)
        sensor = ProgrammeReasonSensor(coord, _make_entry())
        assert sensor.native_value == "Solar: hold"

    def test_reasons_attribute(self, default_programme):
        from heo2.sensor import ProgrammeReasonSensor
        default_programme.reason_log = ["CheapRate: target 80%", "Solar: hold"]
        coord = _make_coordinator(programme=default_programme)
        sensor = ProgrammeReasonSensor(coord, _make_entry())
        attrs = sensor.extra_state_attributes
        assert "reasons" in attrs
        assert len(attrs["reasons"]) == 2


class TestActiveRulesSensor:
    def test_native_value_count(self):
        from heo2.sensor import ActiveRulesSensor
        coord = _make_coordinator(active_rule_names=["cheap_rate_charge", "solar_surplus", "evening_protect"])
        sensor = ActiveRulesSensor(coord, _make_entry())
        assert sensor.native_value == 3

    def test_rules_attribute(self):
        from heo2.sensor import ActiveRulesSensor
        rules = ["cheap_rate_charge", "solar_surplus"]
        coord = _make_coordinator(active_rule_names=rules)
        sensor = ActiveRulesSensor(coord, _make_entry())
        attrs = sensor.extra_state_attributes
        assert attrs["rules"] == rules


# Group 2+3 tests

def _make_coordinator_with_costs(
    daily_import=1.50,
    daily_export=0.80,
    daily_solar=1.20,
    weekly_net=5.50,
    weekly_savings=3.20,
    octopus_monthly=45.00,
    octopus_last_month=52.00,
):
    coord = MagicMock()
    acc = CostAccumulator()
    acc.daily_import_cost = daily_import
    acc.daily_export_revenue = daily_export
    acc.daily_solar_value = daily_solar
    acc.weekly_net_cost = weekly_net
    acc.weekly_savings_vs_flat = weekly_savings
    acc.last_daily_reset = datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)
    acc.last_weekly_reset = datetime(2026, 4, 14, 0, 0, tzinfo=timezone.utc)
    coord.cost_accumulator = acc

    octopus = MagicMock()
    octopus.monthly_bill = octopus_monthly
    octopus.last_month_bill = octopus_last_month
    coord.octopus = octopus

    return coord


class TestDailyImportCostSensor:
    def test_native_value(self):
        from heo2.sensor import DailyImportCostSensor
        coord = _make_coordinator_with_costs(daily_import=1.50)
        sensor = DailyImportCostSensor(coord, _make_entry())
        assert sensor.native_value == pytest.approx(1.50)

    def test_last_reset(self):
        from heo2.sensor import DailyImportCostSensor
        coord = _make_coordinator_with_costs()
        sensor = DailyImportCostSensor(coord, _make_entry())
        assert sensor.last_reset == datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)


class TestDailyExportRevenueSensor:
    def test_native_value(self):
        from heo2.sensor import DailyExportRevenueSensor
        coord = _make_coordinator_with_costs(daily_export=0.80)
        sensor = DailyExportRevenueSensor(coord, _make_entry())
        assert sensor.native_value == pytest.approx(0.80)


class TestDailySolarValueSensor:
    def test_native_value(self):
        from heo2.sensor import DailySolarValueSensor
        coord = _make_coordinator_with_costs(daily_solar=1.20)
        sensor = DailySolarValueSensor(coord, _make_entry())
        assert sensor.native_value == pytest.approx(1.20)


class TestWeeklyNetCostSensor:
    def test_native_value(self):
        from heo2.sensor import WeeklyNetCostSensor
        coord = _make_coordinator_with_costs(weekly_net=5.50)
        sensor = WeeklyNetCostSensor(coord, _make_entry())
        assert sensor.native_value == pytest.approx(5.50)

    def test_last_reset(self):
        from heo2.sensor import WeeklyNetCostSensor
        coord = _make_coordinator_with_costs()
        sensor = WeeklyNetCostSensor(coord, _make_entry())
        assert sensor.last_reset == datetime(2026, 4, 14, 0, 0, tzinfo=timezone.utc)


class TestWeeklySavingsVsFlatSensor:
    def test_native_value(self):
        from heo2.sensor import WeeklySavingsVsFlatSensor
        coord = _make_coordinator_with_costs(weekly_savings=3.20)
        sensor = WeeklySavingsVsFlatSensor(coord, _make_entry())
        assert sensor.native_value == pytest.approx(3.20)


class TestOctopusMonthlyBillSensor:
    def test_native_value(self):
        from heo2.sensor import OctopusMonthlyBillSensor
        coord = _make_coordinator_with_costs(octopus_monthly=45.00)
        sensor = OctopusMonthlyBillSensor(coord, _make_entry())
        assert sensor.native_value == pytest.approx(45.00)

    def test_returns_none_when_no_octopus(self):
        from heo2.sensor import OctopusMonthlyBillSensor
        coord = MagicMock()
        coord.octopus = None
        sensor = OctopusMonthlyBillSensor(coord, _make_entry())
        assert sensor.native_value is None


class TestOctopusLastMonthBillSensor:
    def test_native_value(self):
        from heo2.sensor import OctopusLastMonthBillSensor
        coord = _make_coordinator_with_costs(octopus_last_month=52.00)
        sensor = OctopusLastMonthBillSensor(coord, _make_entry())
        assert sensor.native_value == pytest.approx(52.00)


# Group 4 tests

def _make_coordinator_with_roi(
    total_savings=2500.0,
    payback_progress=14.88,
    estimated_payback_date="2035-06-15",
    system_cost=16800.0,
    additional_costs=0.0,
):
    coord = MagicMock()
    type(coord).total_savings = PropertyMock(return_value=total_savings)
    type(coord).payback_progress = PropertyMock(return_value=payback_progress)
    type(coord).estimated_payback_date = PropertyMock(return_value=estimated_payback_date)
    type(coord).system_cost = PropertyMock(return_value=system_cost)
    type(coord).additional_costs = PropertyMock(return_value=additional_costs)
    coord._config = {
        "system_cost": system_cost,
        "additional_costs": additional_costs,
    }
    return coord


class TestTotalSavingsSensor:
    def test_native_value(self):
        from heo2.sensor import TotalSavingsSensor
        coord = _make_coordinator_with_roi(total_savings=2500.0)
        sensor = TotalSavingsSensor(coord, _make_entry())
        assert sensor.native_value == pytest.approx(2500.0)


class TestPaybackProgressSensor:
    def test_native_value(self):
        from heo2.sensor import PaybackProgressSensor
        coord = _make_coordinator_with_roi(payback_progress=14.88)
        sensor = PaybackProgressSensor(coord, _make_entry())
        # Sensor rounds to 1 decimal place: round(14.88, 1) == 14.9
        assert sensor.native_value == pytest.approx(14.9)


class TestEstimatedPaybackDateSensor:
    def test_native_value(self):
        from heo2.sensor import EstimatedPaybackDateSensor
        coord = _make_coordinator_with_roi(estimated_payback_date="2035-06-15")
        sensor = EstimatedPaybackDateSensor(coord, _make_entry())
        assert sensor.native_value == "2035-06-15"


class TestSystemCostNumber:
    def test_native_value(self):
        from heo2.number import SystemCostNumber
        coord = _make_coordinator_with_roi(system_cost=16800.0)
        sensor = SystemCostNumber(coord, _make_entry())
        assert sensor.native_value == 16800.0


class TestAdditionalCostsNumber:
    def test_native_value(self):
        from heo2.number import AdditionalCostsNumber
        coord = _make_coordinator_with_roi(additional_costs=500.0)
        sensor = AdditionalCostsNumber(coord, _make_entry())
        assert sensor.native_value == 500.0
