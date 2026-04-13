# tests/test_rules/test_igo_dispatch.py
"""Tests for IGODispatchRule."""

from heo2.models import ProgrammeState, ProgrammeInputs
from heo2.rules.baseline import BaselineRule
from heo2.rules.igo_dispatch import IGODispatchRule


def _make_baseline(inputs: ProgrammeInputs) -> ProgrammeState:
    return BaselineRule().apply(ProgrammeState.default(min_soc=20), inputs)


class TestIGODispatchRule:
    def test_no_change_when_not_dispatching(self, default_inputs):
        """No IGO dispatch → no change."""
        default_inputs.igo_dispatching = False
        state = _make_baseline(default_inputs)
        grid_before = [s.grid_charge for s in state.slots]
        rule = IGODispatchRule()
        result = rule.apply(state, default_inputs)
        grid_after = [s.grid_charge for s in result.slots]
        assert grid_before == grid_after

    def test_enables_grid_charge_on_current_slot(self, default_inputs):
        """IGO dispatch → enable grid charge on slot covering 'now'."""
        default_inputs.igo_dispatching = True
        state = _make_baseline(default_inputs)
        rule = IGODispatchRule()
        result = rule.apply(state, default_inputs)
        # now is 12:00 → falls in slot 2 (05:30–18:30)
        from datetime import time
        idx = result.find_slot_at(time(12, 0))
        assert result.slots[idx].grid_charge is True

    def test_raises_soc_target_to_at_least_current(self, default_inputs):
        """IGO dispatch → SOC target ≥ current SOC (charge, never drain)."""
        default_inputs.igo_dispatching = True
        default_inputs.current_soc = 65.0
        state = _make_baseline(default_inputs)
        rule = IGODispatchRule()
        result = rule.apply(state, default_inputs)
        from datetime import time
        idx = result.find_slot_at(time(12, 0))
        assert result.slots[idx].capacity_soc >= 65

    def test_reason_log(self, default_inputs):
        default_inputs.igo_dispatching = True
        state = _make_baseline(default_inputs)
        rule = IGODispatchRule()
        result = rule.apply(state, default_inputs)
        assert any("IGODispatch" in r for r in result.reason_log)
