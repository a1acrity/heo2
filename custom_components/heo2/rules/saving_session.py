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

from ..models import ProgrammeInputs
from ..rule_engine import PRIO_SAVING_SESSION, Rule


class SavingSessionRule(Rule):
    """When a saving session is active, override the current slot to
    drain the battery to floor."""

    name = "saving_session"
    description = "Drain battery to floor during Octoplus saving sessions"
    priority_class = PRIO_SAVING_SESSION

    def propose(self, view, inputs: ProgrammeInputs) -> None:
        if not inputs.saving_session:
            return

        local_now = inputs.now_local()
        try:
            current_idx = view.find_slot_at(local_now.time())
        except ValueError:
            # Programme isn't covering local-now; SafetyRule will fix
            # the contiguity, but this rule has nothing to do.
            return

        floor = int(inputs.min_soc)
        slot_view = view.slots[current_idx]
        before = (slot_view.capacity_soc, slot_view.grid_charge)

        view.claim_slot(
            current_idx, "capacity_soc", floor,
            reason="saving session drain",
        )
        view.claim_slot(
            current_idx, "grid_charge", False,
            reason="saving session drain",
        )
        # SPEC §9 row 3: "Selling first" lets the inverter export to
        # grid at the published Outgoing Octopus rate.
        view.claim_global("work_mode", "Selling first", reason="saving session active")

        if before != (floor, False):
            view.log(
                f"SavingSession: drain slot {current_idx + 1} "
                f"({slot_view.start_time.strftime('%H:%M')}-"
                f"{slot_view.end_time.strftime('%H:%M')}) "
                f"to {floor}% (was cap={before[0]}% gc={before[1]}); "
                f"work_mode -> Selling first"
            )
        else:
            view.log(
                f"SavingSession: active but slot {current_idx + 1} "
                f"already at floor; work_mode -> Selling first"
            )
