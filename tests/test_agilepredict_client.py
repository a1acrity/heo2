# tests/test_agilepredict_client.py
"""Tests for AgilePredict HTTP client, rewritten for the real API schema.

The original tests mocked a fabricated ``{valid_from, valid_to, value_inc_vat}``
schema that doesn't match the live AgilePredict service. The live schema is:

    [{
      "name": "2026-04-18 16:17",
      "created_at": "2026-04-18T16:17:10.292633+01:00",
      "prices": [
        {"date_time": "2026-04-18T17:00:00+01:00",
         "agile_pred": 30.78,
         "agile_low": 30.57,
         "agile_high": 31.0,
         "region": "X"},
        ...
      ]
    }]

Half-hourly records across 15 regions. ``date_time`` is period-start in
local time. See HEO-3 for history.
"""

import pytest
from datetime import datetime, timezone

from heo2.agilepredict_client import AgilePredictClient
from heo2.models import RateSlot

UTC = timezone.utc


# Realistic fixture shape matching the live API
SAMPLE_RESPONSE = [
    {
        "name": "2026-04-18 16:17",
        "created_at": "2026-04-18T16:17:10.292633+01:00",
        "prices": [
            # Region M (NGED Yorkshire, Hull) - what we care about
            {"date_time": "2026-04-18T17:00:00+01:00",
             "agile_pred": 28.5, "agile_low": 28.0, "agile_high": 29.0, "region": "M"},
            {"date_time": "2026-04-18T17:30:00+01:00",
             "agile_pred": 32.1, "agile_low": 31.5, "agile_high": 32.5, "region": "M"},
            {"date_time": "2026-04-18T18:00:00+01:00",
             "agile_pred": 30.0, "agile_low": 29.5, "agile_high": 30.5, "region": "M"},
            # Region X (national average) - should be ignored by default
            {"date_time": "2026-04-18T17:00:00+01:00",
             "agile_pred": 30.78, "agile_low": 30.57, "agile_high": 31.0, "region": "X"},
            {"date_time": "2026-04-18T17:30:00+01:00",
             "agile_pred": 32.5, "agile_low": 32.29, "agile_high": 32.72, "region": "X"},
            # Region A - also ignored
            {"date_time": "2026-04-18T17:00:00+01:00",
             "agile_pred": 29.0, "agile_low": 28.5, "agile_high": 29.5, "region": "A"},
        ],
    }
]


