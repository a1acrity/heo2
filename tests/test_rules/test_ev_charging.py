# tests/test_rules/test_ev_charging.py
"""Tests for EVChargingRule."""

from heo2.models import ProgrammeState, ProgrammeInputs
from heo2.rules.baseline import BaselineRule
from heo2.rules.ev_charging import EVChargingRule


def _make_baseline(inputs: ProgrammeInputs) -> ProgrammeState:
    return BaselineRule().apply(ProgrammeState.default(min_soc=20), inputs)


class TestEVChargingRule:
    def test_no_change_when_not_charging(self, default_inputs):
        default_inputs.ev_charging = False
        state = _make_baseline(default_inputs)
        socs_before = [s.capacity_soc for s in state.slots]
        rule = EVChargingRule()
        result = rule.apply(state, default_inputs)
        assert [s.capacity_soc for s in result.slots] == socs_before

    def test_holds_soc_during_ev_charging(self, default_inputs):
        """EV charging → current slot SOC raised to at least current SOC."""
        default_inputs.ev_charging = True
        default_inputs.current_soc = 60.0
        state = _make_baseline(default_inputs)
        rule = EVChargingRule()
        result = rule.apply(state, default_inputs)
        from datetime import time
        idx = result.find_slot_at(time(12, 0))
        assert result.slots[idx].capacity_soc >= 60

    def test_no_effect_during_igo_dispatch(self, default_inputs):
        """EV + IGO dispatch → IGODispatchRule takes precedence, EV rule skips."""
        default_inputs.ev_charging = True
        default_inputs.igo_dispatching = True
        state = _make_baseline(default_inputs)
        rule = EVChargingRule()
        socs_before = [s.capacity_soc for s in state.slots]
        result = rule.apply(state, default_inputs)
        assert [s.capacity_soc for s in result.slots] == socs_before

    def test_reason_log(self, default_inputs):
        default_inputs.ev_charging = True
        default_inputs.current_soc = 50.0
        state = _make_baseline(default_inputs)
        rule = EVChargingRule()
        result = rule.apply(state, default_inputs)
        assert any("EVCharging" in r for r in result.reason_log)
