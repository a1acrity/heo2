# custom_components/heo2/rules/ev_deferral.py
"""EVDeferralRule -- SPEC §12 EV charge deferral.

When the user has flagged "car not needed tomorrow" via
`switch.heo_ii_defer_ev_when_export_high`, AND the battery is high
enough to ride out a non-charge interval, AND the current export rate
is in a top-pay-back window, this rule switches the inverter to
"Selling first" and flags the programme so the coordinator halts the
zappi charge for the duration. The battery exports at peak rates
instead of feeding the EV at the live published rate.

Triggers (ALL must be true):

* `inputs.defer_ev_eligible` (the user-facing dashboard switch).
* `inputs.current_soc >= deferral_min_soc` (default 80%) - we won't
  defer the EV charge if the battery itself is low.
* `inputs.current_export_rate_p >= deferral_min_export_p` (default
  15 p/kWh). Below that, the SPEC §12 "ridiculous low export" fallback
  cancels the deferral so the car doesn't get stuck unable to charge
  on a quiet pricing day.

Outputs:

* `state.work_mode = "Selling first"` so the inverter exports.
* `state.ev_deferral_active = True` so the coordinator can dispatch
  the zappi service-call (one-shot per transition).

The actual zappi `charge_mode -> Stopped` write happens in the
coordinator (HA service call out of scope for pure rule logic).
"""

from __future__ import annotations

from ..models import ProgrammeInputs, ProgrammeState
from ..rule_engine import Rule


class EVDeferralRule(Rule):
    """SPEC §12 EV charge deferral when conditions favour exporting."""

    name = "ev_deferral"
    description = "Stop EV charge during high export when car not needed"

    def __init__(
        self,
        *,
        deferral_min_soc: float = 80.0,
        deferral_min_export_p: float = 15.0,
    ):
        self.deferral_min_soc = deferral_min_soc
        self.deferral_min_export_p = deferral_min_export_p

    def apply(self, state: ProgrammeState, inputs: ProgrammeInputs) -> ProgrammeState:
        if not inputs.defer_ev_eligible:
            return state

        if inputs.current_soc < self.deferral_min_soc:
            state.reason_log.append(
                f"EVDeferral: eligible but SOC {inputs.current_soc:.0f}% "
                f"below {self.deferral_min_soc:.0f}% threshold; not deferring"
            )
            return state

        export_p = inputs.current_export_rate_p
        if export_p is None:
            state.reason_log.append(
                "EVDeferral: eligible but no live export rate; not deferring"
            )
            return state

        if export_p < self.deferral_min_export_p:
            # SPEC §12 "ridiculous low export" fallback - let the car
            # charge as normal, the export wouldn't be worth it.
            state.reason_log.append(
                f"EVDeferral: eligible but export {export_p:.2f}p below "
                f"{self.deferral_min_export_p:.2f}p threshold; not deferring"
            )
            return state

        # All triggers met. Flip work_mode to Selling first and signal
        # the coordinator to halt the zappi.
        state.work_mode = "Selling first"
        state.ev_deferral_active = True
        state.reason_log.append(
            f"EVDeferral: ACTIVE (SOC {inputs.current_soc:.0f}%, "
            f"export {export_p:.2f}p >= {self.deferral_min_export_p:.2f}p); "
            f"work_mode -> Selling first, zappi -> Stopped"
        )
        return state
