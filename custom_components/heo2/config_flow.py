# custom_components/heo2/config_flow.py
"""Config flow for HEO II — 6-step setup wizard."""

from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_USERNAME, CONF_PASSWORD
from homeassistant.core import callback

from .const import (
    DOMAIN,
    DEFAULT_MIN_SOC,
    DEFAULT_MAX_SOC,
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_MAX_CHARGE_KW,
    DEFAULT_MAX_DISCHARGE_KW,
    DEFAULT_CHARGE_EFFICIENCY,
    DEFAULT_DISCHARGE_EFFICIENCY,
    DEFAULT_IGO_NIGHT_RATE_PENCE,
    DEFAULT_IGO_DAY_RATE_PENCE,
    DEFAULT_LOAD_BASELINE_W,
    DEFAULT_SYSTEM_COST,
    DEFAULT_ADDITIONAL_COSTS,
    DEFAULT_SAVINGS_TO_DATE,
    DEFAULT_INSTALL_DATE,
    MQTT_BASE_TOPIC,
)


class HEO2ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict = {}

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "HEO2OptionsFlow":
        """Expose an OptionsFlow so the integration's Configure button
        appears in Settings -> Devices & services -> HEO II.

        Without this, post-setup config drift (e.g. HEO-9: wrong
        saving_session_entity / appliance switch IDs) requires editing
        `.storage/core.config_entries` directly while HA is stopped,
        which is fragile and easy to mis-do. The OptionsFlow lets the
        user fix entity wiring through the normal HA UI.
        """
        return HEO2OptionsFlow(config_entry)

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_battery()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("mqtt_host", default="localhost"): str,
                vol.Required("mqtt_port", default=1883): vol.Coerce(int),
                vol.Optional("mqtt_username", default=""): str,
                vol.Optional("mqtt_password", default=""): str,
                vol.Required("mqtt_base_topic", default=MQTT_BASE_TOPIC): str,
            }),
        )

    async def async_step_battery(self, user_input=None):
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_tariff()
        return self.async_show_form(
            step_id="battery",
            data_schema=vol.Schema({
                vol.Required("battery_capacity_kwh", default=DEFAULT_BATTERY_CAPACITY_KWH): vol.Coerce(float),
                vol.Required("min_soc", default=DEFAULT_MIN_SOC): vol.Coerce(int),
                vol.Required("max_soc", default=DEFAULT_MAX_SOC): vol.Coerce(int),
                vol.Required("max_charge_kw", default=DEFAULT_MAX_CHARGE_KW): vol.Coerce(float),
                vol.Required("max_discharge_kw", default=DEFAULT_MAX_DISCHARGE_KW): vol.Coerce(float),
                vol.Required("charge_efficiency", default=DEFAULT_CHARGE_EFFICIENCY): vol.Coerce(float),
                vol.Required("discharge_efficiency", default=DEFAULT_DISCHARGE_EFFICIENCY): vol.Coerce(float),
            }),
        )

    async def async_step_tariff(self, user_input=None):
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_entities()
        return self.async_show_form(
            step_id="tariff",
            data_schema=vol.Schema({
                vol.Required("igo_day_rate", default=DEFAULT_IGO_DAY_RATE_PENCE): vol.Coerce(float),
                vol.Required("igo_night_rate", default=DEFAULT_IGO_NIGHT_RATE_PENCE): vol.Coerce(float),
                vol.Required("igo_night_start", default="23:30"): str,
                vol.Required("igo_night_end", default="05:30"): str,
            }),
        )

    async def async_step_entities(self, user_input=None):
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_rules()
        return self.async_show_form(
            step_id="entities",
            data_schema=vol.Schema({
                vol.Required("soc_entity"): str,
                vol.Optional("load_power_entity", default=""): str,
                vol.Optional("pv_power_entity", default=""): str,
                vol.Optional("ev_status_entity", default=""): str,
                vol.Optional("igo_dispatch_entity", default=""): str,
                vol.Optional("saving_session_entity", default=""): str,
                vol.Optional("tapo_wash_entity", default=""): str,
                vol.Optional("tapo_dryer_entity", default=""): str,
                vol.Optional("tapo_dishwasher_entity", default=""): str,
            }),
        )

    async def async_step_rules(self, user_input=None):
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_services()
        return self.async_show_form(
            step_id="rules",
            data_schema=vol.Schema({
                vol.Required("rule_cheap_rate_charge", default=True): bool,
                vol.Required("rule_solar_surplus", default=True): bool,
                vol.Required("rule_export_window", default=True): bool,
                vol.Required("rule_evening_protect", default=True): bool,
                vol.Required("rule_igo_dispatch", default=True): bool,
                vol.Required("rule_ev_charging", default=True): bool,
                vol.Required("max_target_soc", default=100): vol.Coerce(int),
            }),
        )

    async def async_step_services(self, user_input=None):
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_octopus()
        return self.async_show_form(
            step_id="services",
            data_schema=vol.Schema({
                vol.Optional("solcast_api_key", default=""): str,
                vol.Optional("solcast_resource_id", default=""): str,
                vol.Optional("agilepredict_url", default=""): str,
                vol.Required("load_baseline_w", default=DEFAULT_LOAD_BASELINE_W): vol.Coerce(float),
                vol.Required("dry_run", default=True): bool,
            }),
        )

    async def async_step_octopus(self, user_input=None):
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_payback()
        return self.async_show_form(
            step_id="octopus",
            data_schema=vol.Schema({
                vol.Optional("octopus_api_key", default=""): str,
                vol.Optional("octopus_account_number", default=""): str,
                vol.Optional("octopus_mpan", default=""): str,
                vol.Optional("octopus_serial", default=""): str,
                vol.Optional("octopus_product_code", default=""): str,
                vol.Optional("octopus_tariff_code", default=""): str,
            }),
        )

    async def async_step_payback(self, user_input=None):
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(
                title="HEO II",
                data=self._data,
            )
        return self.async_show_form(
            step_id="payback",
            data_schema=vol.Schema({
                vol.Required("system_cost", default=DEFAULT_SYSTEM_COST): vol.Coerce(float),
                vol.Required("additional_costs", default=DEFAULT_ADDITIONAL_COSTS): vol.Coerce(float),
                vol.Required("savings_to_date", default=DEFAULT_SAVINGS_TO_DATE): vol.Coerce(float),
                vol.Required("install_date", default=DEFAULT_INSTALL_DATE): str,
            }),
        )


