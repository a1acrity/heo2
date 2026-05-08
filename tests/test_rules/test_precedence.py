# tests/test_rules/test_precedence.py
"""Cross-rule precedence tests (HEO-31 Phase 3 PR 1).

Pin the current "execution-order defines winner" semantics so the
decide/apply rebuild in PR 2 has a regression net to preserve. Each
test runs a small subset of the registry in registry order against a
fabricated state where two rules' writes collide on the same slot,
then asserts which wins.

The matrix doc (`docs/rule_field_overlap.md`) enumerates the rule
pairs that overlap on each field. This file pins the resolution for
every pair the matrix flagged. F2 is the one matrix-flagged real bug
(SavingSession losing to IGODispatch on overlapping slot) - that
test fails on master and is fixed in this PR.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from heo2.models import (
    PlannedDispatch,
    ProgrammeInputs,
    ProgrammeState,
    RateSlot,
    SlotConfig,
)
from heo2.rules.baseline import BaselineRule
from heo2.rules.eps_mode import EPSModeRule
from heo2.rules.ev_charging import EVChargingRule
from heo2.rules.evening_protect import EveningProtectRule
from heo2.rules.export_window import ExportWindowRule
from heo2.rules.igo_dispatch import IGODispatchRule
from heo2.rules.peak_export_arbitrage import PeakExportArbitrageRule
from heo2.rules.safety import SafetyRule
from heo2.rules.saving_session import SavingSessionRule
from heo2.rules.solar_surplus import SolarSurplusRule


LONDON = ZoneInfo("Europe/London")


def _run(rules, state, inputs):
    """Apply rules in order, mirroring RuleEngine without ProgrammeState.default()."""
    for rule in rules:
        state = rule.apply(state, inputs)
    return state


def _baseline_inputs(*, now_utc, **overrides) -> ProgrammeInputs:
    """Inputs in the same shape `default_inputs` produces, but parameterised."""
    base = dict(
        now=now_utc,
        current_soc=80.0,
        battery_capacity_kwh=20.0,
        min_soc=10.0,
        import_rates=[],
        export_rates=[],
        solar_forecast_kwh=[0.0] * 24,
        load_forecast_kwh=[0.5] * 24,
        igo_dispatching=False,
        saving_session=False,
        saving_session_start=None,
        saving_session_end=None,
        ev_charging=False,
        grid_connected=True,
        active_appliances=[],
        appliance_expected_kwh=0.0,
        local_tz=LONDON,
    )
    base.update(overrides)
    return ProgrammeInputs(**base)


# ---------------------------------------------------------------------------
# F2 - the real bug. SavingSession must beat IGODispatch on the slot it
# is actively draining. Without the guard, an IGO planned dispatch
# overlapping a saving-session slot refills the battery from grid mid-
# session, undoing the £3+/kWh export.
# ---------------------------------------------------------------------------
class TestF2_SavingSessionVsIGODispatch:
    def test_planned_dispatch_does_not_override_active_saving_session_slot(
        self,
    ):
        """A saving session is active right now. A planned IGO dispatch
        covers the same slot. Registry order is SavingSession, then
        IGODispatch. SavingSession's drain (cap=floor, gc=False) must
        survive IGODispatch's pre-position pass."""
        # 17:00 UTC = 18:00 BST -> slot 2 (05:30-18:30 BST) of the
        # default programme.
        now_utc = datetime(2026, 4, 13, 17, 0, tzinfo=timezone.utc)
        # Planned dispatch covers 17:00-18:00 UTC (slot 2 in BST).
        dispatch = PlannedDispatch(
            start=now_utc,
            end=now_utc + timedelta(hours=1),
            charge_kwh=-3.0,
            source="smart-charge",
        )
        inputs = _baseline_inputs(
            now_utc=now_utc,
            saving_session=True,
            saving_session_start=now_utc,
            saving_session_end=now_utc + timedelta(hours=1),
            planned_dispatches=[dispatch],
        )

        state = ProgrammeState.default(min_soc=10)
        state = _run(
            [BaselineRule(), SavingSessionRule(), IGODispatchRule()],
            state,
            inputs,
        )

        # The saving session slot is the one containing local-now.
        # SavingSession set it to cap=floor=10, gc=False. IGODispatch
        # must NOT have reset it.
        local_now = inputs.now_local()
        sess_idx = state.find_slot_at(local_now.time())
        sess_slot = state.slots[sess_idx]
        assert sess_slot.capacity_soc == 10, (
            f"SavingSession drain on slot {sess_idx + 1} was overridden by "
            f"IGODispatch (cap={sess_slot.capacity_soc}, gc={sess_slot.grid_charge}). "
            "Real revenue loss: dispatch refills mid-session, killing the export."
        )
        assert sess_slot.grid_charge is False, (
            f"SavingSession set grid_charge=False on slot {sess_idx + 1}; "
            f"IGODispatch flipped it to {sess_slot.grid_charge}."
        )
        # And IGODispatch should record that it deferred to the session.
        assert any(
            "saving session" in r.lower() for r in state.reason_log
        ), "IGODispatch should log that it skipped the session slot"

    def test_active_dispatch_does_not_override_active_saving_session_slot(
        self,
    ):
        """Same precedence applies to the legacy active-dispatch path
        (igo_dispatching=True, planned_dispatches empty)."""
        now_utc = datetime(2026, 4, 13, 17, 0, tzinfo=timezone.utc)
        inputs = _baseline_inputs(
            now_utc=now_utc,
            saving_session=True,
            saving_session_start=now_utc,
            saving_session_end=now_utc + timedelta(hours=1),
            igo_dispatching=True,
        )

        state = ProgrammeState.default(min_soc=10)
        state = _run(
            [BaselineRule(), SavingSessionRule(), IGODispatchRule()],
            state,
            inputs,
        )

        sess_idx = state.find_slot_at(inputs.now_local().time())
        sess_slot = state.slots[sess_idx]
        assert sess_slot.capacity_soc == 10
        assert sess_slot.grid_charge is False

    def test_planned_dispatch_on_other_slot_still_pre_positions(self):
        """Saving session covers slot 2; planned IGO dispatch covers
        slot 1 (overnight). Slot 1 should still be pre-positioned -
        the guard only protects the session's slot, not the whole
        programme."""
        now_utc = datetime(2026, 4, 13, 17, 0, tzinfo=timezone.utc)
        # Dispatch tonight 23:35-00:35 UTC -> 00:35-01:35 BST -> slot 1
        # (00:00-05:30 BST).
        dispatch_start = datetime(2026, 4, 13, 23, 35, tzinfo=timezone.utc)
        dispatch = PlannedDispatch(
            start=dispatch_start,
            end=dispatch_start + timedelta(hours=1),
        )
        inputs = _baseline_inputs(
            now_utc=now_utc,
            saving_session=True,
            saving_session_start=now_utc,
            saving_session_end=now_utc + timedelta(hours=1),
            planned_dispatches=[dispatch],
        )

        state = ProgrammeState.default(min_soc=10)
        state = _run(
            [BaselineRule(), SavingSessionRule(), IGODispatchRule()],
            state,
            inputs,
        )

        # Slot 1 (00:00-05:30 BST) should be pre-positioned by IGO.
        slot_1 = state.slots[0]
        assert slot_1.grid_charge is True
        assert slot_1.capacity_soc == 100
        # Slot 2 (the session slot) must still be drained.
        sess_idx = state.find_slot_at(inputs.now_local().time())
        assert state.slots[sess_idx].capacity_soc == 10
        assert state.slots[sess_idx].grid_charge is False


