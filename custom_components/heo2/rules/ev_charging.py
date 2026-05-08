# custom_components/heo2/rules/ev_charging.py
"""EVChargingRule -- hold SOC during non-IGO EV charging."""

from __future__ import annotations

from ..models import ProgrammeInputs
from ..rule_engine import PRIO_EV_CHARGING, Rule


class EVChargingRule(Rule):
    """During EV charging (non-IGO), hold battery SOC.

    Don't drain battery to feed the car at 7.86p effective cost when
    grid is available at 7p (IGO) or similar.
    """

    name = "ev_charging"
    description = "Hold battery SOC during EV charging"
    priority_class = PRIO_EV_CHARGING

    def propose(self, view, inputs: ProgrammeInputs) -> None:
        if not inputs.ev_charging:
            return

        if inputs.igo_dispatching:
            return  # IGO dispatch takes precedence

        # Use local-tz-aware lookup. inputs.now is UTC; programme slots
        # are local time-of-day. inputs.now.time() (UTC) was aliasing
        # against local slots in DST - same bug class as HEO-31 PR2 #39.
        now_time = inputs.now_local().time().replace(
            second=0, microsecond=0,
        )
        try:
            idx = view.find_slot_at(now_time)
        except ValueError:
            return

        hold_soc = max(int(inputs.current_soc), int(inputs.min_soc))
        if view.get_slot(idx, "capacity_soc") < hold_soc:
            view.claim_slot(
                idx, "capacity_soc", hold_soc,
                reason="hold during EV charge",
            )

        view.log(
            f"EVCharging: slot {idx + 1} SOC held at {hold_soc}% "
            f"(don't drain battery to feed car)"
        )
