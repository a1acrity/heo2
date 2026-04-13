# tests/test_agilepredict_client.py
"""Tests for AgilePredict HTTP client."""

import pytest
from datetime import datetime, timezone

from heo2.agilepredict_client import AgilePredictClient
from heo2.models import RateSlot


SAMPLE_RESPONSE = [
    {
        "valid_from": "2026-04-13T12:00:00Z",
        "valid_to": "2026-04-13T12:30:00Z",
        "value_inc_vat": 15.5,
    },
    {
        "valid_from": "2026-04-13T12:30:00Z",
        "valid_to": "2026-04-13T13:00:00Z",
        "value_inc_vat": 12.3,
    },
    {
        "valid_from": "2026-04-13T13:00:00Z",
        "valid_to": "2026-04-13T13:30:00Z",
        "value_inc_vat": 8.7,
    },
]


class TestAgilePredictClient:
    @pytest.mark.asyncio
    async def test_parses_rates_to_rate_slots(self, httpx_mock):
        httpx_mock.add_response(json=SAMPLE_RESPONSE)
        client = AgilePredictClient(base_url="https://agilepredict.example.com")
        rates = await client.fetch_export_rates()
        assert len(rates) == 3
        assert isinstance(rates[0], RateSlot)
        assert rates[0].rate_pence == pytest.approx(15.5)

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self, httpx_mock):
        httpx_mock.add_response(status_code=503)
        client = AgilePredictClient(base_url="https://agilepredict.example.com")
        rates = await client.fetch_export_rates()
        assert rates == []

    @pytest.mark.asyncio
    async def test_uses_cache(self, httpx_mock):
        httpx_mock.add_response(json=SAMPLE_RESPONSE)
        client = AgilePredictClient(
            base_url="https://agilepredict.example.com",
            cache_hours=6,
        )
        await client.fetch_export_rates()
        result = await client.fetch_export_rates()
        assert len(httpx_mock.get_requests()) == 1
        assert len(result) == 3
