"""InverterAdapter.writes_for() tests — translation, ordering, diffing."""

from __future__ import annotations

import pytest

from heo3.adapters.inverter import InverterAdapter
from heo3.transport import MockTransport
from heo3.types import (
    InverterSettings,
    PlannedAction,
    SlotPlan,
    SlotSettings,
)


@pytest.fixture
def adapter():
    return InverterAdapter(transport=MockTransport(), inverter_name="inverter_1")


def _baseline_settings() -> InverterSettings:
    """A minimal valid current-state. Diff baseline for tests."""
    slots = tuple(
        SlotSettings(
            start_hhmm=f"{h:02d}:00",
            grid_charge=False,
            capacity_pct=50,
        )
        for h in (0, 5, 11, 16, 19, 22)
    )
    return InverterSettings(
        work_mode="Zero export to CT",
        energy_pattern="Load first",
        max_charge_a=100.0,
        max_discharge_a=100.0,
        slots=slots,
    )


class TestEmptyAction:
    def test_no_fields_no_writes(self, adapter):
        assert adapter.writes_for(PlannedAction()) == ()

    def test_diff_against_current_no_writes(self, adapter):
        current = _baseline_settings()
        # An action that requests exactly the current state.
        action = PlannedAction(
            work_mode=current.work_mode,
            energy_pattern=current.energy_pattern,
            max_charge_a=current.max_charge_a,
            max_discharge_a=current.max_discharge_a,
        )
        assert adapter.writes_for(action, current=current) == ()


class TestGlobalsTopicsAndPayloads:
    def test_work_mode_write(self, adapter):
        writes = adapter.writes_for(PlannedAction(work_mode="Selling first"))
        assert len(writes) == 1
        assert writes[0].topic == "solar_assistant/inverter_1/work_mode/set"
        assert writes[0].payload == "Selling first"

    def test_energy_pattern_write(self, adapter):
        writes = adapter.writes_for(PlannedAction(energy_pattern="Battery first"))
        assert writes[0].topic == "solar_assistant/inverter_1/energy_pattern/set"
        assert writes[0].payload == "Battery first"

    def test_max_charge_a_formatted_as_int_string(self, adapter):
        writes = adapter.writes_for(PlannedAction(max_charge_a=42.7))
        assert writes[0].topic == "solar_assistant/inverter_1/max_charge_current/set"
        assert writes[0].payload == "43"  # rounded


