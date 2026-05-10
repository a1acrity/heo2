"""ActionBuilder tests — 11 constructors per §13."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from heo3.build import ActionBuilder, BASELINE_SLOT_CAPS, BASELINE_SLOT_TIMES
from heo3.compute import RateWindow
from heo3.types import (
    ApplianceAction,
    EVAction,
    PlannedAction,
    SlotPlan,
    SystemConfig,
    TeslaAction,
    TimeRange,
)

from .heo3_fixtures import make_snapshot


@pytest.fixture
def builder():
    return ActionBuilder()


# ── 13a Energy actions ─────────────────────────────────────────────


class TestSellKwh:
    def test_no_kwh_returns_empty(self, builder):
        snap = make_snapshot()
        plan = builder.sell_kwh(
            total_kwh=0, across_slots=[], snap=snap
        )
        assert plan.work_mode is None
        assert plan.slots == ()

    def test_single_active_slot_sets_mode_and_amps(self, builder):
        captured = datetime(2026, 5, 10, 17, 0, tzinfo=timezone.utc)
        snap = make_snapshot(captured_at=captured, soc_pct=80)
        # Active sell window: 17:00-18:00 right now.
        active = RateWindow(
            start=captured,
            end=captured + timedelta(hours=1),
            rate_pence=30.0,
            avg_rate_pence=30.0,
        )
        plan = builder.sell_kwh(
            total_kwh=2.56,  # 2.56 kWh in 1h @ 51.2V → 50A
            across_slots=[active],
            snap=snap,
        )
        assert plan.work_mode == "Selling first"
        assert plan.max_discharge_a == pytest.approx(50.0, rel=0.01)
        assert plan.spec_h4_live_rates is True

    def test_no_active_slot_returns_no_writes(self, builder):
        captured = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
        # All slots in the future
        future = RateWindow(
            start=captured + timedelta(hours=4),
            end=captured + timedelta(hours=5),
            rate_pence=30.0,
            avg_rate_pence=30.0,
        )
        plan = builder.sell_kwh(
            total_kwh=1.0, across_slots=[future], snap=make_snapshot(captured_at=captured)
        )
        # No active slot — work_mode/amps left alone for a future tick.
        assert plan.work_mode is None
        assert plan.max_discharge_a is None


class TestChargeTo:
    def test_basic(self, builder):
        captured = datetime(2026, 5, 10, 0, 30, tzinfo=timezone.utc)
        snap = make_snapshot(captured_at=captured)
        by = captured + timedelta(hours=4)
        plan = builder.charge_to(
            target_soc_pct=80, by=by, snap=snap
        )
        # At least one slot got cap=80 + gc=True.
        assert plan.slots
        for slot in plan.slots:
            assert slot.capacity_pct == 80
            assert slot.grid_charge is True

    def test_with_rate_limit(self, builder):
        captured = datetime(2026, 5, 10, 0, 30, tzinfo=timezone.utc)
        snap = make_snapshot(captured_at=captured)
        by = captured + timedelta(hours=4)
        plan = builder.charge_to(
            target_soc_pct=80, by=by, snap=snap, rate_limit_a=30.0
        )
        assert plan.max_charge_a == 30.0

    def test_by_in_past_is_no_op(self, builder):
        captured = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
        snap = make_snapshot(captured_at=captured)
        plan = builder.charge_to(
            target_soc_pct=80, by=captured - timedelta(hours=1), snap=snap
        )
        assert plan.slots == ()


class TestHoldAt:
    def test_sets_capacity_no_gc(self, builder):
        snap = make_snapshot()
        window = TimeRange(
            start=snap.captured_at,
            end=snap.captured_at + timedelta(hours=4),
        )
        plan = builder.hold_at(soc_pct=50, window=window, snap=snap)
        assert plan.slots
        for slot in plan.slots:
            assert slot.capacity_pct == 50
            assert slot.grid_charge is False
        # work_mode left alone.
        assert plan.work_mode is None


class TestDrainTo:
    def test_sets_capacity_no_gc_no_workmode(self, builder):
        snap = make_snapshot()
        plan = builder.drain_to(
            target_soc_pct=25,
            by=snap.captured_at + timedelta(hours=3),
            snap=snap,
        )
        assert plan.slots
        for slot in plan.slots:
            assert slot.capacity_pct == 25
            assert slot.grid_charge is False


# ── 13b Mode actions ───────────────────────────────────────────────


class TestLockdownEPS:
    def test_all_slots_zero_capacity(self, builder):
        snap = make_snapshot(eps_active=True)
        plan = builder.lockdown_eps(snap)
        assert len(plan.slots) == 6
        for slot in plan.slots:
            assert slot.capacity_pct == 0
            assert slot.grid_charge is False

    def test_stops_ev(self, builder):
        plan = builder.lockdown_eps(make_snapshot(eps_active=True))
        assert plan.ev_action is not None
        assert plan.ev_action.set_mode == "Stopped"

    def test_turns_off_appliances(self, builder):
        plan = builder.lockdown_eps(make_snapshot(eps_active=True))
        assert plan.appliances_action is not None
        assert "washer" in plan.appliances_action.turn_off
        assert "dryer" in plan.appliances_action.turn_off
        assert "dishwasher" in plan.appliances_action.turn_off


class TestBaselineStatic:
    def test_full_static_plan(self, builder):
        plan = builder.baseline_static(make_snapshot())
        assert len(plan.slots) == 6
        assert plan.work_mode == "Zero export to CT"
        assert plan.energy_pattern == "Load first"
        assert plan.max_charge_a == 100.0
        assert plan.max_discharge_a == 100.0
        # Slot capacities match the baseline constants.
        for slot, expected_cap in zip(plan.slots, BASELINE_SLOT_CAPS):
            assert slot.capacity_pct == expected_cap
        for slot, expected_time in zip(plan.slots, BASELINE_SLOT_TIMES):
            assert slot.start_hhmm == expected_time
        # Slot 1 is the cheap-charge slot.
        assert plan.slots[0].grid_charge is True


class TestRestoreDefault:
    def test_resets_globals(self, builder):
        plan = builder.restore_default(make_snapshot())
        assert plan.work_mode == "Zero export to CT"
        assert plan.max_charge_a == 100.0
        # No slot writes — globals only.
        assert plan.slots == ()


# ── 13c Peripheral actions ─────────────────────────────────────────


class TestDeferEV:
    def test_stops_ev(self, builder):
        plan = builder.defer_ev(make_snapshot())
        assert plan.ev_action is not None
        assert plan.ev_action.set_mode == "Stopped"
        assert plan.slots == ()


class TestRestoreEV:
    def test_restores(self, builder):
        plan = builder.restore_ev(make_snapshot())
        assert plan.ev_action is not None
        assert plan.ev_action.restore_previous is True


# ── 13d Composition (merge) ────────────────────────────────────────


class TestMerge:
    def test_empty_returns_default(self, builder):
        plan = builder.merge()
        assert plan.work_mode is None

    def test_combines_disjoint_globals(self, builder):
        a = PlannedAction(work_mode="Selling first")
        b = PlannedAction(max_discharge_a=80.0)
        merged = builder.merge(a, b)
        assert merged.work_mode == "Selling first"
        assert merged.max_discharge_a == 80.0

    def test_conflict_last_wins_with_warning(self, builder, caplog):
        import logging

        caplog.set_level(logging.WARNING)
        a = PlannedAction(work_mode="Selling first")
        b = PlannedAction(work_mode="Zero export to CT")
        merged = builder.merge(a, b)
        assert merged.work_mode == "Zero export to CT"
        assert any("conflict" in rec.message for rec in caplog.records)

    def test_peripheral_conflict_raises(self, builder):
        a = PlannedAction(ev_action=EVAction(set_mode="Stopped"))
        b = PlannedAction(ev_action=EVAction(set_mode="Eco+"))
        with pytest.raises(ValueError, match="ev_action"):
            builder.merge(a, b)

    def test_peripheral_agreement_passes(self, builder):
        a = PlannedAction(ev_action=EVAction(set_mode="Stopped"))
        b = PlannedAction(ev_action=EVAction(set_mode="Stopped"))
        merged = builder.merge(a, b)
        assert merged.ev_action.set_mode == "Stopped"

    def test_slot_union(self, builder):
        a = PlannedAction(
            slots=(SlotPlan(slot_n=1, capacity_pct=80),)
        )
        b = PlannedAction(
            slots=(SlotPlan(slot_n=2, capacity_pct=20),)
        )
        merged = builder.merge(a, b)
        assert len(merged.slots) == 2
        by_n = {s.slot_n: s for s in merged.slots}
        assert by_n[1].capacity_pct == 80
        assert by_n[2].capacity_pct == 20

    def test_slot_field_merging_within_same_slot(self, builder):
        a = PlannedAction(
            slots=(SlotPlan(slot_n=1, start_hhmm="00:00"),)
        )
        b = PlannedAction(
            slots=(SlotPlan(slot_n=1, capacity_pct=80),)
        )
        merged = builder.merge(a, b)
        assert len(merged.slots) == 1
        s = merged.slots[0]
        assert s.start_hhmm == "00:00"  # from a
        assert s.capacity_pct == 80  # from b
