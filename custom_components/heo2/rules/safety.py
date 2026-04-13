# custom_components/heo2/rules/safety.py
"""SafetyRule — final-pass validation and correction. Always on, always last."""

from __future__ import annotations

from datetime import time

from ..models import ProgrammeState, ProgrammeInputs, SlotConfig
from ..rule_engine import Rule


class SafetyRule(Rule):
    """Final validation pass over the programme.

    Enforces:
    - min_soc floor on all slots
    - SOC capped at 100
    - Exactly 6 slots
    - Full 24h coverage (00:00–00:00)
    - Contiguous time boundaries

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

        # Ensure slot 1 starts at 00:00
        if state.slots[0].start_time != time(0, 0):
            fixes.append(f"Slot 1: fixed start to 00:00")
            state.slots[0].start_time = time(0, 0)

        # Ensure last slot ends at 00:00
        if state.slots[-1].end_time != time(0, 0):
            fixes.append(f"Slot 6: fixed end to 00:00")
            state.slots[-1].end_time = time(0, 0)

        # Ensure contiguous
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
