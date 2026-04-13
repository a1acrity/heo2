# custom_components/heo2/__init__.py
"""HEO II — Rule-based SunSynk 6-slot timer programmer."""

from __future__ import annotations

import logging

from .const import DOMAIN

logger = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor", "switch", "number"]


async def async_setup_entry(hass, entry) -> bool:
    """Set up HEO II from a config entry."""
    from .coordinator import HEO2Coordinator

    coordinator = HEO2Coordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry) -> bool:
    """Unload HEO II config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
