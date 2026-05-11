"""WorldGatherer rate-reading tests + BDConfig + IGOConfig."""

from __future__ import annotations

from datetime import datetime, time, timezone

import pytest

from heo3.adapters.world import BDConfig, IGOConfig, WorldGatherer
from heo3.state_reader import MockStateReader


METER_KEY = "18p5009498_2372761090617"
EXPORT_METER_KEY = "18p5009498_2394300396097"


# ── BDConfig ───────────────────────────────────────────────────────


class TestBDConfigFromMeterKey:
    def test_entity_ids_derived(self):
        cfg = BDConfig.from_meter_key(METER_KEY)
        assert cfg.import_current_rate == (
            f"sensor.octopus_energy_electricity_{METER_KEY}_current_rate"
        )
        assert cfg.import_day_rates == (
            f"event.octopus_energy_electricity_{METER_KEY}_current_day_rates"
        )
        assert cfg.export_day_rates == (
            f"event.octopus_energy_electricity_{METER_KEY}_export_current_day_rates"
        )


# ── IGOConfig ──────────────────────────────────────────────────────


class TestIGOConfigDefaults:
    def test_spec_aligned_constants(self):
        c = IGOConfig()
        # Pinned by docs/SPEC.md §1, §10 (HEO II reference values).
        assert c.peak_pence == pytest.approx(24.8423)
        assert c.off_peak_pence == pytest.approx(4.9524)
        assert c.off_peak_start == time(23, 30)
        assert c.off_peak_end == time(5, 30)


# ── Rate parsing ───────────────────────────────────────────────────


def _bd_rates_attr() -> list[dict]:
    """Realistic BD attribute shape for event.*_current_day_rates."""
    return [
        {
            "start": "2026-05-10T00:00:00+01:00",
            "end": "2026-05-10T00:30:00+01:00",
            "value_inc_vat": 0.04952,
            "is_capped": False,
            "is_intelligent_adjusted": False,
        },
        {
            "start": "2026-05-10T00:30:00+01:00",
            "end": "2026-05-10T01:00:00+01:00",
            "value_inc_vat": 0.04952,
        },
    ]


def _gatherer(states=None, attributes=None, *, bd=True):
    return WorldGatherer(
        state_reader=MockStateReader(states or {}, attributes or {}),
        bd_config=BDConfig.from_meter_key(METER_KEY) if bd else None,
    )


class TestReadRatesLive:
    @pytest.mark.asyncio
    async def test_full_rates_parsed(self):
        cfg = BDConfig.from_meter_key(METER_KEY)
        states = {
            cfg.import_current_rate: "0.285844",  # GBP/kWh
            cfg.export_current_rate: "0.1134",
        }
        attrs = {
            cfg.import_day_rates: {"rates": _bd_rates_attr()},
            cfg.export_day_rates: {"rates": _bd_rates_attr()},
            cfg.import_current_rate: {"tariff_code": "E-1R-INTELLI-VAR-22-10-14-M"},
        }
        g = _gatherer(states, attrs)
        rates = await g.read_rates_live()

        assert rates.import_current_pence == pytest.approx(28.5844)
        assert rates.export_current_pence == pytest.approx(11.34)
        assert rates.tariff_code == "E-1R-INTELLI-VAR-22-10-14-M"

        assert len(rates.import_today) == 2
        first = rates.import_today[0]
        assert first.start == datetime(2026, 5, 9, 23, 0, tzinfo=timezone.utc)
        assert first.rate_pence == pytest.approx(4.952)
        assert first.end == datetime(2026, 5, 9, 23, 30, tzinfo=timezone.utc)

    @pytest.mark.asyncio
    async def test_no_bd_config_returns_empty(self):
        g = _gatherer(bd=False)
        rates = await g.read_rates_live()
        assert rates.import_today == ()
        assert rates.import_current_pence is None

    @pytest.mark.asyncio
    async def test_missing_attribute_returns_empty_list(self):
        g = _gatherer({})  # no rates attr → empty
        rates = await g.read_rates_live()
        assert rates.import_today == ()
        assert rates.export_today == ()

    @pytest.mark.asyncio
    async def test_malformed_rate_entries_skipped(self):
        cfg = BDConfig.from_meter_key(METER_KEY)
        attrs = {
            cfg.import_day_rates: {
                "rates": [
                    {"start": "garbage", "end": "x", "value_inc_vat": 0.1},
                    {  # valid
                        "start": "2026-05-10T00:00:00+01:00",
                        "end": "2026-05-10T00:30:00+01:00",
                        "value_inc_vat": 0.05,
                    },
                ]
            },
        }
        g = _gatherer({}, attrs)
        rates = await g.read_rates_live()
        # Only the valid one survived.
        assert len(rates.import_today) == 1


class TestReadRatesFreshness:
    @pytest.mark.asyncio
    async def test_event_state_iso_parsed(self):
        cfg = BDConfig.from_meter_key(METER_KEY)
        states = {
            cfg.import_day_rates: "2026-05-10T15:19:59.663+00:00",
            cfg.export_day_rates: "2026-05-10T15:19:59.664+00:00",
        }
        g = _gatherer(states)
        freshness = await g.read_rates_freshness()
        assert "import_today" in freshness
        assert "export_today" in freshness
        assert freshness["import_today"].tzinfo is not None
        assert freshness["import_today"].year == 2026

    @pytest.mark.asyncio
    async def test_no_bd_returns_empty(self):
        g = _gatherer(bd=False)
        assert await g.read_rates_freshness() == {}

    @pytest.mark.asyncio
    async def test_unparseable_state_omitted(self):
        cfg = BDConfig.from_meter_key(METER_KEY)
        g = _gatherer({cfg.import_day_rates: "garbage"})
        freshness = await g.read_rates_freshness()
        assert "import_today" not in freshness


class TestIGOAccessor:
    def test_igo_property_returns_config(self):
        g = _gatherer()
        assert isinstance(g.igo, IGOConfig)
        assert g.igo.peak_pence == pytest.approx(24.8423)


# ── PredictedRates (AgilePredict) ──────────────────────────────────


class _FakeAgilePredict:
    """Predictable test double for AgilePredictClient.fetch_export_rates."""

    def __init__(self, rates):
        self._rates = rates

    async def fetch_export_rates(self):
        return list(self._rates)


class TestReadRatesPredicted:
    @pytest.mark.asyncio
    async def test_no_client_returns_empty(self):
        g = _gatherer()
        predicted = await g.read_rates_predicted()
        assert predicted.export_pence == ()
        assert predicted.import_pence == ()

    @pytest.mark.asyncio
    async def test_passes_through_export_rates(self):
        from heo3.types import RatePeriod

        sample = [
            RatePeriod(
                start=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
                end=datetime(2026, 5, 10, 12, 30, tzinfo=timezone.utc),
                rate_pence=15.5,
            )
        ]
        g = WorldGatherer(
            state_reader=MockStateReader(),
            agilepredict_client=_FakeAgilePredict(sample),
        )
        predicted = await g.read_rates_predicted()
        assert predicted.export_pence == tuple(sample)
        assert predicted.import_pence == ()
