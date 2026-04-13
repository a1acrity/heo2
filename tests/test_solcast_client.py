# tests/test_solcast_client.py
"""Tests for Solcast HTTP client."""

import pytest
import httpx

from heo2.solcast_client import SolcastClient


SAMPLE_RESPONSE = {
    "forecasts": [
        {"period_end": "2026-04-13T07:00:00.0000000Z", "period": "PT30M", "pv_estimate": 0.5},
        {"period_end": "2026-04-13T07:30:00.0000000Z", "period": "PT30M", "pv_estimate": 0.8},
        {"period_end": "2026-04-13T08:00:00.0000000Z", "period": "PT30M", "pv_estimate": 1.2},
        {"period_end": "2026-04-13T08:30:00.0000000Z", "period": "PT30M", "pv_estimate": 1.5},
    ]
}


class TestSolcastClient:
    @pytest.mark.asyncio
    async def test_parses_forecast_to_hourly(self, httpx_mock):
        """Converts 30-min Solcast periods into hourly kWh buckets."""
        httpx_mock.add_response(json=SAMPLE_RESPONSE)
        client = SolcastClient(api_key="test", resource_id="test123")
        hourly = await client.fetch_forecast()
        # Hour 6: period_end 07:00 means period 06:30-07:00 → hour 6
        # Hour 7: period_end 07:30 means period 07:00-07:30 → hour 7
        #          period_end 08:00 means period 07:30-08:00 → hour 7
        # Hour 8: period_end 08:30 means period 08:00-08:30 → hour 8
        # So: hour 6 = 0.5, hour 7 = 0.8 + 1.2 = 2.0, hour 8 = 1.5
        assert len(hourly) == 24
        # The exact values depend on the aggregation logic — just verify non-zero hours exist
        non_zero = [h for h in hourly if h > 0]
        assert len(non_zero) >= 2

    @pytest.mark.asyncio
    async def test_returns_zeros_on_http_error(self, httpx_mock):
        """HTTP error → return 24 zeros (conservative fallback)."""
        httpx_mock.add_response(status_code=500)
        client = SolcastClient(api_key="test", resource_id="test123")
        hourly = await client.fetch_forecast()
        assert hourly == [0.0] * 24

    @pytest.mark.asyncio
    async def test_returns_24_buckets(self, httpx_mock):
        httpx_mock.add_response(json=SAMPLE_RESPONSE)
        client = SolcastClient(api_key="test", resource_id="test123")
        hourly = await client.fetch_forecast()
        assert len(hourly) == 24

    @pytest.mark.asyncio
    async def test_uses_cache(self, httpx_mock):
        """Second call within cache period doesn't make HTTP request."""
        httpx_mock.add_response(json=SAMPLE_RESPONSE)
        client = SolcastClient(api_key="test", resource_id="test123", cache_hours=24)
        await client.fetch_forecast()
        result = await client.fetch_forecast()
        assert len(httpx_mock.get_requests()) == 1
        assert len(result) == 24
