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

        # HEO-6 fix: pre-fix logic only protected slots whose end_time
        # was <= evening_start (e.g. a slot ending at 18:00 exactly).
        # In Paddy's typical plan, slot 2 runs 05:30-18:30 - it spans
        # evening_start at 18:00, so the old condition skipped it and
        # no protection ever applied. The inverter could discharge
        # slot 2 toward whatever low cap ExportWindow set, leaving
        # the battery empty entering the evening window.
        #
        # New rule: any non-GC slot that overlaps the evening_start
        # boundary (start_time < evening_start AND end_time > evening_start
        # OR end_time == 00:00 wrap) gets its cap raised to required_soc.
        # This is the slot whose `capacity_soc` is in effect AT the
        # moment evening starts, so it determines the SOC the battery
        # carries into the evening window.
        evening_t = time(self.evening_start_hour, 0)

        def _slot_covers_boundary(slot) -> bool:
            # Slot covers the evening boundary iff `evening_t` falls in
            # [slot.start_time, slot.end_time). end==00:00 wraps midnight.
            if slot.end_time == time(0, 0):
                # Slot wraps; covers anything >= start_time
                return slot.start_time <= evening_t
            return slot.start_time <= evening_t < slot.end_time

        modified = False
        for slot in state.slots:
            if _slot_covers_boundary(slot) and not slot.grid_charge:
                if slot.capacity_soc < required_soc:
                    slot.capacity_soc = required_soc
                    modified = True

        if modified:
            state.reason_log.append(
                f"EveningProtect: raised SOC to {required_soc}% in slot "
                f"covering {evening_t.strftime('%H:%M')} "
                f"({evening_demand_kwh:.1f} kWh evening demand)"
            )
        else:
            state.reason_log.append(
                f"EveningProtect: no change needed "
                f"({evening_demand_kwh:.1f} kWh evening demand, "
                f"required {required_soc}%)"
            )
        return state