class TestAgilePredictClient:
    @pytest.mark.asyncio
    async def test_hits_api_root_not_rates_export(self, httpx_mock):
        """Client must GET /api/ (trailing slash), not /api/rates/export
        which 404s on the real service."""
        httpx_mock.add_response(
            url="http://example.test/api/", json=SAMPLE_RESPONSE,
        )
        client = AgilePredictClient(base_url="http://example.test", region="M")
        await client.fetch_export_rates()
        # httpx_mock will fail the test if any other URL was called

    @pytest.mark.asyncio
    async def test_filters_to_configured_region(self, httpx_mock):
        """Default region M should return only M prices."""
        httpx_mock.add_response(json=SAMPLE_RESPONSE)
        client = AgilePredictClient(base_url="http://example.test", region="M")
        rates = await client.fetch_export_rates()
        assert len(rates) == 3
        # Values in M should be 28.5, 32.1, 30.0 (not X's 30.78, 32.5)
        assert rates[0].rate_pence == pytest.approx(28.5)
        assert rates[1].rate_pence == pytest.approx(32.1)
        assert rates[2].rate_pence == pytest.approx(30.0)

    @pytest.mark.asyncio
    async def test_can_request_different_region(self, httpx_mock):
        """Constructor region is configurable."""
        httpx_mock.add_response(json=SAMPLE_RESPONSE)
        client = AgilePredictClient(base_url="http://example.test", region="X")
        rates = await client.fetch_export_rates()
        assert len(rates) == 2
        assert rates[0].rate_pence == pytest.approx(30.78)

    @pytest.mark.asyncio
    async def test_parses_date_time_to_utc_aware_slot(self, httpx_mock):
        """date_time is local time with offset; slots should be UTC-aware."""
        httpx_mock.add_response(json=SAMPLE_RESPONSE)
        client = AgilePredictClient(base_url="http://example.test", region="M")
        rates = await client.fetch_export_rates()
        # 17:00+01:00 == 16:00 UTC
        assert rates[0].start == datetime(2026, 4, 18, 16, 0, tzinfo=UTC)
        # 17:30+01:00 == 16:30 UTC (end of the first 30-min slot)
        assert rates[0].end == datetime(2026, 4, 18, 16, 30, tzinfo=UTC)
        assert rates[0].start.utcoffset() == timezone.utc.utcoffset(None)


    @pytest.mark.asyncio
    async def test_slots_are_half_hourly_and_contiguous(self, httpx_mock):
        """Each slot runs for 30 minutes, and consecutive slots touch."""
        httpx_mock.add_response(json=SAMPLE_RESPONSE)
        client = AgilePredictClient(base_url="http://example.test", region="M")
        rates = await client.fetch_export_rates()
        for r in rates:
            duration = (r.end - r.start).total_seconds()
            assert duration == 1800, f"slot duration {duration}s is not 30 min"
        # Contiguous
        for i in range(len(rates) - 1):
            assert rates[i].end == rates[i + 1].start

    @pytest.mark.asyncio
    async def test_returns_empty_on_http_error(self, httpx_mock):
        """Service down, 503, 404 - all return empty list, not exception."""
        httpx_mock.add_response(status_code=503)
        client = AgilePredictClient(base_url="http://example.test", region="M")
        rates = await client.fetch_export_rates()
        assert rates == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_malformed_json(self, httpx_mock):
        """Garbage response returns empty, not exception."""
        httpx_mock.add_response(text="not json")
        client = AgilePredictClient(base_url="http://example.test", region="M")
        rates = await client.fetch_export_rates()
        assert rates == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_region_not_present(self, httpx_mock):
        """If configured region has no data, return empty."""
        httpx_mock.add_response(json=SAMPLE_RESPONSE)
        # Region Z doesn't exist in the sample
        client = AgilePredictClient(base_url="http://example.test", region="Z")
        rates = await client.fetch_export_rates()
        assert rates == []

    @pytest.mark.asyncio
    async def test_uses_cache_within_cache_window(self, httpx_mock):
        """Second call within cache_hours serves from cache."""
        httpx_mock.add_response(json=SAMPLE_RESPONSE)
        client = AgilePredictClient(
            base_url="http://example.test", region="M", cache_hours=6,
        )
        first = await client.fetch_export_rates()
        second = await client.fetch_export_rates()
        assert len(httpx_mock.get_requests()) == 1
        assert first == second

    @pytest.mark.asyncio
    async def test_skips_malformed_price_entries(self, httpx_mock):
        """One bad record should not kill the whole response."""
        bad_response = [{
            "prices": [
                {"date_time": "2026-04-18T17:00:00+01:00",
                 "agile_pred": 25.0, "region": "M"},
                {"region": "M"},  # missing date_time and agile_pred
                {"date_time": "2026-04-18T17:30:00+01:00",
                 "agile_pred": 30.0, "region": "M"},
                {"date_time": "broken", "agile_pred": 99.0, "region": "M"},
            ]
        }]
        httpx_mock.add_response(json=bad_response)
        client = AgilePredictClient(base_url="http://example.test", region="M")
        rates = await client.fetch_export_rates()
        assert len(rates) == 2
        assert rates[0].rate_pence == pytest.approx(25.0)
        assert rates[1].rate_pence == pytest.approx(30.0)

    @pytest.mark.asyncio
    async def test_handles_empty_outer_array(self, httpx_mock):
        """Service returns [] during initial start-up."""
        httpx_mock.add_response(json=[])
        client = AgilePredictClient(base_url="http://example.test", region="M")
        rates = await client.fetch_export_rates()
        assert rates == []
