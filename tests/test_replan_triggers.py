# tests/test_replan_triggers.py
"""Tests for the SPEC §8 daily-plan / 15-min trigger separation."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from heo2.models import ProgrammeInputs, ProgrammeState, SlotConfig
from heo2.replan_triggers import (
    BaselineSnapshot,
    capture_baseline,
    should_commit_replan,
)


_LON = ZoneInfo("Europe/London")


def _slot(start_h, start_m, end_h, end_m, soc=50, gc=False):
    return SlotConfig(
        start_time=time(start_h, start_m),
        end_time=time(end_h, end_m),
        capacity_soc=soc,
        grid_charge=gc,
    )


def _default_programme() -> ProgrammeState:
    return ProgrammeState(slots=[
        _slot(0, 0, 5, 30),
        _slot(5, 30, 12, 0),
        _slot(12, 0, 16, 0),
        _slot(16, 0, 19, 0),
        _slot(19, 0, 23, 30),
        _slot(23, 30, 0, 0),
    ])


def _inputs(
    *, now=None, current_soc=50.0, solar=None, load=None,
    igo=False, saving=False, grid=True,
):
    return ProgrammeInputs(
        now=now or datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        current_soc=current_soc,
        battery_capacity_kwh=20.0,
        min_soc=10.0,
        import_rates=[],
        export_rates=[],
        solar_forecast_kwh=solar or [1.0] * 24,
        load_forecast_kwh=load or [0.5] * 24,
        igo_dispatching=igo,
        saving_session=saving,
        saving_session_start=None,
        saving_session_end=None,
        ev_charging=False,
        grid_connected=grid,
        active_appliances=[],
        appliance_expected_kwh=0.0,
    )


class TestFirstTickAlwaysCommits:
    def test_no_baseline_commits_immediately(self):
        decision = should_commit_replan(
            new_programme=_default_programme(),
            inputs=_inputs(),
            baseline=None,
            tz=_LON,
            daily_plan_time=time(18, 0),
            replan_solar_pct=25,
            replan_load_pct=25,
            replan_soc_pct=10,
        )
        assert decision.commit
        assert "first plan" in decision.reason


class TestDailyPlanWindow:
    def test_within_18h_window_on_new_date_commits(self):
        # Capture a yesterday-baseline so today's 18:00 fires
        yesterday_inputs = _inputs(
            now=datetime(2026, 4, 30, 18, 0, tzinfo=timezone.utc),
        )
        baseline = capture_baseline(
            _default_programme(), yesterday_inputs, tz=_LON, is_daily_plan=True,
        )
        # Today 18:05 BST = 17:05 UTC
        today_inputs = _inputs(
            now=datetime(2026, 5, 1, 17, 5, tzinfo=timezone.utc),
        )
        decision = should_commit_replan(
            new_programme=_default_programme(),
            inputs=today_inputs,
            baseline=baseline,
            tz=_LON,
            daily_plan_time=time(18, 0),
            replan_solar_pct=25,
            replan_load_pct=25,
            replan_soc_pct=10,
        )
        assert decision.commit
        assert "daily plan" in decision.reason

    def test_already_planned_today_no_commit(self):
        """Second 18:xx tick on the same day shouldn't fire a replan."""
        baseline_inputs = _inputs(
            now=datetime(2026, 5, 1, 17, 0, tzinfo=timezone.utc),  # 18:00 BST
        )
        baseline = capture_baseline(
            _default_programme(), baseline_inputs,
            tz=_LON, is_daily_plan=True,
        )
        # 18:15 BST same day
        next_tick = _inputs(
            now=datetime(2026, 5, 1, 17, 15, tzinfo=timezone.utc),
        )
        decision = should_commit_replan(
            new_programme=_default_programme(),
            inputs=next_tick,
            baseline=baseline,
            tz=_LON,
            daily_plan_time=time(18, 0),
            replan_solar_pct=25,
            replan_load_pct=25,
            replan_soc_pct=10,
        )
        assert not decision.commit
        assert "hold baseline" in decision.reason


