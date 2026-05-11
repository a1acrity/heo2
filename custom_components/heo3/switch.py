"""HEO III switches.

Two user-facing toggles surfaced as HA switch entities:

- switch.heo3_planner_enabled (default ON) — rule engine vs static fallback.
- switch.heo3_tuner_enabled (default OFF) — nightly auto-tuning.

State persists across HA restarts via the entity registry — toggles
made in the UI stick. HA imports are lazy so the module can be
imported by pytest without HA installed.
"""

from __future__ import annotations

import logging
from typing import Any

from .const import DOMAIN

logger = logging.getLogger(__name__)


SWITCH_PLANNER_ENABLED = "planner_enabled"
SWITCH_TUNER_ENABLED = "tuner_enabled"

_SWITCH_DEFAULTS = {
    SWITCH_PLANNER_ENABLED: True,   # Rule engine ON by default
    SWITCH_TUNER_ENABLED: False,    # Auto-tuner OFF by default
}

_SWITCH_LABELS = {
    SWITCH_PLANNER_ENABLED: "HEO III Planner Enabled",
    SWITCH_TUNER_ENABLED: "HEO III Tuner Enabled",
}


async def async_setup_entry(hass, entry, async_add_entities):  # type: ignore[no-untyped-def]
    """Register the two HEO III switch entities."""
    entities = [
        _make_switch(
            entry_id=entry.entry_id,
            switch_key=key,
            label=_SWITCH_LABELS[key],
            default_on=_SWITCH_DEFAULTS[key],
        )
        for key in (SWITCH_PLANNER_ENABLED, SWITCH_TUNER_ENABLED)
    ]
    async_add_entities(entities)


def _make_switch(*, entry_id, switch_key, label, default_on):  # type: ignore[no-untyped-def]
    """Construct one switch entity. HA imports happen here."""
    from homeassistant.components.switch import SwitchEntity
    from homeassistant.helpers.restore_state import RestoreEntity

    class HEO3Switch(SwitchEntity, RestoreEntity):
        _attr_should_poll = False

        def __init__(self) -> None:
            self._entry_id = entry_id
            self._switch_key = switch_key
            self._default_on = default_on
            self._attr_name = label
            self._attr_unique_id = f"heo3_{switch_key}_{entry_id}"
            self._attr_is_on = default_on

        async def async_added_to_hass(self) -> None:
            await super().async_added_to_hass()
            last = await self.async_get_last_state()
            if last is not None and last.state in ("on", "off"):
                self._attr_is_on = last.state == "on"

        async def async_turn_on(self, **kwargs: Any) -> None:
            if not self._attr_is_on:
                self._attr_is_on = True
                self.async_write_ha_state()
                logger.info("HEO III switch %s → on", self._switch_key)

        async def async_turn_off(self, **kwargs: Any) -> None:
            if self._attr_is_on:
                self._attr_is_on = False
                self.async_write_ha_state()
                logger.info("HEO III switch %s → off", self._switch_key)

    return HEO3Switch()


def is_planner_enabled(hass) -> bool:  # type: ignore[no-untyped-def]
    """Read the live state of the planner-enabled switch.

    Coordinator calls this each tick to decide whether to use the
    rule engine or fall back to baseline_static.
    """
    return _read_switch(hass, SWITCH_PLANNER_ENABLED, _SWITCH_DEFAULTS[SWITCH_PLANNER_ENABLED])


def is_tuner_enabled(hass) -> bool:  # type: ignore[no-untyped-def]
    """Read the live state of the tuner-enabled switch."""
    return _read_switch(hass, SWITCH_TUNER_ENABLED, _SWITCH_DEFAULTS[SWITCH_TUNER_ENABLED])


def _read_switch(hass, key: str, default: bool) -> bool:  # type: ignore[no-untyped-def]
    s = hass.states.get(f"switch.heo3_{key}")
    if s is None:
        return default
    return s.state == "on"
