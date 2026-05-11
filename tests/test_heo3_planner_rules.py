"""Tests for the 9 concrete rules across all 3 tiers."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from heo3.compute import Compute
from heo3.planner.rule import (
    ChargeIntent,
    Claim,
    ClaimStrength,
    DeferEVIntent,
    DrainIntent,
    HistoricalView,
    HoldIntent,
    LockdownIntent,
    RuleContext,
    SellIntent,
)
from heo3.planner.rules import (
    CheapRateChargeRule,
    EPSLockdownRule,
    EveningDrainRule,
    EVDeferralRule,
    IGODispatchRule,
    MinSOCFloorRule,
    PeakExportArbitrageRule,
    SavingSessionRule,
    SolarSurplusRule,
)
from heo3.types import (
    EVState,
    IGOPlannedDispatch,
    SystemConfig,
    SystemFlags,
    TimeRange,
    RatePeriod,
)

from .heo3_fixtures import make_snapshot


def _ctx(rule, snap=None) -> RuleContext:
    return RuleContext(
        compute=Compute(),
        parameters={k: v.current for k, v in rule.parameters.items()},
        historical=HistoricalView(),
    )


# ── Tier 1 — EPSLockdownRule ──────────────────────────────────────


class TestEPSLockdownRule:
    def test_no_trigger_when_grid_up(self):
        rule = EPSLockdownRule()
        snap = make_snapshot(eps_active=False)
        assert rule.evaluate(snap, _ctx(rule)) is None

    def test_fires_must_when_eps_active(self):
        rule = EPSLockdownRule()
        snap = make_snapshot(eps_active=True)
        claim = rule.evaluate(snap, _ctx(rule))
        assert claim is not None
        assert claim.strength == ClaimStrength.MUST
        assert isinstance(claim.intent, LockdownIntent)


# ── Tier 1 — MinSOCFloorRule ──────────────────────────────────────


class TestMinSOCFloorRule:
    def test_fires_when_grid_up(self):
        rule = MinSOCFloorRule()
        snap = make_snapshot(eps_active=False, config=SystemConfig(min_soc=15))
        claim = rule.evaluate(snap, _ctx(rule))
        assert claim is not None
        assert claim.strength == ClaimStrength.MUST
        assert isinstance(claim.intent, HoldIntent)
        assert claim.intent.soc_pct == 15

    def test_yields_during_eps(self):
        # EPSLockdownRule overrides; floor rule shouldn't claim.
        rule = MinSOCFloorRule()
        snap = make_snapshot(eps_active=True)
        assert rule.evaluate(snap, _ctx(rule)) is None


# ── Tier 2 — SavingSessionRule ────────────────────────────────────


class TestSavingSessionRule:
    def test_no_trigger_when_inactive(self):
        rule = SavingSessionRule()
        snap = make_snapshot()
        assert rule.evaluate(snap, _ctx(rule)) is None

    def test_fires_during_active_session(self):
        rule = SavingSessionRule()
        captured = datetime(2026, 5, 12, 17, 30, tzinfo=timezone.utc)
        flags = SystemFlags(
            saving_session_active=True,
            saving_session_window=TimeRange(
                start=captured - timedelta(minutes=30),
                end=captured + timedelta(minutes=30),
            ),
        )
        snap = make_snapshot(captured_at=captured)
        snap = replace(snap, flags=flags)
        claim = rule.evaluate(snap, _ctx(rule))
        assert claim is not None
        assert claim.strength == ClaimStrength.PREFER
        assert isinstance(claim.intent, DrainIntent)
        # Target = min_soc + buffer (defaults: 10 + 5 = 15)
        assert claim.intent.target_soc_pct == 15

    def test_no_fire_outside_window(self):
        rule = SavingSessionRule()
        captured = datetime(2026, 5, 12, 19, 0, tzinfo=timezone.utc)
        flags = SystemFlags(
            saving_session_active=True,
            saving_session_window=TimeRange(
                start=captured - timedelta(hours=2),
                end=captured - timedelta(hours=1),  # already ended
            ),
        )
        snap = make_snapshot(captured_at=captured)
        snap = replace(snap, flags=flags)
        assert rule.evaluate(snap, _ctx(rule)) is None


# ── Tier 2 — IGODispatchRule ──────────────────────────────────────


class TestIGODispatchRule:
    def test_no_trigger_when_not_dispatching(self):
        rule = IGODispatchRule()
        snap = make_snapshot()
        assert rule.evaluate(snap, _ctx(rule)) is None

    def test_fires_during_dispatch(self):
        rule = IGODispatchRule()
        captured = datetime(2026, 5, 12, 1, 30, tzinfo=timezone.utc)
        flags = SystemFlags(
            igo_dispatching=True,
            igo_planned=(
                IGOPlannedDispatch(
                    start=captured - timedelta(minutes=30),
                    end=captured + timedelta(hours=2),
                ),
            ),
        )
        snap = replace(make_snapshot(captured_at=captured), flags=flags)
        claim = rule.evaluate(snap, _ctx(rule))
        assert claim is not None
        assert claim.strength == ClaimStrength.PREFER
        assert isinstance(claim.intent, ChargeIntent)
        assert claim.intent.target_soc_pct == 80  # default

    def test_yields_to_saving_session(self):
        # F2 fix: IGO must yield when saving session is active.
        rule = IGODispatchRule()
        captured = datetime(2026, 5, 12, 1, 30, tzinfo=timezone.utc)
        flags = SystemFlags(
            igo_dispatching=True,
            saving_session_active=True,
            saving_session_window=TimeRange(
                start=captured - timedelta(hours=1),
                end=captured + timedelta(hours=1),
            ),
        )
        snap = replace(make_snapshot(captured_at=captured), flags=flags)
        assert rule.evaluate(snap, _ctx(rule)) is None


# ── Tier 2 — EVDeferralRule ───────────────────────────────────────


class TestEVDeferralRule:
    def test_no_trigger_when_defer_disabled(self):
        rule = EVDeferralRule()
        # defer_ev_eligible defaults to False
        snap = make_snapshot()
        assert rule.evaluate(snap, _ctx(rule)) is None

    def test_no_trigger_when_ev_not_charging(self):
        rule = EVDeferralRule()
        flags = SystemFlags(defer_ev_eligible=True)
        snap = replace(
            make_snapshot(),
            flags=flags,
            ev=EVState(charging=False),
        )
        assert rule.evaluate(snap, _ctx(rule)) is None

    def test_fires_during_top_export_window(self):
        rule = EVDeferralRule()
        captured = datetime(2026, 5, 12, 17, 0, tzinfo=timezone.utc)
        # Construct export rates so 17:00-18:00 is the top one.
        export = (
            RatePeriod(
                start=captured,
                end=captured + timedelta(hours=1),
                rate_pence=30.0,
            ),
            RatePeriod(
                start=captured + timedelta(hours=1),
                end=captured + timedelta(hours=2),
                rate_pence=10.0,
            ),
        )
        flags = SystemFlags(defer_ev_eligible=True)
        snap = make_snapshot(captured_at=captured, export_today=export)
        snap = replace(
            snap,
            flags=flags,
            ev=EVState(charging=True),
        )
        claim = rule.evaluate(snap, _ctx(rule))
        assert claim is not None
        assert isinstance(claim.intent, DeferEVIntent)


# ── Tier 3 — CheapRateChargeRule ──────────────────────────────────


class TestCheapRateChargeRule:
    def test_no_trigger_when_no_cheap_window(self):
        rule = CheapRateChargeRule()
        snap = make_snapshot(rates=((), ()))  # no rates
        assert rule.evaluate(snap, _ctx(rule)) is None

    def test_fires_during_cheap_window(self):
        rule = CheapRateChargeRule()
        # cheap_then_peak fixture: 00:00-05:30 is cheap.
        captured = datetime(2026, 5, 12, 2, 0, tzinfo=timezone.utc)
        snap = make_snapshot(
            captured_at=captured,
            soc_pct=20.0,
            today_load_kwh=tuple([1.0] * 24),  # nontrivial load to bridge
            tomorrow_load_kwh=tuple([1.0] * 24),
        )
        claim = rule.evaluate(snap, _ctx(rule))
        assert claim is not None
        assert claim.strength == ClaimStrength.PREFER
        assert isinstance(claim.intent, ChargeIntent)
        # Should target ABOVE current SOC (20) since we need to charge.
        assert claim.intent.target_soc_pct > 20

    def test_no_fire_when_already_above_target(self):
        rule = CheapRateChargeRule()
        captured = datetime(2026, 5, 12, 2, 0, tzinfo=timezone.utc)
        # Battery already at 100% — no further charge needed.
        snap = make_snapshot(captured_at=captured, soc_pct=100.0)
        assert rule.evaluate(snap, _ctx(rule)) is None


# ── Tier 3 — PeakExportArbitrageRule ──────────────────────────────


class TestPeakExportArbitrageRule:
    def test_no_trigger_when_no_export_windows(self):
        rule = PeakExportArbitrageRule()
        snap = make_snapshot(soc_pct=80.0)  # has usable kWh
        assert rule.evaluate(snap, _ctx(rule)) is None

    def test_no_fire_when_no_usable_kwh(self):
        rule = PeakExportArbitrageRule()
        snap = make_snapshot(soc_pct=10.0, config=SystemConfig(min_soc=10))
        # usable_kwh = 0
        assert rule.evaluate(snap, _ctx(rule)) is None

    def test_fires_when_spread_above_threshold(self):
        rule = PeakExportArbitrageRule()
        captured = datetime(2026, 5, 12, 17, 0, tzinfo=timezone.utc)
        # Export at 50p, peak import at ~30p (per cheap_then_peak helper).
        # Spread = 20p, well above default 8p threshold.
        export = (
            RatePeriod(
                start=captured,
                end=captured + timedelta(hours=1),
                rate_pence=50.0,
            ),
        )
        snap = make_snapshot(
            captured_at=captured, soc_pct=80.0, export_today=export,
        )
        claim = rule.evaluate(snap, _ctx(rule))
        assert claim is not None
        assert isinstance(claim.intent, SellIntent)
        assert claim.intent.kwh > 0

    def test_no_fire_when_spread_below_threshold(self):
        rule = PeakExportArbitrageRule()
        captured = datetime(2026, 5, 12, 17, 0, tzinfo=timezone.utc)
        # Export 32p vs peak import 30p = 2p spread, below 8p threshold.
        export = (
            RatePeriod(
                start=captured,
                end=captured + timedelta(hours=1),
                rate_pence=32.0,
            ),
        )
        snap = make_snapshot(
            captured_at=captured, soc_pct=80.0, export_today=export,
        )
        assert rule.evaluate(snap, _ctx(rule)) is None


# ── Tier 3 — SolarSurplusRule ─────────────────────────────────────


class TestSolarSurplusRule:
    def test_no_fire_without_pv_data(self):
        rule = SolarSurplusRule()
        snap = make_snapshot()  # solar_power_w defaults to None
        assert rule.evaluate(snap, _ctx(rule)) is None

    def test_no_fire_below_surplus_threshold(self):
        rule = SolarSurplusRule()
        from heo3.types import InverterState
        snap = make_snapshot(soc_pct=80.0)
        # solar < load + threshold
        snap = replace(snap, inverter=InverterState(
            battery_soc_pct=80.0, solar_power_w=600.0, load_power_w=500.0
        ))
        assert rule.evaluate(snap, _ctx(rule)) is None

    def test_fires_with_surplus_and_headroom(self):
        rule = SolarSurplusRule()
        from heo3.types import InverterState
        snap = make_snapshot(soc_pct=80.0)  # has headroom
        snap = replace(snap, inverter=InverterState(
            battery_soc_pct=80.0, solar_power_w=2500.0, load_power_w=500.0
        ))
        claim = rule.evaluate(snap, _ctx(rule))
        assert claim is not None
        assert claim.strength == ClaimStrength.OFFER
        assert isinstance(claim.intent, HoldIntent)
        assert claim.intent.soc_pct == 100

    def test_no_fire_when_no_headroom(self):
        rule = SolarSurplusRule()
        from heo3.types import InverterState
        snap = make_snapshot(soc_pct=100.0)  # no headroom
        snap = replace(snap, inverter=InverterState(
            battery_soc_pct=100.0, solar_power_w=2500.0, load_power_w=500.0
        ))
        assert rule.evaluate(snap, _ctx(rule)) is None


# ── Tier 3 — EveningDrainRule ─────────────────────────────────────


class TestEveningDrainRule:
    def test_no_fire_outside_evening_window(self):
        rule = EveningDrainRule()
        captured = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
        snap = make_snapshot(captured_at=captured, soc_pct=80.0)
        assert rule.evaluate(snap, _ctx(rule)) is None

    def test_fires_during_evening(self):
        rule = EveningDrainRule()
        # 19:00 BST = 18:00 UTC (in May)
        captured = datetime(2026, 5, 12, 19, 0, tzinfo=timezone.utc)  # 20:00 BST
        snap = make_snapshot(captured_at=captured, soc_pct=80.0)
        claim = rule.evaluate(snap, _ctx(rule))
        assert claim is not None
        assert claim.strength == ClaimStrength.OFFER
        assert isinstance(claim.intent, DrainIntent)
        assert claim.intent.target_soc_pct == 25

    def test_no_fire_already_below_target(self):
        rule = EveningDrainRule()
        captured = datetime(2026, 5, 12, 19, 0, tzinfo=timezone.utc)
        snap = make_snapshot(captured_at=captured, soc_pct=20.0)
        assert rule.evaluate(snap, _ctx(rule)) is None
