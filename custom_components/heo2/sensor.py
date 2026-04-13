# custom_components/heo2/sensor.py
"""Sensor platform for HEO II."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, DEFAULT_APPLIANCES
from .coordinator import HEO2Coordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HEO2Coordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = []
    for i in range(1, 7):
        entities.append(SlotSensor(coordinator, entry, i))
    entities.append(NextActionSensor(coordinator, entry))
    entities.append(LastRunSensor(coordinator, entry))
    entities.append(LoadProfileSensor(coordinator, entry))
    for name in DEFAULT_APPLIANCES:
        entities.append(ApplianceTimingSensor(coordinator, entry, name))
    async_add_entities(entities)


class SlotSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry, slot_num: int):
        super().__init__(coordinator)
        self._slot_num = slot_num
        self._attr_unique_id = f"{entry.entry_id}_slot_{slot_num}"
        self._attr_name = f"HEO II Slot {slot_num}"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.current_programme is None:
            return None
        slot = self.coordinator.current_programme.slots[self._slot_num - 1]
        return f"{slot.capacity_soc}%"

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.current_programme is None:
            return {}
        slot = self.coordinator.current_programme.slots[self._slot_num - 1]
        return {
            "start_time": slot.start_time.strftime("%H:%M"),
            "end_time": slot.end_time.strftime("%H:%M"),
            "capacity_soc": slot.capacity_soc,
            "grid_charge": slot.grid_charge,
        }


class NextActionSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_next_action"
        self._attr_name = "HEO II Next Action"

    @property
    def native_value(self) -> str | None:
        prog = self.coordinator.current_programme
        if prog is None:
            return "No programme"
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).time()
        try:
            idx = prog.find_slot_at(now)
        except ValueError:
            return "Unknown"
        slot = prog.slots[idx]
        action = "Charging" if slot.grid_charge else "Holding"
        if slot.capacity_soc <= 20:
            action = "Draining"
        return f"{action} to {slot.capacity_soc}% until {slot.end_time.strftime('%H:%M')}"


class LastRunSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_last_run"
        self._attr_name = "HEO II Last Run"

    @property
    def native_value(self) -> str | None:
        prog = self.coordinator.current_programme
        if prog is None:
            return None
        return f"{len(prog.reason_log)} rules applied"

    @property
    def extra_state_attributes(self) -> dict:
        prog = self.coordinator.current_programme
        if prog is None:
            return {}
        return {"reason_log": prog.reason_log}


class LoadProfileSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_load_profile"
        self._attr_name = "HEO II Load Profile"

    @property
    def native_value(self) -> str:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        return "weekend" if now.weekday() >= 5 else "weekday"


class ApplianceTimingSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry, appliance: str):
        super().__init__(coordinator)
        self._appliance = appliance
        self._attr_unique_id = f"{entry.entry_id}_best_{appliance}_window"
        self._attr_name = f"HEO II Best {appliance.title()} Window"

    @property
    def native_value(self) -> str | None:
        suggestion = self.coordinator.appliance_suggestions.get(self._appliance)
        if suggestion is None:
            return "No data"
        if suggestion.reason == "solar_surplus":
            return f"Solar surplus at {suggestion.start_hour:02d}:00"
        elif suggestion.reason == "cheap_rate":
            return f"Cheap rate at {suggestion.start_hour:02d}:00"
        return "No good window"

    @property
    def extra_state_attributes(self) -> dict:
        suggestion = self.coordinator.appliance_suggestions.get(self._appliance)
        if suggestion is None:
            return {}
        return {
            "window_start_hour": suggestion.start_hour,
            "duration_hours": suggestion.duration_hours,
            "reason": suggestion.reason,
            "solar_coverage_pct": round(suggestion.solar_coverage_pct, 1),
            "estimated_cost_pence": round(suggestion.estimated_cost_pence, 1),
            "appliance_draw_kw": suggestion.draw_kw,
        }