# ---------------------------------------------------------------------------
# Documents intentional / benign overlaps the matrix called out. These
# pin current behaviour so the decide/apply port in PR 2 must reproduce
# them (or change them deliberately, with the test changes visible in
# the diff).
# ---------------------------------------------------------------------------
class TestExportWindowVsSolarSurplusOnDaySlot:
    def test_export_window_overrides_solar_surplus_drain_target(self):
        """SolarSurplus raises a non-GC day slot's SOC to absorb solar.
        ExportWindow lowers the same slot to evening_floor when a
        worth-selling rate covers it. ExportWindow runs after, wins.
        The matrix calls this 'correct in spirit (sell at high prices >
        hold for solar absorption)'."""
        # 12:00 UTC = 13:00 BST -> slot 2 of default programme.
        now_utc = datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)
        # Build an export-rate environment where 13:00-14:00 BST is
        # genuinely worth selling (well above the IGO replacement cost).
        export_rates = [
            RateSlot(
                start=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
                end=datetime(2026, 4, 13, 12, 30, tzinfo=timezone.utc),
                rate_pence=30.0,
            ),
            RateSlot(
                start=datetime(2026, 4, 13, 12, 30, tzinfo=timezone.utc),
                end=datetime(2026, 4, 13, 13, 0, tzinfo=timezone.utc),
                rate_pence=30.0,
            ),
        ]
        inputs = _baseline_inputs(
            now_utc=now_utc,
            current_soc=80.0,
            export_rates=export_rates,
            solar_forecast_kwh=[2.0] * 24,
            load_forecast_kwh=[0.3] * 24,
        )
        state = ProgrammeState.default(min_soc=10)
        state = _run(
            [BaselineRule(), SolarSurplusRule(), ExportWindowRule()],
            state,
            inputs,
        )

        # Slot 2 (05:30-18:30 BST) covers the worth-selling 13:00 BST
        # window. SolarSurplus raised (or held) it; ExportWindow then
        # lowered it to evening_floor. The drain target wins.
        slot_2 = state.slots[1]
        assert slot_2.capacity_soc < 100, (
            "ExportWindow should drain slot 2 below 100% when a worth-"
            f"selling export rate covers it; got {slot_2.capacity_soc}"
        )


