"""CheapRateChargeRule -- size overnight charge to bridge until PV takes over."""

from __future__ import annotations

from ..models import ProgrammeInputs
from ..rank_pricing import next_cheap_window_end_local
from ..rule_engine import PRIO_CHEAP_RATE_CHARGE, Rule


class CheapRateChargeRule(Rule):
    """Size the overnight grid_charge target so the battery has just
    enough capacity to bridge from end-of-cheap-window through to the
    point tomorrow when PV generation overtakes load.

    The previous incarnation of this rule sized the charge target as
    `expected_demand + profitable_export - expected_solar`. With Octopus
    Outgoing peaks well below the IGO arbitrage threshold, that maths
    pushed the overnight target to 100% even when tomorrow's PV would
    fully refill the battery anyway -- giving up free solar to instead
    cycle through grid-charged kWh paid for at IGO off-peak.

    The new strategy (what the user actually wants from a self-supply
    install):

      target_soc = min_soc
                 + morning_bridge_kwh / battery_capacity_kwh * 100
                 + safety_buffer

    where `morning_bridge_kwh` is the cumulative deficit between
    tomorrow's hourly load forecast and tomorrow's hourly PV forecast,
    summed from the end of the cheap window until the first hour where
    `solar >= load` (the "PV takeover" hour). Beyond that hour PV
    covers load and any surplus charges the battery -- so the overnight
    grid charge only needs to cover the morning gap.

    Edge cases:
      * No tomorrow PV forecast -> fall back to filling battery
        completely (we have no signal of when PV would take over).
      * PV never overtakes load (deep winter) -> sum the whole day's
        deficit; clamps to max_target_soc.
      * Very small bridge (PV takeover early morning) -> safety buffer
        keeps target above min_soc so a forecast miss doesn't strand
        us at floor.

    Implements the SPEC §5 priority 2: "Avoid grid use generally --
    cover load from battery+solar where economical."
    """

    name = "cheap_rate_charge"
    description = "Size overnight charge to bridge from cheap-window end to PV takeover"
    priority_class = PRIO_CHEAP_RATE_CHARGE

    def __init__(
        self,
        max_target_soc: int = 100,
        *,
        cheap_window_end_hour_fallback: int = 5,  # IGO off-peak ends 05:30; we use whole hour 5
        safety_buffer_pct: int = 10,
    ):
        self.max_target_soc = max_target_soc
        self.cheap_window_end_hour_fallback = cheap_window_end_hour_fallback
        self.safety_buffer_pct = safety_buffer_pct

    def propose(self, view, inputs: ProgrammeInputs) -> None:
        tomorrow_solar = inputs.solar_forecast_kwh_tomorrow
        load = inputs.load_forecast_kwh

        end_dt = next_cheap_window_end_local(
            inputs.import_rates, inputs.now_local(), inputs.local_tz,
        )
        cheap_end_hour = (
            end_dt.hour if end_dt is not None
            else self.cheap_window_end_hour_fallback
        )

        if not tomorrow_solar or len(tomorrow_solar) < 24:
            target_soc = self.max_target_soc
            reason = (
                "no tomorrow PV forecast available; "
                f"defaulting to {target_soc}%"
            )
        else:
            bridge_kwh = 0.0
            takeover_hour: int | None = None
            for h in range(cheap_end_hour, 24):
                solar = tomorrow_solar[h]
                load_h = load[h]
                if solar >= load_h and h > cheap_end_hour:
                    takeover_hour = h
                    break
                bridge_kwh += max(0.0, load_h - solar)

            bridge_pct = bridge_kwh / inputs.battery_capacity_kwh * 100
            min_soc_int = int(inputs.min_soc)
            target_soc = int(
                min_soc_int + bridge_pct + self.safety_buffer_pct
            )
            target_soc = max(
                min_soc_int, min(self.max_target_soc, target_soc),
            )

            if takeover_hour is None:
                reason = (
                    f"no PV takeover within forecast horizon "
                    f"(deep winter); target {target_soc}% covers "
                    f"the day's full {bridge_kwh:.1f} kWh deficit"
                )
            else:
                reason = (
                    f"target {target_soc}% bridges "
                    f"{bridge_kwh:.1f} kWh from "
                    f"{cheap_end_hour:02d}:00 to PV takeover "
                    f"at {takeover_hour:02d}:00 "
                    f"(+{self.safety_buffer_pct}% safety buffer)"
                )

        for slot in view.slots:
            if slot.grid_charge:
                view.claim_slot(
                    slot.index, "capacity_soc", target_soc,
                    reason=f"cheap-charge target ({reason})",
                )

        view.log(f"CheapRateCharge: {reason}")
