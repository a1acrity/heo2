# custom_components/heo2/load_profile.py
"""Historical load profile builder. No Home Assistant imports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from statistics import median


@dataclass
class LoadProfile:
    """Hourly load profiles for weekdays and weekends."""
    weekday: list[float]
    weekend: list[float]

    def for_datetime(self, dt: datetime) -> list[float]:
        """Return the appropriate profile for the given datetime."""
        if dt.weekday() >= 5:
            return list(self.weekend)
        return list(self.weekday)

    def with_appliance_overlay(
        self,
        base_profile: list[float],
        start_hour: int,
        duration_hours: int,
        draw_kw: float,
    ) -> list[float]:
        """Add appliance draw on top of a base profile."""
        result = list(base_profile)
        for h in range(start_hour, min(start_hour + duration_hours, 24)):
            result[h] += draw_kw
        return result


class LoadProfileBuilder:
    """Builds hourly median load profiles from historical data."""

    def __init__(self, baseline_w: float = 1900.0):
        self._baseline_kwh = baseline_w / 1000.0
        self._weekday_hours: list[list[float]] = [[] for _ in range(24)]
        self._weekend_hours: list[list[float]] = [[] for _ in range(24)]

    def add_day(self, date: datetime, hourly_kwh: list[float]) -> None:
        """Add one day's hourly load data."""
        if len(hourly_kwh) != 24:
            return
        target = self._weekend_hours if date.weekday() >= 5 else self._weekday_hours
        for hour, kwh in enumerate(hourly_kwh):
            target[hour].append(kwh)

    def build(self) -> LoadProfile:
        """Build the load profile from accumulated data."""
        weekday = [
            median(vals) if vals else self._baseline_kwh
            for vals in self._weekday_hours
        ]
        weekend = [
            median(vals) if vals else self._baseline_kwh
            for vals in self._weekend_hours
        ]
        return LoadProfile(weekday=weekday, weekend=weekend)
