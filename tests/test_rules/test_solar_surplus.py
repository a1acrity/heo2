"""Tests for SolarSurplusRule."""

from datetime import time

from heo2.models import ProgrammeState, ProgrammeInputs, SlotConfig
from heo2.rules.baseline import BaselineRule
from heo2.rules.solar_surplus import SolarSurplusRule


def _make_baseline(inputs: ProgrammeInputs) -> ProgrammeState:
    return BaselineRule().apply(ProgrammeState.default(min_soc=20), inputs)


class TestSolarSurplusRule:
    def test_no_change_when_no_solar(self, default_inputs):
        """Zero solar forecast -> no modifications."""
        default_inputs.solar_forecast_kwh = [0.0] * 24
        state = _make_baseline(default_inputs)
        original_socs = [s.capacity_soc for s in state.slots]
        rule = SolarSurplusRule()
        result = rule.apply(state, default_inputs)
        result_socs = [s.capacity_soc for s in result.slots]
        assert original_socs == result_socs

    def test_day_slot_soc_rises_with_solar(self, default_inputs):
        """Good solar forecast -> day slot SOC target reflects expected PV charge."""
        default_inputs.solar_forecast_kwh = [0.0] * 6 + [2.0] * 10 + [0.0] * 8  # 20 kWh solar
        default_inputs.load_forecast_kwh = [1.0] * 24  # 24 kWh load
        default_inputs.current_soc = 40.0
        state = _make_baseline(default_inputs)
        rule = SolarSurplusRule()
        result = rule.apply(state, default_inputs)
        # Day slot should have a target reflecting solar surplus
        day_slot = result.slots[1]  # 05:30-18:30
        assert day_slot.capacity_soc >= 40  # at least current SOC
        assert day_slot.grid_charge is False  # never grid charge during solar

    def test_does_not_enable_grid_charge(self, default_inputs):
        """SolarSurplusRule must never enable grid charge."""
        default_inputs.solar_forecast_kwh = [0.0] * 6 + [3.0] * 10 + [0.0] * 8
        state = _make_baseline(default_inputs)
        rule = SolarSurplusRule()
        result = rule.apply(state, default_inputs)
        # Check that day slot still has grid_charge=False
        day_slot = result.slots[1]
        assert day_slot.grid_charge is False

    def test_reason_log_entry(self, default_inputs):
        default_inputs.solar_forecast_kwh = [0.0] * 6 + [2.0] * 10 + [0.0] * 8
        state = _make_baseline(default_inputs)
        rule = SolarSurplusRule()
        result = rule.apply(state, default_inputs)
        assert any("SolarSurplus" in r for r in result.reason_log)
