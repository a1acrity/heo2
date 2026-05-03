"""Tests for BaselineRule."""

from datetime import time, datetime, timezone

from heo2.models import ProgrammeState, ProgrammeInputs
from heo2.rules.baseline import BaselineRule


class TestBaselineRule:
    def test_produces_six_slots(self, default_inputs):
        rule = BaselineRule()
        state = ProgrammeState.default(min_soc=20)
        result = rule.apply(state, default_inputs)
        assert len(result.slots) == 6

    def test_slot1_starts_midnight(self, default_inputs):
        rule = BaselineRule()
        state = ProgrammeState.default(min_soc=20)
        result = rule.apply(state, default_inputs)
        assert result.slots[0].start_time == time(0, 0)

    def test_overnight_slots_have_grid_charge(self, default_inputs):
        rule = BaselineRule()
        state = ProgrammeState.default(min_soc=20)
        result = rule.apply(state, default_inputs)
        # Slot 1: 00:00-05:30 should have grid_charge (overnight cheap rate)
        overnight = result.slots[0]
        assert overnight.grid_charge is True
        assert overnight.end_time == time(5, 30)

    def test_day_slot_no_grid_charge(self, default_inputs):
        rule = BaselineRule()
        state = ProgrammeState.default(min_soc=20)
        result = rule.apply(state, default_inputs)
        # Slot 2: 05:30-18:30 should have no grid charge, high SOC target
        day_slot = result.slots[1]
        assert day_slot.grid_charge is False
        assert day_slot.start_time == time(5, 30)
        assert day_slot.capacity_soc == 100  # let solar fill up

    def test_evening_slot_drains_to_min_soc(self, default_inputs):
        rule = BaselineRule()
        state = ProgrammeState.default(min_soc=20)
        result = rule.apply(state, default_inputs)
        # Slot 3: 18:30-23:30 should drain to min_soc
        evening = result.slots[2]
        assert evening.start_time == time(18, 30)
        assert evening.end_time == time(23, 30)
        assert evening.capacity_soc == 20
        assert evening.grid_charge is False

    def test_late_night_slot_grid_charge(self, default_inputs):
        rule = BaselineRule()
        state = ProgrammeState.default(min_soc=20)
        result = rule.apply(state, default_inputs)
        # Slot 4: 23:30-23:57 should have grid charge (next overnight)
        late = result.slots[3]
        assert late.start_time == time(23, 30)
        assert late.grid_charge is True

    def test_filler_slots_present(self, default_inputs):
        rule = BaselineRule()
        state = ProgrammeState.default(min_soc=20)
        result = rule.apply(state, default_inputs)
        # Slots 5 and 6 are short fillers
        assert result.slots[4].duration_minutes() <= 2
        assert result.slots[5].duration_minutes() <= 2

    def test_validates_successfully(self, default_inputs):
        rule = BaselineRule()
        state = ProgrammeState.default(min_soc=20)
        result = rule.apply(state, default_inputs)
        assert result.validate() == []

    def test_reason_log_populated(self, default_inputs):
        rule = BaselineRule()
        state = ProgrammeState.default(min_soc=20)
        result = rule.apply(state, default_inputs)
        assert len(result.reason_log) >= 1
        assert "Baseline" in result.reason_log[0]

    def test_custom_off_peak_window(self, default_inputs):
        rule = BaselineRule(
            off_peak_start=time(0, 30),
            off_peak_end=time(4, 30),
        )
        state = ProgrammeState.default(min_soc=20)
        result = rule.apply(state, default_inputs)
        assert result.slots[0].end_time == time(4, 30)

    def test_sets_default_work_mode(self, default_inputs):
        """SPEC §2: BaselineRule resets work_mode to the safe default
        every tick. SavingSessionRule overrides to 'Selling first'
        when active; once the session ends, baseline runs again on
        the next tick and resets here so the inverter stops exporting."""
        rule = BaselineRule()
        state = ProgrammeState.default(min_soc=20)
        result = rule.apply(state, default_inputs)
        assert result.work_mode == "Zero export to CT"
