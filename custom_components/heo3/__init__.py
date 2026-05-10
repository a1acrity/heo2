"""HEO III — Energy Optimiser (operator module).

Tracking issue: https://github.com/a1acrity/heo2/issues/75
Design doc: docs/HEO_III_DESIGN.md

P1.0: package skeleton + Operator class with stub methods + mock
transport + config_flow shell. The integration loads cleanly in HA
but does no inverter I/O until P1.1+ fills in the adapters.
"""

from __future__ import annotations

import logging

from .const import DOMAIN

logger = logging.getLogger(__name__)

PLATFORMS: list[str] = []  # Sensors/switches added in later phases.


async def async_setup_entry(hass, entry) -> bool:  # type: ignore[no-untyped-def]
    """Set up HEO III from a config entry. P1.0: no-op + log."""
    logger.info(
        "HEO III setup_entry — skeleton only, no I/O until P1.1+. entry_id=%s",
        entry.entry_id,
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"phase": "P1.0"}
    return True


async def async_unload_entry(hass, entry) -> bool:  # type: ignore[no-untyped-def]
    """Tear down."""
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True
