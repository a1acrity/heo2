# tests/test_rank_pricing.py
"""Tests for the rank-based pricing helpers (HEO-30 step 3)."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from heo2.models import RateSlot
from heo2.rank_pricing import (
    bottom_n_pct,
    estimate_profitable_export_kwh,
    filter_today,
    hours_covered_by,
    is_worth_selling,
    select_cheap_charge_windows,
    select_export_top_pct,
    select_worth_selling_windows,
    top_n_pct,
)

UTC = timezone.utc


def _slot(start_h: float, pence: float, day: int = 30, month: int = 4) -> RateSlot:
    """Build a 30-min UTC slot. Hours past 23.5 wrap to subsequent days."""
    from datetime import timedelta
    base = datetime(2026, month, day, 0, 0, tzinfo=UTC)
    start = base + timedelta(minutes=int(round(start_h * 60)))
    end = start + timedelta(minutes=30)
    return RateSlot(start=start, end=end, rate_pence=pence)


# ---------------------------------------------------------------------------
# top_n_pct / bottom_n_pct
# ---------------------------------------------------------------------------


class TestTopNPct:
    def test_returns_top_by_rate_descending(self):
        rates = [_slot(0, 5.0), _slot(1, 20.0), _slot(2, 10.0), _slot(3, 15.0)]
        result = top_n_pct(rates, 50)
        assert [r.rate_pence for r in result] == [20.0, 15.0]

    def test_rounds_count_up(self):
        # 48 slots, top 15% -> ceil(48*0.15) = ceil(7.2) = 8 slots
        rates = [_slot(i / 2, float(i)) for i in range(48)]
        result = top_n_pct(rates, 15)
        assert len(result) == 8
        # Highest 8 are slots 40..47 with values 40..47
        assert min(r.rate_pence for r in result) == 40.0

    def test_returns_at_least_one_when_pct_tiny(self):
        rates = [_slot(0, 5.0), _slot(1, 10.0)]
        # 1% of 2 = 0.02, ceil -> 1. Always at least one.
        result = top_n_pct(rates, 1)
        assert len(result) == 1
        assert result[0].rate_pence == 10.0

    def test_empty_returns_empty(self):
        assert top_n_pct([], 30) == []

    def test_zero_pct_returns_empty(self):
        rates = [_slot(0, 5.0)]
        assert top_n_pct(rates, 0) == []
        assert top_n_pct(rates, -10) == []

    def test_pct_over_100_returns_all(self):
        rates = [_slot(0, 5.0), _slot(1, 10.0)]
        result = top_n_pct(rates, 200)
        assert len(result) == 2


class TestBottomNPct:
    def test_returns_bottom_by_rate_ascending(self):
        rates = [_slot(0, 5.0), _slot(1, 20.0), _slot(2, 10.0), _slot(3, 15.0)]
        result = bottom_n_pct(rates, 50)
        assert [r.rate_pence for r in result] == [5.0, 10.0]

    def test_igo_off_peak_is_bottom_25(self):
        """For an IGO-shaped distribution (mostly day rate 28p, 12 slots
        at 5p) the bottom 25% picks all the off-peak slots."""
        # 36 day-rate slots + 12 off-peak slots = 48 half-hour slots
        rates = (
            [_slot(i / 2, 28.0) for i in range(36)]
            + [_slot(36 + i / 2, 5.0) for i in range(12)]
        )
        result = bottom_n_pct(rates, 25)
        assert len(result) == 12
        assert all(r.rate_pence == 5.0 for r in result)


# ---------------------------------------------------------------------------
# select_export_top_pct
# ---------------------------------------------------------------------------


class TestSelectExportTopPct:
    def test_low_soc_uses_n_low(self):
        n, reason = select_export_top_pct(
            current_soc=30.0, tomorrow_solar_kwh=50.0, daily_load_kwh=20.0,
        )
        assert n == 15
        assert "low" in reason.lower()

    def test_low_tomorrow_solar_uses_n_low(self):
        # high SOC but solar < daily_load * 0.5 -> n_low
        n, _ = select_export_top_pct(
            current_soc=90.0, tomorrow_solar_kwh=5.0, daily_load_kwh=20.0,
        )
        assert n == 15

    def test_high_soc_and_high_solar_uses_n_high(self):
        n, reason = select_export_top_pct(
            current_soc=85.0, tomorrow_solar_kwh=30.0, daily_load_kwh=20.0,
        )
        assert n == 50
        assert "high" in reason.lower()

    def test_medium_uses_n_med(self):
        n, _ = select_export_top_pct(
            current_soc=60.0, tomorrow_solar_kwh=20.0, daily_load_kwh=20.0,
        )
        assert n == 30

    def test_custom_thresholds(self):
        n, _ = select_export_top_pct(
            current_soc=45.0,
            tomorrow_solar_kwh=20.0,
            daily_load_kwh=20.0,
            low_soc_threshold=40.0,  # 45 is no longer "low"
            high_soc_threshold=60.0,  # 45 is "medium"
        )
        assert n == 30


# ---------------------------------------------------------------------------
# is_worth_selling
# ---------------------------------------------------------------------------


class TestIsWorthSelling:
    def test_above_breakeven_is_worth(self):
        # 10p × 0.9025 = 9.025 > 4.95
        assert is_worth_selling(10.0, 4.95, 0.9025) is True

    def test_below_breakeven_is_not_worth(self):
        # 5p × 0.9025 = 4.5125 < 4.95
        assert is_worth_selling(5.0, 4.95, 0.9025) is False

    def test_breakeven_returns_false(self):
        # exact breakeven returns False (strict greater-than)
        assert is_worth_selling(4.95 / 0.9025, 4.95, 0.9025) is False


# ---------------------------------------------------------------------------
# select_worth_selling_windows
# ---------------------------------------------------------------------------


class TestSelectWorthSellingWindows:
    def test_combines_top_pct_and_worth_filter(self):
        # 10 slots, top 30% -> 3 slots. Of those 3, 2 are above breakeven.
        rates = [
            _slot(0, 1.0), _slot(1, 2.0), _slot(2, 3.0), _slot(3, 4.0),
            _slot(4, 5.5), _slot(5, 6.0),  # breakeven ~5.49
            _slot(6, 7.0), _slot(7, 8.0), _slot(8, 9.0), _slot(9, 10.0),
        ]
        result = select_worth_selling_windows(
            rates, n_pct=30, replacement_cost_pence=4.95,
        )
        # Top 3 by rate: 10, 9, 8 - all above breakeven
        assert len(result) == 3
        assert [r.rate_pence for r in result] == [10.0, 9.0, 8.0]

    def test_drops_top_n_slots_that_fail_worth_test(self):
        # All 10 slots in narrow band around breakeven
        rates = [_slot(i, 5.0 + i * 0.1) for i in range(10)]
        # Top 30% = 3 slots: 5.9, 5.8, 5.7. Breakeven for replacement
        # cost 5.49 would be 5.49/0.9025 = 6.083p. So none are worth.
        result = select_worth_selling_windows(
            rates, n_pct=30, replacement_cost_pence=5.49,
        )
        assert result == []


# ---------------------------------------------------------------------------
# filter_today
# ---------------------------------------------------------------------------


class TestFilterToday:
    def test_keeps_slots_starting_today_local(self):
        london = ZoneInfo("Europe/London")
        # now = 2026-05-01 12:00 UTC = 13:00 BST
        now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        rates = [
            # 2026-05-01 23:30 BST (today) = 22:30 UTC
            RateSlot(
                start=datetime(2026, 5, 1, 22, 30, tzinfo=UTC),
                end=datetime(2026, 5, 1, 23, 0, tzinfo=UTC),
                rate_pence=10.0,
            ),
            # 2026-05-02 00:30 BST (tomorrow) = 23:30 UTC on 5/1
            RateSlot(
                start=datetime(2026, 5, 1, 23, 30, tzinfo=UTC),
                end=datetime(2026, 5, 2, 0, 0, tzinfo=UTC),
                rate_pence=5.0,
            ),
        ]
        result = filter_today(rates, now, tz=london)
        assert len(result) == 1
        assert result[0].rate_pence == 10.0


# ---------------------------------------------------------------------------
# hours_covered_by
# ---------------------------------------------------------------------------


class TestHoursCoveredBy:
    def test_30min_slots_collapse_to_starting_hour(self):
        slots = [
            _slot(14.0, 5.0),  # 14:00
            _slot(14.5, 6.0),  # 14:30
            _slot(15.0, 7.0),  # 15:00
        ]
        result = hours_covered_by(slots, tz=UTC)
        assert result == {14, 15}


# ---------------------------------------------------------------------------
# estimate_profitable_export_kwh
# ---------------------------------------------------------------------------


class TestEstimateProfitableExportKwh:
    def test_max_discharge_per_slot(self):
        """Each slot contributes max_discharge_kw * 0.5 hours = 2.5 kWh
        at the default 5 kW max discharge."""
        slots = [_slot(i, 10.0) for i in range(4)]
        result = estimate_profitable_export_kwh(slots, max_discharge_kw=5.0)
        assert result == pytest.approx(10.0)  # 4 * 2.5

    def test_empty_returns_zero(self):
        assert estimate_profitable_export_kwh([]) == 0.0


# ---------------------------------------------------------------------------
# select_cheap_charge_windows
# ---------------------------------------------------------------------------


class TestSelectCheapChargeWindows:
    def test_picks_off_peak_slots(self):
        # IGO-shaped: 36 at day rate, 12 at off-peak
        rates = (
            [_slot(i / 2, 28.0) for i in range(36)]
            + [_slot(36 + i / 2, 5.0) for i in range(12)]
        )
        result = select_cheap_charge_windows(rates, n_pct=25)
        assert len(result) == 12
        assert all(r.rate_pence == 5.0 for r in result)
