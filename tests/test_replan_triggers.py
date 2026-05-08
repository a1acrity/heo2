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


class TestGlobalsChangedCommits:
    """Real production bug 2026-05-08: PeakArbitrageRule flips
    `state.work_mode` from "Zero export to CT" to "Selling first" when
    a tick lands inside an allocated top-priced export slot.
    EVDeferralRule does the same when its triggers cross threshold.
    SavingSession on session-end resets work_mode back to baseline
    (transition False->False is silent).

    None of these transitions are caught by the input-deviation
    triggers above (forecast / SOC / IGO / saving session / grid).
    Without a globals-changed trigger, the new programme is computed
    every tick but never committed - the inverter holds the stale
    baseline `Zero export to CT` and never exports during the day's
    top-priced window. Direct revenue loss.

    Each test below: baseline programme has globals A, new programme
    has globals B, NO input-deviation trigger fires. The trigger MUST
    fire purely on the globals delta.
    """

    def _baseline_with_globals(
        self,
        *,
        work_mode="Zero export to CT",
        energy_pattern="Load first",
        max_charge_a=100.0,
        max_discharge_a=100.0,
    ) -> BaselineSnapshot:
        prog = _default_programme()
        prog.work_mode = work_mode
        prog.energy_pattern = energy_pattern
        prog.max_charge_a = max_charge_a
        prog.max_discharge_a = max_discharge_a
        inputs = _inputs()
        return capture_baseline(prog, inputs, tz=_LON, is_daily_plan=True)

    def _new_programme(
        self,
        *,
        work_mode="Zero export to CT",
        energy_pattern="Load first",
        max_charge_a=100.0,
        max_discharge_a=100.0,
    ) -> ProgrammeState:
        prog = _default_programme()
        prog.work_mode = work_mode
        prog.energy_pattern = energy_pattern
        prog.max_charge_a = max_charge_a
        prog.max_discharge_a = max_discharge_a
        return prog

    def _decide(
        self, *, baseline: BaselineSnapshot, new: ProgrammeState,
    ):
        return should_commit_replan(
            new_programme=new,
            inputs=_inputs(),
            baseline=baseline,
            tz=_LON,
            daily_plan_time=time(18, 0),
            replan_solar_pct=25, replan_load_pct=25, replan_soc_pct=10,
        )

    def test_work_mode_flip_to_selling_first_commits(self):
        """The PeakArbitrage / SavingSession / EVDeferral case: rule
        activated, new programme has work_mode=Selling first, baseline
        had Zero export to CT, no input deviation."""
        baseline = self._baseline_with_globals(work_mode="Zero export to CT")
        new = self._new_programme(work_mode="Selling first")
        decision = self._decide(baseline=baseline, new=new)
        assert decision.commit, decision.reason
        assert "global" in decision.reason.lower() or "work_mode" in decision.reason.lower()

    def test_work_mode_flip_back_to_zero_export_commits(self):
        """Rule deactivated (e.g. saving session ended, peak window
        passed). New programme reverts to Baseline's `Zero export to
        CT`; baseline still carried `Selling first`. Without commit
        the inverter stays in Selling first forever."""
        baseline = self._baseline_with_globals(work_mode="Selling first")
        new = self._new_programme(work_mode="Zero export to CT")
        decision = self._decide(baseline=baseline, new=new)
        assert decision.commit, decision.reason

    def test_energy_pattern_change_commits(self):
        baseline = self._baseline_with_globals(energy_pattern="Load first")
        new = self._new_programme(energy_pattern="Battery first")
        decision = self._decide(baseline=baseline, new=new)
        assert decision.commit, decision.reason

    def test_max_discharge_a_change_commits(self):
        """PeakArbitrage throttles `max_discharge_a` by spare amount
        when active. Different from Baseline's 100A default."""
        baseline = self._baseline_with_globals(max_discharge_a=100.0)
        new = self._new_programme(max_discharge_a=49.0)
        decision = self._decide(baseline=baseline, new=new)
        assert decision.commit, decision.reason

    def test_max_charge_a_change_commits(self):
        baseline = self._baseline_with_globals(max_charge_a=100.0)
        new = self._new_programme(max_charge_a=50.0)
        decision = self._decide(baseline=baseline, new=new)
        assert decision.commit, decision.reason

    def test_globals_unchanged_no_commit(self):
        """Same globals between baseline and new: no globals trigger,
        no other trigger fired - hold the baseline. Existing 15-min
        no-op behaviour preserved."""
        baseline = self._baseline_with_globals(
            work_mode="Zero export to CT",
            energy_pattern="Load first",
            max_charge_a=100.0,
            max_discharge_a=100.0,
        )
        new = self._new_programme(
            work_mode="Zero export to CT",
            energy_pattern="Load first",
            max_charge_a=100.0,
            max_discharge_a=100.0,
        )
        decision = self._decide(baseline=baseline, new=new)
        assert not decision.commit

    def test_max_discharge_a_within_tolerance_no_commit(self):
        """Float compare uses tol=0.5 to match MqttWriter.diff_globals
        - 99.7 vs 100.0 is below the write threshold, so no spurious
        commit on noisy float arithmetic."""
        baseline = self._baseline_with_globals(max_discharge_a=100.0)
        new = self._new_programme(max_discharge_a=99.8)
        decision = self._decide(baseline=baseline, new=new)
        assert not decision.commit

    def test_work_mode_case_insensitive(self):
        """SA round-trips work_mode strings with occasional case
        flicker. Equality check should casefold like
        MqttWriter.diff_globals does."""
        baseline = self._baseline_with_globals(work_mode="Zero export to CT")
        new = self._new_programme(work_mode="zero export to ct")
        decision = self._decide(baseline=baseline, new=new)
        assert not decision.commit
