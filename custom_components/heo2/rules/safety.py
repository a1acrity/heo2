# custom_components/heo2/rules/safety.py
"""SafetyRule — final-pass validation and correction. Always on, always last."""

from __future__ import annotations

from datetime import time

from ..models import ProgrammeState, ProgrammeInputs, SlotConfig
from ..rule_engine import Rule


# Sunsynk inverter timer fields have 5-minute granularity. Sending a
# value like 23:57 returns "Saved" from SA but the inverter floors to
# 23:55, then SA's polling reflects 23:55 in the entity. If HEO II
# leaves un-snapped values in the plan, the PR 2 post-write verify
# (SPEC §7 H6) would latch a permanent mismatch reason because the
# plan and the read-back never agree. Snap proactively in the rule
# engine so what's written is what comes back. Verified manually on
# 2026-05-02: 23:57/23:58/23:56/23:51 all floor to the prior :X0/:X5.
_TIME_GRANULARITY_MINUTES = 5


def _snap_to_granularity(t: time) -> time:
    """Floor a `time` to the nearest 5-min boundary (Sunsynk timer
    granularity). Same direction as the hardware itself uses.
    """
    snapped_minute = (t.minute // _TIME_GRANULARITY_MINUTES) * _TIME_GRANULARITY_MINUTES
    return time(t.hour, snapped_minute)


class SafetyRule(Rule):
    """Final validation pass over the programme.

    Enforces:
    - min_soc floor on all slots
    - SOC capped at 100
    - Exactly 6 slots
    - Full 24h coverage (00:00–00:00)
    - Contiguous time boundaries
    - Sunsynk 5-minute time granularity (floor each boundary)

    Cannot be disabled.
    """

    name = "safety"
    description = "Final validation: enforce constraints and fix violations"

    @property
    def enabled(self) -> bool:
        return True

    @enabled.setter
    def enabled(self, value: bool) -> None:
        pass  # cannot be disabled

    def apply(self, state: ProgrammeState, inputs: ProgrammeInputs) -> ProgrammeState:
        min_soc = int(inputs.min_soc)
        fixes: list[str] = []

        # Fix slot count if wrong
        if len(state.slots) != 6:
            fixes.append(f"Reset to default: had {len(state.slots)} slots")
            state = ProgrammeState.default(min_soc=min_soc)

        # Enforce SOC bounds
        for i, slot in enumerate(state.slots):
            if slot.capacity_soc < min_soc:
                fixes.append(f"Slot {i + 1}: raised SOC {slot.capacity_soc}→{min_soc}%")
                slot.capacity_soc = min_soc
            if slot.capacity_soc > 100:
                fixes.append(f"Slot {i + 1}: capped SOC {slot.capacity_soc}→100%")
                slot.capacity_soc = 100

        # Snap every boundary to Sunsynk's 5-min granularity. Doing this
        # before the contiguous fix-up below means any boundary the rule
        # engine produced like 23:57 collapses to 23:55, both for that
        # slot's start and the previous slot's end.
        for i, slot in enumerate(state.slots):
            snapped_start = _snap_to_granularity(slot.start_time)
            if snapped_start != slot.start_time:
                fixes.append(
                    f"Slot {i + 1}: snapped start "
                    f"{slot.start_time.strftime('%H:%M')}→"
                    f"{snapped_start.strftime('%H:%M')} (5-min granularity)"
                )
                slot.start_time = snapped_start
            snapped_end = _snap_to_granularity(slot.end_time)
            if snapped_end != slot.end_time:
                slot.end_time = snapped_end

        # Ensure slot 1 starts at 00:00
        if state.slots[0].start_time != time(0, 0):
            fixes.append(f"Slot 1: fixed start to 00:00")
            state.slots[0].start_time = time(0, 0)

        # Ensure last slot ends at 00:00
        if state.slots[-1].end_time != time(0, 0):
            fixes.append(f"Slot 6: fixed end to 00:00")
            state.slots[-1].end_time = time(0, 0)

        # Ensure contiguous (after snapping, otherwise a snap on slot N's
        # start could leave a gap between slots N-1 and N).
        for i in range(len(state.slots) - 1):
            if state.slots[i].end_time != state.slots[i + 1].start_time:
                fixes.append(
                    f"Slot {i + 2}: fixed start "
                    f"{state.slots[i + 1].start_time}→{state.slots[i].end_time}"
                )
                state.slots[i + 1].start_time = state.slots[i].end_time

        if fixes:
            state.reason_log.append(f"Safety: {'; '.join(fixes)}")

        return state
