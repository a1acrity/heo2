"""World Gatherer — read-only collation of HA entities.

Rates (BD + IGO + AgilePredict), forecasts (Solcast + HEO-5),
flags (IGO dispatch, saving session, EPS, temperature alarms).

P1.0 stub. Full implementation across P1.4 / P1.5 / P1.6.
"""

from __future__ import annotations

from datetime import datetime

from ..types import (
    LiveRates,
    LoadForecast,
    PredictedRates,
    SolarForecast,
    SystemFlags,
)


class WorldGatherer:
    """One pass over external HA state per snapshot tick.

    All values are read from HA entities — never from the network
    directly. The integrations (BottlecapDave, Solcast, octopus_energy,
    teslemetry, etc.) handle the upstream calls; this layer just
    collates their state into the operator's typed view.
    """

    def __init__(self, hass) -> None:  # type: ignore[no-untyped-def]
        self._hass = hass

    async def read_rates_live(self) -> LiveRates:
        raise NotImplementedError("P1.4 — World Gatherer rates")

    async def read_rates_predicted(self) -> PredictedRates:
        raise NotImplementedError("P1.4 — World Gatherer rates")

    async def read_rates_freshness(self) -> dict[str, datetime]:
        raise NotImplementedError("P1.4 — World Gatherer rates")

    async def read_solar_forecast(self) -> SolarForecast:
        raise NotImplementedError("P1.5 — World Gatherer forecasts")

    async def read_load_forecast(self) -> LoadForecast:
        raise NotImplementedError("P1.5 — World Gatherer forecasts")

    async def read_flags(self) -> SystemFlags:
        raise NotImplementedError("P1.6 — World Gatherer flags")
