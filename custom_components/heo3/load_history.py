"""Pure aggregation functions that turn HA recorder state history into the
(date, hourly_kwh_list) shape LoadProfileBuilder.add_day() expects.

Ported as-is from heo2/load_history.py per the §21 resolution. The
math has no Home Assistant imports so it can be unit-tested in
isolation.
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
    source_type: str = "power_watts",
) -> dict[date, list[float]]:
    """Group samples by local date and aggregate each into 24 hourly kWh buckets.

    Returns a dict mapping each local date covered by the samples to its
    24-element hourly kWh list. Convenience wrapper for the coordinator
    so it can loop and call LoadProfileBuilder.add_day() per entry.

    Args:
        samples: list of (timestamp, value) pairs
        tz: local timezone for day bucketing
        source_type: either ``"power_watts"`` (the historical behaviour —
            samples are instantaneous power readings in watts, integrated
            trapezoidally) or ``"cumulative_kwh"`` (samples are a
            monotonically-increasing kWh meter, aggregated by delta).
    """
    if not samples:
        return {}

    if source_type == "cumulative_kwh":
        aggregator = aggregate_cumulative_kwh_to_hourly
    elif source_type == "power_watts":
        aggregator = aggregate_samples_to_hourly_kwh
    else:
        raise ValueError(f"Unknown source_type: {source_type!r}")

    ordered = sorted(samples, key=lambda p: p[0])
    first_local = ordered[0][0].astimezone(tz)
    last_local = ordered[-1][0].astimezone(tz)
    dates: list[date] = []
    d = first_local.date()
    while d <= last_local.date():
        dates.append(d)
        d = d + timedelta(days=1)

    result: dict[date, list[float]] = {}
    for d in dates:
        result[d] = aggregator(ordered, d, tz)
    return result


def states_to_power_samples(states) -> list[tuple]:
    """Convert HA recorder State-like objects into (datetime, watts) tuples.

    Defensive against ``unknown`` / ``unavailable`` / ``None`` states and
    malformed numeric values. Non-numeric states, states missing
    attributes, and states with a None timestamp are all silently
    skipped so one bad record cannot break the whole history fetch.

    Returns the list ordered by ``last_changed`` ascending. Caller is
    responsible for treating the returned watts as "load power" including
    its sign (negative = export); the aggregator clamps to zero.
    """
    out: list[tuple] = []
    for s in states:
        try:
            raw = s.state
            ts = s.last_changed
        except AttributeError:
            continue
        if raw in (None, "unknown", "unavailable"):
            continue
        try:
            watts = float(raw)
        except (ValueError, TypeError):
            continue
        if ts is None:
            continue
        out.append((ts, watts))
    out.sort(key=lambda p: p[0])
    return out


def aggregate_cumulative_kwh_to_hourly(
    samples: list[tuple[datetime, float]],
    target_date: date,
    tz: tzinfo,
    max_interval_seconds: float = DEFAULT_MAX_INTERVAL_SECONDS,
) -> list[float]:
    """Aggregate (timestamp, cumulative_kwh) samples into 24 hourly kWh values.

    For entities that expose total household consumption as a monotonically
    increasing kWh counter (``state_class: total_increasing``). The aggregator
    computes kWh deltas between consecutive samples and prorates them across
    the hour buckets they span.

    This is the correct entity shape for learning a true household load
    profile. It captures all consumption regardless of source (grid import,
    PV self-consumption, or battery discharge), which is exactly what the
    LoadProfileBuilder's median needs.

    Args:
        samples: list of (datetime_aware, cumulative_kwh) pairs.
        target_date: the local date to aggregate for.
        tz: timezone used to interpret ``target_date`` boundaries.
        max_interval_seconds: intervals longer than this are dropped,
            assuming the sensor was offline rather than genuinely flat.

    Returns:
        24 floats, index 0 is 00:00 local of ``target_date``, etc.

    Notes:
        - Meter resets (counter going down) are silently skipped for the
          interval containing the reset. Happens on inverter restart.
        - The kWh delta is spread across the interval linearly (constant
          power assumption within the interval). For sub-hour sampling this
          is equivalent to trapezoidal integration.
    """
    if len(samples) < 2:
        return [0.0] * 24

    ordered = sorted(samples, key=lambda p: p[0])

    day_start_local = datetime(
        target_date.year, target_date.month, target_date.day, tzinfo=tz,
    )
    day_end_local = day_start_local + timedelta(days=1)

    hourly = [0.0] * 24

    for (t1, v1), (t2, v2) in zip(ordered, ordered[1:]):
        if t2 <= t1:
            continue
        interval_seconds = (t2 - t1).total_seconds()
        if interval_seconds <= 0 or interval_seconds > max_interval_seconds:
            continue

        delta_kwh = v2 - v1
        if delta_kwh < 0:
            # Meter reset (e.g. inverter restart). Don't invent energy.
            continue
        if delta_kwh == 0:
            continue

        # Clip interval to the target day
        a = max(t1, day_start_local)
        b = min(t2, day_end_local)
        if b <= a:
            continue

        # kWh per second within this interval (constant-power assumption)
        kwh_per_second = delta_kwh / interval_seconds

        # Walk hour boundaries, prorating the energy
        cursor = a
        while cursor < b:
            local_cursor = cursor.astimezone(tz)
            next_hour_local = (local_cursor + timedelta(hours=1)).replace(
                minute=0, second=0, microsecond=0,
            )
            next_boundary = min(next_hour_local, b)
            seg_secs = (next_boundary - cursor).total_seconds()
            seg_kwh = kwh_per_second * seg_secs

            hour_index = local_cursor.hour
            if 0 <= hour_index < 24:
                hourly[hour_index] += seg_kwh

            cursor = next_boundary

    return hourly
