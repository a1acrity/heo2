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
    """Project SOC for each clock hour of today, indexed 0-23.

    `trajectory[h]` is the projected SOC AT clock hour `h` local
    time. Hours BEFORE `current_hour` are filled with `current_soc`
    (we don't have actuals; this is the best we can do without
    history). Hours from `current_hour` onward are simulated forward
    using the programme slots and forecast arrays (which are also
    local-hour indexed).

    The chart x-axis can therefore plot `trajectory[h]` at clock hour
    `h` directly, without any anchor offset. Pre-2026-05-03 the
    function returned `trajectory[i] = SOC i hours from now` which
    required the chart to know `current_hour` to place each point
    correctly; an off-by-current_hour bug surfaced for evening users.
    """
    trajectory: list[float] = [current_soc] * 24

    soc = current_soc
    for h in range(current_hour, 24):
        trajectory[h] = soc
        hour_time = time(h, 0)

        solar_kwh = solar_forecast_kwh[h]
        load_kwh = load_forecast_kwh[h]

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
