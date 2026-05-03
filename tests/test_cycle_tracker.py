# tests/test_cycle_tracker.py
"""Tests for the H7 cycle budget tracker."""

from __future__ import annotations

import pytest

from heo2.cycle_tracker import CycleTracker


class TestCycleTracker:
    def test_no_observation_yet_reports_zero_cycles(self):
        t = CycleTracker(battery_capacity_kwh=20.0)
        assert t.cycles_today == 0.0

    def test_first_observation_seeds_baseline_and_reports_zero(self):
        t = CycleTracker(battery_capacity_kwh=20.0)
        t.observe(150.0)
        # Same value is still our baseline; no cycles yet.
        assert t.cycles_today == 0.0

    def test_subsequent_observation_reports_cycles(self):
        t = CycleTracker(battery_capacity_kwh=20.0)
        t.observe(150.0)
        t.observe(160.0)  # +10 kWh out -> 0.5 cycles
        assert t.cycles_today == pytest.approx(0.5, abs=0.001)

    def test_daily_reset_zeroes_cycles_today(self):
        t = CycleTracker(battery_capacity_kwh=20.0)
        t.observe(150.0)
        t.observe(170.0)  # 1.0 cycles before reset
        assert t.cycles_today == pytest.approx(1.0)

        t.reset_daily()
        # New baseline = last observation; cycles_today now 0.
        assert t.cycles_today == pytest.approx(0.0)

        # Further observation shows fresh cycles since reset.
        t.observe(180.0)
        assert t.cycles_today == pytest.approx(0.5, abs=0.001)

    def test_counter_reset_reseeds_without_negative_cycles(self):
        """If SA restarts and the cumulative counter drops to a lower
        value, the tracker should re-seed rather than report negative
        cycles."""
        t = CycleTracker(battery_capacity_kwh=20.0)
        t.observe(150.0)
        t.observe(155.0)  # 0.25 cycles
        # Counter reset to 5 (e.g. SA storage wiped)
        t.observe(5.0)
        assert t.cycles_today == 0.0
        t.observe(10.0)  # 5 kWh after the reset
        assert t.cycles_today == pytest.approx(0.25, abs=0.001)

    def test_zero_capacity_does_not_divide_by_zero(self):
        t = CycleTracker(battery_capacity_kwh=0.0)
        t.observe(100.0)
        t.observe(200.0)
        assert t.cycles_today == 0.0


class TestBudgetExceededAlert:
    """SPEC §1 / H7: alert ON when 3 consecutive days each > 2 cycles."""

    def _three_days(self, t: CycleTracker, daily_kwh: list[float]) -> None:
        """Simulate `len(daily_kwh)` days each draining `daily_kwh[i]`
        kWh out of the battery."""
        running_total = 0.0
        t.observe(running_total)
        for kwh in daily_kwh:
            running_total += kwh
            t.observe(running_total)
            t.reset_daily()

    def test_no_history_no_alert(self):
        t = CycleTracker(battery_capacity_kwh=20.0)
        assert t.budget_exceeded is False

    def test_two_breaching_days_not_yet_alert(self):
        """Only fires after 3 consecutive breaches; 2 isn't enough."""
        t = CycleTracker(battery_capacity_kwh=20.0)
        # 50 kWh out per day = 2.5 cycles
        self._three_days(t, [50.0, 50.0])
        assert t.budget_exceeded is False
        assert t.history == pytest.approx([2.5, 2.5])

    def test_three_breaching_days_fires_alert(self):
        t = CycleTracker(battery_capacity_kwh=20.0)
        self._three_days(t, [50.0, 50.0, 50.0])
        assert t.budget_exceeded is True

    def test_quiet_day_in_window_clears_alert(self):
        """A single sub-budget day in the rolling window keeps alert off."""
        t = CycleTracker(battery_capacity_kwh=20.0)
        # day 1: 2.5 cycles (breach), day 2: 1 cycle (under), day 3: 2.5 (breach)
        self._three_days(t, [50.0, 20.0, 50.0])
        assert t.budget_exceeded is False

    def test_history_trimmed_to_window(self):
        """Only the last 3 finished days are kept."""
        t = CycleTracker(battery_capacity_kwh=20.0)
        self._three_days(t, [50.0, 50.0, 50.0, 50.0, 50.0])
        # Five finished days; history should hold only last 3
        assert len(t.history) == 3

    def test_today_in_progress_does_not_count_toward_alert(self):
        """A heavy-use day that hasn't finished should NOT spike the
        alert; only completed days count, otherwise an evening export
        would alert and a quiet morning would clear it."""
        t = CycleTracker(battery_capacity_kwh=20.0)
        # First two completed days each at 2.5 cycles
        self._three_days(t, [50.0, 50.0])
        # Today is in progress at 5 cycles - no third reset_daily yet
        # so daily_history has only 2 entries.
        running_total = 100.0  # cumulative after the 2 days
        t.observe(running_total + 100.0)  # mid-day, +5 cycles today
        assert t.cycles_today == 5.0
        # Still only 2 finished days -> no alert despite today being heavy.
        assert t.budget_exceeded is False

    def test_custom_daily_budget(self):
        """daily_budget is configurable per-install."""
        t = CycleTracker(battery_capacity_kwh=20.0, daily_budget=1.0)
        # Three days each at 1.5 cycles (>1.0 budget)
        self._three_days(t, [30.0, 30.0, 30.0])
        assert t.budget_exceeded is True
