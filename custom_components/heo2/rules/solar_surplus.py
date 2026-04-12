"""SolarSurplusRule -- set day targets to capture PV without grid charge."""

from __future__ import annotations

from ..models import ProgrammeState, ProgrammeInputs
from ..rule_engine import Rule


class SolarSurplusRule(Rule):
    """During forecast solar hours, set hold-or-rise SOC targets.

    Allows PV to charge battery naturally. Never enables grid charge.
    """

    name = "solar_surplus"
    description = "Set day-time SOC targets based on solar forecast"

    def apply(self, state: ProgrammeState, inputs: ProgrammeInputs) -> ProgrammeState:
        total_solar = sum(inputs.solar_forecast_kwh)
        if total_solar <= 0:
            return state  # nothing to do

        # Calculate net solar surplus during day hours (06:00-18:00)
        day_solar = inputs.solar_kwh_between(6, 18)
        day_load = inputs.load_kwh_between(6, 18)
        net_surplus_kwh = day_solar - day_load

        if net_surplus_kwh <= 0:
            state.reason_log.append(
                f"SolarSurplus: no surplus (solar {day_solar:.1f} kWh "
                f"< load {day_load:.1f} kWh)"
            )
            return state

        # How much SOC would the surplus add?
        surplus_soc = net_surplus_kwh / inputs.battery_capacity_kwh * 100

        # Set day slot target: current SOC + expected surplus, capped at 100
        for slot in state.slots:
            if not slot.grid_charge:
                # Only modify slots that overlap with solar hours
                start_hour = slot.start_time.hour
                end_hour = slot.end_time.hour if slot.end_time > slot.start_time else 24
                if start_hour < 18 and end_hour > 6:
                    # This slot overlaps with solar production
                    new_target = min(100, int(inputs.current_soc + surplus_soc))
                    new_target = max(new_target, int(inputs.min_soc))
                    slot.capacity_soc = new_target

        state.reason_log.append(
            f"SolarSurplus: day target raised -- surplus {net_surplus_kwh:.1f} kWh "
            f"(+{surplus_soc:.0f}% SOC)"
        )
        return state
