"""EV charging-state predicate. Pure function, no HA imports.

The coordinator's `_read_ev_charging()` reads an entity state and asks
"is the EV actively drawing now?". The vocabulary depends on which
entity the user wired:

  * binary_sensor.* -> "on" / "off"
  * Tesla integration sensor (e.g. sensor.{name}_charging) ->
    "charging" / "starting" / "stopped" / "complete" / "disconnected" /
    "no_power"
  * myenergi zappi status sensor ->
    "Charging" / "Boosting" / "Paused" / "EV Disconnected" / ...

This module owns the truthy-state set so the rule lives in one place
and is unit-testable without HA imports.

Captured 2026-05-06: pre-fix, ev_charging signal stayed False all day
because `_read_entity_bool` only matched on/true/1, never "charging".
EVChargingRule never fired, battery drained to feed the EV at peak
import rate.
"""

from __future__ import annotations


_EV_CHARGING_STATES = frozenset(
    {"on", "true", "1", "charging", "starting", "boosting"}
)


def is_ev_charging_state(state_str: str | None) -> bool:
    """True iff `state_str` indicates the EV charger is actively drawing.

    None / empty / "unknown" / "unavailable" -> False.
    Match is case-insensitive.
    """
    if not state_str:
        return False
    return state_str.lower() in _EV_CHARGING_STATES
