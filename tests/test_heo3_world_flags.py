"""WorldGatherer.read_flags + EPS detector tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from heo3.adapters.world import (
    FlagsConfig,
    WorldGatherer,
    _EPSDetector,
)
from heo3.state_reader import MockStateReader


# ── EPS detector unit tests ────────────────────────────────────────


class TestEPSDetector:
    def test_voltage_present_no_eps(self):
        d = _EPSDetector()
        assert d.update(240.0, datetime.now(timezone.utc)) is False

    def test_voltage_zero_brief_no_eps(self):
        d = _EPSDetector(debounce_s=5.0)
        t0 = datetime(2026, 5, 10, 18, 0, 0, tzinfo=timezone.utc)
        assert d.update(0.0, t0) is False
        # Same instant — still not 5s
        assert d.update(0.0, t0 + timedelta(seconds=2)) is False

    def test_voltage_zero_for_5s_triggers(self):
        d = _EPSDetector(debounce_s=5.0)
        t0 = datetime(2026, 5, 10, 18, 0, 0, tzinfo=timezone.utc)
        d.update(0.0, t0)
        # 5s later it triggers.
        assert d.update(0.0, t0 + timedelta(seconds=5)) is True

    def test_voltage_recovers_resets(self):
        d = _EPSDetector(debounce_s=5.0)
        t0 = datetime(2026, 5, 10, 18, 0, 0, tzinfo=timezone.utc)
        d.update(0.0, t0)
        # Voltage comes back at 3s — clears the timer.
        assert d.update(240.0, t0 + timedelta(seconds=3)) is False
        # Next zero starts fresh.
        d.update(0.0, t0 + timedelta(seconds=4))
        # 4s after that is only 4s of zero — not enough.
        assert d.update(0.0, t0 + timedelta(seconds=8)) is False
        # 5s after the new start triggers.
        assert d.update(0.0, t0 + timedelta(seconds=9)) is True

    def test_voltage_unknown_resets(self):
        d = _EPSDetector(debounce_s=5.0)
        t0 = datetime(2026, 5, 10, 18, 0, 0, tzinfo=timezone.utc)
        d.update(0.0, t0)
        # Sensor goes unknown — treat as not-EPS (don't accumulate).
        assert d.update(None, t0 + timedelta(seconds=3)) is False
        # Even after another 5s of None, still no EPS (we don't know).
        assert d.update(None, t0 + timedelta(seconds=10)) is False

    def test_threshold_voltage_treated_as_zero(self):
        d = _EPSDetector(debounce_s=5.0, threshold_v=10.0)
        t0 = datetime(2026, 5, 10, 18, 0, 0, tzinfo=timezone.utc)
        d.update(5.0, t0)  # below threshold counts as "0"
        assert d.update(5.0, t0 + timedelta(seconds=5)) is True


# ── read_flags ─────────────────────────────────────────────────────


class TestReadFlagsEmpty:
    @pytest.mark.asyncio
    async def test_no_state_reader_returns_defaults(self):
        g = WorldGatherer()
        flags = await g.read_flags()
        assert flags.eps_active is False
        assert flags.grid_connected is True
        assert flags.igo_dispatching is None
        assert flags.saving_session_active is None
        assert flags.defer_ev_eligible is False


class TestIGOFlags:
    @pytest.mark.asyncio
    async def test_dispatching_and_planned(self):
        cfg = FlagsConfig(
            igo_dispatching_entity="binary_sensor.octopus_intelligent_dispatching"
        )
        attrs = {
            cfg.igo_dispatching_entity: {
                "planned_dispatches": [
                    {
                        "start": "2026-05-11T00:00:00+00:00",
                        "end": "2026-05-11T03:00:00+00:00",
                        "charge_in_kwh": 7.5,
                        "source": "smart-charge",
                    },
                ]
            }
        }
        g = WorldGatherer(
            state_reader=MockStateReader(
                {cfg.igo_dispatching_entity: "on"}, attrs
            ),
            flags_config=cfg,
        )
        flags = await g.read_flags()
        assert flags.igo_dispatching is True
        assert len(flags.igo_planned) == 1
        assert flags.igo_planned[0].charge_kwh == 7.5
        assert flags.igo_planned[0].source == "smart-charge"

    @pytest.mark.asyncio
    async def test_malformed_planned_entries_skipped(self):
        cfg = FlagsConfig(igo_dispatching_entity="binary_sensor.x")
        attrs = {
            cfg.igo_dispatching_entity: {
                "planned_dispatches": [
                    {"start": "garbage", "end": "x"},
                    {  # valid
                        "start": "2026-05-11T00:00:00+00:00",
                        "end": "2026-05-11T03:00:00+00:00",
                    },
                ]
            }
        }
        g = WorldGatherer(
            state_reader=MockStateReader({}, attrs),
            flags_config=cfg,
        )
        flags = await g.read_flags()
        assert len(flags.igo_planned) == 1


class TestSavingSession:
    @pytest.mark.asyncio
    async def test_active_session_with_window(self):
        cfg = FlagsConfig(
            saving_session_entity="binary_sensor.octoplus_saving_sessions"
        )
        attrs = {
            cfg.saving_session_entity: {
                "current_session_start": "2026-05-10T17:00:00+00:00",
                "current_session_end": "2026-05-10T18:00:00+00:00",
                "octoplus_session_rewards_pence_per_kwh": 350.0,
            }
        }
        g = WorldGatherer(
            state_reader=MockStateReader(
                {cfg.saving_session_entity: "on"}, attrs
            ),
            flags_config=cfg,
        )
        flags = await g.read_flags()
        assert flags.saving_session_active is True
        assert flags.saving_session_window is not None
        assert flags.saving_session_window.start.hour == 17
        assert flags.saving_session_price_pence == 350.0


class TestTemperatureAlarms:
    @pytest.mark.asyncio
    async def test_inverter_alarm_above_threshold(self):
        cfg = FlagsConfig(inverter_temperature_alarm_c=65.0)
        g = WorldGatherer(
            state_reader=MockStateReader(
                {cfg.inverter_temperature_entity: "70"}
            ),
            flags_config=cfg,
        )
        flags = await g.read_flags()
        assert flags.inverter_temperature_alarm is True

    @pytest.mark.asyncio
    async def test_battery_alarm_outside_safe_range(self):
        cfg = FlagsConfig(
            battery_temperature_min_c=5.0, battery_temperature_max_c=50.0
        )
        # Too cold:
        g = WorldGatherer(
            state_reader=MockStateReader(
                {cfg.battery_temperature_entity: "2"}
            ),
            flags_config=cfg,
        )
        flags = await g.read_flags()
        assert flags.battery_temperature_alarm is True

        # In range:
        g2 = WorldGatherer(
            state_reader=MockStateReader(
                {cfg.battery_temperature_entity: "25"}
            ),
            flags_config=cfg,
        )
        assert (await g2.read_flags()).battery_temperature_alarm is False


class TestEPSIntegration:
    @pytest.mark.asyncio
    async def test_grid_voltage_at_zero_then_5s(self):
        cfg = FlagsConfig(eps_debounce_s=5.0)
        reader = MockStateReader({cfg.grid_voltage_entity: "0"})
        g = WorldGatherer(state_reader=reader, flags_config=cfg)

        t0 = datetime(2026, 5, 10, 18, 0, 0, tzinfo=timezone.utc)
        first = await g.read_flags(now=t0)
        assert first.eps_active is False
        # 5s later: triggers.
        triggered = await g.read_flags(now=t0 + timedelta(seconds=5))
        assert triggered.eps_active is True
        assert triggered.grid_connected is False


class TestDeferEV:
    @pytest.mark.asyncio
    async def test_user_switch_on(self):
        cfg = FlagsConfig()
        g = WorldGatherer(
            state_reader=MockStateReader(
                {cfg.defer_ev_eligible_entity: "on"}
            ),
            flags_config=cfg,
        )
        flags = await g.read_flags()
        assert flags.defer_ev_eligible is True

    @pytest.mark.asyncio
    async def test_user_switch_off_or_missing(self):
        g = WorldGatherer(
            state_reader=MockStateReader(),  # missing → False
            flags_config=FlagsConfig(),
        )
        flags = await g.read_flags()
        assert flags.defer_ev_eligible is False
