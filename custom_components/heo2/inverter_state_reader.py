# custom_components/heo2/inverter_state_reader.py
"""Read the currently programmed 6-slot state from HA entities that
Solar Assistant publishes via MQTT discovery.

Pure logic layer takes a `state_lookup` callable so it can be unit
tested without HA. The HA-side helper `read_from_hass` is a thin
adapter that supplies a real lookup.

This module is the source of truth for "what's on the inverter right
now" and is used by the coordinator to seed and maintain its tracked
``last_known_programme`` against which diffs are computed.
"""

from __future__ import annotations

import logging
from datetime import time
from typing import Callable

from .models import ProgrammeState, SlotConfig

logger = logging.getLogger(__name__)

# SA publishes these as snake_case in MQTT; HA mirrors them as entity IDs
# under the sa_inverter_{N} prefix after discovery.
_CAPACITY_FMT = "sensor.sa_inverter_{inv}_capacity_point_{n}"
_GRID_CHARGE_FMT = "sensor.sa_inverter_{inv}_grid_charge_point_{n}"
_TIME_FMT = "sensor.sa_inverter_{inv}_time_point_{n}"


# Fallback defaults when an entity is missing or unparseable. Chosen to
# be recognisable as "we don't know" placeholders rather than realistic
# values: SOC at 50 and grid_charge False and a 04:00 time stamp.
_FALLBACK_SOC = 50
_FALLBACK_GRID_CHARGE = False
_FALLBACK_TIME = time(4, 0)


def parse_bool(raw: str) -> bool:
    """SA publishes grid_charge as 'true'/'false' strings. Be lenient."""
    return str(raw).strip().lower() in ("true", "on", "enabled", "yes", "1")


def parse_time(raw: str) -> time | None:
    """SA publishes time_point as 'HH:MM'. Return None on unparseable."""
    if not raw:
        return None
    text = str(raw).strip()
    if ":" not in text:
        return None
    try:
        h, m = text.split(":", 1)
        return time(int(h), int(m))
    except (ValueError, TypeError):
        return None


def parse_soc(raw: str) -> int | None:
    """SA publishes capacity_point as an integer 0-100. Return None if
    unparseable or out of range."""
    try:
        v = int(float(str(raw).strip()))
    except (ValueError, TypeError):
        return None
    if not (0 <= v <= 100):
        return None
    return v


def read_programme_state(
    state_lookup: Callable[[str], str | None],
    inverter_name: str = "inverter_1",
) -> ProgrammeState:
    """Build a ProgrammeState from live HA entity values.

    `state_lookup(entity_id)` must return the state string or None if
    the entity is missing/unavailable.

    Any unparseable slot value is replaced with a fallback. The coordinator
    still computes a valid diff against this; worst case is a spurious
    write-back to the "real" value on next tick, which is fine.

    Note on slot start/end times:
      SA's `time_point_N` is the END of slot N (equivalently the START
      of slot N+1). This mirrors the Sunsynk programme semantics. The
      returned SlotConfig has:
        start_time = time_point_{n-1}  (time_point_6 wraps for slot 1)
        end_time   = time_point_{n}
    """
    inv = inverter_name.replace("inverter_", "")

    # Read all 6 time_points first so we can pair them
    time_points: list[time] = []
    for n in range(1, 7):
        raw = state_lookup(_TIME_FMT.format(inv=inv, n=n))
        parsed = parse_time(raw) if raw else None
        time_points.append(parsed if parsed is not None else _FALLBACK_TIME)

    slots: list[SlotConfig] = []
    for n in range(1, 7):
        cap_raw = state_lookup(_CAPACITY_FMT.format(inv=inv, n=n))
        gc_raw = state_lookup(_GRID_CHARGE_FMT.format(inv=inv, n=n))

        cap = parse_soc(cap_raw) if cap_raw else None
        if cap is None:
            cap = _FALLBACK_SOC

        gc = parse_bool(gc_raw) if gc_raw else _FALLBACK_GRID_CHARGE

        # time_point_N is slot N's end; slot N's start is time_point_{N-1},
        # with slot 1's start being time_point_6 (wraps midnight).
        start_idx = (n - 2) % 6  # -1 -> 5, 0 -> 0, etc
        start_time = time_points[start_idx]
        end_time = time_points[n - 1]

        slots.append(SlotConfig(
            start_time=start_time,
            end_time=end_time,
            capacity_soc=cap,
            grid_charge=gc,
        ))

    return ProgrammeState(slots=slots, reason_log=[])


def read_from_hass(
    hass,
    inverter_name: str = "inverter_1",
) -> ProgrammeState:
    """HA-side adapter. Calls hass.states.get(...).state for each
    required entity and delegates to read_programme_state.
    """
    def _lookup(entity_id: str) -> str | None:
        state = hass.states.get(entity_id) if hass else None
        if state is None:
            return None
        if state.state in ("unknown", "unavailable", "none", ""):
            return None
        return state.state

    return read_programme_state(_lookup, inverter_name=inverter_name)
