"""Tests for Octopus Energy billing client."""

import pytest
from datetime import datetime, timezone

from heo2.octopus import OctopusBillingFetcher


SAMPLE_CONSUMPTION = {
    "results": [
        {"consumption": 10.5, "interval_start": "2026-04-01T00:00:00Z", "interval_end": "2026-04-01T00:30:00Z"},
        {"consumption": 8.2, "interval_start": "2026-04-01T00:30:00Z", "interval_end": "2026-04-01T01:00:00Z"},
    ],
    "count": 2, "next": None, "previous": None,
}

SAMPLE_RATES = {
    "results": [
        {"value_inc_vat": 27.88, "valid_from": "2026-04-01T00:00:00Z", "valid_to": "2026-04-01T00:30:00Z"},
        {"value_inc_vat": 25.50, "valid_from": "2026-04-01T00:30:00Z", "valid_to": "2026-04-01T01:00:00Z"},
    ],
    "count": 2, "next": None, "previous": None,
}


class TestOctopusBillingFetcher:
    @pytest.mark.asyncio
    async def test_calculates_monthly_bill(self, httpx_mock):
        httpx_mock.add_response(json=SAMPLE_CONSUMPTION)
        httpx_mock.add_response(json=SAMPLE_RATES)
        fetcher = OctopusBillingFetcher(
            api_key="test_key", mpan="1234567890", serial="ABC123",
            product_code="AGILE-FLEX-22-11-25", tariff_code="E-1R-AGILE-FLEX-22-11-25-C",
        )
        bill = await fetcher.fetch_monthly_bill(now=datetime(2026, 4, 15, 6, 0, tzinfo=timezone.utc))
        assert bill > 0.0

    @pytest.mark.asyncio
    async def test_returns_zero_on_http_error(self, httpx_mock):
        httpx_mock.add_response(status_code=401)
        fetcher = OctopusBillingFetcher(
            api_key="bad_key", mpan="1234567890", serial="ABC123",
            product_code="AGILE-FLEX-22-11-25", tariff_code="E-1R-AGILE-FLEX-22-11-25-C",
        )
        bill = await fetcher.fetch_monthly_bill(now=datetime(2026, 4, 15, 6, 0, tzinfo=timezone.utc))
        assert bill == 0.0

    @pytest.mark.asyncio
    async def test_empty_consumption_returns_zero(self, httpx_mock):
        httpx_mock.add_response(json={"results": [], "count": 0, "next": None, "previous": None})
        httpx_mock.add_response(json=SAMPLE_RATES)
        fetcher = OctopusBillingFetcher(
            api_key="test_key", mpan="1234567890", serial="ABC123",
            product_code="AGILE-FLEX-22-11-25", tariff_code="E-1R-AGILE-FLEX-22-11-25-C",
        )
        bill = await fetcher.fetch_monthly_bill(now=datetime(2026, 4, 15, 6, 0, tzinfo=timezone.utc))
        assert bill == 0.0

    def test_calculates_bill_from_consumption_and_rates(self):
        consumption = [
            {"consumption": 10.0, "interval_start": "2026-04-01T00:00:00Z"},
            {"consumption": 5.0, "interval_start": "2026-04-01T00:30:00Z"},
        ]
        rates = [
            {"value_inc_vat": 20.0, "valid_from": "2026-04-01T00:00:00Z", "valid_to": "2026-04-01T00:30:00Z"},
            {"value_inc_vat": 30.0, "valid_from": "2026-04-01T00:30:00Z", "valid_to": "2026-04-01T01:00:00Z"},
        ]
        bill = OctopusBillingFetcher._calculate_bill(consumption, rates)
        assert bill == pytest.approx(3.50, abs=0.01)
