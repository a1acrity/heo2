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
# SPEC §2 globals. SA's discovery publishes work_mode / energy_pattern
# as `select` platform entries but HA registers the state under the
# `sensor.sa_inverter_*` namespace (verified via REST at deploy time).
# `select.sa_inverter_1_work_mode` returns 404; `sensor.sa_inverter_1_work_mode`
# returns the current value.
_WORK_MODE_FMT = "sensor.sa_inverter_{inv}_work_mode"
_ENERGY_PATTERN_FMT = "sensor.sa_inverter_{inv}_energy_pattern"
# SA exposes charge/discharge limits as numeric current sensors in
# Amps. ProgrammeState carries them as `max_charge_a` / `max_discharge_a`
# floats; conversion to/from watts is the user's concern (multiply by
# battery nominal voltage, ~51.2V for 4x BP51.2 in series).
_MAX_CHARGE_CURRENT_FMT = "sensor.sa_inverter_{inv}_max_charge_current"
_MAX_DISCHARGE_CURRENT_FMT = "sensor.sa_inverter_{inv}_max_discharge_current"


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
      SA's `time_point_N` is the START of slot N (the Sunsynk timer
      register convention - verified against the SA UI mapping).
      Slot N covers [time_point_N, time_point_{N+1}), with slot 6
      wrapping to time_point_1 for its end. So the returned SlotConfig
      for slot N has:
        start_time = time_point_{n}
        end_time   = time_point_{n+1 mod 6}  (= time_point_1 for slot 6)

      Pre-2026-05-02 this module had the convention reversed (treating
      time_point_N as slot N's END). That bug shifted all SOC and
      grid_charge writes by one slot relative to their intended time
      windows - see commit message for HEO-31.
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

        # time_point_N is slot N's START. Slot N runs from time_point_N
        # to time_point_{N+1}, with slot 6 wrapping to time_point_1.
        start_time = time_points[n - 1]
        end_time = time_points[n % 6]  # n=6 -> index 0 (wraps midnight)

        slots.append(SlotConfig(
            start_time=start_time,
            end_time=end_time,
            capacity_soc=cap,
            grid_charge=gc,
        ))

    work_mode_raw = state_lookup(_WORK_MODE_FMT.format(inv=inv))
    work_mode = work_mode_raw.strip() if work_mode_raw else None
    energy_pattern_raw = state_lookup(_ENERGY_PATTERN_FMT.format(inv=inv))
    energy_pattern = energy_pattern_raw.strip() if energy_pattern_raw else None

    def _parse_amps(raw: str | None) -> float | None:
        if raw is None:
            return None
        try:
            return float(raw)
        except (ValueError, TypeError):
            return None

    max_charge_a = _parse_amps(
        state_lookup(_MAX_CHARGE_CURRENT_FMT.format(inv=inv))
    )
    max_discharge_a = _parse_amps(
        state_lookup(_MAX_DISCHARGE_CURRENT_FMT.format(inv=inv))
    )

    return ProgrammeState(
        slots=slots,
        reason_log=[],
        work_mode=work_mode,
        energy_pattern=energy_pattern,
        max_charge_a=max_charge_a,
        max_discharge_a=max_discharge_a,
    )


def read_from_hass(
    hass,
    inverter_name: str = "inverter_1",
) -> ProgrammeState | None:
    """HA-side adapter. Reads SA-published slot entities into a
    ProgrammeState.

    Returns None if ANY of the 18 required entities (6 slots x 3 params:
    capacity_point, time_point, grid_charge_point) is missing or in an
    unknown/unavailable state. This handles the HA startup race: on the
    first coordinator tick, HA's MQTT discovery may not have populated
    the SA entities yet, so we'd fall back to junk values (cap=50 etc.)
    and compute a bogus diff. Returning None lets the caller defer
    seeding to the next tick when discovery has completed.

    When all entities are present, returns a valid ProgrammeState built
    from them (delegates to read_programme_state).
    """
    def _lookup(entity_id: str) -> str | None:
        state = hass.states.get(entity_id) if hass else None
        if state is None:
            return None
        if state.state in ("unknown", "unavailable", "none", ""):
            return None
        return state.state

    # Pre-flight: verify all 18 required entities are populated before
    # building a ProgrammeState. Any missing means HA MQTT discovery
    # hasn't finished or the bridge is down; either way, don't seed.
    inv = inverter_name.replace("inverter_", "")
    required_templates = [
        "sensor.sa_inverter_{inv}_capacity_point_{n}",
        "sensor.sa_inverter_{inv}_time_point_{n}",
        "sensor.sa_inverter_{inv}_grid_charge_point_{n}",
    ]
    missing: list[str] = []
    for n in range(1, 7):
        for tmpl in required_templates:
            entity_id = tmpl.format(inv=inv, n=n)
            if _lookup(entity_id) is None:
                missing.append(entity_id)

    if missing:
        logger.info(
            "inverter_state_reader: %d/%d entities missing/unavailable "
            "(e.g. %s); deferring seed until next tick",
            len(missing), 18, missing[0],
        )
        return None

    return read_programme_state(_lookup, inverter_name=inverter_name)
