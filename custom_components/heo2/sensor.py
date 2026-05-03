# custom_components/heo2/sensor.py
"""Sensor platform for HEO II."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorEntity, SensorStateClass, SensorDeviceClass
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

    # Dashboard sensors (Group 1: Forecast & Plan)
    entities.append(SolarForecastTodaySensor(coordinator, entry))
    entities.append(SolarForecastHourlySensor(coordinator, entry))
    entities.append(LoadForecastHourlySensor(coordinator, entry))
    entities.append(ImportRatesSensor(coordinator, entry))
    entities.append(ExportRatesSensor(coordinator, entry))
    entities.append(CurrentImportRateSensor(coordinator, entry))
    entities.append(CurrentExportRateSensor(coordinator, entry))
    entities.append(SOCTrajectorySensor(coordinator, entry))
    entities.append(ProgrammeSlotsSensor(coordinator, entry))
    entities.append(ProgrammeReasonSensor(coordinator, entry))
    entities.append(ActiveRulesSensor(coordinator, entry))
    entities.append(ProjectionTodaySensor(coordinator, entry))
    entities.append(GranularitySnapSensor(coordinator, entry))
    entities.append(CyclesTodaySensor(coordinator, entry))

    # Dashboard sensors (Group 2: Cost Accumulator)
    entities.append(DailyImportCostSensor(coordinator, entry))
    entities.append(DailyExportRevenueSensor(coordinator, entry))
    entities.append(DailySolarValueSensor(coordinator, entry))
    entities.append(WeeklyNetCostSensor(coordinator, entry))
    entities.append(WeeklySavingsVsFlatSensor(coordinator, entry))

    # Dashboard sensors (Group 3: Octopus Billing — only if configured)
    if entry.data.get("octopus_api_key"):
        entities.append(OctopusMonthlyBillSensor(coordinator, entry))
        entities.append(OctopusLastMonthBillSensor(coordinator, entry))

    # Dashboard sensors (Group 4: ROI / Payback)
    entities.append(TotalSavingsSensor(coordinator, entry))
    entities.append(PaybackProgressSensor(coordinator, entry))
    entities.append(EstimatedPaybackDateSensor(coordinator, entry))

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


# ---------------------------------------------------------------------------
# Group 1 — Forecast & Plan sensors (coordinator, every 15 min)
# ---------------------------------------------------------------------------


class SolarForecastTodaySensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.ENERGY

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_solar_forecast_today"
        self._attr_name = "HEO II Solar Forecast Today"

    @property
    def native_value(self) -> float | None:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return None
        return round(sum(inputs.solar_forecast_kwh), 2)

    @property
    def extra_state_attributes(self) -> dict:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return {}
        return {"hourly": inputs.solar_forecast_kwh}


class SolarForecastHourlySensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.ENERGY

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_solar_forecast_hourly"
        self._attr_name = "HEO II Solar Forecast Hourly"

    @property
    def native_value(self) -> float | None:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return None
        # Forecast array is local-hour indexed; project UTC->local.
        hour = inputs.now_local().hour
        return inputs.solar_forecast_kwh[hour]

    @property
    def extra_state_attributes(self) -> dict:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return {}
        return {"forecast": inputs.solar_forecast_kwh}


class LoadForecastHourlySensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "W"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.POWER

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_load_forecast_hourly"
        self._attr_name = "HEO II Load Forecast Hourly"

    @property
    def native_value(self) -> float | None:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return None
        # Forecast array is local-hour indexed; project UTC->local.
        hour = inputs.now_local().hour
        return round(inputs.load_forecast_kwh[hour] * 1000, 0)

    @property
    def extra_state_attributes(self) -> dict:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return {}
        return {"forecast": [round(kwh * 1000, 0) for kwh in inputs.load_forecast_kwh]}


class ImportRatesSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "p/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_import_rates"
        self._attr_name = "HEO II Import Rates"

    @property
    def native_value(self) -> float | None:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return None
        return inputs.rate_at(inputs.now)

    @property
    def extra_state_attributes(self) -> dict:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return {}
        return {
            "rates": [
                {
                    "start": rs.start.isoformat(),
                    "end": rs.end.isoformat(),
                    "rate": rs.rate_pence,
                }
                for rs in inputs.import_rates
            ]
        }


class ExportRatesSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "p/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_export_rates"
        self._attr_name = "HEO II Export Rates"

    @property
    def native_value(self) -> float | None:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return None
        return inputs.export_rate_at(inputs.now)

    @property
    def extra_state_attributes(self) -> dict:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return {}
        return {
            "rates": [
                {
                    "start": rs.start.isoformat(),
                    "end": rs.end.isoformat(),
                    "rate": rs.rate_pence,
                }
                for rs in inputs.export_rates
            ]
        }


class CurrentImportRateSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "p/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_current_import_rate"
        self._attr_name = "HEO II Current Import Rate"

    @property
    def native_value(self) -> float | None:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return None
        return inputs.rate_at(inputs.now)


class CurrentExportRateSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "p/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_current_export_rate"
        self._attr_name = "HEO II Current Export Rate"

    @property
    def native_value(self) -> float | None:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return None
        return inputs.export_rate_at(inputs.now)


class SOCTrajectorySensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_soc_trajectory"
        self._attr_name = "HEO II SOC Trajectory"

    @property
    def native_value(self) -> float | None:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return None
        return round(inputs.current_soc, 1)

    @property
    def extra_state_attributes(self) -> dict:
        return {"trajectory": self.coordinator.soc_trajectory}


class ProgrammeSlotsSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_programme_slots"
        self._attr_name = "HEO II Programme Slots"

    @property
    def native_value(self) -> str | None:
        prog = self.coordinator.current_programme
        if prog is None:
            return None
        return f"{len(prog.slots)} slots active"

    @property
    def extra_state_attributes(self) -> dict:
        prog = self.coordinator.current_programme
        if prog is None:
            return {}
        return {
            "slots": [
                {
                    "start": slot.start_time.strftime("%H:%M"),
                    "end": slot.end_time.strftime("%H:%M"),
                    "soc": slot.capacity_soc,
                    "grid_charge": slot.grid_charge,
                }
                for slot in prog.slots
            ]
        }


class ProgrammeReasonSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_programme_reason"
        self._attr_name = "HEO II Programme Reason"

    @property
    def native_value(self) -> str | None:
        prog = self.coordinator.current_programme
        if prog is None or not prog.reason_log:
            return None
        return prog.reason_log[-1]

    @property
    def extra_state_attributes(self) -> dict:
        prog = self.coordinator.current_programme
        if prog is None:
            return {}
        return {"reasons": prog.reason_log}


class ActiveRulesSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_active_rules"
        self._attr_name = "HEO II Active Rules"

    @property
    def native_value(self) -> int:
        return len(self.coordinator.active_rule_names)

    @property
    def extra_state_attributes(self) -> dict:
        return {"rules": self.coordinator.active_rule_names}


class ProjectionTodaySensor(CoordinatorEntity, SensorEntity):
    """SPEC §6 projection report: 1-line summary of expected daily return
    plus structured kWh / pence breakdown for the dashboard."""

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_projection_today"
        self._attr_name = "HEO II Projection Today"

    @property
    def native_value(self) -> str | None:
        p = self.coordinator.projection_today
        if p is None:
            return None
        return p.summary()

    @property
    def extra_state_attributes(self) -> dict:
        p = self.coordinator.projection_today
        if p is None:
            return {"warnings": self.coordinator.validation_warnings}
        return {
            "expected_return_pence": round(p.expected_return_pence, 2),
            "sells_kwh": round(p.sells_kwh, 2),
            "sells_avg_pence": (
                round(p.sells_avg_pence, 2)
                if p.sells_avg_pence is not None else None
            ),
            "imports_kwh": round(p.imports_kwh, 2),
            "imports_avg_pence": (
                round(p.imports_avg_pence, 2)
                if p.imports_avg_pence is not None else None
            ),
            "peak_import_kwh": round(p.peak_import_kwh, 2),
            "warnings": self.coordinator.validation_warnings,
        }


class CyclesTodaySensor(CoordinatorEntity, SensorEntity):
    """SPEC H7: cycles consumed since the last local-midnight reset.
    Soft target is <=2 cycles/day; a follow-up binary_sensor can fire
    when 3 consecutive days breach. Native value is float (e.g. 1.42)."""

    _attr_native_unit_of_measurement = "cycles"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_cycles_today"
        self._attr_name = "HEO II Cycles Today"

    @property
    def native_value(self) -> float:
        return round(self.coordinator.cycle_tracker.cycles_today, 3)


class GranularitySnapSensor(CoordinatorEntity, SensorEntity):
    """Surfaces the 5-min Sunsynk timer-granularity snaps applied by
    SafetyRule on the latest tick. Native value is the count; the
    `snaps` attribute is the list of `slot N <start|end> HH:MM->HH:MM`
    strings. A non-zero value isn't a fault - it's a transparent record
    that the rule engine produced a boundary the hardware couldn't
    store exactly. Useful when chasing 'why did the inverter store a
    different time than I sent' questions.
    """

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_granularity_snaps"
        self._attr_name = "HEO II Granularity Snaps"

    @property
    def native_value(self) -> int:
        return len(self.coordinator.granularity_snaps)

    @property
    def extra_state_attributes(self) -> dict:
        return {"snaps": self.coordinator.granularity_snaps}


# ---------------------------------------------------------------------------
# Group 2 — Cost Accumulator sensors (continuous, daily/weekly reset)
# ---------------------------------------------------------------------------


class DailyImportCostSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "£"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_daily_import_cost"
        self._attr_name = "HEO II Daily Import Cost"

    @property
    def native_value(self) -> float:
        return round(self.coordinator.cost_accumulator.daily_import_cost, 2)

    @property
    def last_reset(self) -> datetime | None:
        return self.coordinator.cost_accumulator.last_daily_reset


class DailyExportRevenueSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "£"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_daily_export_revenue"
        self._attr_name = "HEO II Daily Export Revenue"

    @property
    def native_value(self) -> float:
        return round(self.coordinator.cost_accumulator.daily_export_revenue, 2)

    @property
    def last_reset(self) -> datetime | None:
        return self.coordinator.cost_accumulator.last_daily_reset


class DailySolarValueSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "£"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_daily_solar_value"
        self._attr_name = "HEO II Daily Solar Value"

    @property
    def native_value(self) -> float:
        return round(self.coordinator.cost_accumulator.daily_solar_value, 2)

    @property
    def last_reset(self) -> datetime | None:
        return self.coordinator.cost_accumulator.last_daily_reset


class WeeklyNetCostSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "£"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_weekly_net_cost"
        self._attr_name = "HEO II Weekly Net Cost"

    @property
    def native_value(self) -> float:
        return round(self.coordinator.cost_accumulator.weekly_net_cost, 2)

    @property
    def last_reset(self) -> datetime | None:
        return self.coordinator.cost_accumulator.last_weekly_reset


class WeeklySavingsVsFlatSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "£"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_weekly_savings_vs_flat"
        self._attr_name = "HEO II Weekly Savings vs Flat"

    @property
    def native_value(self) -> float:
        return round(self.coordinator.cost_accumulator.weekly_savings_vs_flat, 2)

    @property
    def last_reset(self) -> datetime | None:
        return self.coordinator.cost_accumulator.last_weekly_reset


# ---------------------------------------------------------------------------
# Group 3 — Octopus Billing sensors (daily at 06:00, optional)
# ---------------------------------------------------------------------------


class OctopusMonthlyBillSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "£"
    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_octopus_monthly_bill"
        self._attr_name = "HEO II Octopus Monthly Bill"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.octopus is None:
            return None
        return round(self.coordinator.octopus.monthly_bill, 2)


class OctopusLastMonthBillSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "£"
    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_octopus_last_month_bill"
        self._attr_name = "HEO II Octopus Last Month Bill"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.octopus is None:
            return None
        return round(self.coordinator.octopus.last_month_bill, 2)


# ---------------------------------------------------------------------------
# Group 4 — ROI / Payback sensors
# ---------------------------------------------------------------------------


class TotalSavingsSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "£"
    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_total_savings"
        self._attr_name = "HEO II Total Savings"

    @property
    def native_value(self) -> float:
        return round(self.coordinator.total_savings, 2)


class PaybackProgressSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_payback_progress"
        self._attr_name = "HEO II Payback Progress"

    @property
    def native_value(self) -> float:
        return round(self.coordinator.payback_progress, 1)


class EstimatedPaybackDateSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_estimated_payback_date"
        self._attr_name = "HEO II Estimated Payback Date"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.estimated_payback_date
