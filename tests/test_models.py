"""Tests for HEO II data model."""

from datetime import time, datetime, timezone

from heo2.models import (
    RateSlot,
    SlotConfig,
    ProgrammeState,
    ProgrammeInputs,
)


class TestSlotConfig:
    def test_duration_minutes_normal(self):
        slot = SlotConfig(
            start_time=time(5, 30),
            end_time=time(18, 30),
            capacity_soc=100,
            grid_charge=False,
        )
        assert slot.duration_minutes() == 780  # 13 hours

    def test_duration_minutes_crosses_midnight(self):
        slot = SlotConfig(
            start_time=time(23, 30),
            end_time=time(0, 0),
            capacity_soc=100,
            grid_charge=True,
        )
        assert slot.duration_minutes() == 30

    def test_contains_time_normal(self):
        slot = SlotConfig(
            start_time=time(5, 30),
            end_time=time(18, 30),
            capacity_soc=100,
            grid_charge=False,
        )
        assert slot.contains_time(time(12, 0)) is True
        assert slot.contains_time(time(5, 30)) is True  # inclusive start
        assert slot.contains_time(time(18, 30)) is False  # exclusive end
        assert slot.contains_time(time(4, 0)) is False

    def test_contains_time_crosses_midnight(self):
        slot = SlotConfig(
            start_time=time(23, 0),
            end_time=time(5, 0),
            capacity_soc=100,
            grid_charge=True,
        )
        assert slot.contains_time(time(23, 30)) is True
        assert slot.contains_time(time(1, 0)) is True
        assert slot.contains_time(time(12, 0)) is False


class TestProgrammeState:
    def test_default_has_six_slots(self):
        ps = ProgrammeState.default(min_soc=20)
        assert len(ps.slots) == 6

    def test_default_covers_full_day(self):
        ps = ProgrammeState.default(min_soc=20)
        assert ps.slots[0].start_time == time(0, 0)
        # Last slot should end at 00:00 (midnight wrap)
        assert ps.slots[-1].end_time == time(0, 0)

    def test_default_all_slots_at_min_soc(self):
        ps = ProgrammeState.default(min_soc=25)
        for slot in ps.slots:
            assert slot.capacity_soc == 25
            assert slot.grid_charge is False

    def test_default_slots_contiguous(self):
        ps = ProgrammeState.default(min_soc=20)
        for i in range(len(ps.slots) - 1):
            assert ps.slots[i].end_time == ps.slots[i + 1].start_time

    def test_find_slot_at(self):
        ps = ProgrammeState.default(min_soc=20)
        # Default slots: 00:00-04:00, 04:00-08:00, ...
        idx = ps.find_slot_at(time(6, 0))
        assert ps.slots[idx].start_time == time(4, 0)

    def test_validate_passes_for_default(self):
        ps = ProgrammeState.default(min_soc=20)
        errors = ps.validate()
        assert errors == []

    def test_validate_catches_wrong_slot_count(self):
        ps = ProgrammeState(slots=[], reason_log=[])
        errors = ps.validate()
        assert any("6 slots" in e for e in errors)

    def test_validate_catches_soc_below_zero(self):
        ps = ProgrammeState.default(min_soc=20)
        ps.slots[0].capacity_soc = -1
        errors = ps.validate()
        assert any("SOC" in e for e in errors)

    def test_validate_catches_gap(self):
        ps = ProgrammeState.default(min_soc=20)
        ps.slots[1].start_time = time(5, 0)  # gap between slot 0 end (04:00) and slot 1 start (05:00)
        errors = ps.validate()
        assert any("contiguous" in e.lower() or "gap" in e.lower() for e in errors)

    def test_insert_boundary(self):
        ps = ProgrammeState.default(min_soc=20)
        # Default has 6 evenly-spaced slots. Split the 04:00-08:00 slot at 06:00.
        ok = ps.insert_boundary(time(6, 0), reason="test split")
        assert ok is True
        assert len(ps.slots) == 6  # still 6 -- consumed a filler
        # Check that a slot boundary exists at 06:00
        boundaries = [s.start_time for s in ps.slots] + [ps.slots[-1].end_time]
        assert time(6, 0) in boundaries

    def test_insert_boundary_returns_false_when_no_fillers(self):
        """All 6 slots are long -- no filler to consume."""
        ps = ProgrammeState(
            slots=[
                SlotConfig(time(0, 0), time(4, 0), 20, False),
                SlotConfig(time(4, 0), time(8, 0), 20, False),
                SlotConfig(time(8, 0), time(12, 0), 20, False),
                SlotConfig(time(12, 0), time(16, 0), 20, False),
                SlotConfig(time(16, 0), time(20, 0), 20, False),
                SlotConfig(time(20, 0), time(0, 0), 20, False),
            ],
            reason_log=[],
        )
        ok = ps.insert_boundary(time(10, 0), reason="test")
        assert ok is False  # no short fillers to consume


class TestRateSlot:
    def test_rate_at_time(self):
        rs = RateSlot(
            start=datetime(2026, 4, 13, 5, 30, tzinfo=timezone.utc),
            end=datetime(2026, 4, 13, 23, 30, tzinfo=timezone.utc),
            rate_pence=27.88,
        )
        assert rs.rate_pence == 27.88
