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
    *,
    solar_forecast_kwh_tomorrow: list[float] | None = None,
    horizon_hours: int = 30,
) -> list[float]:
    """Project SOC for each clock hour, indexed 0..horizon_hours-1.

    Indices 0..23 correspond to today's clock hours; indices 24..29
    (default 30-hour horizon) project into tomorrow's first 6 hours
    so the dashboard chart can show the overnight recharge. Hours
    BEFORE `current_hour` are filled with `current_soc` (no history).

    `solar_forecast_kwh_tomorrow` is optional: when present, indices
    24+ use tomorrow's solar; when absent, they wrap today's array
    (less accurate but better than nothing). Load forecast wraps
    today's array since HEO-5's learned profile is shape-equivalent
    across days.

    The chart x-axis can plot `trajectory[h]` at clock hour `h`
    (clamping h>=24 to tomorrow's hours): a 30h span shows the day's
    drain plus tomorrow's morning charge.
    """
    trajectory: list[float] = [current_soc] * horizon_hours

    soc = current_soc
    for h in range(current_hour, horizon_hours):
        trajectory[h] = soc

        # Index into local-hour-indexed forecasts. h>=24 wraps to
        # tomorrow's forecast for solar (preferred) or today's (fallback).
        if h < 24:
            solar_kwh = solar_forecast_kwh[h]
        elif solar_forecast_kwh_tomorrow:
            solar_kwh = solar_forecast_kwh_tomorrow[h - 24]
        else:
            solar_kwh = solar_forecast_kwh[h - 24]
        load_kwh = load_forecast_kwh[h % 24]

        # Slot lookup uses time-of-day (0..23) wrapped via mod 24.
        hour_time = time(h % 24, 0)

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
