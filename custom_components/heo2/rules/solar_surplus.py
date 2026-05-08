"""SolarSurplusRule -- set day targets to capture PV without grid charge."""

from __future__ import annotations

from ..models import ProgrammeInputs
from ..rule_engine import PRIO_SOLAR_SURPLUS, Rule


class SolarSurplusRule(Rule):
    """During forecast solar hours, set hold-or-rise SOC targets.

    Allows PV to charge battery naturally. Never enables grid charge.
    """

    name = "solar_surplus"
    description = "Set day-time SOC targets based on solar forecast"
    priority_class = PRIO_SOLAR_SURPLUS

    def propose(self, view, inputs: ProgrammeInputs) -> None:
        total_solar = sum(inputs.solar_forecast_kwh)
        if total_solar <= 0:
            return

        day_solar = inputs.solar_kwh_between(6, 18)
        day_load = inputs.load_kwh_between(6, 18)
        net_surplus_kwh = day_solar - day_load

        if net_surplus_kwh <= 0:
            view.log(
                f"SolarSurplus: no surplus (solar {day_solar:.1f} kWh "
                f"< load {day_load:.1f} kWh)"
            )
            return

        surplus_soc = net_surplus_kwh / inputs.battery_capacity_kwh * 100

        for slot in view.slots:
            if slot.grid_charge:
                continue
            start_hour = slot.start_time.hour
            end_hour = slot.end_time.hour if slot.end_time > slot.start_time else 24
            if start_hour < 18 and end_hour > 6:
                new_target = min(100, int(inputs.current_soc + surplus_soc))
                new_target = max(new_target, int(inputs.min_soc))
                view.claim_slot(
                    slot.index, "capacity_soc", new_target,
                    reason=f"day surplus +{surplus_soc:.0f}%",
                )

        view.log(
            f"SolarSurplus: day target raised -- surplus {net_surplus_kwh:.1f} kWh "
            f"(+{surplus_soc:.0f}% SOC)"
        )
