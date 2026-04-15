# custom_components/heo2/__init__.py
"""HEO II — Rule-based SunSynk 6-slot timer programmer."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .const import DOMAIN

logger = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor", "switch", "number"]


async def async_setup_entry(hass, entry) -> bool:
    """Set up HEO II from a config entry."""
    from homeassistant.core import callback
    from homeassistant.helpers.event import (
        async_track_state_change_event,
        async_track_time_change,
    )

    from .coordinator import HEO2Coordinator

    coordinator = HEO2Coordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Wire up CostTracker state-change listeners
    unsub_callbacks = []
    config = dict(entry.data)

    load_entity = config.get("load_power_entity", "")
    pv_entity = config.get("pv_power_entity", "")

    if load_entity:

        @callback
        def _handle_load_change(event) -> None:
            new_state = event.data.get("new_state")
            if new_state is None or new_state.state in ("unknown", "unavailable"):
                return
            try:
                watts = float(new_state.state)
            except (ValueError, TypeError):
                return
            now = datetime.now(timezone.utc)
            inputs = coordinator.last_inputs
            rate = inputs.rate_at(now) if inputs else 0.0
            coordinator.cost_accumulator.update_load(
                watts=watts, now=now, import_rate_pence=rate or 0.0
            )

        unsub_callbacks.append(
            async_track_state_change_event(hass, [load_entity], _handle_load_change)
        )

    if pv_entity:

        @callback
        def _handle_pv_change(event) -> None:
            new_state = event.data.get("new_state")
            if new_state is None or new_state.state in ("unknown", "unavailable"):
                return
            try:
                watts = float(new_state.state)
            except (ValueError, TypeError):
                return
            now = datetime.now(timezone.utc)
            inputs = coordinator.last_inputs
            import_rate = inputs.rate_at(now) if inputs else 0.0
            export_rate = inputs.export_rate_at(now) if inputs else 0.0
            coordinator.cost_accumulator.update_pv(
                watts=watts,
                now=now,
                import_rate_pence=import_rate or 0.0,
                export_rate_pence=export_rate or 0.0,
            )

        unsub_callbacks.append(
            async_track_state_change_event(hass, [pv_entity], _handle_pv_change)
        )

    # Daily reset at midnight
    @callback
    def _daily_reset(_now) -> None:
        now = datetime.now(timezone.utc)
        coordinator.cost_accumulator.reset_daily(now)
        logger.info("CostTracker: daily reset")

    unsub_callbacks.append(
        async_track_time_change(hass, _daily_reset, hour=0, minute=0, second=0)
    )

    # Weekly reset Monday 00:00
    @callback
    def _weekly_reset(_now) -> None:
        now = datetime.now(timezone.utc)
        if now.weekday() == 0:  # Monday
            coordinator.cost_accumulator.reset_weekly(now)
            logger.info("CostTracker: weekly reset")

    unsub_callbacks.append(
        async_track_time_change(hass, _weekly_reset, hour=0, minute=1, second=0)
    )

    # Octopus billing fetch at 06:00 daily (if configured)
    if coordinator.octopus is not None:

        @callback
        def _octopus_fetch(_now) -> None:
            now = datetime.now(timezone.utc)
            # Check for month rollover
            if now.day == 1:
                coordinator.octopus.snapshot_month_end()
            hass.async_create_task(coordinator.octopus.fetch_monthly_bill(now))
            logger.info("OctopusBillingFetcher: daily fetch triggered")

        unsub_callbacks.append(
            async_track_time_change(hass, _octopus_fetch, hour=6, minute=0, second=0)
        )

    # Store cleanup callbacks
    hass.data[DOMAIN][f"{entry.entry_id}_unsub"] = unsub_callbacks

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry) -> bool:
    """Unload HEO II config entry."""
    # Unsubscribe all event listeners
    unsub_key = f"{entry.entry_id}_unsub"
    for unsub in hass.data[DOMAIN].get(unsub_key, []):
        unsub()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        hass.data[DOMAIN].pop(unsub_key, None)
    return unload_ok
