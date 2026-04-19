# custom_components/heo2/load_history.py
"""Pure aggregation functions that turn HA recorder state history into the
(date, hourly_kwh_list) shape LoadProfileBuilder.add_day() expects.

The caller (the coordinator) fetches raw recorder history and extracts
(timestamp, watts) samples, then hands them to these functions. The math
is deliberately kept in this module - no Home Assistant imports - so it
can be unit-tested without any HA framework.

Covers HEO-5: previously LoadProfileBuilder.add_day() was never called,
so the load profile was always flat baseline. This is the missing link.
"""

from __future__ import annotations

from datetime import datetime, date, timedelta, tzinfo


SECONDS_PER_HOUR = 3600
WH_PER_KWH = 1000

# Intervals longer than this are treated as device-offline gaps, not flat
# extrapolation. HA recorder writes at least hourly even for unchanged
# values, so any gap longer than this almost certainly means the sensor
# was unavailable.
DEFAULT_MAX_INTERVAL_SECONDS = 2 * SECONDS_PER_HOUR


def aggregate_samples_to_hourly_kwh(
    samples: list[tuple[datetime, float]],
    target_date: date,
    tz: tzinfo,
    max_interval_seconds: float = DEFAULT_MAX_INTERVAL_SECONDS,
) -> list[float]:
    """Aggregate (timestamp, watts) samples into 24 hourly kWh values for
    ``target_date`` interpreted in ``tz``.

    Uses trapezoidal integration between consecutive samples. Power is
    assumed to ramp linearly between samples, which for trapezoidal
    integration gives the exact integral. Intervals that span hour
    boundaries are split at the boundary and each portion contributes
    to its own hour bucket.

    Negative power values (export periods) are clamped to zero because
    a load profile represents consumption, not net flow.

    Input samples are sorted defensively; caller need not pre-sort.
    Samples with the same timestamp are collapsed to the first.

    Args:
        samples: list of (datetime_aware, watts) pairs.
        target_date: the local date to aggregate for.
        tz: timezone used to interpret ``target_date`` boundaries.

    Returns:
        24 floats, index 0 is 00:00 local of ``target_date``, etc.
    """
    if len(samples) < 2:
        return [0.0] * 24

    # Defensive sort and negative clamp
    ordered = sorted(
        ((ts, max(0.0, float(w))) for ts, w in samples),
        key=lambda p: p[0],
    )

    # Local midnight boundaries for the target day
    day_start_local = datetime(
        target_date.year, target_date.month, target_date.day, tzinfo=tz,
    )
    day_end_local = day_start_local + timedelta(days=1)

    hourly = [0.0] * 24

    for (t1, w1), (t2, w2) in zip(ordered, ordered[1:]):
        if t2 <= t1:
            continue  # duplicate or reversed timestamp, skip
        interval_seconds = (t2 - t1).total_seconds()
        if interval_seconds <= 0:
            continue
        if interval_seconds > max_interval_seconds:
            # Gap too large — sensor was probably unavailable. Don't
            # invent energy by interpolating across the gap.
            continue

        # Skip intervals that don't touch the target day at all
        if t2 <= day_start_local or t1 >= day_end_local:
            continue

        # Walk hour boundaries that fall inside this interval
        _accumulate_interval(
            hourly, t1, w1, t2, w2,
            day_start_local, day_end_local, tz,
        )

    return hourly


def _accumulate_interval(
    hourly: list[float],
    t1: datetime, w1: float,
    t2: datetime, w2: float,
    day_start: datetime, day_end: datetime,
    tz: tzinfo,
) -> None:
    """Add the energy contribution of one (t1, w1) -> (t2, w2) interval
    into ``hourly`` buckets, splitting at hour boundaries."""
    interval_secs = (t2 - t1).total_seconds()
    slope = (w2 - w1) / interval_secs  # watts per second

    # Clamp the interval to the target day
    a = max(t1, day_start)
    b = min(t2, day_end)
    if b <= a:
        return

    # Walk from a to b, stopping at every hour boundary in between
    cursor = a
    while cursor < b:
        # Next hour boundary in local time
        local_cursor = cursor.astimezone(tz)
        next_hour_local = (local_cursor + timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0,
        )
        next_boundary = min(next_hour_local, b)

        # Integrate from cursor to next_boundary
        # Power at cursor: linear interp between t1 and t2
        dt_a = (cursor - t1).total_seconds()
        dt_b = (next_boundary - t1).total_seconds()
        p_a = w1 + slope * dt_a
        p_b = w1 + slope * dt_b
        # Clamp again after interpolation in case interval straddles zero
        p_a = max(0.0, p_a)
        p_b = max(0.0, p_b)

        seg_secs = (next_boundary - cursor).total_seconds()
        # Energy (Wh) = avg_W * hours = (p_a + p_b) / 2 * seg_secs / 3600
        energy_wh = (p_a + p_b) / 2.0 * seg_secs / SECONDS_PER_HOUR
        energy_kwh = energy_wh / WH_PER_KWH

        # Which local hour does this segment belong to?
        hour_index = local_cursor.hour
        if 0 <= hour_index < 24:
            hourly[hour_index] += energy_kwh

        cursor = next_boundary


def learn_days_from_samples(
    samples: list[tuple[datetime, float]],
    tz: tzinfo,
) -> dict[date, list[float]]:
    """Group samples by local date and aggregate each into 24 hourly kWh buckets.

    Returns a dict mapping each local date covered by the samples to its
    24-element hourly kWh list. Convenience wrapper for the coordinator
    so it can loop and call LoadProfileBuilder.add_day() per entry.
    """
    if not samples:
        return {}

    ordered = sorted(samples, key=lambda p: p[0])
    # Find all local dates touched by the samples
    first_local = ordered[0][0].astimezone(tz)
    last_local = ordered[-1][0].astimezone(tz)
    dates: list[date] = []
    d = first_local.date()
    while d <= last_local.date():
        dates.append(d)
        d = d + timedelta(days=1)

    result: dict[date, list[float]] = {}
    for d in dates:
        result[d] = aggregate_samples_to_hourly_kwh(ordered, d, tz)
    return result
