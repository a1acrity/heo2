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
    """Always-on safety floor.

    Holds the active slot's capacity_pct ≥ config.min_soc. Stops
    other rules from draining below floor.

    This is a tier-1 MUST. Other rules can claim higher SOC; this
    rule just enforces "not lower than floor". The Arbiter resolves
    by tier: this MUST always wins on the dimension it covers.
    """

    name = "min_soc_floor"
    tier = 1
    description = "Holds active slot ≥ config.min_soc, regardless of other rules"

    def __init__(self) -> None:
        # Max we'll claim is the user-set min_soc itself; nothing to tune
        # here other than via SystemConfig.min_soc which is user-set.
        self._params: dict[str, Tunable] = {}

    @property
    def parameters(self) -> dict[str, Tunable]:
        return self._params

    def evaluate(self, snap: Snapshot, ctx: RuleContext) -> Claim | None:
        # If EPS is active, EPSLockdownRule overrides anyway.
        if snap.flags.eps_active:
            return None

        floor = snap.config.min_soc
        # Window: from now to end of next slot transition (1h granularity
        # is fine for a floor — the inverter will respect it indefinitely
        # if the active slot's capacity_pct is set to >= floor).
        window = TimeRange(
            start=snap.captured_at,
            end=snap.captured_at + timedelta(hours=1),
        )
        return Claim(
            rule_name=self.name,
            intent=HoldIntent(soc_pct=floor, window=window),
            rationale=f"min_soc floor at {floor}%",
            strength=ClaimStrength.MUST,
            horizon=window,
            expected_pence_impact=0.0,
        )
