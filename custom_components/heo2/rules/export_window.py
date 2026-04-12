"""ExportWindowRule -- drain battery during profitable export windows."""

from __future__ import annotations

from datetime import time as dt_time

from ..models import ProgrammeState, ProgrammeInputs
from ..rule_engine import Rule
from ..const import EFFECTIVE_STORED_COST_PENCE


class ExportWindowRule(Rule):
    """During profitable export windows, set low SOC targets to drain battery.

    The drain target is floored at enough SOC to cover evening demand.
    """

    name = "export_window"
    description = "Drain battery during profitable Agile Outgoing windows"

    def apply(self, state: ProgrammeState, inputs: ProgrammeInputs) -> ProgrammeState:
        if not inputs.export_rates:
            return state

        # Find profitable export hours
        profitable_hours: list[int] = []
        for hour in range(24):
            from datetime import datetime, timezone
            hour_start = inputs.now.replace(
                hour=hour, minute=0, second=0, microsecond=0
            )
            export_rate = inputs.export_rate_at(hour_start)
            if export_rate is not None and export_rate > EFFECTIVE_STORED_COST_PENCE:
                profitable_hours.append(hour)

        if not profitable_hours:
            return state

        # Calculate evening demand reserve (18:30-23:30 ~ hours 18-24)
        evening_demand_kwh = inputs.load_kwh_between(18, 24)
        export_floor_soc = int(
            inputs.min_soc + (evening_demand_kwh / inputs.battery_capacity_kwh * 100)
        )
        export_floor_soc = min(export_floor_soc, 100)

        drain_target = max(export_floor_soc, int(inputs.min_soc))

        # Apply to slots that overlap with profitable hours
        modified = False
        for slot in state.slots:
            start_hour = slot.start_time.hour
            end_hour = slot.end_time.hour if slot.end_time > slot.start_time else 24
            slot_hours = set(range(start_hour, end_hour))

            if slot_hours & set(profitable_hours) and not slot.grid_charge:
                if slot.capacity_soc > drain_target:
                    slot.capacity_soc = drain_target
                    modified = True

        if modified:
            state.reason_log.append(
                f"ExportWindow: drain to {drain_target}% during profitable export "
                f"(hours {profitable_hours}, floor from {evening_demand_kwh:.1f} kWh evening demand)"
            )
        return state
