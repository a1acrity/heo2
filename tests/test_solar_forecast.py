# tests/test_solar_forecast.py
"""Tests for solar forecast adapter that reads from HACS solcast_solar.

Covers HEO-4: HEO II's own Solcast client over-reported by 4x because it
summed kW without multiplying by 0.5h and collapsed multi-day forecasts
into 24 hourly buckets. The agreed fix is to stop calling Solcast directly
and consume the HACS solcast_solar integration's already-correct values.
"""

from datetime import date

import pytest

from heo2.solar_forecast import solar_forecast_from_hacs


# Realistic fixture: shape of sensor.solcast_pv_forecast_forecast_today's
# detailedHourly attribute, sampled live from Paddy's HA on 2026-04-18.
# 24 hourly entries, period_start in local time, pv_estimate is kWh per hour.
REAL_HACS_DETAILED_HOURLY = [
    {"period_start": "2026-04-18T00:00:00+01:00", "pv_estimate": 0.0},
    {"period_start": "2026-04-18T01:00:00+01:00", "pv_estimate": 0.0},
    {"period_start": "2026-04-18T02:00:00+01:00", "pv_estimate": 0.0},
    {"period_start": "2026-04-18T03:00:00+01:00", "pv_estimate": 0.0},
    {"period_start": "2026-04-18T04:00:00+01:00", "pv_estimate": 0.0},
    {"period_start": "2026-04-18T05:00:00+01:00", "pv_estimate": 0.0},
    {"period_start": "2026-04-18T06:00:00+01:00", "pv_estimate": 0.1497},
    {"period_start": "2026-04-18T07:00:00+01:00", "pv_estimate": 1.2196},
    {"period_start": "2026-04-18T08:00:00+01:00", "pv_estimate": 2.968},
    {"period_start": "2026-04-18T09:00:00+01:00", "pv_estimate": 4.3901},
    {"period_start": "2026-04-18T10:00:00+01:00", "pv_estimate": 5.5},
    {"period_start": "2026-04-18T11:00:00+01:00", "pv_estimate": 6.0},
    {"period_start": "2026-04-18T12:00:00+01:00", "pv_estimate": 6.1672},
    {"period_start": "2026-04-18T13:00:00+01:00", "pv_estimate": 6.0},
    {"period_start": "2026-04-18T14:00:00+01:00", "pv_estimate": 5.5},
    {"period_start": "2026-04-18T15:00:00+01:00", "pv_estimate": 4.5},
    {"period_start": "2026-04-18T16:00:00+01:00", "pv_estimate": 3.5},
    {"period_start": "2026-04-18T17:00:00+01:00", "pv_estimate": 2.5},
    {"period_start": "2026-04-18T18:00:00+01:00", "pv_estimate": 1.5},
    {"period_start": "2026-04-18T19:00:00+01:00", "pv_estimate": 0.5},
    {"period_start": "2026-04-18T20:00:00+01:00", "pv_estimate": 0.1},
    {"period_start": "2026-04-18T21:00:00+01:00", "pv_estimate": 0.0},
    {"period_start": "2026-04-18T22:00:00+01:00", "pv_estimate": 0.0},
    {"period_start": "2026-04-18T23:00:00+01:00", "pv_estimate": 0.0},
]

TARGET = date(2026, 4, 18)


