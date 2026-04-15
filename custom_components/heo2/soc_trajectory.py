"""SOC trajectory forward simulation. No Home Assistant imports."""

from __future__ import annotations

from datetime import time

from .models import SlotConfig


def calculate_soc_trajectory(
    current_soc: float,
    solar_forecast_kwh: list[float],
    load_forecast_kwh: list[float],
    programme_slots: list[SlotConfig],
    battery_capacity_kwh: float,
    max_charge_kw: float,
    charge_efficiency: float,
    discharge_efficiency: float,
    min_soc: float,
    max_soc: float,
    current_hour: int,
) -> list[float]:
    """Forward-simulate battery SOC for the next 24 hours."""
    trajectory: list[float] = []
    soc = current_soc

    for step in range(24):
        trajectory.append(soc)

        hour_idx = (current_hour + step) % 24
        hour_time = time(hour_idx, 0)

        solar_kwh = solar_forecast_kwh[hour_idx]
        load_kwh = load_forecast_kwh[hour_idx]

        net_kwh = (solar_kwh * charge_efficiency) - (load_kwh / discharge_efficiency)

        for slot in programme_slots:
            if slot.contains_time(hour_time) and slot.grid_charge:
                if soc < slot.capacity_soc:
                    needed_kwh = (slot.capacity_soc - soc) / 100.0 * battery_capacity_kwh
                    available_kwh = max_charge_kw * charge_efficiency
                    net_kwh += min(needed_kwh, available_kwh)
                break

        soc += (net_kwh / battery_capacity_kwh) * 100.0
        soc = max(min_soc, min(max_soc, soc))

    return trajectory
