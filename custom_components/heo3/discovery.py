"""Auto-discovery of HEO III config from HA entity registry.

Each function scans hass.states for the right entity-name pattern and
returns the discovered value (or None). Used by __init__.py at setup
time so the user doesn't have to enter half a dozen entity IDs by
hand.

Discovery is best-effort. If an integration isn't installed or has
unusual naming, the discovery returns None and the operator's
adapter degrades gracefully (returns empty/None for that data).
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def _all_entity_ids(hass) -> list[str]:  # type: ignore[no-untyped-def]
    return [s.entity_id for s in hass.states.async_all()]


# ── BD (octopus_energy) ───────────────────────────────────────────


_BD_DAY_RATES_PATTERN = re.compile(
    r"^event\.octopus_energy_electricity_(?P<key>[^_]+_[^_]+)_current_day_rates$"
)


def discover_bd_meter_key(hass) -> str | None:  # type: ignore[no-untyped-def]
    """Find the BD electricity import meter key {mpan}_{serial}.

    Picks the import meter (no `_export_` infix). Returns None if
    no event.octopus_energy_electricity_..._current_day_rates entity
    exists.
    """
    candidates = []
    for eid in _all_entity_ids(hass):
        m = _BD_DAY_RATES_PATTERN.match(eid)
        if m:
            candidates.append(m.group("key"))
    if not candidates:
        return None
    if len(candidates) > 1:
        logger.warning(
            "Multiple BD import meters detected: %s. Using first.",
            candidates,
        )
    return candidates[0]


# ── IGO smart-charge ──────────────────────────────────────────────


def discover_igo_dispatching_entity(
    hass,  # type: ignore[no-untyped-def]
) -> str | None:
    for eid in _all_entity_ids(hass):
        if eid.startswith("binary_sensor.octopus_energy_") and eid.endswith(
            "_intelligent_dispatching"
        ):
            return eid
    return None


# ── Octoplus saving sessions ──────────────────────────────────────


def discover_saving_session_entity(
    hass,  # type: ignore[no-untyped-def]
) -> str | None:
    for eid in _all_entity_ids(hass):
        if (
            eid.startswith("binary_sensor.octopus_energy_")
            and eid.endswith("_octoplus_saving_sessions")
        ):
            return eid
    return None


# ── Zappi ─────────────────────────────────────────────────────────


_ZAPPI_CHARGE_MODE_PATTERN = re.compile(
    r"^select\.(myenergi_zappi_\d+)_charge_mode$"
)


def discover_zappi_prefix(hass) -> str | None:  # type: ignore[no-untyped-def]
    """Returns e.g. 'myenergi_zappi_22752031' (no domain prefix)."""
    for eid in _all_entity_ids(hass):
        m = _ZAPPI_CHARGE_MODE_PATTERN.match(eid)
        if m:
            return m.group(1)
    return None


# ── Tesla (Teslemetry) ────────────────────────────────────────────


_TESLA_LOCATED_PATTERN = re.compile(
    r"^binary_sensor\.([a-z0-9_]+)_located_at_home$"
)


def discover_tesla_vehicle(hass) -> str | None:  # type: ignore[no-untyped-def]
    """Returns the vehicle short-name (e.g. 'natalia') if Teslemetry
    exposes a `binary_sensor.<vehicle>_located_at_home` paired with a
    `switch.<vehicle>_charge`.
    """
    located_match = None
    for eid in _all_entity_ids(hass):
        m = _TESLA_LOCATED_PATTERN.match(eid)
        if m:
            located_match = m.group(1)
            break
    if located_match is None:
        return None
    # Confirm the matching charge switch exists — narrows away from
    # non-Tesla "located_at_home" sensors (e.g. for Wi-Fi presence).
    if f"switch.{located_match}_charge" in _all_entity_ids(hass):
        return located_match
    return None


# ── Inverter sensor overrides ─────────────────────────────────────

# Real SA naming on Paddy's install differs from the leaf names HEO III
# uses internally. Discovery tries both the "expected" and the "real"
# entity IDs and returns an override map for the ones that need it.

_INVERTER_OVERRIDE_CANDIDATES: dict[str, list[str]] = {
    "battery_soc": [
        "sensor.sa_total_battery_state_of_charge",
        "sensor.sa_inverter_1_battery_soc",
    ],
    "solar_power": [
        "sensor.sa_inverter_1_pv_power",
        "sensor.sa_inverter_1_solar_power",
    ],
    "inverter_temperature": [
        "sensor.sa_inverter_1_temperature",
        "sensor.sa_inverter_1_inverter_temperature",
    ],
}


def _has_real_state(hass, entity_id: str) -> bool:  # type: ignore[no-untyped-def]
    """True if the entity exists AND has a non-null/non-unavailable state.

    Just checking registry presence isn't enough — HA can keep stale
    registry entries for entities that are no longer publishing
    (e.g. after an integration is reconfigured). We want only entities
    that are actually live.
    """
    s = hass.states.get(entity_id)
    if s is None:
        return False
    if s.state in (None, "unknown", "unavailable", ""):
        return False
    return True


def discover_inverter_sensor_overrides(
    hass,  # type: ignore[no-untyped-def]
) -> dict[str, str]:
    """Return {leaf: full_entity_id} for any leaf whose default name
    isn't live on this install but a known alternative is.

    Walks each leaf's candidate list in priority order; returns the
    first candidate that's actually publishing state. Only adds an
    override when the chosen candidate differs from the default
    (`sensor.sa_inverter_1_<leaf>`).
    """
    out: dict[str, str] = {}
    for leaf, candidates in _INVERTER_OVERRIDE_CANDIDATES.items():
        default = f"sensor.sa_inverter_1_{leaf}"
        for alt in candidates:
            if _has_real_state(hass, alt):
                if alt != default:
                    out[leaf] = alt
                break
    return out


# ── Combined ──────────────────────────────────────────────────────


def discover_all(hass) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Run every discoverer in one call, return a dict for logging."""
    return {
        "bd_meter_key": discover_bd_meter_key(hass),
        "igo_dispatching_entity": discover_igo_dispatching_entity(hass),
        "saving_session_entity": discover_saving_session_entity(hass),
        "zappi_prefix": discover_zappi_prefix(hass),
        "tesla_vehicle": discover_tesla_vehicle(hass),
        "inverter_sensor_overrides": discover_inverter_sensor_overrides(hass),
    }
