# custom_components/heo2/rank_pricing.py
"""Rank-based pricing helpers for HEO II rules.

Pure functions, no Home Assistant imports. Unit-testable in isolation.

Implements SPEC §5a: rank within today's published rates rather than
fixed pence thresholds. Adapts automatically to seasonal Agile shifts
and tariff changes without code edits.

The fixed 6p / 7.86p thresholds in the legacy rules made decisions
based on absolute price, which broke whenever winter Agile distributions
shifted (median ~5p; 6p triggers half the day, draining the reserve)
versus summer (where 6p is the bottom of the distribution). Rank gives
us "sell in the top 30% of today's prices" which adapts automatically.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Iterable

from .models import RateSlot


def filter_today(
    rates: Iterable[RateSlot],
    now: datetime,
    tz=None,
) -> list[RateSlot]:
    """Return slots whose START falls on the same LOCAL date as `now`.

    `tz` is the local timezone (ZoneInfo). If None, the now timestamp's
    own tzinfo is used; if that's also None, naive comparison.
    """
    if tz is not None:
        today_local_date = now.astimezone(tz).date()
    elif now.tzinfo is not None:
        today_local_date = now.date()
    else:
        today_local_date = now.date()

    out: list[RateSlot] = []
    for r in rates:
        start = r.start
        if tz is not None:
            start_local = start.astimezone(tz)
        elif start.tzinfo is not None and now.tzinfo is not None:
            start_local = start.astimezone(now.tzinfo)
        else:
            start_local = start
        if start_local.date() == today_local_date:
            out.append(r)
    return out


def top_n_pct(rates: list[RateSlot], n_pct: int | float) -> list[RateSlot]:
    """Return the top N% of `rates` by rate_pence.

    Sorted descending. Count is rounded UP so n_pct=15 of 48 slots
    returns 8 slots (`ceil(48 * 0.15)`). Empty input or n_pct<=0
    returns empty.
    """
    if not rates or n_pct <= 0:
        return []
    if n_pct >= 100:
        return sorted(rates, key=lambda r: r.rate_pence, reverse=True)
    count = max(1, math.ceil(len(rates) * n_pct / 100))
    return sorted(rates, key=lambda r: r.rate_pence, reverse=True)[:count]


def bottom_n_pct(rates: list[RateSlot], n_pct: int | float) -> list[RateSlot]:
    """Return the bottom N% of `rates` by rate_pence (lowest first).

    Same rounding convention as `top_n_pct`.
    """
    if not rates or n_pct <= 0:
        return []
    if n_pct >= 100:
        return sorted(rates, key=lambda r: r.rate_pence)
    count = max(1, math.ceil(len(rates) * n_pct / 100))
    return sorted(rates, key=lambda r: r.rate_pence)[:count]


def select_export_top_pct(
    current_soc: float,
    tomorrow_solar_kwh: float,
    daily_load_kwh: float,
    *,
    low_soc_threshold: float = 50.0,
    high_soc_threshold: float = 80.0,
    high_solar_kwh: float | None = None,
    low_solar_kwh: float | None = None,
    n_low: int = 15,
    n_med: int = 30,
    n_high: int = 50,
) -> tuple[int, str]:
    """Choose `N` for top-N% export windows per SPEC §5a.

    - Low SOC OR low tomorrow forecast -> `n_low` (only the very best)
    - High SOC AND high tomorrow forecast -> `n_high` (sell aggressively)
    - else -> `n_med`

    Defaults if not supplied: high_solar = daily_load_kwh,
    low_solar = daily_load_kwh * 0.5. The intuition: if tomorrow's PV
    will cover today's load, we can sell more of today's stored energy.

    Returns (n_pct, reason_string) so the rule can log which branch fired.
    """
    high_solar = high_solar_kwh if high_solar_kwh is not None else daily_load_kwh
    low_solar = low_solar_kwh if low_solar_kwh is not None else daily_load_kwh * 0.5

    soc_low = current_soc < low_soc_threshold
    soc_high = current_soc >= high_soc_threshold
    solar_low = tomorrow_solar_kwh < low_solar
    solar_high = tomorrow_solar_kwh >= high_solar

    if soc_low or solar_low:
        return n_low, (
            f"top {n_low}% (soc={current_soc:.0f}% low={soc_low}, "
            f"tomorrow_solar={tomorrow_solar_kwh:.1f} kWh low={solar_low})"
        )
    if soc_high and solar_high:
        return n_high, (
            f"top {n_high}% (soc={current_soc:.0f}% high, "
            f"tomorrow_solar={tomorrow_solar_kwh:.1f} kWh high)"
        )
    return n_med, (
        f"top {n_med}% (soc={current_soc:.0f}%, "
        f"tomorrow_solar={tomorrow_solar_kwh:.1f} kWh)"
    )


def is_worth_selling(
    export_rate_pence: float,
    replacement_cost_pence: float,
    round_trip_efficiency: float = 0.9025,
) -> bool:
    """SPEC §5a: a window is worth selling in if
    `export_rate * round_trip_efficiency > replacement_cost`.

    Replacement cost is "the cheapest rate at which we can re-charge",
    typically the next IGO off-peak slot (~4.95p) or for variable
    tariffs the next bottom-25% import slot.
    """
    return export_rate_pence * round_trip_efficiency > replacement_cost_pence


def select_worth_selling_windows(
    export_rates_today: list[RateSlot],
    n_pct: int | float,
    replacement_cost_pence: float,
    round_trip_efficiency: float = 0.9025,
) -> list[RateSlot]:
    """Top-N% AND worth-selling - the actual windows to drain into.

    Sorted by rate_pence DESCENDING (highest revenue first).
    """
    top = top_n_pct(export_rates_today, n_pct)
    return [
        r for r in top
        if is_worth_selling(
            r.rate_pence, replacement_cost_pence, round_trip_efficiency,
        )
    ]


def hours_covered_by(
    slots: list[RateSlot],
    tz,
) -> set[int]:
    """Return the set of LOCAL hour indices (0-23) covered by any slot.

    A 30-min slot covers exactly one hour (its starting hour). Two slots
    in the same hour collapse to one entry. `tz` should be the local
    timezone (ZoneInfo). Slots without tzinfo are returned by their
    naive hour.
    """
    out: set[int] = set()
    for s in slots:
        if tz is not None and s.start.tzinfo is not None:
            local = s.start.astimezone(tz)
        else:
            local = s.start
        out.add(local.hour)
    return out


def estimate_profitable_export_kwh(
    worth_selling_windows: list[RateSlot],
    max_discharge_kw: float = 5.0,
) -> float:
    """Estimate kWh we can profitably export today.

    Each worth-selling 30-min slot contributes up to
    `max_discharge_kw * 0.5` kWh. Assumes the battery has enough capacity
    and SOC headroom; the caller is responsible for clamping. Used as
    an upper bound for the cheap-rate-charge target calculation.
    """
    return len(worth_selling_windows) * max_discharge_kw * 0.5


def select_cheap_charge_windows(
    import_rates_today: list[RateSlot],
    n_pct: int | float = 25,
) -> list[RateSlot]:
    """SPEC §5a charging-from-grid logic.

    Returns the bottom-N% of today's import rates - the windows where
    grid_charge=True is justified. For IGO this picks out the off-peak
    slots (which are the cheapest by construction); for variable tariffs
    it generalises naturally.
    """
    return bottom_n_pct(import_rates_today, n_pct)


# Maximum p/kWh spread within a "cheap band". A cohort member whose
# rate is more than this above the cohort minimum is treated as
# padding from `bottom_n_pct`'s ceil() rounding rather than a genuine
# cheap slot. Calibrated for bimodal tariffs (IGO 7p/28p, Octopus
# Cosy 7.5p/22p): the gap between cheap and standard is much larger
# than 5p, so the filter cleanly drops the padding. For continuous
# Agile (intra-band variation typically 1-2p) it's permissive.
#
# Captured 2026-05-06: ceil(54 * 0.25) = 14 selected 13 cheap slots
# (6.9p) PLUS one day-rate slot (28.58p) as padding to reach 14. Sort
# stability put the earliest day-rate slot (20:00 UTC tonight) at the
# front of the cohort. `next_cheap_window_end_local` then walked from
# 20:00, found no contiguous neighbour in cohort, returned 20:30 UTC.
# CheapRateChargeRule walked the bridge from hour 21 (BST), saw zero
# PV, hit "deep winter" branch, set overnight target to 47% instead
# of the correct ~25-30% bridge-to-PV-takeover at hour 8.
_CHEAP_BAND_TOLERANCE_P = 5.0


def _cheap_band_filter(cohort: list[RateSlot]) -> list[RateSlot]:
    """Drop cohort members whose rate sits a tier above the cheapest.

    Rationale: `bottom_n_pct` uses `ceil()` to size the cohort, which
    can pull in one cross-tier "padding" slot when the count rounds up
    across a rate-band boundary. The padding slot is sort-stable so it
    ends up at the EARLIEST position among same-rate ties - which then
    misleads `next_cheap_window_*_local` into pointing at the wrong
    time-of-day. Filtering by `<= min + tolerance` removes that padding
    cleanly without affecting continuous-Agile cohorts.
    """
    if not cohort:
        return []
    min_rate = min(r.rate_pence for r in cohort)
    return [r for r in cohort if r.rate_pence <= min_rate + _CHEAP_BAND_TOLERANCE_P]


def next_cheap_window_start_local(
    import_rates: list[RateSlot],
    now_local: datetime,
    tz=None,
    n_pct: int | float = 25,
) -> datetime | None:
    """Return the LOCAL datetime at which the next cheap import window
    starts, or None if no upcoming rates are loaded.

    "Cheap" = bottom-N% of upcoming import rates filtered to the
    cheapest band (within `_CHEAP_BAND_TOLERANCE_P` of the cohort
    minimum). The earliest start among that filtered cohort is
    returned. Picking the earliest (rather than the lowest-priced)
    handles the IGO case where 23:30-05:30 is one contiguous off-peak
    run: any 30-min slot in that block belongs to the cheap cohort, and
    we want the FIRST one as the bridge horizon.

    Used to replace hardcoded 23:30 cheap-window assumptions across
    rules. Adapts to Saving Sessions, IGO bonus dispatches, and any
    non-standard cheap slot Octopus publishes.
    """
    if not import_rates:
        return None
    future: list[tuple[datetime, RateSlot]] = []
    for r in import_rates:
        start_local = (
            r.start.astimezone(tz)
            if tz is not None and r.start.tzinfo is not None
            else r.start
        )
        if start_local > now_local:
            future.append((start_local, r))
    if not future:
        return None
    cohort = _cheap_band_filter(bottom_n_pct([r for _, r in future], n_pct))
    if not cohort:
        return None
    cohort_ids = {id(r) for r in cohort}
    return min(s for s, r in future if id(r) in cohort_ids)


def next_cheap_window_end_local(
    import_rates: list[RateSlot],
    now_local: datetime,
    tz=None,
    n_pct: int | float = 25,
) -> datetime | None:
    """Return the LOCAL datetime at which the next cheap import window
    ENDS, or None if no upcoming rates are loaded.

    Walks the cohort (bottom-N%, filtered to the cheapest band) starting
    from the earliest cheap slot and extends while consecutive slots in
    that cohort abut. The end of the last contiguous cheap slot is
    returned.

    Used by CheapRateChargeRule to anchor the morning-bridge calc:
    SOC must reach `target_soc` by this time; from here forward the
    battery depletes until PV takeover.
    """
    if not import_rates:
        return None
    future: list[tuple[datetime, datetime, RateSlot]] = []
    for r in import_rates:
        start_local = (
            r.start.astimezone(tz)
            if tz is not None and r.start.tzinfo is not None
            else r.start
        )
        end_local = (
            r.end.astimezone(tz)
            if tz is not None and r.end.tzinfo is not None
            else r.end
        )
        if start_local > now_local:
            future.append((start_local, end_local, r))
    if not future:
        return None
    cohort = _cheap_band_filter(bottom_n_pct([t[2] for t in future], n_pct))
    if not cohort:
        return None
    cohort_ids = {id(r) for r in cohort}
    future.sort(key=lambda t: t[0])
    first_idx = next(
        (i for i, (_, _, r) in enumerate(future) if id(r) in cohort_ids),
        None,
    )
    if first_idx is None:
        return None
    end_local = future[first_idx][1]
    for i in range(first_idx + 1, len(future)):
        s, e, r = future[i]
        if id(r) not in cohort_ids:
            break
        if s != end_local:
            break
        end_local = e
    return end_local
