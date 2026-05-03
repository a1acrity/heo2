# tests/test_rules/test_winter_low_pv.py
"""Tests for WinterLowPVRule (SPEC §9 row 5)."""

from __future__ import annotations

from datetime import time

from heo2.models import ProgrammeInputs, ProgrammeState, SlotConfig
from heo2.rules.winter_low_pv import WinterLowPVRule


def _slot(start_h, start_m, end_h, end_m, soc, gc):
    return SlotConfig(
        start_time=time(start_h, start_m),
        end_time=time(end_h, end_m),
        capacity_soc=soc,
        grid_charge=gc,
    )


def _spec_programme():
    """Plausible mid-chain state: ExportWindow has drained the day +
    evening to floor, baseline has overnight at <100, SavingSession /
    IGO etc. haven't fired."""
    return ProgrammeState(slots=[
        _slot(0, 0, 5, 30, 80, True),    # baseline overnight target
        _slot(5, 30, 16, 0, 20, False),  # day, drained by ExportWindow
        _slot(16, 0, 19, 0, 20, False),  # evening drain target
        _slot(19, 0, 23, 30, 20, False),
        _slot(23, 30, 23, 55, 80, True),
        _slot(23, 55, 0, 0, 10, False),
    ], reason_log=[])


class TestWinterLowPVRule:
    def test_inactive_when_solar_exceeds_load(self, default_inputs):
        default_inputs.solar_forecast_kwh = [2.0] * 24  # 48 kWh
        default_inputs.load_forecast_kwh = [1.0] * 24   # 24 kWh
        default_inputs.is_winter_low_pv = False
        prog = _spec_programme()
        socs_before = [s.capacity_soc for s in prog.slots]

        result = WinterLowPVRule().apply(prog, default_inputs)
        assert [s.capacity_soc for s in result.slots] == socs_before

    def test_active_raises_overnight_charge_to_100(self, default_inputs):
        """In winter, overnight is the only meaningful charge window;
        fill the battery completely."""
        default_inputs.solar_forecast_kwh = [0.5] * 24  # 12 kWh
        default_inputs.load_forecast_kwh = [1.0] * 24   # 24 kWh
        default_inputs.is_winter_low_pv = True
        prog = _spec_programme()

        result = WinterLowPVRule().apply(prog, default_inputs)
        # Both grid_charge=True slots should be raised to 100%
        assert result.slots[0].capacity_soc == 100  # overnight
        assert result.slots[4].capacity_soc == 100  # late-night

    def test_active_does_not_touch_non_gc_slots(self, default_inputs):
        """Regression for 2026-05-03: pre-fix the rule raised every
        non-GC slot's cap to a day-load-sized floor (often 100). That
        pinned the battery at 100% through the evening and the grid
        carried all load. Now the rule deliberately skips non-GC slots
        - EveningProtectRule handles the evening reserve floor."""
        default_inputs.solar_forecast_kwh = [0.5] * 24
        default_inputs.load_forecast_kwh = [1.0] * 24
        default_inputs.is_winter_low_pv = True
        prog = _spec_programme()
        evening_drain_cap_before = prog.slots[2].capacity_soc  # 18:30-23:30 cap=10
        result = WinterLowPVRule().apply(prog, default_inputs)
        # Evening drain slot stays at 10 - WinterLowPV no longer
        # raises it. The battery is free to discharge through evening.
        assert result.slots[2].capacity_soc == evening_drain_cap_before

    def test_active_does_not_change_already_complete_overnight(
        self, default_inputs,
    ):
        """If overnight slots are already at 100, rule logs no-change."""
        default_inputs.solar_forecast_kwh = [0.5] * 24
        default_inputs.load_forecast_kwh = [1.0] * 24
        default_inputs.is_winter_low_pv = True
        prog = _spec_programme()
        for s in prog.slots:
            if s.grid_charge:
                s.capacity_soc = 100
        result = WinterLowPVRule().apply(prog, default_inputs)
        assert all(s.capacity_soc == 100 for s in result.slots if s.grid_charge)
        assert any("already at 100%" in r for r in result.reason_log)

    def test_reason_log_records_changes(self, default_inputs):
        default_inputs.solar_forecast_kwh = [0.5] * 24
        default_inputs.load_forecast_kwh = [1.0] * 24
        default_inputs.is_winter_low_pv = True
        prog = _spec_programme()
        result = WinterLowPVRule().apply(prog, default_inputs)
        assert any("WinterLowPV" in r for r in result.reason_log)
