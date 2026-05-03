# tests/test_rules/test_saving_session.py
"""Tests for SavingSessionRule (HEO-10)."""

from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from heo2.models import ProgrammeInputs, ProgrammeState, RateSlot, SlotConfig
from heo2.rules.saving_session import SavingSessionRule


def _slot(start_h, start_m, end_h, end_m, soc, gc):
    return SlotConfig(
        start_time=time(start_h, start_m),
        end_time=time(end_h, end_m),
        capacity_soc=soc,
        grid_charge=gc,
    )


def _spec_programme():
    """A programme matching SPEC §5 baseline plus a high-SOC slot 2
    so we can see SavingSession actually drain it."""
    return ProgrammeState(slots=[
        _slot(0, 0, 5, 30, 80, True),
        _slot(5, 30, 18, 30, 80, False),  # daytime hold high
        _slot(18, 30, 23, 30, 80, False),  # evening with reserve
        _slot(23, 30, 23, 55, 80, True),
        _slot(23, 55, 23, 55, 10, False),
        _slot(23, 55, 0, 0, 10, False),
    ], reason_log=[])


def _inputs(*, now_utc, saving=False, tz=None):
    return ProgrammeInputs(
        now=now_utc,
        current_soc=80.0,
        battery_capacity_kwh=20.0,
        min_soc=10.0,
        import_rates=[],
        export_rates=[],
        solar_forecast_kwh=[0.0] * 24,
        load_forecast_kwh=[0.5] * 24,
        igo_dispatching=False,
        saving_session=saving,
        saving_session_start=None,
        saving_session_end=None,
        ev_charging=False,
        grid_connected=True,
        active_appliances=[],
        appliance_expected_kwh=0.0,
        local_tz=tz,
    )


class TestSavingSessionRule:
    def test_inactive_session_is_noop(self):
        rule = SavingSessionRule()
        prog = _spec_programme()
        socs_before = [s.capacity_soc for s in prog.slots]
        gcs_before = [s.grid_charge for s in prog.slots]

        result = rule.apply(prog, _inputs(
            now_utc=datetime(2026, 5, 2, 17, 0, tzinfo=timezone.utc),
            saving=False,
        ))

        assert [s.capacity_soc for s in result.slots] == socs_before
        assert [s.grid_charge for s in result.slots] == gcs_before
        assert not any("SavingSession" in r for r in result.reason_log)

    def test_active_session_drains_current_slot_to_floor(self):
        """Session at 18:00 BST falls in slot 3 (18:30 BST exclusive
        end). Set now=17:00 BST = 16:00 UTC, slot 2 active. Slot 2
        should drain to min_soc=10, gc=False."""
        rule = SavingSessionRule()
        prog = _spec_programme()
        london = ZoneInfo("Europe/London")

        result = rule.apply(prog, _inputs(
            now_utc=datetime(2026, 5, 2, 16, 0, tzinfo=timezone.utc),  # 17:00 BST
            saving=True,
            tz=london,
        ))

        # Slot 2 (05:30-18:30 BST) is the active local slot at 17:00 BST.
        assert result.slots[1].capacity_soc == 10
        assert result.slots[1].grid_charge is False
        # Other slots preserved
        assert result.slots[0].capacity_soc == 80  # overnight cheap-charge intact
        assert result.slots[2].capacity_soc == 80  # evening reserve intact
        assert any("SavingSession" in r for r in result.reason_log)

    def test_no_local_tz_falls_back_to_now_tzinfo(self):
        """When local_tz is unset (e.g. a test that doesn't care) the
        rule still runs against now's own tzinfo. UTC `now` directly
        feeds the slot lookup."""
        rule = SavingSessionRule()
        prog = _spec_programme()

        # 17:00 UTC, no local_tz -> looks up slot containing 17:00.
        # Slot 2 (05:30-18:30) contains 17:00.
        result = rule.apply(prog, _inputs(
            now_utc=datetime(2026, 5, 2, 17, 0, tzinfo=timezone.utc),
            saving=True,
            tz=None,
        ))
        assert result.slots[1].capacity_soc == 10

    def test_already_at_floor_logs_no_change(self):
        rule = SavingSessionRule()
        prog = _spec_programme()
        prog.slots[1].capacity_soc = 10
        prog.slots[1].grid_charge = False

        result = rule.apply(prog, _inputs(
            now_utc=datetime(2026, 5, 2, 16, 0, tzinfo=timezone.utc),
            saving=True,
            tz=ZoneInfo("Europe/London"),
        ))
        assert result.slots[1].capacity_soc == 10
        assert any("already at floor" in r for r in result.reason_log)

    def test_active_session_sets_work_mode_selling_first(self):
        """SPEC §2 / §9 row 3: drain via 'Selling first' work mode so
        the inverter actually exports during the session.
        """
        rule = SavingSessionRule()
        prog = _spec_programme()
        result = rule.apply(prog, _inputs(
            now_utc=datetime(2026, 5, 2, 16, 0, tzinfo=timezone.utc),
            saving=True,
            tz=ZoneInfo("Europe/London"),
        ))
        assert result.work_mode == "Selling first"

    def test_inactive_session_leaves_work_mode_unset(self):
        rule = SavingSessionRule()
        prog = _spec_programme()
        # work_mode is None at construction; the rule shouldn't touch it.
        prog.work_mode = "Zero export to CT"
        result = rule.apply(prog, _inputs(
            now_utc=datetime(2026, 5, 2, 16, 0, tzinfo=timezone.utc),
            saving=False,
        ))
        assert result.work_mode == "Zero export to CT"

    def test_overnight_slot_correctly_resolved_through_local_tz(self):
        """A session at 00:30 BST = 23:30 UTC. Without tz, lookup uses
        UTC 23:30 which falls in slot 4 (23:30-23:55). With tz, we
        project to BST 00:30 which falls in slot 1 (00:00-05:30).
        Verifies the tz path picks slot 1."""
        rule = SavingSessionRule()
        prog = _spec_programme()
        london = ZoneInfo("Europe/London")

        result = rule.apply(prog, _inputs(
            now_utc=datetime(2026, 5, 2, 23, 30, tzinfo=timezone.utc),
            saving=True,
            tz=london,
        ))
        # 00:30 BST -> slot 1
        assert result.slots[0].capacity_soc == 10
        assert result.slots[0].grid_charge is False
        # Slot 4 (23:30-23:55 BST) untouched
        assert result.slots[3].capacity_soc == 80
        assert result.slots[3].grid_charge is True
