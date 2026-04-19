# tests/test_load_history.py
"""Tests for load history aggregator.

Covers HEO-5: LoadProfileBuilder was never fed any historical data because
the coordinator never called add_day(). This module provides the pure
functions that convert HA recorder state history into the (date, hourly_kwh)
shape that add_day() expects.
"""

from datetime import datetime, date, timezone, timedelta
from zoneinfo import ZoneInfo

import pytest

from heo2.load_history import (
    aggregate_samples_to_hourly_kwh,
    learn_days_from_samples,
)

UTC = timezone.utc
LONDON = ZoneInfo("Europe/London")


def _mk_samples(*pairs, tz=UTC):
    """Convenience: build samples from list of (ISO8601-ish, watts) pairs."""
    out = []
    for ts, w in pairs:
        if isinstance(ts, str):
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
        else:
            dt = ts
        out.append((dt, float(w)))
    return out


class TestAggregateSamplesToHourlyKwh:
    def test_empty_samples_returns_zeros(self):
        out = aggregate_samples_to_hourly_kwh([], date(2026, 4, 18), UTC)
        assert out == [0.0] * 24

    def test_single_sample_returns_zeros(self):
        """One sample alone gives no interval to integrate over."""
        samples = _mk_samples(("2026-04-18T12:00:00+00:00", 2000))
        out = aggregate_samples_to_hourly_kwh(samples, date(2026, 4, 18), UTC)
        assert out == [0.0] * 24

    def test_flat_constant_power_for_one_hour(self):
        """2 kW held for one hour = 2 kWh in that hour."""
        samples = _mk_samples(
            ("2026-04-18T12:00:00+00:00", 2000),
            ("2026-04-18T13:00:00+00:00", 2000),
        )
        out = aggregate_samples_to_hourly_kwh(samples, date(2026, 4, 18), UTC)
        assert out[12] == pytest.approx(2.0)
        # Every other hour zero
        assert sum(out) == pytest.approx(2.0)

    def test_flat_constant_power_across_hour_boundary(self):
        """2 kW held from 12:30 to 13:30 = 1 kWh in hour 12 and 1 kWh in hour 13."""
        samples = _mk_samples(
            ("2026-04-18T12:30:00+00:00", 2000),
            ("2026-04-18T13:30:00+00:00", 2000),
        )
        out = aggregate_samples_to_hourly_kwh(samples, date(2026, 4, 18), UTC)
        assert out[12] == pytest.approx(1.0)
        assert out[13] == pytest.approx(1.0)

    def test_trapezoidal_integration(self):
        """Power ramp 1 kW at 12:00 to 3 kW at 13:00 integrates to 2 kWh.

        Trapezoidal: ((1 + 3) / 2) * 1h = 2 kWh."""
        samples = _mk_samples(
            ("2026-04-18T12:00:00+00:00", 1000),
            ("2026-04-18T13:00:00+00:00", 3000),
        )
        out = aggregate_samples_to_hourly_kwh(samples, date(2026, 4, 18), UTC)
        assert out[12] == pytest.approx(2.0)

    def test_full_day_of_flat_load(self):
        """1 kW continuous for 24 hours = 24 kWh spread evenly.

        Uses generous max_interval because the synthetic samples are
        only at day boundaries; real data would have many samples per hour."""
        samples = _mk_samples(
            ("2026-04-18T00:00:00+00:00", 1000),
            ("2026-04-19T00:00:00+00:00", 1000),
        )
        out = aggregate_samples_to_hourly_kwh(
            samples, date(2026, 4, 18), UTC,
            max_interval_seconds=25 * 3600,
        )
        assert sum(out) == pytest.approx(24.0)
        for h in range(24):
            assert out[h] == pytest.approx(1.0)

    def test_negative_power_clamped_to_zero(self):
        """Solar export makes the entity negative. Load profile ignores that."""
        samples = _mk_samples(
            ("2026-04-18T12:00:00+00:00", -2000),
            ("2026-04-18T13:00:00+00:00", -2000),
        )
        out = aggregate_samples_to_hourly_kwh(samples, date(2026, 4, 18), UTC)
        assert out[12] == 0.0
        assert sum(out) == 0.0

    def test_samples_outside_target_date_bookend_correctly(self):
        """Sample before midnight provides the initial value; sample after
        provides the terminal value. Both are needed to integrate the full day."""
        samples = _mk_samples(
            ("2026-04-17T23:00:00+00:00", 1000),
            ("2026-04-18T00:00:00+00:00", 1000),
            ("2026-04-18T01:00:00+00:00", 1000),
            ("2026-04-18T12:00:00+00:00", 1000),
            ("2026-04-18T23:00:00+00:00", 1000),
            ("2026-04-19T00:00:00+00:00", 1000),
            ("2026-04-19T01:00:00+00:00", 1000),
        )
        out = aggregate_samples_to_hourly_kwh(
            samples, date(2026, 4, 18), UTC,
            max_interval_seconds=12 * 3600,
        )
        # 1 kW all day = 24 kWh, 1 kWh per hour
        assert sum(out) == pytest.approx(24.0)
        assert out[0] == pytest.approx(1.0)
        assert out[23] == pytest.approx(1.0)

    def test_step_change_mid_hour(self):
        """1 kW until 12:30, then 3 kW until 13:00.

        Hour 12: 0.5h at 1 kW (0.5 kWh) + 0.5h trapezoid from 1-3 kW
        trap = ((1+3)/2)*0.5 = 1 kWh. Total hour 12 = 1.5 kWh."""
        samples = _mk_samples(
            ("2026-04-18T12:00:00+00:00", 1000),
            ("2026-04-18T12:30:00+00:00", 1000),
            ("2026-04-18T13:00:00+00:00", 3000),
        )
        out = aggregate_samples_to_hourly_kwh(samples, date(2026, 4, 18), UTC)
        assert out[12] == pytest.approx(1.5)

    def test_filters_to_target_date_only(self):
        """Samples from other days must not contribute to today's buckets."""
        samples = _mk_samples(
            ("2026-04-17T12:00:00+00:00", 99999),
            ("2026-04-17T13:00:00+00:00", 99999),
            ("2026-04-19T12:00:00+00:00", 99999),
            ("2026-04-19T13:00:00+00:00", 99999),
            # Only these two pairs are today
            ("2026-04-18T12:00:00+00:00", 2000),
            ("2026-04-18T13:00:00+00:00", 2000),
        )
        out = aggregate_samples_to_hourly_kwh(samples, date(2026, 4, 18), UTC)
        assert out[12] == pytest.approx(2.0)
        assert sum(out) == pytest.approx(2.0)

    def test_bst_date_bucketing(self):
        """target_date = 2026-04-18 LOCAL means 23:00 UTC 2026-04-17 is today.

        In BST (UTC+1): local midnight 2026-04-18 = 23:00 UTC 2026-04-17."""
        samples = _mk_samples(
            # 00:30 BST = 23:30 UTC previous day
            ("2026-04-17T23:30:00+00:00", 2000),
            ("2026-04-18T00:30:00+00:00", 2000),
        )
        out = aggregate_samples_to_hourly_kwh(samples, date(2026, 4, 18), LONDON)
        # In local time: 00:30 to 01:30, all hour 0 and hour 1 of 18 April local
        # 2 kW for 1 hour = 2 kWh split half in hour 0, half in hour 1
        assert out[0] == pytest.approx(1.0)
        assert out[1] == pytest.approx(1.0)
        assert sum(out) == pytest.approx(2.0)

    def test_unsorted_samples_are_sorted(self):
        """Don't rely on caller to sort. Receive in any order, integrate in time order."""
        samples = _mk_samples(
            ("2026-04-18T13:00:00+00:00", 2000),
            ("2026-04-18T12:00:00+00:00", 2000),
        )
        out = aggregate_samples_to_hourly_kwh(samples, date(2026, 4, 18), UTC)
        assert out[12] == pytest.approx(2.0)

    def test_watts_input_produces_kwh_output(self):
        """Watt-seconds of energy divided by 3.6e6 gives kWh. Make this explicit."""
        # 1000 W held for 1 hour = 3600000 watt-seconds = 1 kWh
        samples = _mk_samples(
            ("2026-04-18T12:00:00+00:00", 1000),
            ("2026-04-18T13:00:00+00:00", 1000),
        )
        out = aggregate_samples_to_hourly_kwh(samples, date(2026, 4, 18), UTC)
        assert out[12] == pytest.approx(1.0)

    def test_gap_longer_than_max_interval_is_dropped(self):
        """Large gaps between samples indicate the device was offline.

        Don't invent energy by interpolating across such gaps."""
        samples = _mk_samples(
            ("2026-04-18T10:00:00+00:00", 1000),
            ("2026-04-18T11:00:00+00:00", 1000),
            # 3-hour gap - device offline
            ("2026-04-18T14:00:00+00:00", 1000),
            ("2026-04-18T15:00:00+00:00", 1000),
        )
        out = aggregate_samples_to_hourly_kwh(samples, date(2026, 4, 18), UTC)
        # Only the 10-11 and 14-15 intervals should contribute
        assert out[10] == pytest.approx(1.0)
        assert out[14] == pytest.approx(1.0)
        # The interpolated 11-14 gap should NOT contribute
        assert out[11] == 0.0
        assert out[12] == 0.0
        assert out[13] == 0.0

    def test_configurable_max_interval_allows_sparse_data(self):
        """Caller can loosen the stale-interval check for known-sparse data."""
        samples = _mk_samples(
            ("2026-04-18T10:00:00+00:00", 1000),
            # 5-hour gap, but caller says it's fine
            ("2026-04-18T15:00:00+00:00", 1000),
        )
        out = aggregate_samples_to_hourly_kwh(
            samples, date(2026, 4, 18), UTC,
            max_interval_seconds=6 * 3600,
        )
        # 1 kW over 5 hours = 5 kWh
        assert sum(out) == pytest.approx(5.0)


