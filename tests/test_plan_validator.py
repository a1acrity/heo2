# tests/test_plan_validator.py
"""Tests for the SPEC §6 / H5 pre-write plan validator."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from heo2.models import ProgrammeInputs, ProgrammeState, RateSlot, SlotConfig
from heo2.plan_validator import ValidationResult, validate_plan


def _slot(start_h, start_m, end_h, end_m, soc, gc):
    return SlotConfig(
        start_time=time(start_h, start_m),
        end_time=time(end_h, end_m),
        capacity_soc=soc,
        grid_charge=gc,
    )


def _good_programme(min_soc=10):
    """Spec-shaped 6-slot programme: cheap-charge overnight, hold day,
    drain in evening export window."""
    return ProgrammeState(slots=[
        _slot(0, 0, 5, 30, 80, True),   # IGO cheap window: charge to 80
        _slot(5, 30, 16, 0, 80, False), # day: hold
        _slot(16, 0, 19, 0, min_soc, False),  # evening drain window
        _slot(19, 0, 23, 30, min_soc, False),
        _slot(23, 30, 23, 59, 80, True),
        _slot(23, 59, 0, 0, 80, True),
    ])


def _half_hour_rates(start, hours, rate_pence):
    out = []
    for i in range(hours * 2):
        out.append(RateSlot(
            start=start + timedelta(minutes=30 * i),
            end=start + timedelta(minutes=30 * (i + 1)),
            rate_pence=rate_pence,
        ))
    return out


def _igo_import_rates_today(midnight: datetime) -> list[RateSlot]:
    """Standard IGO shape: ~5p 23:30-05:30, ~25p the rest of the day."""
    out = []
    for i in range(48):
        start = midnight + timedelta(minutes=30 * i)
        end = start + timedelta(minutes=30)
        h = start.hour
        rate = 4.95 if h < 5 or (h == 5 and start.minute < 30) or h >= 23 \
            else 24.84
        if h == 23 and start.minute < 30:
            rate = 24.84
        out.append(RateSlot(start=start, end=end, rate_pence=rate))
    return out


def _inputs(
    *, programme=None, current_soc=50.0, igo=True, midnight=None,
    export_rates=None,
):
    midnight = midnight or datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
    return ProgrammeInputs(
        now=midnight,
        current_soc=current_soc,
        battery_capacity_kwh=20.0,
        min_soc=10.0,
        import_rates=_igo_import_rates_today(midnight) if igo else [],
        export_rates=export_rates or _half_hour_rates(midnight, 24, 8.0),
        solar_forecast_kwh=[0.0] * 24,
        load_forecast_kwh=[1.0] * 24,
        igo_dispatching=False,
        saving_session=False,
        saving_session_start=None,
        saving_session_end=None,
        ev_charging=False,
        grid_connected=True,
        active_appliances=[],
        appliance_expected_kwh=0.0,
    )


class TestValidatePlanStructural:
    def test_good_plan_passes(self):
        result = validate_plan(_good_programme(), _inputs())
        assert result.passed
        assert result.errors == []
        assert result.projection is not None

    def test_wrong_slot_count_fails(self):
        prog = ProgrammeState(slots=[
            _slot(0, 0, 12, 0, 50, False),
            _slot(12, 0, 0, 0, 50, False),
        ])
        result = validate_plan(prog, _inputs())
        assert not result.passed
        assert any("expected 6 slots" in e for e in result.errors)

    def test_slot1_not_starting_at_midnight_fails(self):
        prog = _good_programme()
        prog.slots[0].start_time = time(1, 0)
        result = validate_plan(prog, _inputs())
        assert not result.passed
        assert any("slot 1 must start 00:00" in e for e in result.errors)

    def test_non_contiguous_fails(self):
        prog = _good_programme()
        prog.slots[2].start_time = time(17, 0)  # gap from slot 2 end (16:00)
        result = validate_plan(prog, _inputs())
        assert not result.passed
        assert any("structural" in e for e in result.errors)

    def test_soc_below_min_fails(self):
        prog = _good_programme()
        prog.slots[2].capacity_soc = 5  # min_soc is 10
        result = validate_plan(prog, _inputs())
        assert not result.passed
        assert any("SOC" in e for e in result.errors)


class TestValidatePlanH1PeakCharge:
    def test_grid_charge_in_peak_window_rejects(self):
        """A GC=True slot covering the day-rate IGO peak hours fires H1."""
        prog = _good_programme()
        # Slot 2 (05:30-16:00) at 80% no-GC. Replace with a GC=True slot
        # that overlaps the IGO peak day-rate window.
        prog.slots[1] = _slot(5, 30, 16, 0, 80, True)
        result = validate_plan(prog, _inputs())
        assert not result.passed
        assert any("H1 violation" in e for e in result.errors)
        # The reason should call out that the peak rate is what makes
        # this a violation
        assert any("peak rate" in e for e in result.errors)

    def test_grid_charge_only_in_off_peak_passes_h1(self):
        """The good programme has GC=True only 23:30-05:30 (off-peak)."""
        result = validate_plan(_good_programme(), _inputs())
        assert result.passed


class TestValidatePlanCheapWindowWarning:
    def test_no_grid_charge_in_cheap_window_warns(self):
        """A plan that never grid-charges produces a warning, not error."""
        prog = ProgrammeState(slots=[
            _slot(0, 0, 5, 30, 50, False),
            _slot(5, 30, 8, 0, 50, False),
            _slot(8, 0, 12, 0, 50, False),
            _slot(12, 0, 16, 0, 50, False),
            _slot(16, 0, 23, 30, 50, False),
            _slot(23, 30, 0, 0, 50, False),
        ])
        result = validate_plan(prog, _inputs())
        # No H1 violation, no structural issue; warning only
        assert result.passed
        assert any("cheap window" in w for w in result.warnings)


class TestValidatePlanProjection:
    def test_projection_populated_even_on_reject(self):
        prog = _good_programme()
        # Force a structural reject (slot count)
        prog.slots = prog.slots[:2]
        result = validate_plan(prog, _inputs())
        assert not result.passed
        # Projection still attempted; may be empty values, but not None
        assert result.projection is not None

    def test_peak_import_emits_warning_not_error(self):
        """SPEC H1: forced peak import (battery hits floor) is a
        warning, not a hard reject - reality wins."""
        # A plan that doesn't charge -> battery drains -> forced peak
        # imports during the day at IGO peak rate.
        prog = ProgrammeState(slots=[
            _slot(0, 0, 5, 30, 10, False),
            _slot(5, 30, 8, 0, 10, False),
            _slot(8, 0, 12, 0, 10, False),
            _slot(12, 0, 16, 0, 10, False),
            _slot(16, 0, 23, 30, 10, False),
            _slot(23, 30, 0, 0, 10, False),
        ])
        result = validate_plan(prog, _inputs(current_soc=10.0))
        # No H1 plan-level error: no GC=True in peak.
        assert all("H1 violation" not in e for e in result.errors)
        # But a peak-import warning is present from the projection.
        assert any("peak-rate import" in w for w in result.warnings)


class TestValidatePlanTimezone:
    def test_uk_summer_peak_at_0430_utc_does_not_alias_into_overnight_slot(self):
        """Regression: a peak rate slot at 04:30 UTC (= 05:30 BST) was
        being aliased into the 00:00-05:30 BST overnight cheap-charge
        slot, falsely flagging an H1 violation. The fix passes tz to
        validate_plan and projects rate.start onto local time-of-day
        before comparing against programme slots.

        Observed in PROD on 2026-05-02: BottlecapDave returned 28.58p
        at 04:30 UTC (peak day rate boundary), the validator flagged
        slot 1 (00:00-05:30 BST, GC=True) as covering peak, the plan
        was rejected on every tick.
        """
        london = ZoneInfo("Europe/London")
        # A summer day in BST (DST active)
        midnight_utc = datetime(2026, 5, 2, 23, 0, tzinfo=timezone.utc)
        # Generate IGO-shaped UTC rates: ~5p 22:30 UTC -> 04:30 UTC,
        # peak (28.58p) 04:30 UTC -> 22:30 UTC. (= 23:30 BST -> 05:30 BST
        # off-peak, peak the rest of the day.)
        rates = []
        for i in range(48):
            start = midnight_utc + timedelta(minutes=30 * i)
            end = start + timedelta(minutes=30)
            local_h = start.astimezone(london).hour
            local_m = start.astimezone(london).minute
            is_offpeak = (
                local_h < 5 or (local_h == 5 and local_m < 30) or
                local_h >= 23 and (local_h > 23 or local_m >= 30)
            )
            rates.append(RateSlot(start=start, end=end, rate_pence=4.95 if is_offpeak else 28.58))

        # A spec-shaped plan: GC overnight only (00:00-05:30 BST).
        prog = ProgrammeState(slots=[
            _slot(0, 0, 5, 30, 80, True),    # BST overnight cheap-charge
            _slot(5, 30, 16, 0, 80, False),  # BST day hold
            _slot(16, 0, 19, 0, 10, False),  # BST evening drain
            _slot(19, 0, 23, 30, 10, False),
            _slot(23, 30, 23, 55, 80, True), # post-23:30 cheap window
            _slot(23, 55, 0, 0, 80, True),
        ])

        # Build inputs with a "now" early in the day so all 48 rates fall
        # within the validator's "today".
        inputs = ProgrammeInputs(
            now=datetime(2026, 5, 3, 0, 0, tzinfo=timezone.utc),
            current_soc=50.0, battery_capacity_kwh=20.0, min_soc=10.0,
            import_rates=rates, export_rates=[],
            solar_forecast_kwh=[0.0] * 24, load_forecast_kwh=[0.5] * 24,
            igo_dispatching=False, saving_session=False,
            saving_session_start=None, saving_session_end=None,
            ev_charging=False, grid_connected=True,
            active_appliances=[], appliance_expected_kwh=0.0,
        )

        result = validate_plan(prog, inputs, tz=london, peak_threshold_p=24.0)
        # No H1 violation: GC slots are 00:00-05:30 BST and 23:30+ BST,
        # both fully inside the off-peak window.
        h1_errors = [e for e in result.errors if "H1 violation" in e]
        assert h1_errors == [], (
            f"unexpected H1 violation(s): {h1_errors}; "
            f"all errors: {result.errors}"
        )
        # Sanity: validator passed
        assert result.passed
