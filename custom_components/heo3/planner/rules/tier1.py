"""Tier 1 — Safety rules. MUST claims that other rules cannot override."""

from __future__ import annotations

from datetime import timedelta

from ...types import Snapshot, TimeRange
from ..rule import (
    Claim,
    ClaimStrength,
    HoldIntent,
    LockdownIntent,
    Rule,
    RuleContext,
    Tunable,
)


class EPSLockdownRule:
    """SPEC H3: when grid voltage drops, lock down everything.

    Tier-1 MUST that overrides every other rule. The Arbiter's
    lockdown short-circuit ensures the resulting PlannedAction has
    all slots cap=0%, gc=False, EV stopped, appliances off.
    """

    name = "eps_lockdown"
    tier = 1
    description = "SPEC H3 — grid down, lock down inverter + peripherals"

    def __init__(self) -> None:
        self._params: dict[str, Tunable] = {}

    @property
    def parameters(self) -> dict[str, Tunable]:
        return self._params

    def evaluate(self, snap: Snapshot, ctx: RuleContext) -> Claim | None:
        if not snap.flags.eps_active:
            return None
        return Claim(
            rule_name=self.name,
            intent=LockdownIntent(),
            rationale="EPS active — grid down, locking inverter to floor",
            strength=ClaimStrength.MUST,
            horizon=TimeRange(
                start=snap.captured_at,
                end=snap.captured_at + timedelta(hours=24),
            ),
            expected_pence_impact=0.0,
        )


class MinSOCFloorRule:
    """Observable floor — only fires when no other rule has claimed the slot.

    The OPERATOR's safety validation (inverter_validate.SafetyError)
    already rejects plans with slot capacity_pct < config.min_soc.
    This rule is the OBSERVABLE counterpart: it claims the floor as
    OFFER so the user can see "no other rule decided, falling back
    to min_soc floor". Other rules override.

    Even though it's tier=1 by classification (safety), the strength
    is OFFER so it yields to any rule with a real opinion. The hard
    safety enforcement lives at the operator layer where it belongs.
    """

    name = "min_soc_floor"
    tier = 1
    description = "Observable fallback to config.min_soc (operator enforces hard)"

    def __init__(self) -> None:
        self._params: dict[str, Tunable] = {}

    @property
    def parameters(self) -> dict[str, Tunable]:
        return self._params

    def evaluate(self, snap: Snapshot, ctx: RuleContext) -> Claim | None:
        if snap.flags.eps_active:
            return None

        floor = snap.config.min_soc
        window = TimeRange(
            start=snap.captured_at,
            end=snap.captured_at + timedelta(hours=1),
        )
        return Claim(
            rule_name=self.name,
            intent=HoldIntent(soc_pct=floor, window=window),
            rationale=f"min_soc floor at {floor}% (fallback)",
            strength=ClaimStrength.OFFER,
            horizon=window,
            expected_pence_impact=0.0,
        )