class TestLearnDaysFromSamples:
    def test_returns_dict_keyed_by_date(self):
        """Multi-day samples yield one entry per covered date."""
        samples = _mk_samples(
            ("2026-04-16T12:00:00+00:00", 1000),
            ("2026-04-16T13:00:00+00:00", 1000),
            ("2026-04-17T12:00:00+00:00", 2000),
            ("2026-04-17T13:00:00+00:00", 2000),
            ("2026-04-18T12:00:00+00:00", 3000),
            ("2026-04-18T13:00:00+00:00", 3000),
        )
        result = learn_days_from_samples(samples, UTC)
        assert set(result.keys()) == {date(2026, 4, 16), date(2026, 4, 17), date(2026, 4, 18)}
        assert result[date(2026, 4, 16)][12] == pytest.approx(1.0)
        assert result[date(2026, 4, 17)][12] == pytest.approx(2.0)
        assert result[date(2026, 4, 18)][12] == pytest.approx(3.0)

    def test_each_day_has_24_buckets(self):
        samples = _mk_samples(
            ("2026-04-17T12:00:00+00:00", 1000),
            ("2026-04-18T12:00:00+00:00", 1000),
        )
        result = learn_days_from_samples(samples, UTC)
        for d, buckets in result.items():
            assert len(buckets) == 24

    def test_empty_samples_returns_empty_dict(self):
        assert learn_days_from_samples([], UTC) == {}

    def test_respects_local_timezone(self):
        """Day boundary is local midnight, not UTC midnight."""
        samples = _mk_samples(
            # 22:00 UTC 2026-04-18 = 23:00 BST still 2026-04-18 local
            ("2026-04-18T22:00:00+00:00", 1000),
            # 00:00 UTC 2026-04-19 = 01:00 BST on 2026-04-19
            ("2026-04-19T00:00:00+00:00", 1000),
        )
        result = learn_days_from_samples(samples, LONDON)
        # This spans local dates 2026-04-18 and 2026-04-19
        assert date(2026, 4, 18) in result
        assert date(2026, 4, 19) in result
