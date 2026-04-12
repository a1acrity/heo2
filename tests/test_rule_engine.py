"""Tests for the rule engine framework."""

from datetime import time

from heo2.models import ProgrammeState, ProgrammeInputs, SlotConfig
from heo2.rule_engine import Rule, RuleEngine


class StubRuleSetSoc(Rule):
    """Test rule that sets slot 0 SOC to 80."""
    name = "stub_soc"
    description = "Test rule"

    def apply(self, state: ProgrammeState, inputs: ProgrammeInputs) -> ProgrammeState:
        state.slots[0].capacity_soc = 80
        state.reason_log.append("StubRuleSetSoc: set slot 0 to 80%")
        return state


class StubRuleGridCharge(Rule):
    """Test rule that enables grid charge on slot 0."""
    name = "stub_grid"
    description = "Test rule"

    def apply(self, state: ProgrammeState, inputs: ProgrammeInputs) -> ProgrammeState:
        state.slots[0].grid_charge = True
        state.reason_log.append("StubRuleGridCharge: enabled grid charge")
        return state


class TestRuleEngine:
    def test_empty_engine_returns_default(self, default_inputs):
        engine = RuleEngine(rules=[])
        result = engine.calculate(default_inputs)
        assert len(result.slots) == 6
        assert result.reason_log == []

    def test_single_rule_applied(self, default_inputs):
        engine = RuleEngine(rules=[StubRuleSetSoc()])
        result = engine.calculate(default_inputs)
        assert result.slots[0].capacity_soc == 80

    def test_rules_applied_in_order(self, default_inputs):
        engine = RuleEngine(rules=[StubRuleSetSoc(), StubRuleGridCharge()])
        result = engine.calculate(default_inputs)
        assert result.slots[0].capacity_soc == 80
        assert result.slots[0].grid_charge is True
        assert len(result.reason_log) == 2

    def test_disabled_rule_skipped(self, default_inputs):
        rule = StubRuleSetSoc()
        rule.enabled = False
        engine = RuleEngine(rules=[rule])
        result = engine.calculate(default_inputs)
        assert result.slots[0].capacity_soc == 20  # unchanged

    def test_min_soc_from_inputs(self, default_inputs):
        engine = RuleEngine(rules=[])
        result = engine.calculate(default_inputs)
        for slot in result.slots:
            assert slot.capacity_soc == int(default_inputs.min_soc)
