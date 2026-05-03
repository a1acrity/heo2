# custom_components/heo2/rules/saving_session.py
"""SavingSessionRule -- drain to floor when an Octoplus saving session is
active.

SPEC §9 row 3:

    Saving Session | saving_session_active | Drain to min_soc as fast
    as inverter allows (Selling first + max discharge); resume normal
    at session end.

Octoplus Saving Sessions typically pay £3+/kWh of battery export. Missing
one is directly equivalent to losing £10-50 of revenue.

This rule fires when `inputs.saving_session` is True. It overrides the
slot containing local-now to `capacity_soc=min_soc, grid_charge=False`
so the inverter discharges the battery to the floor. Other slots are
left untouched so the session ending mid-tick (next coordinator pass)
returns the schedule to normal.

Work-mode / discharge-rate writes (SPEC §2 items 4-7) aren't yet wired
into MqttWriter; once they are, this rule can also force "Selling first"
+ max discharge for the duration of the session. For now, the SOC + GC
override gives the inverter permission to discharge against load + grid
export; the existing work mode handles the rest.
"""

from __future__ import annotations

from ..models import ProgrammeInputs, ProgrammeState
from ..rule_engine import Rule


class SavingSessionRule(Rule):
    """When a saving session is active, override the current slot to
    drain the battery to floor."""

    name = "saving_session"
    description = "Drain battery to floor during Octoplus saving sessions"

    def apply(self, state: ProgrammeState, inputs: ProgrammeInputs) -> ProgrammeState:
        if not inputs.saving_session:
            return state

        local_now = inputs.now_local()
        try:
            current_idx = state.find_slot_at(local_now.time())
        except ValueError:
            # Programme isn't covering local-now; SafetyRule will fix
            # the contiguity, but this rule has nothing to do.
            return state

        slot = state.slots[current_idx]
        floor = int(inputs.min_soc)

        before = (slot.capacity_soc, slot.grid_charge)
        slot.capacity_soc = floor
        slot.grid_charge = False
        after = (slot.capacity_soc, slot.grid_charge)

        # SPEC §9 row 3: "Selling first" lets the inverter export to
        # grid at the published Outgoing Octopus rate. Without this the
        # current "Zero export to CT" mode would just hold load coverage,
        # not export, and the £3+/kWh saving-session price wouldn't be
        # captured. Reset is handled by BaselineRule when the session
        # ends (saving_session=False).
        state.work_mode = "Selling first"

        if before != after:
            state.reason_log.append(
                f"SavingSession: drain slot {current_idx + 1} "
                f"({slot.start_time.strftime('%H:%M')}-"
                f"{slot.end_time.strftime('%H:%M')}) "
                f"to {floor}% (was cap={before[0]}% gc={before[1]}); "
                f"work_mode -> Selling first"
            )
        else:
            state.reason_log.append(
                f"SavingSession: active but slot {current_idx + 1} "
                f"already at floor; work_mode -> Selling first"
            )
        return state
