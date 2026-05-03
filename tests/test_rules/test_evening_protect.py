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

    def test_heo6_slot_spanning_evening_boundary_gets_protected(
        self, default_inputs,
    ):
        """HEO-6 regression: pre-fix, the rule only protected slots
        whose `end_time <= 18:00`. Production plan slot 2 is 05:30-18:30
        (ends AFTER evening_start), so the bug left it unprotected and
        the inverter could discharge through the day to the export
        target, leaving zero reserve at 18:00.

        Now: any non-GC slot covering the evening_start time-of-day
        gets the floor.
        """
        from heo2.models import SlotConfig
        # Realistic production-shaped programme: charge overnight,
        # day slot 05:30-18:30 set to a low cap by some upstream rule,
        # evening drain slot 18:30-23:30, etc.
        prog = ProgrammeState(slots=[
            SlotConfig(time(0, 0), time(5, 30), 80, True),
            SlotConfig(time(5, 30), time(18, 30), 25, False),  # day slot
            SlotConfig(time(18, 30), time(23, 30), 10, False),
            SlotConfig(time(23, 30), time(23, 55), 80, True),
            SlotConfig(time(23, 55), time(23, 55), 10, False),
            SlotConfig(time(23, 55), time(0, 0), 10, False),
        ], reason_log=[])
        default_inputs.load_forecast_kwh = [1.0] * 18 + [2.5] * 6
        rule = EveningProtectRule()
        result = rule.apply(prog, default_inputs)
        # Slot 2 covers 18:00 (05:30 <= 18:00 < 18:30) -> must be raised.
        # required_soc = 20 + (15 kWh / 20 kWh * 100) = 95
        assert result.slots[1].capacity_soc >= 80, (
            f"slot 2 (05:30-18:30) was NOT protected; cap stayed at "
            f"{result.slots[1].capacity_soc}"
        )

    def test_grid_charge_slot_spanning_boundary_not_overridden(
        self, default_inputs,
    ):
        """A GC=True slot must keep its grid_charge target whatever
        time it spans - the cheap-charge rule owns those caps."""
        from heo2.models import SlotConfig
        prog = ProgrammeState(slots=[
            SlotConfig(time(0, 0), time(20, 0), 50, True),  # GC spanning evening
            SlotConfig(time(20, 0), time(22, 0), 30, False),
            SlotConfig(time(22, 0), time(23, 0), 30, False),
            SlotConfig(time(23, 0), time(23, 30), 30, False),
            SlotConfig(time(23, 30), time(23, 55), 30, False),
            SlotConfig(time(23, 55), time(0, 0), 30, False),
        ], reason_log=[])
        default_inputs.load_forecast_kwh = [1.0] * 18 + [2.5] * 6
        rule = EveningProtectRule()
        result = rule.apply(prog, default_inputs)
        # Slot 1 is GC=True, must NOT be touched
        assert result.slots[0].capacity_soc == 50
        assert result.slots[0].grid_charge is True