class TestQuantitativeTriggers:
    def _baseline_at(self, hour_utc=12, soc=50.0, solar=None, load=None):
        inputs = _inputs(
            now=datetime(2026, 5, 1, hour_utc, 0, tzinfo=timezone.utc),
            current_soc=soc,
            solar=solar or [1.0] * 24,
            load=load or [0.5] * 24,
        )
        return capture_baseline(
            _default_programme(), inputs, tz=_LON, is_daily_plan=True,
        )

    def test_solar_deviation_above_threshold_commits(self):
        baseline = self._baseline_at(
            hour_utc=12, solar=[1.0] * 24,  # rest-of-day = 12 kWh
        )
        # Same time on the same day, solar forecast halved -> 50% dev
        new = _inputs(
            now=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            solar=[0.5] * 24,
        )
        decision = should_commit_replan(
            new_programme=_default_programme(),
            inputs=new, baseline=baseline, tz=_LON,
            daily_plan_time=time(18, 0),
            replan_solar_pct=25, replan_load_pct=25, replan_soc_pct=10,
        )
        assert decision.commit
        assert "solar" in decision.reason

    def test_load_deviation_above_threshold_commits(self):
        baseline = self._baseline_at(load=[0.5] * 24)
        new = _inputs(
            now=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            load=[1.0] * 24,  # 100% deviation
        )
        decision = should_commit_replan(
            new_programme=_default_programme(),
            inputs=new, baseline=baseline, tz=_LON,
            daily_plan_time=time(18, 0),
            replan_solar_pct=25, replan_load_pct=25, replan_soc_pct=10,
        )
        assert decision.commit
        assert "load" in decision.reason

    def test_soc_deviation_above_threshold_commits(self):
        baseline = self._baseline_at(soc=50.0)
        new = _inputs(
            now=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            current_soc=70.0,  # 20pt deviation
        )
        decision = should_commit_replan(
            new_programme=_default_programme(),
            inputs=new, baseline=baseline, tz=_LON,
            daily_plan_time=time(18, 0),
            replan_solar_pct=25, replan_load_pct=25, replan_soc_pct=10,
        )
        assert decision.commit
        assert "SOC" in decision.reason

    def test_small_deviation_no_commit(self):
        baseline = self._baseline_at(soc=50.0)
        new = _inputs(
            now=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            current_soc=55.0,
        )
        decision = should_commit_replan(
            new_programme=_default_programme(),
            inputs=new, baseline=baseline, tz=_LON,
            daily_plan_time=time(18, 0),
            replan_solar_pct=25, replan_load_pct=25, replan_soc_pct=10,
        )
        assert not decision.commit


class TestEventTriggers:
    def _baseline(self, *, igo=False, saving=False, grid=True):
        inputs = _inputs(igo=igo, saving=saving, grid=grid)
        return capture_baseline(
            _default_programme(), inputs, tz=_LON, is_daily_plan=True,
        )

    def test_igo_dispatch_announced_commits(self):
        baseline = self._baseline(igo=False)
        new = _inputs(igo=True)
        decision = should_commit_replan(
            new_programme=_default_programme(),
            inputs=new, baseline=baseline, tz=_LON,
            daily_plan_time=time(18, 0),
            replan_solar_pct=25, replan_load_pct=25, replan_soc_pct=10,
        )
        assert decision.commit
        assert "IGO dispatch" in decision.reason

    def test_saving_session_commits(self):
        baseline = self._baseline(saving=False)
        new = _inputs(saving=True)
        decision = should_commit_replan(
            new_programme=_default_programme(),
            inputs=new, baseline=baseline, tz=_LON,
            daily_plan_time=time(18, 0),
            replan_solar_pct=25, replan_load_pct=25, replan_soc_pct=10,
        )
        assert decision.commit
        assert "saving session" in decision.reason

    def test_grid_restored_commits(self):
        baseline = self._baseline(grid=False)
        new = _inputs(grid=True)
        decision = should_commit_replan(
            new_programme=_default_programme(),
            inputs=new, baseline=baseline, tz=_LON,
            daily_plan_time=time(18, 0),
            replan_solar_pct=25, replan_load_pct=25, replan_soc_pct=10,
        )
        assert decision.commit
        assert "grid restored" in decision.reason

    def test_igo_dispatching_continuing_no_commit(self):
        """Already-dispatching baseline + still-dispatching new = no trigger."""
        baseline = self._baseline(igo=True)
        new = _inputs(igo=True)
        decision = should_commit_replan(
            new_programme=_default_programme(),
            inputs=new, baseline=baseline, tz=_LON,
            daily_plan_time=time(18, 0),
            replan_solar_pct=25, replan_load_pct=25, replan_soc_pct=10,
        )
        assert not decision.commit
