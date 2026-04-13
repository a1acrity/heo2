# tests/test_load_profile.py
"""Tests for historical load profile builder."""

from datetime import datetime, timezone
from statistics import median

from heo2.load_profile import LoadProfileBuilder, LoadProfile


class TestLoadProfileBuilder:
    def test_weekday_weekend_split(self):
        """Separate profiles for weekdays and weekends."""
        builder = LoadProfileBuilder(baseline_w=1900.0)
        builder.add_day(
            date=datetime(2026, 4, 13, tzinfo=timezone.utc),
            hourly_kwh=[1.0] * 24,
        )
        builder.add_day(
            date=datetime(2026, 4, 14, tzinfo=timezone.utc),
            hourly_kwh=[1.5] * 24,
        )
        builder.add_day(
            date=datetime(2026, 4, 18, tzinfo=timezone.utc),
            hourly_kwh=[2.0] * 24,
        )
        profile = builder.build()
        assert profile.weekday[0] == 1.25
        assert profile.weekend[0] == 2.0

    def test_falls_back_to_baseline(self):
        """No historical data → flat baseline profile."""
        builder = LoadProfileBuilder(baseline_w=1900.0)
        profile = builder.build()
        assert profile.weekday[0] == 1.9
        assert profile.weekend[0] == 1.9

    def test_get_profile_for_datetime(self):
        """Selects weekday or weekend profile based on day."""
        builder = LoadProfileBuilder(baseline_w=1900.0)
        builder.add_day(
            date=datetime(2026, 4, 13, tzinfo=timezone.utc),
            hourly_kwh=[1.0] * 24,
        )
        builder.add_day(
            date=datetime(2026, 4, 18, tzinfo=timezone.utc),
            hourly_kwh=[2.0] * 24,
        )
        profile = builder.build()
        weekday_load = profile.for_datetime(datetime(2026, 4, 20, tzinfo=timezone.utc))
        assert weekday_load[0] == 1.0
        weekend_load = profile.for_datetime(datetime(2026, 4, 25, tzinfo=timezone.utc))
        assert weekend_load[0] == 2.0

    def test_24_hour_profiles(self):
        """Profiles always have exactly 24 hourly values."""
        builder = LoadProfileBuilder(baseline_w=1900.0)
        builder.add_day(
            date=datetime(2026, 4, 13, tzinfo=timezone.utc),
            hourly_kwh=[1.0] * 24,
        )
        profile = builder.build()
        assert len(profile.weekday) == 24
        assert len(profile.weekend) == 24

    def test_appliance_overlay(self):
        """Add appliance draw to a profile."""
        builder = LoadProfileBuilder(baseline_w=1900.0)
        profile = builder.build()
        base = list(profile.weekday)
        overlaid = profile.with_appliance_overlay(
            base_profile=base,
            start_hour=10,
            duration_hours=2,
            draw_kw=2.5,
        )
        assert overlaid[10] == base[10] + 2.5
        assert overlaid[11] == base[11] + 2.5
        assert overlaid[12] == base[12]
