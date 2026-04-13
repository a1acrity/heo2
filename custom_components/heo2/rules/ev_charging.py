# custom_components/heo2/rules/ev_charging.py
"""EVChargingRule — hold SOC during non-IGO EV charging."""

from __future__ import annotations

from ..models import ProgrammeState, ProgrammeInputs
from ..rule_engine import Rule


class EVChargingRule(Rule):
    """During EV charging (non-IGO), hold battery SOC.

    Don't drain battery to feed the car at 7.86p effective cost when
    grid is available at 7p (IGO) or similar.
    """

    name = "ev_charging"
    description = "Hold battery SOC during EV charging"

    def apply(self, state: ProgrammeState, inputs: ProgrammeInputs) -> ProgrammeState:
        if not inputs.ev_charging:
            return state

        if inputs.igo_dispatching:
            return state  # IGO dispatch takes precedence

        now_time = inputs.now.time().replace(second=0, microsecond=0)
        try:
            idx = state.find_slot_at(now_time)
        except ValueError:
            return state

        hold_soc = max(int(inputs.current_soc), int(inputs.min_soc))
        if state.slots[idx].capacity_soc < hold_soc:
            state.slots[idx].capacity_soc = hold_soc

        state.reason_log.append(
            f"EVCharging: slot {idx + 1} SOC held at {hold_soc}% "
            f"(don't drain battery to feed car)"
        )
        return state
