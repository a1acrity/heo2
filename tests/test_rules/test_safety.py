# tests/test_rules/test_safety.py
"""Tests for SafetyRule."""

from datetime import time

from heo2.models import ProgrammeState, ProgrammeInputs, SlotConfig
from heo2.rules.safety import SafetyRule


class TestSafetyRule:
    def test_enforces_min_soc_floor(self, default_inputs):
        """No slot should have SOC target below min_soc."""
        state = ProgrammeState.default(min_soc=20)
        state.slots[2].capacity_soc = 10  # below min
        rule = SafetyRule()
        result = rule.apply(state, default_inputs)
        assert result.slots[2].capacity_soc >= 20

    def test_ensures_six_slots(self, default_inputs):
        """If somehow we have fewer than 6 slots, SafetyRule must fix it."""
        state = ProgrammeState(
            slots=[
                SlotConfig(time(0, 0), time(12, 0), 50, False),
                SlotConfig(time(12, 0), time(0, 0), 50, False),
            ],
            reason_log=[],
        )
        rule = SafetyRule()
        result = rule.apply(state, default_inputs)
        assert len(result.slots) == 6

    def test_ensures_starts_at_midnight(self, default_inputs):
        state = ProgrammeState.default(min_soc=20)
        rule = SafetyRule()
        result = rule.apply(state, default_inputs)
        assert result.slots[0].start_time == time(0, 0)

    def test_ensures_contiguous(self, default_inputs):
        state = ProgrammeState.default(min_soc=20)
        rule = SafetyRule()
        result = rule.apply(state, default_inputs)
        for i in range(5):
            assert result.slots[i].end_time == result.slots[i + 1].start_time

    def test_clamps_soc_to_100(self, default_inputs):
        """SOC target above 100 gets clamped."""
        state = ProgrammeState.default(min_soc=20)
        state.slots[0].capacity_soc = 150
        rule = SafetyRule()
        result = rule.apply(state, default_inputs)
        assert result.slots[0].capacity_soc == 100

    def test_cannot_be_disabled(self):
        rule = SafetyRule()
        rule.enabled = False  # someone tries to disable it
        assert rule.enabled is True  # SafetyRule overrides

    def test_reason_log_records_fixes(self, default_inputs):
        state = ProgrammeState.default(min_soc=20)
        state.slots[0].capacity_soc = 5  # below min
        rule = SafetyRule()
        result = rule.apply(state, default_inputs)
        assert any("Safety" in r for r in result.reason_log)

    def test_valid_programme_passes_unchanged(self, default_inputs):
        """A valid programme should pass through with minimal changes."""
        state = ProgrammeState.default(min_soc=20)
        socs_before = [s.capacity_soc for s in state.slots]
        rule = SafetyRule()
        result = rule.apply(state, default_inputs)
        socs_after = [s.capacity_soc for s in result.slots]
        assert socs_before == socs_after
