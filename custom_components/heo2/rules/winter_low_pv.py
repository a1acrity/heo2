# custom_components/heo2/rules/winter_low_pv.py
"""WinterLowPVRule -- SPEC §9 row 5 / implicit winter operating mode.

Triggered when the daily solar forecast is below the daily load
forecast. In that regime, the priorities shift away from "drain to
export when prices are high" toward "preserve cycles for our own use,
charge fully overnight, defend a higher evening floor".

Concrete behaviours:

* Every grid_charge=True slot is forced to capacity_soc=100 (override
  any lower target CheapRateChargeRule chose). Winter overnight is
  the only meaningful charge window, so use it fully.
* Every non-GC slot occurring AFTER any cheap-charge window is given
  a higher floor (max of the existing cap and a winter floor based
  on the day's load demand). Without this, ExportWindowRule may have
  already drained slots to min_soc earlier in the chain.
* Slot caps are NOT lowered - the rule only raises floors. So
  EveningProtect / SafetyRule downstream behave as normal.

Rule order (rules/__init__.py): runs AFTER ExportWindowRule and
EveningProtectRule but BEFORE SavingSessionRule / IGODispatchRule /
EVChargingRule / EPSModeRule. Saving sessions and EV charging events
should still win over the seasonal default.
"""

from __future__ import annotations

from ..models import ProgrammeInputs, ProgrammeState
from ..rule_engine import Rule


class WinterLowPVRule(Rule):
    """Override charge / floor targets when daily PV < daily load."""

    name = "winter_low_pv"
    description = (
        "Winter / low-PV mode: max overnight charge + raise daytime floor"
    )

    def apply(self, state: ProgrammeState, inputs: ProgrammeInputs) -> ProgrammeState:
        if not inputs.is_winter_low_pv:
            return state

        max_target = 100
        modified: list[str] = []
        # Winter mode raises ONLY grid_charge=True (overnight) slot
        # caps to 100%. CheapRateChargeRule may have picked a smarter
        # partial target based on tomorrow's solar; in winter that's
        # wrong - the cheap window is the only meaningful charge
        # opportunity, so use it fully.
        #
        # Pre-2026-05-03 the rule ALSO raised non-GC slot floors to a
        # day-load-sized number (often clamped to 100). That was wrong:
        # it pinned the inverter at 100% during the day AND evening,
        # so the battery never discharged and the grid covered all
        # load. EveningProtectRule already handles the legitimate
        # "raise pre-evening floor for evening reserve" need; this
        # rule deliberately does NOT touch non-GC slots.
        for i, slot in enumerate(state.slots):
            if not slot.grid_charge:
                continue
            if slot.capacity_soc < max_target:
                modified.append(
                    f"slot {i + 1} GC cap "
                    f"{slot.capacity_soc}->{max_target}"
                )
                slot.capacity_soc = max_target

        daily_solar = sum(inputs.solar_forecast_kwh)
        daily_load = sum(inputs.load_forecast_kwh)
        if modified:
            state.reason_log.append(
                f"WinterLowPV: solar {daily_solar:.1f} < load "
                f"{daily_load:.1f} kWh; "
                + "; ".join(modified)
            )
        else:
            state.reason_log.append(
                f"WinterLowPV: solar {daily_solar:.1f} < load "
                f"{daily_load:.1f} kWh; GC slots already at 100%"
            )
        return state
