# custom_components/heo2/binary_sensor.py
"""Binary sensor platform for HEO II."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
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
        HealthySensor(coordinator, entry),
        WritesBlockedSensor(coordinator, entry),
        EPSActiveSensor(coordinator, entry),
        CycleBudgetExceededSensor(coordinator, entry),
    ])


class HealthySensor(CoordinatorEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_healthy"
        self._attr_name = "HEO II Healthy"

    @property
    def is_on(self) -> bool:
        return self.coordinator.healthy


class WritesBlockedSensor(CoordinatorEntity, BinarySensorEntity):
    """Dashboard alert indicator: ON when HEO II cannot write the
    programme to the inverter right now.

    Uses device_class=problem so the HA UI colours it red when on,
    and green when off. Extra state attribute 'reason' explains why
    when blocked. Useful as a conditional card trigger on dashboards.
    """
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_writes_blocked"
        self._attr_name = "HEO II Writes Blocked"

    @property
    def is_on(self) -> bool:
        return self.coordinator.writes_blocked

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "reason": self.coordinator.writes_blocked_reason or "writes enabled",
        }


class EPSActiveSensor(CoordinatorEntity, BinarySensorEntity):
    """SPEC §9 / H3 dashboard banner: ON when the grid is down and the
    inverter is supplying the house from EPS / battery. While active:
    SOC floor relaxes to 0%, EV/washer/dryer/dishwasher get
    switch.turn_off, MQTT writes are suppressed.
    """
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_eps_active"
        self._attr_name = "HEO II EPS Active"

    @property
    def is_on(self) -> bool:
        return self.coordinator.eps_active


class CycleBudgetExceededSensor(CoordinatorEntity, BinarySensorEntity):
    """SPEC §1 / H7: ON when the battery has run > daily_budget cycles
    on each of the last 3 finished days. Today's in-progress count
    doesn't trip the alert until midnight seals the day's total."""
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_cycle_budget_exceeded"
        self._attr_name = "HEO II Cycle Budget Exceeded"

    @property
    def is_on(self) -> bool:
        return self.coordinator.cycle_tracker.budget_exceeded

    @property
    def extra_state_attributes(self) -> dict:
        t = self.coordinator.cycle_tracker
        return {
            "history": [round(c, 3) for c in t.history],
            "daily_budget": t.daily_budget,
        }
