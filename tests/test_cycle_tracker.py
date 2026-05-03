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
