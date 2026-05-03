# custom_components/heo2/switch.py
"""Switch platform for HEO II."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HEO2Coordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HEO2Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        EnabledSwitch(coordinator, entry),
        DeferEvWhenExportHighSwitch(coordinator, entry),
    ])


class EnabledSwitch(CoordinatorEntity, SwitchEntity):
    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_enabled"
        self._attr_name = "HEO II Enabled"

    @property
    def is_on(self) -> bool:
        return self.coordinator.enabled

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.enabled = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.enabled = False
        self.async_write_ha_state()


class DeferEvWhenExportHighSwitch(CoordinatorEntity, SwitchEntity):
    """SPEC §12 user-facing dashboard toggle. ON = "car not needed
    tomorrow, feel free to halt EV charge during top export windows".

    Persists via entry.options so the toggle survives HA restart -
    the user typically flips it the night before they don't need the
    car, and an unintentional revert at midnight would defeat the
    purpose. Same `_persist_option` pattern as the NumberEntity sliders.
    """

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_defer_ev_when_export_high"
        self._attr_name = "HEO II Defer EV When Export High"

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator._config.get(
            "defer_ev_when_export_high", False,
        ))

    async def _set(self, value: bool) -> None:
        new_options = {
            **(self._entry.options or {}),
            "defer_ev_when_export_high": value,
        }
        self.hass.config_entries.async_update_entry(
            self._entry, options=new_options,
        )
        self.coordinator._config["defer_ev_when_export_high"] = value
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set(False)
