"""WorldGatherer forecast tests — Solcast + HEO-5 load model."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from heo3.adapters.world import (
    LoadModelConfig,
    MockLoadHistoryReader,
    SolcastConfig,
    WorldGatherer,
)
from heo3.solar_forecast import solar_forecast_from_hacs
from heo3.state_reader import MockStateReader


TZ = ZoneInfo("Europe/London")


# ── solar_forecast_from_hacs (port of heo2 helper) ─────────────────


def _solcast_entries(target: date, key_value_pairs: list[tuple[int, float]]):
    return [
        {
            "period_start": datetime(
                target.year, target.month, target.day, h, 0, tzinfo=TZ
            ).isoformat(),
            "pv_estimate": v,
            "pv_estimate10": v * 0.7,
            "pv_estimate90": v * 1.3,
        }
        for h, v in key_value_pairs
    ]


class TestSolarForecastFromHacs:
    def test_24_buckets_filled(self):
        target = date(2026, 5, 10)
        entries = _solcast_entries(target, [(h, float(h)) for h in range(24)])
        out = solar_forecast_from_hacs(entries, target)
        assert len(out) == 24
        assert out[0] == 0.0
        assert out[12] == 12.0
        assert out[23] == 23.0

    def test_other_dates_filtered(self):
        target = date(2026, 5, 10)
        entries = _solcast_entries(target, [(11, 5.0)])
        # Add an entry on the next day — shouldn't appear in target's buckets.
        entries.append(
            {
                "period_start": datetime(2026, 5, 11, 11, 0, tzinfo=TZ).isoformat(),
                "pv_estimate": 99.0,
            }
        )
        out = solar_forecast_from_hacs(entries, target)
        assert out[11] == 5.0
        # 99.0 didn't leak into the target day's hour 11.

    def test_p10_p90_keys(self):
        target = date(2026, 5, 10)
        entries = _solcast_entries(target, [(11, 10.0)])
        p10 = solar_forecast_from_hacs(entries, target, "pv_estimate10")
        p90 = solar_forecast_from_hacs(entries, target, "pv_estimate90")
        assert p10[11] == pytest.approx(7.0)
        assert p90[11] == pytest.approx(13.0)

    def test_malformed_entries_skipped(self):
        target = date(2026, 5, 10)
        entries = [
            {"period_start": "garbage"},
            {"period_start": datetime(2026, 5, 10, 9, 0, tzinfo=TZ).isoformat(), "pv_estimate": 4.0},
        ]
        out = solar_forecast_from_hacs(entries, target)
        assert out[9] == 4.0


# ── WorldGatherer.read_solar_forecast ──────────────────────────────


class TestReadSolarForecast:
    @pytest.mark.asyncio
    async def test_no_state_reader_returns_empty(self):
        g = WorldGatherer()
        f = await g.read_solar_forecast()
        assert f.today_p50_kwh == ()
        assert f.tomorrow_p50_kwh == ()
        assert f.last_updated is None

    @pytest.mark.asyncio
    async def test_full_forecast_parsed(self, monkeypatch):
        cfg = SolcastConfig()
        # Use today/tomorrow in UK time.
        now_uk = datetime.now(TZ).date()
        tomorrow_uk = now_uk + timedelta(days=1)
        attrs = {
            cfg.forecast_today: {
                "detailedHourly": _solcast_entries(now_uk, [(11, 4.5), (12, 6.2)])
            },
            cfg.forecast_tomorrow: {
                "detailedHourly": _solcast_entries(tomorrow_uk, [(13, 5.0)])
            },
        }
        states = {cfg.api_last_polled: "2026-05-10T04:45:37+00:00"}
        g = WorldGatherer(
            state_reader=MockStateReader(states, attrs),
            local_tz="Europe/London",
        )
        f = await g.read_solar_forecast()
        assert f.today_p50_kwh[11] == 4.5
        assert f.today_p50_kwh[12] == 6.2
        assert f.tomorrow_p50_kwh[13] == 5.0
        assert f.last_updated is not None
        assert f.last_updated.year == 2026


# ── LoadForecast (HEO-5 model) ─────────────────────────────────────


class TestReadLoadForecast:
    @pytest.mark.asyncio
    async def test_no_history_returns_empty(self):
        g = WorldGatherer(state_reader=MockStateReader())
        f = await g.read_load_forecast()
        assert f.today_hourly_kwh == ()
        assert f.tomorrow_hourly_kwh == ()
        assert isinstance(f.day_of_week, int)

    @pytest.mark.asyncio
    async def test_history_produces_24_buckets(self):
        # Synthesise 14 days of evenly-spaced power samples so the
        # aggregator has meaningful data.
        start = datetime.now(TZ) - timedelta(days=14)
        samples = []
        for d in range(14):
            day_start = start + timedelta(days=d)
            for h in range(24):
                samples.append(
                    (day_start + timedelta(hours=h), 1500.0)  # 1.5 kW constant
                )
        # Sentinel sample at end of last day so trapezoidal sees a closing edge.
        samples.append((start + timedelta(days=14), 1500.0))

        history = MockLoadHistoryReader(samples)
        g = WorldGatherer(
            state_reader=MockStateReader(),
            load_history_reader=history,
            load_model_config=LoadModelConfig(
                source_type="power_watts", learn_days=14
            ),
            local_tz="Europe/London",
        )
        f = await g.read_load_forecast()
        assert len(f.today_hourly_kwh) == 24
        assert len(f.tomorrow_hourly_kwh) == 24
        # 1.5 kW for 1 hour = 1.5 kWh per bucket. Within rounding.
        for kwh in f.today_hourly_kwh:
            assert kwh == pytest.approx(1.5, abs=0.05)

    @pytest.mark.asyncio
    async def test_baseline_used_when_no_data_for_hour(self):
        # No samples at all → builder uses baseline_w (1900 W → 1.9 kWh).
        g = WorldGatherer(
            state_reader=MockStateReader(),
            load_history_reader=MockLoadHistoryReader([]),
            load_model_config=LoadModelConfig(baseline_w=1900.0),
        )
        f = await g.read_load_forecast()
        assert all(kwh == pytest.approx(1.9) for kwh in f.today_hourly_kwh)
