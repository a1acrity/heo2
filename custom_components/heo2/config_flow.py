# custom_components/heo2/config_flow.py
"""Config flow for HEO II — 6-step setup wizard."""

from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_USERNAME, CONF_PASSWORD

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
