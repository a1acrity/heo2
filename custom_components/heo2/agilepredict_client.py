# custom_components/heo2/agilepredict_client.py
"""AgilePredict export rate forecast client. No Home Assistant imports."""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

import httpx

from .models import RateSlot

logger = logging.getLogger(__name__)


class AgilePredictClient:
    """Fetches Agile Outgoing export rate forecasts from AgilePredict."""

    def __init__(
        self,
        base_url: str = "https://agilepredict.com",
        cache_hours: int = 6,
    ):
        self._base_url = base_url.rstrip("/")
        self._cache_hours = cache_hours
        self._cache: list[RateSlot] | None = None
        self._cache_time: datetime | None = None

    async def fetch_export_rates(self) -> list[RateSlot]:
        """Fetch export rate forecast. Returns empty list on error."""
        if self._cache is not None and self._cache_time is not None:
            age = datetime.now(timezone.utc) - self._cache_time
            if age < timedelta(hours=self._cache_hours):
                return list(self._cache)

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self._base_url}/api/rates/export",
                    timeout=30.0,
                )
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, Exception) as exc:
            logger.warning("AgilePredict fetch failed: %s", exc)
            return []

        rates = self._parse_rates(data if isinstance(data, list) else [])
        self._cache = rates
        self._cache_time = datetime.now(timezone.utc)
        return list(rates)

    def _parse_rates(self, entries: list[dict]) -> list[RateSlot]:
        """Parse API response into RateSlot objects."""
        rates = []
        for entry in entries:
            try:
                valid_from = datetime.fromisoformat(
                    entry["valid_from"].replace("Z", "+00:00")
                )
                valid_to = datetime.fromisoformat(
                    entry["valid_to"].replace("Z", "+00:00")
                )
                rate_pence = float(entry.get("value_inc_vat", 0.0))
                rates.append(RateSlot(start=valid_from, end=valid_to, rate_pence=rate_pence))
            except (KeyError, ValueError) as exc:
                logger.debug("Skipping malformed rate entry: %s", exc)
                continue
        return rates
