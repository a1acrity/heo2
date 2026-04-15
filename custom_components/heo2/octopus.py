"""Octopus Energy billing client. No Home Assistant imports."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

OCTOPUS_BASE_URL = "https://api.octopus.energy/v1"


class OctopusBillingFetcher:
    """Fetches consumption data from Octopus Energy and calculates monthly bill."""

    def __init__(self, api_key: str, mpan: str, serial: str, product_code: str, tariff_code: str) -> None:
        self._api_key = api_key
        self._mpan = mpan
        self._serial = serial
        self._product_code = product_code
        self._tariff_code = tariff_code
        self.monthly_bill: float = 0.0
        self.last_month_bill: float = 0.0

    async def fetch_monthly_bill(self, now: datetime) -> float:
        """Fetch consumption since start of month and calculate bill in GBP. Returns 0.0 on error."""
        start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        try:
            async with httpx.AsyncClient() as client:
                consumption_resp = await client.get(
                    f"{OCTOPUS_BASE_URL}/electricity-meter-points/{self._mpan}"
                    f"/meters/{self._serial}/consumption/",
                    params={"period_from": start_of_month.isoformat(), "page_size": 25000},
                    auth=(self._api_key, ""),
                    timeout=30.0,
                )
                consumption_resp.raise_for_status()
                consumption_data = consumption_resp.json().get("results", [])

                rates_resp = await client.get(
                    f"{OCTOPUS_BASE_URL}/products/{self._product_code}"
                    f"/electricity-tariffs/{self._tariff_code}/standard-unit-rates/",
                    params={"period_from": start_of_month.isoformat(), "page_size": 25000},
                    timeout=30.0,
                )
                rates_resp.raise_for_status()
                rates_data = rates_resp.json().get("results", [])

        except (httpx.HTTPError, Exception) as exc:
            logger.warning("Octopus API fetch failed: %s", exc)
            return 0.0

        bill = self._calculate_bill(consumption_data, rates_data)
        self.monthly_bill = bill
        return bill

    @staticmethod
    def _calculate_bill(consumption: list[dict], rates: list[dict]) -> float:
        """Match each consumption interval to its rate and sum the cost. Returns total in GBP."""
        rate_lookup: dict[str, float] = {}
        for rate in rates:
            valid_from = rate.get("valid_from", "")
            rate_lookup[valid_from] = rate.get("value_inc_vat", 0.0)

        total_pence = 0.0
        for entry in consumption:
            kwh = entry.get("consumption", 0.0)
            interval_start = entry.get("interval_start", "")
            rate_pence = rate_lookup.get(interval_start, 0.0)
            total_pence += kwh * rate_pence

        return total_pence / 100.0

    def snapshot_month_end(self) -> None:
        """Call on the 1st of a new month to save previous month's bill."""
        self.last_month_bill = self.monthly_bill
        self.monthly_bill = 0.0
