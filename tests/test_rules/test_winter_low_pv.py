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

    def test_active_raises_non_gc_floor_to_day_load(self, default_inputs):
        """Non-GC slots get a floor sized for the day's load demand."""
        default_inputs.solar_forecast_kwh = [0.5] * 24  # 12 kWh
        default_inputs.load_forecast_kwh = [1.0] * 24   # 24 kWh, full day
        default_inputs.is_winter_low_pv = True
        default_inputs.battery_capacity_kwh = 20.0
        # Day floor = min_soc(20) + (24 / 20 * 100) = 20 + 120 = clamped to 100
        prog = _spec_programme()
        result = WinterLowPVRule().apply(prog, default_inputs)
        for i, slot in enumerate(result.slots):
            if not slot.grid_charge:
                assert slot.capacity_soc >= 20, (
                    f"slot {i + 1} non-GC floor not raised: "
                    f"{slot.capacity_soc}"
                )

    def test_active_does_not_lower_existing_high_soc(self, default_inputs):
        """The rule only raises floors, never lowers them."""
        # Pick light load so day_floor is low, then verify a slot
        # already above that floor stays put.
        default_inputs.solar_forecast_kwh = [0.5] * 24  # 12 kWh
        default_inputs.load_forecast_kwh = [0.1] * 24   # 2.4 kWh
        default_inputs.is_winter_low_pv = True
        default_inputs.battery_capacity_kwh = 20.0
        # day_floor = 20 + (2.4/20*100) = 32
        prog = _spec_programme()
        prog.slots[2].capacity_soc = 75  # already above day_floor=32
        result = WinterLowPVRule().apply(prog, default_inputs)
        assert result.slots[2].capacity_soc == 75

    def test_reason_log_records_changes(self, default_inputs):
        default_inputs.solar_forecast_kwh = [0.5] * 24
        default_inputs.load_forecast_kwh = [1.0] * 24
        default_inputs.is_winter_low_pv = True
        prog = _spec_programme()
        result = WinterLowPVRule().apply(prog, default_inputs)
        assert any("WinterLowPV" in r for r in result.reason_log)
