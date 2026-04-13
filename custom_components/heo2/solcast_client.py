# custom_components/heo2/solcast_client.py
"""Solcast PV forecast HTTP client. No Home Assistant imports."""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

import httpx

logger = logging.getLogger(__name__)

SOLCAST_BASE_URL = "https://api.solcast.com.au"


class SolcastClient:
    """Fetches PV forecast from Solcast and converts to 24 hourly kWh buckets."""

    def __init__(
        self,
        api_key: str,
        resource_id: str,
        cache_hours: int = 48,
    ):
        self._api_key = api_key
        self._resource_id = resource_id
        self._cache_hours = cache_hours
        self._cache: list[float] | None = None
        self._cache_time: datetime | None = None

    async def fetch_forecast(self) -> list[float]:
        """Fetch forecast, returning 24 hourly kWh values (index 0 = 00:00 UTC).

        Returns zeros on error (conservative fallback: assume no solar).
        """
        if self._cache is not None and self._cache_time is not None:
            age = datetime.now(timezone.utc) - self._cache_time
            if age < timedelta(hours=self._cache_hours):
                return list(self._cache)

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{SOLCAST_BASE_URL}/rooftop_sites/{self._resource_id}/forecasts",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    params={"format": "json"},
                    timeout=30.0,
                )
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, Exception) as exc:
            logger.warning("Solcast fetch failed: %s", exc)
            return [0.0] * 24

        hourly = self._aggregate_to_hourly(data.get("forecasts", []))
        self._cache = hourly
        self._cache_time = datetime.now(timezone.utc)
        return list(hourly)

    def _aggregate_to_hourly(self, forecasts: list[dict]) -> list[float]:
        """Convert 30-min Solcast periods into 24 hourly kWh buckets."""
        hourly = [0.0] * 24

        for entry in forecasts:
            period_end_str = entry.get("period_end", "")
            pv_kw = entry.get("pv_estimate", 0.0)

            try:
                # Handle the Solcast timestamp format with many decimal places
                clean = period_end_str.replace("Z", "+00:00")
                # Strip excess decimal places
                if "." in clean:
                    parts = clean.split(".")
                    tz_part = ""
                    decimal = parts[1]
                    for i, c in enumerate(decimal):
                        if not c.isdigit():
                            tz_part = decimal[i:]
                            decimal = decimal[:i]
                            break
                    clean = parts[0] + "." + decimal[:6] + tz_part
                period_end = datetime.fromisoformat(clean)
            except (ValueError, AttributeError):
                continue

            # period_end is the END of a 30-min period
            # Assign to the hour of the period start
            period_start = period_end - timedelta(minutes=30)
            hour = period_start.hour
            if 0 <= hour < 24:
                hourly[hour] += pv_kw

        return hourly