class TestEPSWinsOverEverything:
    def test_eps_drives_every_slot_to_zero_and_disables_gc(self):
        """EPSModeRule is the second-to-last in the registry and
        overrides every prior write: cap=0, gc=False on every slot.
        SafetyRule then runs but in EPS mode permits the 0 floor."""
        now_utc = datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)
        inputs = _baseline_inputs(
            now_utc=now_utc,
            eps_active=True,
            grid_connected=False,
            current_soc=80.0,
        )
        state = ProgrammeState.default(min_soc=10)
        # Pick a varied set so EPS has something to override.
        state = _run(
            [
                BaselineRule(),
                EveningProtectRule(),
                EPSModeRule(),
                SafetyRule(),
            ],
            state,
            inputs,
        )
        for i, s in enumerate(state.slots):
            assert s.capacity_soc == 0, (
                f"slot {i + 1} cap={s.capacity_soc} after EPS; expected 0"
            )
            assert s.grid_charge is False, (
                f"slot {i + 1} gc={s.grid_charge} after EPS; expected False"
            )


class TestEVChargingDeferredDuringIGODispatch:
    def test_ev_charging_does_not_lower_dispatch_target(self):
        """EVChargingRule raises the slot containing now to
        max(current_soc, min_soc). When igo_dispatching=True it is a
        no-op (matrix row 8) so IGODispatch's cap=100 survives."""
        now_utc = datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)
        inputs = _baseline_inputs(
            now_utc=now_utc,
            current_soc=40.0,
            igo_dispatching=True,
            ev_charging=True,
        )
        state = ProgrammeState.default(min_soc=10)
        state = _run(
            [BaselineRule(), IGODispatchRule(), EVChargingRule()],
            state,
            inputs,
        )

        idx = state.find_slot_at(inputs.now_local().time())
        slot = state.slots[idx]
        # IGODispatch sets cap=100; EVCharging is skipped during dispatch.
        assert slot.capacity_soc == 100
        assert slot.grid_charge is True


class TestWorkModeChain:
    """work_mode is written by Baseline (Zero export to CT) and three
    rules that all converge on 'Selling first'. Last-writer-wins; all
    three Selling-first writes are idempotent.
    """

    def test_baseline_default(self):
        now_utc = datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)
        inputs = _baseline_inputs(now_utc=now_utc)
        state = ProgrammeState.default(min_soc=10)
        state = BaselineRule().apply(state, inputs)
        assert state.work_mode == "Zero export to CT"

    def test_saving_session_overrides_baseline(self):
        now_utc = datetime(2026, 4, 13, 17, 0, tzinfo=timezone.utc)
        inputs = _baseline_inputs(
            now_utc=now_utc,
            saving_session=True,
            saving_session_start=now_utc,
            saving_session_end=now_utc + timedelta(hours=1),
        )
        state = ProgrammeState.default(min_soc=10)
        state = _run([BaselineRule(), SavingSessionRule()], state, inputs)
        assert state.work_mode == "Selling first"

    def test_peak_arbitrage_overrides_baseline_when_active(self):
        # PeakExportArbitrageRule flips work_mode to Selling first only
        # when actively inside an allocated top-priced slot AND has spare.
        now_utc = datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)
        # Today only - filter_today uses inputs.now's day.
        export_today = [
            RateSlot(
                start=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
                end=datetime(2026, 4, 13, 12, 30, tzinfo=timezone.utc),
                rate_pence=40.0,
            ),
        ]
        # Cheap window: the IGO replacement at 23:30 BST.
        import_today = [
            RateSlot(
                start=datetime(2026, 4, 13, 22, 30, tzinfo=timezone.utc),
                end=datetime(2026, 4, 14, 4, 30, tzinfo=timezone.utc),
                rate_pence=7.0,
            ),
            RateSlot(
                start=datetime(2026, 4, 13, 4, 30, tzinfo=timezone.utc),
                end=datetime(2026, 4, 13, 22, 30, tzinfo=timezone.utc),
                rate_pence=27.88,
            ),
        ]
        inputs = _baseline_inputs(
            now_utc=now_utc,
            current_soc=90.0,  # plenty spare above floor
            import_rates=import_today,
            export_rates=export_today,
            solar_forecast_kwh=[3.0] * 24,
            load_forecast_kwh=[0.3] * 24,
        )
        state = ProgrammeState.default(min_soc=10)
        state = _run(
            [BaselineRule(), PeakExportArbitrageRule()], state, inputs,
        )
        assert state.work_mode == "Selling first"
