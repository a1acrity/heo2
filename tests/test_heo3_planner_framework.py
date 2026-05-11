"""Planner framework tests — Tunable, Arbiter, RuleEngine, SwitchablePlanner."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from heo3.build import ActionBuilder
from heo3.compute import Compute
from heo3.planner.arbiter import Arbiter
from heo3.planner.engine import RuleEngine, SwitchablePlanner
from heo3.planner.rule import (
    ChargeIntent,
    Claim,
    ClaimStrength,
    DeferEVIntent,
    HoldIntent,
    LockdownIntent,
    Rule,
    RuleContext,
    Tunable,
    TeslaLimitIntent,
)
from heo3.types import Snapshot, TimeRange

from .heo3_fixtures import make_snapshot


# ── Tunable ───────────────────────────────────────────────────────


class TestTunable:
    def test_default_initialised(self):
        t = Tunable("x", default=5.0, lower=0.0, upper=10.0)
        assert t.current == 5.0

    def test_default_outside_bounds_raises(self):
        with pytest.raises(ValueError, match="outside bounds"):
            Tunable("x", default=15.0, lower=0.0, upper=10.0)

    def test_set_within_bounds(self):
        t = Tunable("x", default=5.0, lower=0.0, upper=10.0)
        assert t.set(7.0) == 7.0
        assert t.current == 7.0

    def test_set_clamps_above_upper(self):
        t = Tunable("x", default=5.0, lower=0.0, upper=10.0)
        assert t.set(15.0) == 10.0
        assert t.current == 10.0

    def test_set_clamps_below_lower(self):
        t = Tunable("x", default=5.0, lower=0.0, upper=10.0)
        assert t.set(-2.0) == 0.0
        assert t.current == 0.0


# ── Test rule helpers ─────────────────────────────────────────────


class _StubRule:
    """Minimal Rule for testing the engine + arbiter."""

    def __init__(
        self, *, name: str, tier: int, claim: Claim | None = None,
        raises: bool = False
    ) -> None:
        self._name = name
        self._tier = tier
        self._claim = claim
        self._raises = raises
        self._params: dict[str, Tunable] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def tier(self) -> int:
        return self._tier

    @property
    def description(self) -> str:
        return "test"

    @property
    def parameters(self) -> dict[str, Tunable]:
        return self._params

    def evaluate(self, snap, ctx):
        if self._raises:
            raise RuntimeError("oops")
        return self._claim


def _claim(name: str, intent, *, strength=ClaimStrength.PREFER, snap=None):
    snap = snap or make_snapshot()
    return Claim(
        rule_name=name,
        intent=intent,
        rationale=f"{name} fired",
        strength=strength,
        horizon=TimeRange(
            start=snap.captured_at, end=snap.captured_at + timedelta(hours=1)
        ),
    )


# ── Arbiter ───────────────────────────────────────────────────────


class TestArbiter:
    def test_no_claims_returns_empty_action(self):
        arb = Arbiter(ActionBuilder())
        snap = make_snapshot()
        result = arb.arbitrate([], snap)
        assert result.outcomes == []
        assert result.action.work_mode is None

    def test_must_claim_wins_absolutely(self):
        arb = Arbiter(ActionBuilder())
        snap = make_snapshot(eps_active=True)
        lockdown = _claim("eps", LockdownIntent(), strength=ClaimStrength.MUST, snap=snap)
        prefer_charge = _claim(
            "cheap_charge",
            ChargeIntent(target_soc_pct=80, by_time=snap.captured_at + timedelta(hours=4)),
            snap=snap,
        )
        result = arb.arbitrate([lockdown, prefer_charge], snap)
        assert result.winning_rule_names == ("eps",)
        # The PREFER charge claim is overridden.
        for o in result.outcomes:
            if o.claim.rule_name == "cheap_charge":
                assert not o.won
                assert "lockdown" in o.reason

    def test_two_prefers_same_dimension_first_wins(self):
        arb = Arbiter(ActionBuilder())
        snap = make_snapshot()
        a = _claim("a", ChargeIntent(target_soc_pct=80, by_time=snap.captured_at + timedelta(hours=4)), snap=snap)
        b = _claim("b", ChargeIntent(target_soc_pct=60, by_time=snap.captured_at + timedelta(hours=4)), snap=snap)
        result = arb.arbitrate([a, b], snap)
        assert "a" in result.winning_rule_names
        assert "b" not in result.winning_rule_names

    def test_different_intent_types_dont_conflict(self):
        arb = Arbiter(ActionBuilder())
        snap = make_snapshot()
        charge = _claim("charge", ChargeIntent(target_soc_pct=80, by_time=snap.captured_at + timedelta(hours=4)), snap=snap)
        defer_ev = _claim("defer", DeferEVIntent(), snap=snap)
        result = arb.arbitrate([charge, defer_ev], snap)
        # Both should win — different dimensions.
        assert "charge" in result.winning_rule_names
        assert "defer" in result.winning_rule_names

    def test_offer_yields_to_prefer_on_same_dim(self):
        arb = Arbiter(ActionBuilder())
        snap = make_snapshot()
        prefer = _claim(
            "high",
            ChargeIntent(target_soc_pct=80, by_time=snap.captured_at + timedelta(hours=4)),
            strength=ClaimStrength.PREFER, snap=snap,
        )
        offer = _claim(
            "low",
            ChargeIntent(target_soc_pct=60, by_time=snap.captured_at + timedelta(hours=4)),
            strength=ClaimStrength.OFFER, snap=snap,
        )
        result = arb.arbitrate([prefer, offer], snap)
        assert "high" in result.winning_rule_names
        assert "low" not in result.winning_rule_names

    def test_audit_includes_losing_claims(self):
        arb = Arbiter(ActionBuilder())
        snap = make_snapshot()
        a = _claim("a", ChargeIntent(target_soc_pct=80, by_time=snap.captured_at + timedelta(hours=4)), snap=snap)
        b = _claim("b", ChargeIntent(target_soc_pct=60, by_time=snap.captured_at + timedelta(hours=4)), snap=snap)
        result = arb.arbitrate([a, b], snap)
        audit = result.to_audit_claims()
        assert len(audit) == 2
        names = {c["rule_name"] for c in audit}
        assert names == {"a", "b"}

    def test_rationale_includes_winning_rules(self):
        arb = Arbiter(ActionBuilder())
        snap = make_snapshot()
        a = _claim("a", DeferEVIntent(), snap=snap)
        b = _claim("b", TeslaLimitIntent(charge_limit_pct=80), snap=snap)
        result = arb.arbitrate([a, b], snap)
        assert "a:" in result.rationale
        assert "b:" in result.rationale


# ── RuleEngine ────────────────────────────────────────────────────


class TestRuleEngine:
    @pytest.mark.asyncio
    async def test_no_rules_returns_empty_decision(self):
        engine = RuleEngine([], compute=Compute(), arbiter=Arbiter(ActionBuilder()))
        decision = await engine.decide(make_snapshot())
        assert decision.active_rules == ()

    @pytest.mark.asyncio
    async def test_runs_all_rules_in_tier_order(self):
        snap = make_snapshot()
        r3 = _StubRule(
            name="opt", tier=3,
            claim=_claim("opt", DeferEVIntent(), strength=ClaimStrength.OFFER, snap=snap),
        )
        r1 = _StubRule(
            name="safety", tier=1,
            claim=_claim("safety", TeslaLimitIntent(charge_limit_pct=70), strength=ClaimStrength.MUST, snap=snap),
        )
        engine = RuleEngine([r3, r1], compute=Compute(), arbiter=Arbiter(ActionBuilder()))
        decision = await engine.decide(snap)
        # Both should fire (no conflict).
        assert "safety" in decision.active_rules
        assert "opt" in decision.active_rules

    @pytest.mark.asyncio
    async def test_rule_exception_skipped_not_propagated(self):
        snap = make_snapshot()
        r_bad = _StubRule(name="bad", tier=2, raises=True)
        r_good = _StubRule(
            name="good", tier=2,
            claim=_claim("good", DeferEVIntent(), snap=snap),
        )
        engine = RuleEngine(
            [r_bad, r_good], compute=Compute(), arbiter=Arbiter(ActionBuilder())
        )
        decision = await engine.decide(snap)
        assert decision.active_rules == ("good",)

    @pytest.mark.asyncio
    async def test_find_rule(self):
        r = _StubRule(name="x", tier=1)
        engine = RuleEngine([r], compute=Compute(), arbiter=Arbiter(ActionBuilder()))
        assert engine.find_rule("x") is r
        assert engine.find_rule("nope") is None


# ── SwitchablePlanner ─────────────────────────────────────────────


class TestSwitchablePlanner:
    @pytest.mark.asyncio
    async def test_uses_rule_engine_when_enabled(self):
        rule_engine = MagicMock()
        from heo3.coordinator import Decision
        from heo3.types import PlannedAction

        async def decide(snap):
            return Decision(
                action=PlannedAction(rationale="rule"),
                active_rules=("rule_engine",),
            )

        rule_engine.decide = decide
        fallback = MagicMock()
        async def fallback_decide(snap):
            return Decision(
                action=PlannedAction(rationale="static"),
                active_rules=("static",),
            )
        fallback.decide = fallback_decide

        planner = SwitchablePlanner(rule_engine, fallback, is_enabled=lambda: True)
        decision = await planner.decide(make_snapshot())
        assert decision.active_rules == ("rule_engine",)

    @pytest.mark.asyncio
    async def test_uses_fallback_when_disabled(self):
        from heo3.coordinator import Decision
        from heo3.types import PlannedAction

        rule_engine = MagicMock()
        async def re_decide(snap):
            raise AssertionError("should not be called")
        rule_engine.decide = re_decide

        fallback = MagicMock()
        async def fallback_decide(snap):
            return Decision(
                action=PlannedAction(rationale="static"),
                active_rules=("static",),
            )
        fallback.decide = fallback_decide

        planner = SwitchablePlanner(rule_engine, fallback, is_enabled=lambda: False)
        decision = await planner.decide(make_snapshot())
        assert decision.active_rules == ("static",)