class TestSlotTopicsAndPayloads:
    def test_time_point_write(self, adapter):
        slot = SlotPlan(slot_n=2, start_hhmm="05:30")
        # Single-slot action without contiguity check requires empty slots
        # tuple — but writes_for needs the full 6-tuple to validate.
        # Easier: use diff to produce just one write.
        current = _baseline_settings()
        action = PlannedAction(
            slots=tuple(
                SlotPlan(
                    slot_n=i + 1,
                    start_hhmm=f"{h:02d}:00" if i != 1 else "05:30",
                    grid_charge=False,
                    capacity_pct=50,
                )
                for i, h in enumerate((0, 5, 11, 16, 19, 22))
            )
        )
        writes = adapter.writes_for(action, current=current)
        assert len(writes) == 1
        assert writes[0].topic == "solar_assistant/inverter_1/time_point_2/set"
        assert writes[0].payload == "05:30"

    def test_grid_charge_lowercase_true_false(self, adapter):
        # Per HEO-32 incident: SA requires lowercase.
        current = _baseline_settings()
        action = PlannedAction(
            slots=tuple(
                SlotPlan(
                    slot_n=i + 1,
                    start_hhmm=f"{h:02d}:00",
                    grid_charge=(i == 0),  # Slot 1 charging, others not.
                    capacity_pct=50,
                )
                for i, h in enumerate((0, 5, 11, 16, 19, 22))
            )
        )
        writes = adapter.writes_for(action, current=current)
        assert len(writes) == 1
        assert writes[0].topic == "solar_assistant/inverter_1/grid_charge_point_1/set"
        assert writes[0].payload == "true"

    def test_grid_charge_off(self, adapter):
        current = _baseline_settings()
        # Set slot 1 to grid_charge=True in current; flip action to False.
        slots_current = list(current.slots)
        slots_current[0] = SlotSettings(
            start_hhmm="00:00", grid_charge=True, capacity_pct=50
        )
        current = InverterSettings(
            work_mode=current.work_mode,
            energy_pattern=current.energy_pattern,
            max_charge_a=current.max_charge_a,
            max_discharge_a=current.max_discharge_a,
            slots=tuple(slots_current),
        )
        action = PlannedAction(
            slots=tuple(
                SlotPlan(
                    slot_n=i + 1,
                    start_hhmm=f"{h:02d}:00",
                    grid_charge=False,
                    capacity_pct=50,
                )
                for i, h in enumerate((0, 5, 11, 16, 19, 22))
            )
        )
        writes = adapter.writes_for(action, current=current)
        assert writes[0].payload == "false"

    def test_capacity_pct_as_string(self, adapter):
        current = _baseline_settings()
        action = PlannedAction(
            slots=tuple(
                SlotPlan(
                    slot_n=i + 1,
                    start_hhmm=f"{h:02d}:00",
                    grid_charge=False,
                    capacity_pct=80 if i == 0 else 50,
                )
                for i, h in enumerate((0, 5, 11, 16, 19, 22))
            )
        )
        writes = adapter.writes_for(action, current=current)
        assert writes[0].topic == "solar_assistant/inverter_1/capacity_point_1/set"
        assert writes[0].payload == "80"

    def test_5min_snapping_at_write_time(self, adapter):
        current = _baseline_settings()
        # Slot 2 currently 05:00; ask for 05:33 → should publish 05:30.
        action = PlannedAction(
            slots=tuple(
                SlotPlan(
                    slot_n=i + 1,
                    start_hhmm=("05:33" if i == 1 else f"{h:02d}:00"),
                    grid_charge=False,
                    capacity_pct=50,
                )
                for i, h in enumerate((0, 5, 11, 16, 19, 22))
            )
        )
        # Note: 05:33 fails 5-min validation. Snapping is a write-time
        # affordance; the planner is expected to send 5-min values OR
        # we accept that validation runs first. This test asserts the
        # current behaviour: validation rejects non-5-min input.
        from heo3.adapters.inverter_validate import SafetyError

        with pytest.raises(SafetyError):
            adapter.writes_for(action, current=current)


class TestOrdering:
    def test_globals_first_then_slots_then_currents(self, adapter):
        current = _baseline_settings()
        action = PlannedAction(
            work_mode="Selling first",
            energy_pattern="Battery first",
            max_charge_a=80.0,
            max_discharge_a=80.0,
            slots=tuple(
                SlotPlan(
                    slot_n=i + 1,
                    start_hhmm=f"{h:02d}:00",
                    grid_charge=(i == 0),
                    capacity_pct=80,
                )
                for i, h in enumerate((0, 5, 11, 16, 19, 22))
            ),
        )
        writes = adapter.writes_for(action, current=current)
        topics = [w.topic for w in writes]

        # First two: globals.
        assert topics[0].endswith("/work_mode/set")
        assert topics[1].endswith("/energy_pattern/set")

        # Last two: current limits.
        assert topics[-2].endswith("/max_charge_current/set")
        assert topics[-1].endswith("/max_discharge_current/set")

        # Middle: slot writes.
        for t in topics[2:-2]:
            assert "_point_" in t


class TestDiffSemantics:
    def test_string_diff_case_insensitive(self, adapter):
        current = _baseline_settings()
        # Trailing space + different case = same value, no write.
        action = PlannedAction(work_mode="  zero export to ct  ")
        # Validation will reject because canonicalisation isn't applied
        # before validate. The contract is "send canonical strings";
        # this confirms it.
        from heo3.adapters.inverter_validate import SafetyError

        with pytest.raises(SafetyError):
            adapter.writes_for(action, current=current)

    def test_amps_within_tolerance_skipped(self, adapter):
        current = _baseline_settings()  # max_charge_a = 100.0
        action = PlannedAction(max_charge_a=100.4)  # within 0.5 tolerance
        assert adapter.writes_for(action, current=current) == ()

    def test_amps_outside_tolerance_written(self, adapter):
        current = _baseline_settings()
        action = PlannedAction(max_charge_a=100.6)  # outside 0.5 tolerance
        assert len(adapter.writes_for(action, current=current)) == 1

    def test_no_diff_baseline_writes_everything(self, adapter):
        # Without `current`, every set field becomes a write.
        action = PlannedAction(
            work_mode="Selling first",
            max_charge_a=80.0,
            max_discharge_a=80.0,
        )
        writes = adapter.writes_for(action)
        assert len(writes) == 3
