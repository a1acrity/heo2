"""CheapRateChargeRule -- calculates optimal overnight charge target."""

from __future__ import annotations

from datetime import timedelta

from ..models import ProgrammeState, ProgrammeInputs
from ..rule_engine import Rule
from ..const import EFFECTIVE_STORED_COST_PENCE


class CheapRateChargeRule(Rule):
    """Calculate worth-charging target SOC for overnight cheap-rate slots.

    Only charge what we'll consume or profitably export -- avoids wasting
    cycles on battery that will sit unused.
    """

    name = "cheap_rate_charge"
    description = "Calculate overnight charge target based on expected demand and export"

    def __init__(self, max_target_soc: int = 100):
        self.max_target_soc = max_target_soc

    def apply(self, state: ProgrammeState, inputs: ProgrammeInputs) -> ProgrammeState:
        # Expected demand: sum of load forecast for full day
        expected_demand_kwh = sum(inputs.load_forecast_kwh)

        # Expected solar generation
        expected_solar_kwh = sum(inputs.solar_forecast_kwh)

        # Expected profitable export: hours where export rate > effective stored cost
        expected_profitable_export_kwh = 0.0
        for hour in range(24):
            hour_start = inputs.now.replace(
                hour=hour, minute=0, second=0, microsecond=0
            )
            export_rate = inputs.export_rate_at(hour_start)
            if export_rate is not None and export_rate > EFFECTIVE_STORED_COST_PENCE:
                # We can profitably export up to max_discharge_kw per hour
                # but cap at what the battery can deliver (simplified to 5 kWh/hr)
                expected_profitable_export_kwh += min(
                    5.0, inputs.battery_capacity_kwh * 0.25
                )

        # Worth-charging calculation from spec
        worth_charging_kwh = (
            expected_demand_kwh
            + expected_profitable_export_kwh
            - expected_solar_kwh
        )

        # Convert to SOC percentage
        if worth_charging_kwh <= 0:
            target_soc = int(inputs.min_soc)
        else:
            target_soc = int(
                inputs.min_soc
                + (worth_charging_kwh / inputs.battery_capacity_kwh * 100)
            )

        # Clamp to valid range
        target_soc = max(int(inputs.min_soc), min(self.max_target_soc, target_soc))

        # Apply to overnight charge slots (grid_charge=True slots)
        for slot in state.slots:
            if slot.grid_charge:
                slot.capacity_soc = target_soc

        state.reason_log.append(
            f"CheapRateCharge: target {target_soc}% "
            f"(demand {expected_demand_kwh:.1f} kWh, "
            f"solar {expected_solar_kwh:.1f} kWh, "
            f"export {expected_profitable_export_kwh:.1f} kWh, "
            f"worth charging {worth_charging_kwh:.1f} kWh)"
        )
        return state
