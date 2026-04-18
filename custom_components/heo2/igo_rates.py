# custom_components/heo2/igo_rates.py
"""Builders for Octopus Intelligent Go import rate slots.

Pure functions, no Home Assistant imports. Testable in isolation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import RateSlot

UTC = timezone.utc


def build_igo_import_rates(
    now: datetime,
    tz: ZoneInfo,
    night_start: str = "23:30",
    night_end: str = "05:30",
    night_rate_pence: float = 7.0,
    day_rate_pence: float = 27.88,
) -> list[RateSlot]:
    """Build IGO import rate slots relative to `now`.

    Octopus Intelligent Go charges `night_rate_pence` during the local-time
    window `night_start` to `night_end` (which crosses midnight), and
    `day_rate_pence` at all other times.

    Returns three contiguous slots covering from today's local midnight
    through tomorrow's `night_end` — about 29.5 hours of coverage, enough
    for any rule making decisions about the next 24 hours.

    All returned datetimes are UTC-aware, matching the convention used
    by the rule engine's `rate_at()` comparisons.

    Args:
        now: Current time (any timezone; used only to anchor "today" in `tz`).
        tz: Local timezone for interpreting the night window.
        night_start: Local "HH:MM" when the night rate begins.
        night_end: Local "HH:MM" when the night rate ends (next day).
        night_rate_pence: Price during the night window.
        day_rate_pence: Price outside the night window.
    """
    nh, nm = _parse_hhmm(night_start)
    eh, em = _parse_hhmm(night_end)

    # Anchor "today" in local time
    today_local = now.astimezone(tz).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    # Local-time window boundaries
    night_end_today = today_local.replace(hour=eh, minute=em)
    night_start_today = today_local.replace(hour=nh, minute=nm)
    night_end_tomorrow = night_end_today + timedelta(days=1)

    return [
        RateSlot(
            start=today_local.astimezone(UTC),
            end=night_end_today.astimezone(UTC),
            rate_pence=night_rate_pence,
        ),
        RateSlot(
            start=night_end_today.astimezone(UTC),
            end=night_start_today.astimezone(UTC),
            rate_pence=day_rate_pence,
        ),
        RateSlot(
            start=night_start_today.astimezone(UTC),
            end=night_end_tomorrow.astimezone(UTC),
            rate_pence=night_rate_pence,
        ),
    ]


def _parse_hhmm(s: str) -> tuple[int, int]:
    """Parse an 'HH:MM' string into (hour, minute). Raises ValueError on bad input."""
    parts = s.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Expected HH:MM, got {s!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"Out of range HH:MM: {s!r}")
    return h, m
