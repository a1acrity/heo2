"""Safety-invariant unit tests for inverter writes (§17)."""

from __future__ import annotations

import pytest

from heo3.adapters.inverter_validate import (
    SafetyError,
    snap_to_5min,
    validate_action,
)
from heo3.types import PlannedAction, SlotPlan


def _all_six_slots(start_hours=(0, 5, 11, 16, 19, 22), capacity=50):
    """Build a contiguous, valid 6-slot tuple."""
    return tuple(
        SlotPlan(
            slot_n=i + 1,
            start_hhmm=f"{h:02d}:00",
            grid_charge=False,
            capacity_pct=capacity,
        )
        for i, h in enumerate(start_hours)
    )


class TestSnapTo5Min:
    @pytest.mark.parametrize(
        "raw,snapped",
        [
            ("23:57", "23:55"),
            ("00:04", "00:00"),
            ("12:30", "12:30"),
            ("06:11", "06:10"),
            ("06:14", "06:10"),
            ("06:15", "06:15"),
        ],
    )
    def test_floors_to_5_minute_boundary(self, raw, snapped):
        assert snap_to_5min(raw) == snapped


class TestGlobals:
    def test_valid_work_mode_passes(self):
        validate_action(
            PlannedAction(work_mode="Selling first"), min_soc=10, eps_active=False
        )

    def test_invalid_work_mode_rejected(self):
        with pytest.raises(SafetyError, match="work_mode"):
            validate_action(
                PlannedAction(work_mode="Sell"), min_soc=10, eps_active=False
            )

    def test_invalid_energy_pattern_rejected(self):
        with pytest.raises(SafetyError, match="energy_pattern"):
            validate_action(
                PlannedAction(energy_pattern="Battery"),
                min_soc=10,
                eps_active=False,
            )

    @pytest.mark.parametrize("amps", [-1.0, 351.0, 1000.0])
    def test_amps_out_of_range(self, amps):
        with pytest.raises(SafetyError, match="max_charge_a"):
            validate_action(
                PlannedAction(max_charge_a=amps), min_soc=10, eps_active=False
            )

    @pytest.mark.parametrize("amps", [0.0, 1.0, 100.0, 350.0])
    def test_amps_within_range(self, amps):
        validate_action(
            PlannedAction(max_charge_a=amps), min_soc=10, eps_active=False
        )


class TestSlots:
    def test_empty_slots_skip_slot_validation(self):
        # No slots = "don't touch slots this tick" — no contiguity needed.
        validate_action(
            PlannedAction(work_mode="Selling first"),
            min_soc=10,
            eps_active=False,
        )

    def test_partial_slot_subset_allowed(self):
        # Build constructors emit partial subsets (e.g. drain_to
        # only touches the slot covering [now, by]). Validator
        # should accept any subset 1-6.
        validate_action(
            PlannedAction(
                slots=(
                    SlotPlan(slot_n=5, capacity_pct=25),
                    SlotPlan(slot_n=6, capacity_pct=25),
                ),
            ),
            min_soc=10, eps_active=False,
        )

    def test_too_many_slots_rejected(self):
        slots = tuple(
            SlotPlan(slot_n=i) for i in [1, 2, 3, 4, 5, 6, 1]
        )
        with pytest.raises(SafetyError, match="max 6"):
            validate_action(
                PlannedAction(slots=slots), min_soc=10, eps_active=False,
            )

    def test_duplicate_slot_n_rejected(self):
        # 6 slots but with a duplicate slot_n
        slots = tuple(
            SlotPlan(slot_n=i) for i in [1, 2, 2, 4, 5, 6]
        )
        with pytest.raises(SafetyError, match="duplicate"):
            validate_action(
                PlannedAction(slots=slots), min_soc=10, eps_active=False,
            )

    def test_six_valid_slots_pass(self):
        validate_action(
            PlannedAction(slots=_all_six_slots()),
            min_soc=10,
            eps_active=False,
        )

    @pytest.mark.parametrize("soc", [-1, 101, 200])
    def test_soc_out_of_range(self, soc):
        slots = _all_six_slots(capacity=soc)
        with pytest.raises(SafetyError, match="capacity_pct"):
            validate_action(
                PlannedAction(slots=slots), min_soc=10, eps_active=False
            )

    def test_below_min_soc_rejected(self):
        slots = _all_six_slots(capacity=5)  # below min_soc=10
        with pytest.raises(SafetyError, match="below min_soc"):
            validate_action(
                PlannedAction(slots=slots), min_soc=10, eps_active=False
            )

    def test_below_min_soc_allowed_during_eps(self):
        # SPEC H3: EPS lockdown overrides min_soc (cap=0 is required).
        slots = _all_six_slots(capacity=0)
        validate_action(
            PlannedAction(slots=slots), min_soc=10, eps_active=True
        )

    def test_non_5min_minute_rejected(self):
        slots = list(_all_six_slots())
        slots[0] = SlotPlan(
            slot_n=1, start_hhmm="00:03", grid_charge=False, capacity_pct=50
        )
        with pytest.raises(SafetyError, match="5-min boundary"):
            validate_action(
                PlannedAction(slots=tuple(slots)),
                min_soc=10,
                eps_active=False,
            )

    def test_slot_1_must_start_at_midnight(self):
        slots = list(_all_six_slots())
        slots[0] = SlotPlan(
            slot_n=1, start_hhmm="01:00", grid_charge=False, capacity_pct=50
        )
        with pytest.raises(SafetyError, match="slot 1 must start at 00:00"):
            validate_action(
                PlannedAction(slots=tuple(slots)),
                min_soc=10,
                eps_active=False,
            )

    def test_malformed_hhmm_rejected(self):
        slots = list(_all_six_slots())
        slots[0] = SlotPlan(
            slot_n=1, start_hhmm="garbage", grid_charge=False, capacity_pct=50
        )
        with pytest.raises(SafetyError, match="not HH:MM"):
            validate_action(
                PlannedAction(slots=tuple(slots)),
                min_soc=10,
                eps_active=False,
            )

    def test_invalid_slot_n_rejected(self):
        slots = (
            SlotPlan(slot_n=7, start_hhmm="00:00", grid_charge=False, capacity_pct=50),
        ) + tuple(
            SlotPlan(slot_n=i, start_hhmm=f"{(i-1)*4:02d}:00", grid_charge=False, capacity_pct=50)
            for i in range(2, 7)
        )
        with pytest.raises(SafetyError, match="slot_n must be 1..6"):
            validate_action(
                PlannedAction(slots=slots), min_soc=10, eps_active=False
            )
