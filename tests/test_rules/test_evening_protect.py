# tests/test_rules/test_evening_protect.py
"""Tests for EveningProtectRule."""

from datetime import time

from heo2.models import ProgrammeState, ProgrammeInputs
from heo2.rules.baseline import BaselineRule
from heo2.rules.evening_protect import EveningProtectRule


def _make_baseline(inputs: ProgrammeInputs) -> ProgrammeState:
    return BaselineRule().apply(ProgrammeState.default(min_soc=20), inputs)


class TestEveningProtectRule:
    def test_raises_pre_evening_slot_soc(self, default_inputs):
        """Ensures enough battery reserve before evening peak."""
        default_inputs.load_forecast_kwh = [1.0] * 18 + [2.0] * 6
        state = _make_baseline(default_inputs)
        rule = EveningProtectRule()
        result = rule.apply(state, default_inputs)
        day_slot = result.slots[1]  # 05:30–18:30
        assert day_slot.capacity_soc >= 80

    def test_no_change_when_evening_demand_low(self, default_inputs):
        """Low evening demand → existing programme is fine."""
        default_inputs.load_forecast_kwh = [1.0] * 18 + [0.5] * 6
        state = _make_baseline(default_inputs)
        day_soc_before = state.slots[1].capacity_soc
        rule = EveningProtectRule()
        result = rule.apply(state, default_inputs)
        day_slot = result.slots[1]
        assert day_slot.capacity_soc >= int(default_inputs.min_soc) + 15

    def test_reason_log(self, default_inputs):
        default_inputs.load_forecast_kwh = [1.0] * 18 + [2.0] * 6
        state = _make_baseline(default_inputs)
        rule = EveningProtectRule()
        result = rule.apply(state, default_inputs)
        assert any("EveningProtect" in r for r in result.reason_log)

    def test_custom_evening_window(self, default_inputs):
        """Custom evening start/end."""
        default_inputs.load_forecast_kwh = [1.0] * 17 + [3.0] * 7
        state = _make_baseline(default_inputs)
        rule = EveningProtectRule(evening_start_hour=17, evening_end_hour=24)
        result = rule.apply(state, default_inputs)
        assert any("EveningProtect" in r for r in result.reason_log)
