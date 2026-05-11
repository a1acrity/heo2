"""RuleEngine — runs all rules, builds RuleContext, hands claims to Arbiter.

Conforms to the Planner Protocol from coordinator.py — exposes
`async decide(snap) -> Decision`.

Owns the rule list (sorted by tier) and the current parameter values.
The Tuner adjusts parameters by name + value; engine reflects the
new value into the next tick's RuleContext.
"""

from __future__ import annotations

import logging

from ..compute import Compute
from ..coordinator import Decision
from ..types import PlannedAction, Snapshot
from .arbiter import Arbiter
from .rule import HistoricalView, Rule, RuleContext

logger = logging.getLogger(__name__)


class RuleEngine:
    """The decision-making layer. Implements the Planner Protocol."""

    def __init__(
        self,
        rules: list[Rule],
        *,
        compute: Compute,
        arbiter: Arbiter,
        get_historical: callable | None = None,
    ) -> None:
        # Sort by tier so logging + iteration is deterministic.
        self._rules = sorted(rules, key=lambda r: (r.tier, r.name))
        self._compute = compute
        self._arbiter = arbiter
        self._get_historical = get_historical or (lambda: HistoricalView())

    async def decide(self, snap: Snapshot) -> Decision:
        """Run all rules + arbitrate + return Decision.

        Returns a Decision even when no rules fire (empty action +
        empty active_rules tuple). The coordinator + tracker treat
        this as "tick happened, nothing to do".
        """
        historical = self._get_historical()

        claims = []
        for rule in self._rules:
            ctx = RuleContext(
                compute=self._compute,
                parameters={
                    k: v.current for k, v in rule.parameters.items()
                },
                historical=historical,
            )
            try:
                claim = rule.evaluate(snap, ctx)
            except Exception:
                logger.exception(
                    "RuleEngine: rule %s raised, skipping", rule.name
                )
                continue
            if claim is not None:
                claims.append(claim)

        result = self._arbiter.arbitrate(claims, snap)

        return Decision(
            action=result.action,
            rationale=result.rationale,
            active_rules=result.winning_rule_names,
            claims=result.to_audit_claims(),
        )

    def find_rule(self, name: str) -> Rule | None:
        for rule in self._rules:
            if rule.name == name:
                return rule
        return None

    @property
    def rules(self) -> list[Rule]:
        return list(self._rules)


# ── SwitchablePlanner ─────────────────────────────────────────────


class SwitchablePlanner:
    """Wraps RuleEngine + a fallback so users can disable the rule
    engine via switch.heo3_planner_enabled.

    Coordinator-facing: delegates `decide` based on the switch state
    at call time (read fresh each tick). Lets the user kill-switch
    back to baseline_static without restarting HA.
    """

    def __init__(
        self,
        rule_engine: RuleEngine,
        fallback,  # any Planner Protocol implementer (e.g. StaticBaselinePlanner)
        is_enabled: callable,
    ) -> None:
        self._rule_engine = rule_engine
        self._fallback = fallback
        self._is_enabled = is_enabled

    async def decide(self, snap: Snapshot) -> Decision:
        if self._is_enabled():
            return await self._rule_engine.decide(snap)
        return await self._fallback.decide(snap)
