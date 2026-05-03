# tests/test_rules/test_ev_deferral.py
"""Tests for EVDeferralRule (SPEC §12)."""

from __future__ import annotations

from datetime import time

from heo2.models import ProgrammeInputs, ProgrammeState, SlotConfig
from heo2.rules.ev_deferral import EVDeferralRule


def _slot(start_h, start_m, end_h, end_m, soc, gc):
    return SlotConfig(
        start_time=time(start_h, start_m),
        end_time=time(end_h, end_m),
        capacity_soc=soc,
        grid_charge=gc,
    )


def _empty_programme():
    return ProgrammeState(slots=[
        _slot(0, 0, 5, 30, 80, True),
        _slot(5, 30, 18, 30, 80, False),
        _slot(18, 30, 23, 30, 10, False),
        _slot(23, 30, 23, 55, 80, True),
        _slot(23, 55, 23, 55, 10, False),
        _slot(23, 55, 0, 0, 10, False),
    ], reason_log=[])


class TestEVDeferralRule:
    def test_no_op_when_user_toggle_off(self, default_inputs):
        default_inputs.defer_ev_eligible = False
        default_inputs.current_soc = 90.0
        default_inputs.current_export_rate_p = 25.0
        prog = _empty_programme()
        result = EVDeferralRule().apply(prog, default_inputs)
        assert result.ev_deferral_active is False
        assert result.work_mode is None  # untouched

    def test_no_op_when_soc_below_threshold(self, default_inputs):
        default_inputs.defer_ev_eligible = True
        default_inputs.current_soc = 50.0  # below default 80
        default_inputs.current_export_rate_p = 25.0
        prog = _empty_programme()
        result = EVDeferralRule().apply(prog, default_inputs)
        assert result.ev_deferral_active is False
        assert result.work_mode is None
        assert any("SOC" in r and "below" in r for r in result.reason_log)

    def test_no_op_when_no_export_rate(self, default_inputs):
        default_inputs.defer_ev_eligible = True
        default_inputs.current_soc = 90.0
        default_inputs.current_export_rate_p = None
        prog = _empty_programme()
        result = EVDeferralRule().apply(prog, default_inputs)
        assert result.ev_deferral_active is False
        assert any("no live export rate" in r for r in result.reason_log)

    def test_ridiculous_low_export_no_op(self, default_inputs):
        """SPEC §12 fallback: don't defer when export is too low to
        be worth it - let the car charge as normal."""
        default_inputs.defer_ev_eligible = True
        default_inputs.current_soc = 90.0
        default_inputs.current_export_rate_p = 5.0  # below default 15
        prog = _empty_programme()
        result = EVDeferralRule().apply(prog, default_inputs)
        assert result.ev_deferral_active is False
        assert any("below" in r and "threshold" in r for r in result.reason_log)

    def test_active_when_all_triggers_met(self, default_inputs):
        default_inputs.defer_ev_eligible = True
        default_inputs.current_soc = 90.0
        default_inputs.current_export_rate_p = 25.0
        prog = _empty_programme()
        result = EVDeferralRule().apply(prog, default_inputs)
        assert result.ev_deferral_active is True
        assert result.work_mode == "Selling first"
        assert any("ACTIVE" in r for r in result.reason_log)

    def test_custom_thresholds(self, default_inputs):
        """Allow operator to lower the SOC + export thresholds for
        more aggressive deferral."""
        default_inputs.defer_ev_eligible = True
        default_inputs.current_soc = 65.0  # would fail default 80
        default_inputs.current_export_rate_p = 8.0  # would fail default 15
        rule = EVDeferralRule(
            deferral_min_soc=60.0, deferral_min_export_p=7.0,
        )
        prog = _empty_programme()
        result = rule.apply(prog, default_inputs)
        assert result.ev_deferral_active is True
