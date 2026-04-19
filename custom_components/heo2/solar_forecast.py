# custom_components/heo2/solar_forecast.py
"""Adapter: convert HACS solcast_solar detailedHourly into HEO II's
24-bucket hourly kWh list.

HEO II previously made its own HTTP calls to Solcast via solcast_client.py,
which mis-aggregated kW as kWh and collapsed multi-day forecasts into one
day's 24 buckets, over-reporting by 4x (HEO-4). The HACS solcast_solar
integration already converts to kWh correctly and exposes per-hour values
as an entity attribute. This module consumes that attribute, no HTTP.

Pure function, no Home Assistant imports. Testable in isolation.
"""

from __future__ import annotations

from datetime import date, datetime


def solar_forecast_from_hacs(
    detailed_hourly: list[dict],
    target_date: date,
    key: str = "pv_estimate",
) -> list[float]:
    """Project an HACS solcast_solar ``detailedHourly`` attribute onto a
    24-bucket hourly kWh list for a specific local date.

    Args:
        detailed_hourly: The list attribute from
            ``sensor.solcast_pv_forecast_forecast_today`` (or _tomorrow).
            Each entry is ``{"period_start": "<ISO8601 local>", "pv_estimate": kWh, ...}``.
        target_date: Local date to filter entries to. Entries whose
            ``period_start`` falls on this date contribute to their
            matching hour bucket; others are ignored.
        key: Which value field to read. Defaults to ``pv_estimate`` (P50
            median). Use ``pv_estimate10`` for conservative planning or
            ``pv_estimate90`` for optimistic.

    Returns:
        List of 24 floats, one kWh value per hour of ``target_date`` local time.
        Missing hours default to 0.0. Missing ``key`` in an entry defaults
        that hour to 0.0. Entries with unparseable ``period_start`` are
        silently skipped so one bad entry cannot break the whole aggregation.
    """
    hourly: list[float] = [0.0] * 24

    for entry in detailed_hourly:
        ts = entry.get("period_start", "")
        # HACS solcast_solar publishes period_start as a datetime.datetime
        # object when read via state.attributes (it's stored as such in HA's
        # attribute registry). When we query the sensor over REST API HA
        # serialises it to ISO string. Accept both.
        if isinstance(ts, datetime):
            period_start = ts
        else:
            try:
                period_start = datetime.fromisoformat(str(ts))
            except (ValueError, TypeError):
                # Malformed timestamp: skip, don't fail the whole forecast.
                continue

        if period_start.date() != target_date:
            continue

        hour = period_start.hour
        if not (0 <= hour < 24):
            continue

        hourly[hour] = float(entry.get(key, 0.0))

    return hourly
