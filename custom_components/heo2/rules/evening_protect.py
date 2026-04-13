# custom_components/heo2/rules/evening_protect.py
"""EveningProtectRule — protect SOC for evening peak demand."""

from __future__ import annotations

from ..models import ProgrammeState, ProgrammeInputs
from ..rule_engine import Rule


class EveningProtectRule(Rule):
    """Ensure enough battery SOC to cover evening demand without grid import.

    Sets a floor on the pre-evening slot so the battery isn't drained
    before the 27.88p peak period.
    """

    name = "evening_protect"
    description = "Protect SOC reserve for evening peak demand"

    def __init__(self, evening_start_hour: int = 18, evening_end_hour: int = 24):
        self.evening_start_hour = evening_start_hour
        self.evening_end_hour = evening_end_hour

    def apply(self, state: ProgrammeState, inputs: ProgrammeInputs) -> ProgrammeState:
        evening_demand_kwh = inputs.load_kwh_between(
            self.evening_start_hour, self.evening_end_hour
        )

        if evening_demand_kwh <= 0:
            return state

        required_soc = int(
            inputs.min_soc + (evening_demand_kwh / inputs.battery_capacity_kwh * 100)
        )
        required_soc = min(required_soc, 100)

        from datetime import time
        evening_mins = self.evening_start_hour * 60

        modified = False
        for slot in state.slots:
            slot_end_mins = slot.end_time.hour * 60 + slot.end_time.minute
            if slot_end_mins == 0:
                slot_end_mins = 1440

            if slot_end_mins <= evening_mins and not slot.grid_charge:
                if slot.capacity_soc < required_soc:
                    slot.capacity_soc = required_soc
                    modified = True

        if modified:
            state.reason_log.append(
                f"EveningProtect: raised pre-evening SOC to {required_soc}% "
                f"({evening_demand_kwh:.1f} kWh evening demand)"
            )
        else:
            state.reason_log.append(
                f"EveningProtect: no change needed "
                f"({evening_demand_kwh:.1f} kWh evening demand, "
                f"required {required_soc}%)"
            )
        return state
