# custom_components/heo2/rules/eps_mode.py
"""EPSModeRule -- SPEC §9 row 2 / hard rule H3.

When the grid is down and the inverter is supplying via EPS, normal
SOC reserves don't apply: the goal is to keep the house running until
the grid comes back. The rule:

* Overrides every slot's `capacity_soc` to 0% so the inverter can
  discharge below the user-configured min_soc floor.
* Disables `grid_charge` on every slot (no grid to charge from).

The coordinator handles the rest of H3 (turn off EV/washer/dryer/
dishwasher via switch.turn_off, suppress MQTT writes via writes_blocked,
banner via binary_sensor.heo_ii_eps_active). The rule itself only
shapes the programme.

When EPS clears (grid restored) the rule returns the slots to
whatever the upstream rules produced, since this rule is a no-op
when `eps_active=False`. The replan_triggers logic detects the
False -> False -> True or True -> False transition and commits a
fresh baseline.
"""

from __future__ import annotations

from ..models import ProgrammeInputs, ProgrammeState
from ..rule_engine import Rule


class EPSModeRule(Rule):
    """When EPS is active, drop SOC floor to 0 and disable grid charge."""

    name = "eps_mode"
    description = "Override SOC floor + disable grid charge during EPS"

    def apply(self, state: ProgrammeState, inputs: ProgrammeInputs) -> ProgrammeState:
        if not inputs.eps_active:
            return state

        for slot in state.slots:
            slot.capacity_soc = 0
            slot.grid_charge = False

        state.reason_log.append(
            "EPSMode: grid down, all slots cap=0% gc=False "
            "(H3: allow battery drain to 0%)"
        )
        return state