class TestSolarForecastFromHacs:
    def test_returns_24_hourly_values(self):
        """Output is always a 24-element list, regardless of input size."""
        out = solar_forecast_from_hacs(REAL_HACS_DETAILED_HOURLY, TARGET)
        assert len(out) == 24

    def test_zero_input_gives_all_zeros(self):
        out = solar_forecast_from_hacs([], TARGET)
        assert out == [0.0] * 24

    def test_values_match_input_in_right_buckets(self):
        """Midday peak (12:00 local) should be 6.1672 kWh."""
        out = solar_forecast_from_hacs(REAL_HACS_DETAILED_HOURLY, TARGET)
        assert out[12] == pytest.approx(6.1672)
        # Early morning zero
        assert out[3] == pytest.approx(0.0)
        # 07:00 bucket
        assert out[7] == pytest.approx(1.2196)

    def test_total_matches_integration_state(self):
        """Sum of hourly values equals the HACS sensor's 'today' total."""
        out = solar_forecast_from_hacs(REAL_HACS_DETAILED_HOURLY, TARGET)
        total = sum(out)
        # Fixture sums to ~50 kWh which is the HACS reported value
        assert total == pytest.approx(50.19, abs=0.5)

    def test_filters_out_other_dates(self):
        """Entries with period_start on another date are ignored."""
        mixed = REAL_HACS_DETAILED_HOURLY + [
            # Tomorrow at 12:00 should NOT land in today's hour 12
            {"period_start": "2026-04-19T12:00:00+01:00", "pv_estimate": 999.0},
            # Yesterday at 06:00 should NOT land in today's hour 6
            {"period_start": "2026-04-17T06:00:00+01:00", "pv_estimate": 888.0},
        ]
        out = solar_forecast_from_hacs(mixed, TARGET)
        # Midday should be UNCHANGED (not 999 + 6.1672)
        assert out[12] == pytest.approx(6.1672)
        assert out[6] == pytest.approx(0.1497)
        # Total still matches today only
        assert sum(out) == pytest.approx(50.19, abs=0.5)

    def test_missing_hour_defaults_to_zero(self):
        """If an hour is missing from input, that bucket is 0.0 not an error."""
        sparse = [
            {"period_start": "2026-04-18T12:00:00+01:00", "pv_estimate": 5.0},
        ]
        out = solar_forecast_from_hacs(sparse, TARGET)
        assert out[12] == 5.0
        assert out[11] == 0.0
        assert out[13] == 0.0

    def test_uses_p10_when_requested(self):
        """Risk-budgeted callers can request pv_estimate10 instead of median."""
        sample = [{
            "period_start": "2026-04-18T12:00:00+01:00",
            "pv_estimate": 6.1672,
            "pv_estimate10": 2.615,
            "pv_estimate90": 8.6563,
        }]
        median = solar_forecast_from_hacs(sample, TARGET)
        conservative = solar_forecast_from_hacs(sample, TARGET, key="pv_estimate10")
        optimistic = solar_forecast_from_hacs(sample, TARGET, key="pv_estimate90")
        assert median[12] == pytest.approx(6.1672)
        assert conservative[12] == pytest.approx(2.615)
        assert optimistic[12] == pytest.approx(8.6563)

    def test_malformed_period_start_is_skipped_not_raised(self):
        """Bad data in one entry should not fail the whole aggregation."""
        mixed = [
            {"period_start": "not-a-timestamp", "pv_estimate": 999.0},
            {"period_start": "2026-04-18T12:00:00+01:00", "pv_estimate": 6.0},
        ]
        out = solar_forecast_from_hacs(mixed, TARGET)
        assert out[12] == pytest.approx(6.0)

    def test_missing_pv_estimate_treated_as_zero(self):
        """Entry without the requested key defaults that hour to 0.0."""
        bad = [{"period_start": "2026-04-18T12:00:00+01:00"}]
        out = solar_forecast_from_hacs(bad, TARGET)
        assert out[12] == 0.0

    def test_regression_no_4x_overreport(self):
        """The HEO-4 headline: total must NOT be 4x the per-day integral.

        If someone later re-introduces the 'sum kW without *0.5' bug, or
        reintroduces multi-day collapse, this test catches it."""
        out = solar_forecast_from_hacs(REAL_HACS_DETAILED_HOURLY, TARGET)
        total = sum(out)
        # The synthetic 4x bug would have produced ~200+ kWh. The correct
        # one-day total is around 50 kWh. Set a generous ceiling at 2x.
        assert total < 100, f"Suspiciously high forecast {total} kWh, possible regression of HEO-4"