# Fields shown in the OptionsFlow. Entity wiring (HEO-9 motivating
# use case), dry_run, and the SPEC §10 tunable rule knobs (HEO-11).
# Battery / tariff / payback rarely change and stay in the initial
# setup wizard.
_OPTIONS_ENTITY_FIELDS = (
    "soc_entity",
    "load_power_entity",
    "pv_power_entity",
    "ev_status_entity",
    "igo_dispatch_entity",
    "saving_session_entity",
    "tapo_wash_entity",
    "tapo_dryer_entity",
    "tapo_dishwasher_entity",
)

# (key, default, type) - SPEC §10 rule knobs
_OPTIONS_RULE_KNOBS = (
    ("peak_threshold_p", 24.0, float),
    ("max_target_soc", 100, int),
    ("daily_plan_time", "18:00", str),
    ("replan_solar_pct", 25, int),
    ("replan_load_pct", 25, int),
    ("replan_soc_pct", 10, int),
    ("sell_top_pct_default", 30, int),
    ("cheap_charge_bottom_pct", 25, int),
    ("cycle_budget", 2.0, float),  # H7 daily soft target
)


class HEO2OptionsFlow(config_entries.OptionsFlow):
    """Lets the user adjust entity IDs and dry_run after initial setup.

    Saved options are written to `entry.options`; the coordinator
    merges `{**entry.data, **entry.options}` into `_config` at startup
    so options take precedence. Reload happens automatically via HA's
    config-entries plumbing once the OptionsFlow returns
    `async_create_entry`.
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            # Strip empty strings so they don't shadow data defaults
            # the user may have set via the wizard.
            cleaned = {
                k: v for k, v in user_input.items()
                if not (isinstance(v, str) and v == "")
            }
            return self.async_create_entry(title="", data=cleaned)

        # Pre-fill from existing options first, then data.
        merged = {**self.config_entry.data, **self.config_entry.options}
        schema_dict: dict = {}

        # Entity wiring fields (always strings)
        for key in _OPTIONS_ENTITY_FIELDS:
            schema_dict[vol.Optional(
                key, default=str(merged.get(key, "")),
            )] = str

        # dry_run flag (always shown)
        schema_dict[vol.Required(
            "dry_run", default=bool(merged.get("dry_run", True)),
        )] = bool

        # SPEC §10 rule knobs (HEO-11). Coerce to the declared type at
        # save time so the coordinator's `_cfg_float`/`_cfg_int` can
        # parse them. Pre-fill with the user's current value or the
        # SPEC default.
        for key, default, kind in _OPTIONS_RULE_KNOBS:
            current = merged.get(key, default)
            if kind is float:
                schema_dict[vol.Optional(
                    key, default=float(current),
                )] = vol.Coerce(float)
            elif kind is int:
                schema_dict[vol.Optional(
                    key, default=int(current),
                )] = vol.Coerce(int)
            else:  # str (e.g. daily_plan_time = "HH:MM")
                schema_dict[vol.Optional(
                    key, default=str(current),
                )] = str

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_dict),
        )
