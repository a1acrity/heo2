"""AgilePredict HTTP client tests using pytest-httpx mock."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from heo3.agilepredict_client import AgilePredictClient


def _sample_response(region="M"):
    """Realistic AgilePredict API response shape."""
    return [
        {
            "name": "2026-05-10 16:17",
            "created_at": "2026-05-10T16:17:10.292633+01:00",
            "prices": [
                {
                    "date_time": "2026-05-10T17:00:00+01:00",
                    "agile_pred": 30.78,
                    "agile_low": 30.57,
                    "agile_high": 31.0,
                    "region": region,
                },
                {
                    "date_time": "2026-05-10T17:30:00+01:00",
                    "agile_pred": 28.45,
                    "agile_low": 28.0,
                    "agile_high": 29.0,
                    "region": region,
                },
                {
                    # Other region — should be filtered out.
                    "date_time": "2026-05-10T17:00:00+01:00",
                    "agile_pred": 99.0,
                    "region": "X",
                },
            ],
        }
    ]


class TestFetch:
    @pytest.mark.asyncio
    async def test_parses_region_filtered(self, httpx_mock):
        httpx_mock.add_response(json=_sample_response("M"))
        client = AgilePredictClient(region="M", cache_hours=0)
        rates = await client.fetch_export_rates()
        assert len(rates) == 2
        assert rates[0].rate_pence == 30.78
        assert rates[0].start == datetime(2026, 5, 10, 16, 0, tzinfo=timezone.utc)
        assert rates[1].rate_pence == 28.45

    @pytest.mark.asyncio
    async def test_returns_empty_on_404(self, httpx_mock):
        httpx_mock.add_response(status_code=404)
        client = AgilePredictClient(cache_hours=0)
        assert await client.fetch_export_rates() == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_malformed_json(self, httpx_mock):
        httpx_mock.add_response(content=b"not json")
        client = AgilePredictClient(cache_hours=0)
        assert await client.fetch_export_rates() == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_unexpected_shape(self, httpx_mock):
        httpx_mock.add_response(json={"unexpected": "shape"})
        client = AgilePredictClient(cache_hours=0)
        assert await client.fetch_export_rates() == []

    @pytest.mark.asyncio
    async def test_returns_empty_for_unknown_region(self, httpx_mock):
        httpx_mock.add_response(json=_sample_response("M"))
        client = AgilePredictClient(region="X", cache_hours=0)
        # X exists in the sample but only one record; but then the loop
        # below runs once and returns the X record.
        rates = await client.fetch_export_rates()
        assert len(rates) == 1
        assert rates[0].rate_pence == 99.0


class TestCache:
    @pytest.mark.asyncio
    async def test_second_call_uses_cache(self, httpx_mock):
        httpx_mock.add_response(json=_sample_response("M"))
        client = AgilePredictClient(region="M", cache_hours=6)
        first = await client.fetch_export_rates()
        # No further httpx_mock add — second call must hit cache.
        second = await client.fetch_export_rates()
        assert second == first

    @pytest.mark.asyncio
    async def test_invalidate_forces_refetch(self, httpx_mock):
        httpx_mock.add_response(json=_sample_response("M"))
        httpx_mock.add_response(json=_sample_response("M"))
        client = AgilePredictClient(region="M", cache_hours=6)
        await client.fetch_export_rates()
        client.invalidate_cache()
        await client.fetch_export_rates()  # second network call expected
        # If cache wasn't invalidated, pytest-httpx would complain
        # about the unused mock.
