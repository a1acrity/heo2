# custom_components/heo2/number.py
"""Number platform for HEO II."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, DEFAULT_MIN_SOC
from .coordinator import HEO2Coordinator


async def _persist_option(
    hass: HomeAssistant, entry: ConfigEntry, key: str, value,
) -> None:
    """Write a single option into entry.options so it survives HA
    restart. Mirror the value into coordinator._config too so the
    next tick sees it without waiting for the update_listener reload.
    """
    new_options = {**(entry.options or {}), key: value}
    hass.config_entries.async_update_entry(entry, options=new_options)
    coordinator: HEO2Coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator._config[key] = value


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HEO2Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        MinSocNumber(coordinator, entry),
        SystemCostNumber(coordinator, entry),
        AdditionalCostsNumber(coordinator, entry),
    ])


class MinSocNumber(CoordinatorEntity, NumberEntity):
    _attr_native_min_value = 10
    _attr_native_max_value = 50
    _attr_native_step = 5
    _attr_mode = NumberMode.SLIDER
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_min_soc"
        self._attr_name = "HEO II Min SOC"

    @property
    def native_value(self) -> float:
        return self.coordinator._config.get("min_soc", DEFAULT_MIN_SOC)

    async def async_set_native_value(self, value: float) -> None:
        await _persist_option(
            self.hass, self.coordinator._entry, "min_soc", value,
        )
        await self.coordinator.async_request_refresh()


class SystemCostNumber(CoordinatorEntity, NumberEntity):
    _attr_native_min_value = 0
    _attr_native_max_value = 100000
    _attr_native_step = 100
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "£"

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_system_cost"
        self._attr_name = "HEO II System Cost"

    @property
    def native_value(self) -> float:
        return self.coordinator._config.get("system_cost", 16800.0)

    async def async_set_native_value(self, value: float) -> None:
        await _persist_option(
            self.hass, self.coordinator._entry, "system_cost", value,
        )
        self.async_write_ha_state()


class AdditionalCostsNumber(CoordinatorEntity, NumberEntity):
    _attr_native_min_value = 0
    _attr_native_max_value = 50000
    _attr_native_step = 50
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "£"

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_additional_costs"
        self._attr_name = "HEO II Additional Costs"

    @property
    def native_value(self) -> float:
        return self.coordinator._config.get("additional_costs", 0.0)

    async def async_set_native_value(self, value: float) -> None:
        await _persist_option(
            self.hass, self.coordinator._entry, "additional_costs", value,
        )
        self.async_write_ha_state()
