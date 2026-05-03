# tests/test_rules/test_eps_mode.py
"""Tests for EPSModeRule (SPEC H3 / §9 row 2)."""

from __future__ import annotations

from datetime import time

from heo2.models import ProgrammeInputs, ProgrammeState, SlotConfig
from heo2.rules.eps_mode import EPSModeRule
from heo2.rules.safety import SafetyRule


def _slot(start_h, start_m, end_h, end_m, soc, gc):
    return SlotConfig(
        start_time=time(start_h, start_m),
        end_time=time(end_h, end_m),
        capacity_soc=soc,
        grid_charge=gc,
    )


def _normal_programme():
    return ProgrammeState(slots=[
        _slot(0, 0, 5, 30, 80, True),
        _slot(5, 30, 18, 30, 65, False),
        _slot(18, 30, 23, 30, 10, False),
        _slot(23, 30, 23, 55, 80, True),
        _slot(23, 55, 23, 55, 10, False),
        _slot(23, 55, 0, 0, 10, False),
    ], reason_log=[])


class TestEPSModeRule:
    def test_no_change_when_eps_inactive(self, default_inputs):
        default_inputs.eps_active = False
        prog = _normal_programme()
        socs_before = [s.capacity_soc for s in prog.slots]
        gcs_before = [s.grid_charge for s in prog.slots]

        result = EPSModeRule().apply(prog, default_inputs)
        assert [s.capacity_soc for s in result.slots] == socs_before
        assert [s.grid_charge for s in result.slots] == gcs_before

    def test_eps_active_drops_all_caps_to_zero(self, default_inputs):
        default_inputs.eps_active = True
        prog = _normal_programme()
        result = EPSModeRule().apply(prog, default_inputs)
        for slot in result.slots:
            assert slot.capacity_soc == 0
            assert slot.grid_charge is False
        assert any("EPSMode" in r for r in result.reason_log)

    def test_safety_does_not_re_clamp_soc_when_eps_active(
        self, default_inputs,
    ):
        """SafetyRule normally clamps SOC < min_soc up to min_soc.
        Under EPS the effective floor is 0; otherwise EPSModeRule's
        cap=0 would be undone by the next rule.
        """
        default_inputs.eps_active = True
        prog = _normal_programme()
        prog = EPSModeRule().apply(prog, default_inputs)
        prog = SafetyRule().apply(prog, default_inputs)
        for slot in prog.slots:
            assert slot.capacity_soc == 0

    def test_safety_still_clamps_soc_normally_when_eps_inactive(
        self, default_inputs,
    ):
        """Sanity: the EPS-aware floor doesn't break the normal path."""
        default_inputs.eps_active = False
        prog = ProgrammeState(slots=[
            _slot(0, 0, 5, 30, 5, True),  # below min_soc=20 from default
            _slot(5, 30, 18, 30, 65, False),
            _slot(18, 30, 23, 30, 10, False),
            _slot(23, 30, 23, 55, 80, True),
            _slot(23, 55, 23, 55, 10, False),
            _slot(23, 55, 0, 0, 10, False),
        ], reason_log=[])
        prog = SafetyRule().apply(prog, default_inputs)
        # min_soc=20 from default fixture
        assert prog.slots[0].capacity_soc >= 20
