# tests/test_igo_rates.py
"""Tests for IGO import rate slot builder.

Covers HEO-1: the original coordinator code built UTC-midnight-relative
rate windows, producing a one-hour shift during BST. These tests lock
in the correct local-time semantics.
"""

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pytest

from heo2.igo_rates import build_igo_import_rates
from heo2.models import RateSlot

UTC = timezone.utc
LONDON = ZoneInfo("Europe/London")


class TestBuildIGOImportRates:
    def test_returns_three_contiguous_slots(self):
        """Three slots covering ~29.5 hours: night-start-of-today, day, night-ending-tomorrow."""
        now = datetime(2026, 4, 18, 18, 0, tzinfo=UTC)  # 19:00 BST
        rates = build_igo_import_rates(now, tz=LONDON)
        assert len(rates) == 3
        assert all(isinstance(r, RateSlot) for r in rates)
        # Contiguous
        assert rates[0].end == rates[1].start
        assert rates[1].end == rates[2].start

    def test_bst_night_rate_starts_at_local_23_30(self):
        """In BST, the upcoming night-rate window (slot 3) must start at 23:30 LOCAL,
        which is 22:30 UTC. The original bug produced 23:30 UTC = 00:30 BST."""
        now = datetime(2026, 4, 18, 18, 0, tzinfo=UTC)  # 19:00 BST
        rates = build_igo_import_rates(now, tz=LONDON)
        night = rates[2]
        # 23:30 BST on 2026-04-18 = 22:30 UTC
        assert night.start == datetime(2026, 4, 18, 22, 30, tzinfo=UTC)
        # 05:30 BST on 2026-04-19 = 04:30 UTC
        assert night.end == datetime(2026, 4, 19, 4, 30, tzinfo=UTC)

    def test_bst_night_rate_value_is_7p(self):
        now = datetime(2026, 4, 18, 18, 0, tzinfo=UTC)
        rates = build_igo_import_rates(
            now, tz=LONDON, night_rate_pence=7.0, day_rate_pence=27.88
        )
        assert rates[0].rate_pence == 7.0
        assert rates[1].rate_pence == 27.88
        assert rates[2].rate_pence == 7.0

    def test_rate_at_time_just_before_igo_starts(self):
        """At 23:29 BST the rate is still DAY rate. At 23:30 BST it flips to NIGHT."""
        now = datetime(2026, 4, 18, 18, 0, tzinfo=UTC)
        rates = build_igo_import_rates(
            now, tz=LONDON, night_rate_pence=7.0, day_rate_pence=27.88
        )
        # 23:29 BST = 22:29 UTC
        t_day = datetime(2026, 4, 18, 22, 29, tzinfo=UTC)
        # 23:30 BST = 22:30 UTC
        t_night = datetime(2026, 4, 18, 22, 30, tzinfo=UTC)

        def rate_at(dt, rs):
            for r in rs:
                if r.start <= dt < r.end:
                    return r.rate_pence
            return None

        assert rate_at(t_day, rates) == 27.88
        assert rate_at(t_night, rates) == 7.0

    def test_rate_at_time_at_igo_boundary_in_bst(self):
        """01:00 BST on 19 April = 00:00 UTC on 19 April.
        Should still be NIGHT rate (IGO runs until 05:30 local)."""
        now = datetime(2026, 4, 18, 18, 0, tzinfo=UTC)
        rates = build_igo_import_rates(
            now, tz=LONDON, night_rate_pence=7.0, day_rate_pence=27.88
        )
        t = datetime(2026, 4, 19, 0, 0, tzinfo=UTC)  # 01:00 BST
        hit = None
        for r in rates:
            if r.start <= t < r.end:
                hit = r.rate_pence
                break
        assert hit == 7.0, "01:00 BST should be night rate"

    def test_utc_winter_night_rate_unchanged(self):
        """In winter (GMT = UTC), local and UTC are the same."""
        now = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        rates = build_igo_import_rates(now, tz=LONDON)
        # Night window slot 3: 23:30 GMT today = 23:30 UTC today
        assert rates[2].start == datetime(2026, 1, 15, 23, 30, tzinfo=UTC)
        assert rates[2].end == datetime(2026, 1, 16, 5, 30, tzinfo=UTC)

    def test_custom_night_window_crossing_midnight(self):
        """Alternative night schedule 22:30-06:30 BST.

        Exercises the boundary maths with non-default values, still
        crossing midnight as IGO-like tariffs always do."""
        now = datetime(2026, 4, 18, 18, 0, tzinfo=UTC)
        rates = build_igo_import_rates(
            now, tz=LONDON, night_start="22:30", night_end="06:30"
        )
        # 22:30 BST today = 21:30 UTC today
        assert rates[2].start == datetime(2026, 4, 18, 21, 30, tzinfo=UTC)
        # 06:30 BST tomorrow = 05:30 UTC tomorrow
        assert rates[2].end == datetime(2026, 4, 19, 5, 30, tzinfo=UTC)

    def test_slots_are_utc_aware(self):
        """Every slot datetime must be UTC-aware for rate_at() comparisons."""
        now = datetime(2026, 4, 18, 18, 0, tzinfo=UTC)
        rates = build_igo_import_rates(now, tz=LONDON)
        for r in rates:
            assert r.start.tzinfo is not None
            assert r.end.tzinfo is not None
            assert r.start.utcoffset() == timedelta(0), "start not UTC-normalised"
            assert r.end.utcoffset() == timedelta(0), "end not UTC-normalised"

    def test_rejects_malformed_hhmm(self):
        """Bad input should raise ValueError, not silently accept."""
        now = datetime(2026, 4, 18, 18, 0, tzinfo=UTC)
        with pytest.raises(ValueError):
            build_igo_import_rates(now, tz=LONDON, night_start="bogus")
        with pytest.raises(ValueError):
            build_igo_import_rates(now, tz=LONDON, night_end="25:00")
        with pytest.raises(ValueError):
            build_igo_import_rates(now, tz=LONDON, night_start="23:99")
