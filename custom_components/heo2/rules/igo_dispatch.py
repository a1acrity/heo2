# custom_components/heo2/rules/igo_dispatch.py
"""IGODispatchRule — charge battery during IGO dispatch periods."""

from __future__ import annotations

from ..models import ProgrammeState, ProgrammeInputs
from ..rule_engine import Rule


class IGODispatchRule(Rule):
    """When IGO dispatch is active, enable grid charge and raise SOC target.

    Never drain battery during dispatch — the cheap rate means we should
    be filling up, not selling.
    """

    name = "igo_dispatch"
    description = "Enable grid charge during IGO dispatch"

    def apply(self, state: ProgrammeState, inputs: ProgrammeInputs) -> ProgrammeState:
        if not inputs.igo_dispatching:
            return state

        now_time = inputs.now.time().replace(second=0, microsecond=0)
        try:
            idx = state.find_slot_at(now_time)
        except ValueError:
            return state

        slot = state.slots[idx]
        slot.grid_charge = True
        slot.capacity_soc = max(slot.capacity_soc, int(inputs.current_soc), 100)

        state.reason_log.append(
            f"IGODispatch: slot {idx + 1} grid charge enabled, "
            f"SOC target {slot.capacity_soc}%"
        )
        return state
