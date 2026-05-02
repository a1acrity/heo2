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


class TestIGODispatchPlannedDispatches:
    """HEO-8: pre-position covering slots when planned_dispatches has
    entries, even with igo_dispatching=False.
    """

    def test_planned_dispatch_pre_positions_covering_slot(
        self, default_inputs,
    ):
        """A planned dispatch from 14:00-15:00 UTC falls inside slot 2
        (05:30-18:30) of the default programme (UTC, no tz). The slot
        should be set to grid_charge=True + cap=100."""
        from datetime import datetime, timezone, timedelta
        from heo2.models import PlannedDispatch
        # default_inputs.now is 12:00 UTC; dispatch in 2h
        dispatch_start = datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc)
        dispatch_end = dispatch_start + timedelta(hours=1)
        default_inputs.planned_dispatches = [PlannedDispatch(
            start=dispatch_start,
            end=dispatch_end,
            charge_kwh=-7.0,
            source="smart-charge",
        )]
        default_inputs.igo_dispatching = False

        state = _make_baseline(default_inputs)
        rule = IGODispatchRule()
        result = rule.apply(state, default_inputs)

        from datetime import time
        idx = result.find_slot_at(time(14, 0))
        assert result.slots[idx].grid_charge is True
        assert result.slots[idx].capacity_soc == 100
        assert any("IGODispatch" in r for r in result.reason_log)
        assert any("planned" in r for r in result.reason_log)

    def test_planned_dispatch_outside_24h_horizon_ignored(
        self, default_inputs,
    ):
        """Dispatch tomorrow afternoon (>24h) shouldn't pre-position
        anything today. The rule's horizon is 24h."""
        from datetime import timedelta
        from heo2.models import PlannedDispatch
        future = default_inputs.now + timedelta(hours=48)
        default_inputs.planned_dispatches = [PlannedDispatch(
            start=future,
            end=future + timedelta(hours=1),
        )]
        default_inputs.igo_dispatching = False
        state = _make_baseline(default_inputs)
        grid_before = [s.grid_charge for s in state.slots]
        socs_before = [s.capacity_soc for s in state.slots]
        rule = IGODispatchRule()
        result = rule.apply(state, default_inputs)
        assert [s.grid_charge for s in result.slots] == grid_before
        assert [s.capacity_soc for s in result.slots] == socs_before

    def test_already_started_dispatch_still_pre_positions_remaining_window(
        self, default_inputs,
    ):
        """A dispatch that started 30 min ago and runs another 30 min:
        we should still set the covering slot to gc=True + cap=100,
        because we're inside the cheap window right now."""
        from datetime import timedelta
        from heo2.models import PlannedDispatch
        start = default_inputs.now - timedelta(minutes=30)
        end = default_inputs.now + timedelta(minutes=30)
        default_inputs.planned_dispatches = [PlannedDispatch(
            start=start, end=end,
        )]
        default_inputs.igo_dispatching = True  # active too
        state = _make_baseline(default_inputs)
        rule = IGODispatchRule()
        result = rule.apply(state, default_inputs)
        from datetime import time
        idx = result.find_slot_at(time(12, 0))
        assert result.slots[idx].grid_charge is True
        assert result.slots[idx].capacity_soc == 100

    def test_multiple_planned_dispatches_collect_all_covering_slots(
        self, default_inputs,
    ):
        """Two dispatches - one covers slot 2, one covers slot 3.
        Both slots should be pre-positioned."""
        from datetime import datetime, timezone, timedelta
        from heo2.models import PlannedDispatch
        d1 = PlannedDispatch(
            start=datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc),
            end=datetime(2026, 4, 13, 15, 0, tzinfo=timezone.utc),
        )
        # Slot 3 of default programme is 16:00-23:59 (insert_boundary
        # not used, so default boundaries).
        d2 = PlannedDispatch(
            start=datetime(2026, 4, 13, 19, 0, tzinfo=timezone.utc),
            end=datetime(2026, 4, 13, 20, 0, tzinfo=timezone.utc),
        )
        default_inputs.planned_dispatches = [d1, d2]
        default_inputs.igo_dispatching = False
        state = _make_baseline(default_inputs)
        rule = IGODispatchRule()
        result = rule.apply(state, default_inputs)
        from datetime import time
        idx1 = result.find_slot_at(time(14, 0))
        idx2 = result.find_slot_at(time(19, 0))
        # Different slots; both pre-positioned
        assert result.slots[idx1].grid_charge is True
        assert result.slots[idx1].capacity_soc == 100
        assert result.slots[idx2].grid_charge is True
        assert result.slots[idx2].capacity_soc == 100

    def test_empty_planned_dispatches_is_noop(self, default_inputs):
        """Empty planned_dispatches list with no active dispatch leaves
        the programme unchanged."""
        default_inputs.planned_dispatches = []
        default_inputs.igo_dispatching = False
        state = _make_baseline(default_inputs)
        grid_before = [s.grid_charge for s in state.slots]
        socs_before = [s.capacity_soc for s in state.slots]
        rule = IGODispatchRule()
        result = rule.apply(state, default_inputs)
        assert [s.grid_charge for s in result.slots] == grid_before
        assert [s.capacity_soc for s in result.slots] == socs_before
