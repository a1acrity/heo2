# custom_components/heo2/__init__.py
"""HEO II — Rule-based SunSynk 6-slot timer programmer."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .const import DOMAIN

logger = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor", "switch", "number"]

# H7 cycle history persistence. Stored at
# /config/.storage/heo2_cycle_history (one Store per integration
# instance, keyed by entry_id). Bumped if the schema changes.
_CYCLE_STORE_VERSION = 1
_CYCLE_STORE_KEY_FMT = "heo2_cycle_history_{entry_id}"

# Last-known SOC persistence. Survives HA restart so the SOC fallback
# ladder (live > cache > 50% cold-boot) doesn't flunk to 50% just
# because SA's SOC entity hasn't populated yet on first tick.
_SOC_STORE_VERSION = 1
_SOC_STORE_KEY_FMT = "heo2_last_known_soc_{entry_id}"


async def async_setup_entry(hass, entry) -> bool:
    """Set up HEO II from a config entry."""
    from homeassistant.core import callback
    from homeassistant.helpers.event import (
        async_track_state_change_event,
        async_track_time_change,
    )
    from homeassistant.helpers.storage import Store

    from .coordinator import HEO2Coordinator

    coordinator = HEO2Coordinator(hass, entry)

    # H7: load persisted cycle history BEFORE first_refresh so the
    # 3-day rolling alert keeps its memory across HA restarts. Without
    # this the alert would need 3 fresh days of breaches to re-fire
    # after every restart.
    cycle_store = Store(
        hass,
        _CYCLE_STORE_VERSION,
        _CYCLE_STORE_KEY_FMT.format(entry_id=entry.entry_id),
    )
    stored = await cycle_store.async_load() or {}
    history = stored.get("daily_history") or []
    if isinstance(history, list):
        coordinator.cycle_tracker.daily_history = [
            float(c) for c in history if isinstance(c, (int, float))
        ]
    coordinator._cycle_store = cycle_store

    # Last-known SOC: load BEFORE first_refresh so the SOC fallback
    # ladder picks the persisted value over the 50% cold-boot when
    # SA's entity is still `unknown` on the first tick after restart.
    soc_store = Store(
        hass,
        _SOC_STORE_VERSION,
        _SOC_STORE_KEY_FMT.format(entry_id=entry.entry_id),
    )
    soc_stored = await soc_store.async_load() or {}
    saved_soc = soc_stored.get("last_known_soc")
    if isinstance(saved_soc, (int, float)) and 0 <= saved_soc <= 100:
        coordinator._last_known_soc = float(saved_soc)
    coordinator._soc_store = soc_store

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Seed the load profile from recorder history (HEO-5). Fire-and-forget
    # so we do not block startup on a DB query. The first coordinator tick
    # uses the flat baseline; subsequent ticks use the learned profile
    # once this task completes.
    #
    # Wrap the coroutine so exceptions surface in the log. Without the
    # wrapper, hass.async_create_task will swallow uncaught exceptions
    # silently and the caller has no way to see why the task did nothing.
    async def _seed_load_profile() -> None:
        # NOTE: using warning level temporarily so messages surface at HA's
        # default log level. Drop back to info once HEO-5 is proven working.
        logger.warning("HEO-5 startup refresh: scheduling load-profile learn")
        try:
            n_days = await coordinator.async_refresh_load_profile_from_recorder()
            logger.warning("HEO-5 startup refresh: complete, %d days learned", n_days)
        except Exception:
            logger.exception("HEO-5 startup refresh: raised")

    hass.async_create_task(_seed_load_profile())

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
        coordinator.cycle_tracker.reset_daily()
        # Persist the just-finished day's cycles into storage so the
        # 3-day rolling alert survives HA restart.
        hass.async_create_task(coordinator.persist_cycle_history())
        logger.info("CostTracker + CycleTracker: daily reset")

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

    # HEO-9: when the user saves new options via the OptionsFlow, HA
    # fires update_listener. Reload the entry so the coordinator
    # re-reads the merged config (entry.data + entry.options) instead
    # of staying on the cached snapshot from setup time.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass, entry) -> None:
    """Reload the integration when the OptionsFlow saves new options."""
    await hass.config_entries.async_reload(entry.entry_id)


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
