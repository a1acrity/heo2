"""AgilePredict export rate forecast client.

AgilePredict (http://agilepredict.com) publishes half-hourly Agile-
Outgoing-like rate forecasts for every GB DNO region. This client
fetches the response, filters to one region, and returns a list of
30-minute RatePeriod objects with UTC-aware boundaries.

Per SPEC H4: AgilePredict output NEVER reaches the inverter — it's
visualisation-only fodder for daily-plan rendering.

Ported from heo2/agilepredict_client.py — same external API, uses
HEO III's RatePeriod type. Pure HTTP client; no HA imports.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from .types import RatePeriod

logger = logging.getLogger(__name__)

# Default DNO region. M = NGED Yorkshire (Hull).
# A Eastern, B East Midlands, C London, D Merseyside & N Wales,
# E West Midlands, F North Eastern, G North Western, H Southern,
# J South Eastern, K Southern Western, L South Western, M Yorkshire,
# N Southern Scotland, P Northern Scotland, X national average.
DEFAULT_REGION = "M"


class AgilePredictClient:
    """Fetches Agile Outgoing export rate forecasts from AgilePredict.

    Construct once, call `fetch_export_rates()` repeatedly. Results
    are cached in memory for `cache_hours` to avoid hammering the
    service. Returns an empty list on any error so the WorldGatherer
    can always tick.
    """

    def __init__(
        self,
        base_url: str = "https://agilepredict.com",
        region: str = DEFAULT_REGION,
        cache_hours: int = 6,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._region = region
        self._cache_hours = cache_hours
        self._cache: list[RatePeriod] | None = None
        self._cache_time: datetime | None = None

    async def fetch_export_rates(self) -> list[RatePeriod]:
        if self._cache is not None and self._cache_time is not None:
            age = datetime.now(timezone.utc) - self._cache_time
            if age < timedelta(hours=self._cache_hours):
                return list(self._cache)

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self._base_url}/api/",
                    timeout=30.0,
                )
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("AgilePredict fetch failed: %s", exc)
            return []
        except Exception as exc:  # pragma: no cover
            logger.warning("AgilePredict unexpected error: %s", exc)
            return []

        rates = self._parse_rates(data)
        self._cache = rates
        self._cache_time = datetime.now(timezone.utc)
        return list(rates)

    def _parse_rates(self, data: list) -> list[RatePeriod]:
        """Region-filter and parse the AgilePredict response."""
        if not isinstance(data, list) or not data:
            return []
        first = data[0]
        if not isinstance(first, dict):
            return []
        prices = first.get("prices", [])
        if not isinstance(prices, list):
            return []

        rates: list[RatePeriod] = []
        for entry in prices:
            if not isinstance(entry, dict):
                continue
            if entry.get("region") != self._region:
                continue
            ts = entry.get("date_time")
            pence = entry.get("agile_pred")
            if ts is None or pence is None:
                continue
            try:
                start_local = datetime.fromisoformat(str(ts))
                start_utc = start_local.astimezone(timezone.utc)
                end_utc = start_utc + timedelta(minutes=30)
                rate_pence = float(pence)
            except (ValueError, TypeError) as exc:
                logger.debug(
                    "Skipping malformed AgilePredict entry %r: %s", entry, exc
                )
                continue
            rates.append(
                RatePeriod(start=start_utc, end=end_utc, rate_pence=rate_pence)
            )

        rates.sort(key=lambda r: r.start)
        return rates

    def invalidate_cache(self) -> None:
        """Force the next fetch to hit the network. For tests + manual ops."""
        self._cache = None
        self._cache_time = None
