# custom_components/heo2/rules/evening_protect.py
"""EveningProtectRule -- protect SOC for evening peak demand."""

from __future__ import annotations

from datetime import time

from ..models import ProgrammeInputs
from ..rule_engine import PRIO_EVENING_PROTECT, Rule


class EveningProtectRule(Rule):
    """Ensure enough battery SOC to cover evening demand without grid import.

    Sets a floor on the pre-evening slot so the battery isn't drained
    before the 27.88p peak period.
    """

    name = "evening_protect"
    description = "Protect SOC reserve for evening peak demand"
    priority_class = PRIO_EVENING_PROTECT

    def __init__(self, evening_start_hour: int = 18, evening_end_hour: int = 24):
        self.evening_start_hour = evening_start_hour
        self.evening_end_hour = evening_end_hour

    def propose(self, view, inputs: ProgrammeInputs) -> None:
        evening_demand_kwh = inputs.load_kwh_between(
            self.evening_start_hour, self.evening_end_hour
        )

        if evening_demand_kwh <= 0:
            return

        required_soc = int(
            inputs.min_soc + (evening_demand_kwh / inputs.battery_capacity_kwh * 100)
        )
        required_soc = min(required_soc, 100)

        evening_t = time(self.evening_start_hour, 0)

        def _slot_covers_boundary(slot) -> bool:
            # Slot covers the evening boundary iff `evening_t` falls in
            # [slot.start_time, slot.end_time). end==00:00 wraps midnight.
            if slot.end_time == time(0, 0):
                return slot.start_time <= evening_t
            return slot.start_time <= evening_t < slot.end_time

        modified = False
        for slot in view.slots:
            if _slot_covers_boundary(slot) and not slot.grid_charge:
                if slot.capacity_soc < required_soc:
                    view.claim_slot(
                        slot.index, "capacity_soc", required_soc,
                        reason=f"evening demand {evening_demand_kwh:.1f} kWh",
                    )
                    modified = True

        if modified:
            view.log(
                f"EveningProtect: raised SOC to {required_soc}% in slot "
                f"covering {evening_t.strftime('%H:%M')} "
                f"({evening_demand_kwh:.1f} kWh evening demand)"
            )
        else:
            view.log(
                f"EveningProtect: no change needed "
                f"({evening_demand_kwh:.1f} kWh evening demand, "
                f"required {required_soc}%)"
            )
