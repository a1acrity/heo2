"""Config flow for HEO III — single-step shell for P1.0.

Real wizard (MQTT broker, inverter name, peripheral entity IDs,
storage path) lands incrementally as the adapter phases need them.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT

from .const import DEFAULT_MQTT_HOST, DEFAULT_MQTT_PORT, DOMAIN


class HEO3ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None):
        """Single-step setup: just the SA broker host/port."""
        if user_input is not None:
            return self.async_create_entry(
                title="HEO III",
                data=user_input,
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=DEFAULT_MQTT_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_MQTT_PORT): int,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)
