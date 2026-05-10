"""Solcast HACS attribute → 24-bucket hourly kWh list.

Ported from heo2/solar_forecast.py. The HACS solcast_solar integration
already converts kW to kWh correctly and exposes per-hour values as the
`detailedHourly` attribute (or `detailedForecast` for half-hourly).
This module consumes that attribute. No Home Assistant imports.
"""

from __future__ import annotations

from datetime import date, datetime


def solar_forecast_from_hacs(
    detailed_hourly: list[dict],
    target_date: date,
    key: str = "pv_estimate",
) -> list[float]:
    """Project a Solcast `detailedHourly` attribute into a 24-bucket
    hourly kWh list for `target_date` local time.

    Args:
        detailed_hourly: list from `sensor.solcast_pv_forecast_forecast_today`'s
            `detailedHourly` attribute. Each entry:
            `{"period_start": "<ISO8601>", "pv_estimate": kWh, ...}`.
        target_date: local date to filter to.
        key: which value field to read.
            - "pv_estimate" (default) = P50 median
            - "pv_estimate10" = conservative
            - "pv_estimate90" = optimistic

    Returns:
        24 floats, kWh per local hour. Missing entries default to 0.0.
        Malformed entries silently skipped — one bad row doesn't break
        the whole forecast.
    """
    hourly: list[float] = [0.0] * 24

    for entry in detailed_hourly:
        ts = entry.get("period_start", "")
        # HA serialises period_start to ISO when read via REST; in-process
        # via hass.states it's a datetime object. Accept both.
        if isinstance(ts, datetime):
            period_start = ts
        else:
            try:
                period_start = datetime.fromisoformat(str(ts))
            except (ValueError, TypeError):
                continue

        if period_start.date() != target_date:
            continue

        hour = period_start.hour
        if not (0 <= hour < 24):
            continue

        hourly[hour] = float(entry.get(key, 0.0))

    return hourly
