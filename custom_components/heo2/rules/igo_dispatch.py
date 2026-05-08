# custom_components/heo2/rules/igo_dispatch.py
"""IGODispatchRule -- pre-position and ride out IGO smart-charge dispatches.

Two code paths inside one rule:

1. **Active dispatch** (`igo_dispatching=True`): set the slot containing
   local-now to grid_charge=True + cap=100. This was the original HEO
   behaviour from before HEO-8.

2. **Planned dispatches** (HEO-8): for every entry in
   `inputs.planned_dispatches` whose window starts in the next 24h, set
   every covering programme slot to grid_charge=True + cap=100 so the
   battery is already at full when Octopus takes control.

The two paths agree on intent (be full + charging during the cheap
window) so they can both run; the active-dispatch path is a no-op when
the planned-dispatch path has already pre-positioned the slot.
"""

from __future__ import annotations

from datetime import timedelta

from ..models import PlannedDispatch, ProgrammeInputs
from ..rule_engine import PRIO_IGO_DISPATCH, Rule


def _slot_indices_covering_dispatch(
    view,
    dispatch: PlannedDispatch,
    inputs: ProgrammeInputs,
) -> list[int]:
    """Return programme slot indices that overlap the dispatch window
    in local time-of-day. Multi-slot dispatches collect every covering
    slot index. A dispatch fully in tomorrow returns no indices for
    today's programme; a dispatch crossing midnight returns indices
    for both halves.
    """
    tz = inputs.local_tz
    if tz is not None:
        start_local = (
            dispatch.start.astimezone(tz)
            if dispatch.start.tzinfo is not None
            else dispatch.start
        )
        end_local = (
            dispatch.end.astimezone(tz)
            if dispatch.end.tzinfo is not None
            else dispatch.end
        )
    else:
        start_local = dispatch.start
        end_local = dispatch.end

    # 15-min sample sweep is finer than any inverter boundary HEO II
    # writes (5-min). Avoids hand-rolling overlap maths for the wrap-
    # midnight case.
    indices: set[int] = set()
    cursor = start_local
    while cursor < end_local:
        try:
            idx = view.find_slot_at(cursor.time())
            indices.add(idx)
        except ValueError:
            pass
        cursor += timedelta(minutes=15)
    return sorted(indices)


class IGODispatchRule(Rule):
    """Enable grid charge + cap=100 around IGO dispatches (active and planned)."""

    name = "igo_dispatch"
    description = "Enable grid charge during IGO dispatch"
    priority_class = PRIO_IGO_DISPATCH

    def propose(self, view, inputs: ProgrammeInputs) -> None:
        modified_log: list[str] = []

        # SavingSessionRule (runs earlier in the registry) drains the
        # slot containing local-now to floor + gc=False during a saving
        # session. An IGO dispatch covering the same slot would refill
        # the battery from grid mid-session and lose the £3+/kWh export
        # revenue. See docs/rule_field_overlap.md F2. Skip the session
        # slot in both paths.
        saving_session_slot_idx: int | None = None
        if inputs.saving_session:
            try:
                local_now = inputs.now_local()
                saving_session_slot_idx = view.find_slot_at(local_now.time())
            except ValueError:
                saving_session_slot_idx = None

        # Path 1: planned dispatches (HEO-8). Pre-position covering slots.
        if inputs.planned_dispatches:
            now = inputs.now
            horizon_end = now + timedelta(hours=24)
            covered_slot_idxs: set[int] = set()
            for d in inputs.planned_dispatches:
                if d.end <= now or d.start >= horizon_end:
                    continue
                for idx in _slot_indices_covering_dispatch(view, d, inputs):
                    covered_slot_idxs.add(idx)

            for idx in sorted(covered_slot_idxs):
                if idx == saving_session_slot_idx:
                    modified_log.append(
                        f"slot {idx + 1} held by saving session, skip pre-position"
                    )
                    continue
                slot = view.slots[idx]
                if not slot.grid_charge or slot.capacity_soc < 100:
                    view.claim_slot(idx, "grid_charge", True, reason="planned dispatch")
                    view.claim_slot(idx, "capacity_soc", 100, reason="planned dispatch")
                    modified_log.append(
                        f"slot {idx + 1} pre-positioned for planned dispatch"
                    )

        # Path 2: in-progress dispatch. Force the active slot to gc=True
        # + cap=100 even if planned_dispatches is empty (e.g. older BD
        # versions that don't expose the attribute).
        if inputs.igo_dispatching:
            try:
                local_now = inputs.now_local()
                idx = view.find_slot_at(local_now.time())
            except ValueError:
                idx = None
            if idx is not None and idx != saving_session_slot_idx:
                current_cap = view.get_slot(idx, "capacity_soc")
                new_cap = max(current_cap, 100)
                view.claim_slot(idx, "grid_charge", True, reason="active dispatch")
                view.claim_slot(idx, "capacity_soc", new_cap, reason="active dispatch")
                modified_log.append(
                    f"slot {idx + 1} active dispatch (cap={new_cap}%)"
                )
            elif idx is not None and idx == saving_session_slot_idx:
                modified_log.append(
                    f"slot {idx + 1} active dispatch held by saving session"
                )

        if modified_log:
            view.log(
                f"IGODispatch: {'; '.join(modified_log)}"
            )
