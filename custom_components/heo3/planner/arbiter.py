"""Arbiter — resolves rule Claims into a single PlannedAction.

Three-pass:
1. Tier 1 MUSTs (safety): EPSLockdown, MinSOCFloor. Absolute.
2. Tier 2 modes (event-driven): SavingSession, IGODispatch, etc.
   PREFER beats OFFER. Same-strength conflicts: warn + use list order
   as deterministic tie-break.
3. Tier 3 optimisation: same conflict resolution as tier 2.
   Cannot override tier-1 or tier-2 outcomes.

Output: ArbitrationResult containing the resolved PlannedAction +
the full audit (winning + losing claims).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..build import ActionBuilder
from ..types import (
    ApplianceAction,
    EVAction,
    PlannedAction,
    Snapshot,
    TeslaAction,
)
from .rule import (
    Claim,
    ClaimStrength,
    ChargeIntent,
    DeferEVIntent,
    DrainIntent,
    HoldIntent,
    LockdownIntent,
    RestoreEVIntent,
    SellIntent,
    TeslaLimitIntent,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArbitrationOutcome:
    """One claim's fate after arbitration."""

    claim: Claim
    won: bool
    reason: str  # human-readable: "won (only PREFER claim)" / "lost to <other>"


@dataclass
class ArbitrationResult:
    """Output of the Arbiter."""

    action: PlannedAction
    outcomes: list[ArbitrationOutcome] = field(default_factory=list)

    @property
    def winning_rule_names(self) -> tuple[str, ...]:
        return tuple(
            o.claim.rule_name for o in self.outcomes if o.won
        )

    @property
    def rationale(self) -> str:
        """Human-readable summary built from winning claims."""
        winners = [o.claim for o in self.outcomes if o.won]
        if not winners:
            return "no rules fired"
        # Group by rule, list rationales.
        return "; ".join(f"{c.rule_name}: {c.rationale}" for c in winners)

    def to_audit_claims(self) -> tuple[dict, ...]:
        """Convert outcomes to dicts for the observability sensor."""
        return tuple(
            {
                "rule_name": o.claim.rule_name,
                "intent": type(o.claim.intent).__name__,
                "rationale": o.claim.rationale,
                "strength": o.claim.strength.value,
                "won": o.won,
                "arbitration": o.reason,
                "expected_pence_impact": o.claim.expected_pence_impact,
            }
            for o in self.outcomes
        )


class Arbiter:
    """Resolves Claims from multiple rules into one PlannedAction."""

    def __init__(self, builder: ActionBuilder) -> None:
        self._builder = builder

    def arbitrate(
        self, claims: list[Claim], snap: Snapshot
    ) -> ArbitrationResult:
        """Three-pass tier resolution. Returns ArbitrationResult."""
        outcomes: list[ArbitrationOutcome] = []

        # Group by tier — Tier 1 wins absolutely; lower tiers can't
        # override its decisions on the affected dimensions.
        tier1 = [c for c in claims if c.strength == ClaimStrength.MUST]
        tier2 = [c for c in claims if c.strength == ClaimStrength.PREFER]
        tier3 = [c for c in claims if c.strength == ClaimStrength.OFFER]

        # Pass 1 — MUSTs always win. If conflicting MUSTs, that's a bug.
        tier1_action = self._build_action(tier1, snap)
        for claim in tier1:
            outcomes.append(
                ArbitrationOutcome(claim=claim, won=True, reason="MUST (tier-1)")
            )

        # If a Lockdown MUST fired, return immediately — it overrides
        # everything else by design.
        if any(isinstance(c.intent, LockdownIntent) for c in tier1):
            for claim in tier2 + tier3:
                outcomes.append(
                    ArbitrationOutcome(
                        claim=claim, won=False, reason="overridden by lockdown"
                    )
                )
            return ArbitrationResult(action=tier1_action, outcomes=outcomes)

        # Pass 2 — PREFERs. Resolve same-dimension conflicts by list order.
        tier2_winners, tier2_losers = self._resolve_within_tier(tier2)
        for c in tier2_winners:
            outcomes.append(
                ArbitrationOutcome(claim=c, won=True, reason="PREFER, no conflict")
            )
        for c, reason in tier2_losers:
            outcomes.append(
                ArbitrationOutcome(claim=c, won=False, reason=reason)
            )

        # Pass 3 — OFFERs. Yield to anything tier-1 or tier-2 already covers.
        tier3_winners, tier3_losers = self._resolve_within_tier(tier3)
        # Filter tier-3 to ones that don't conflict with tier-1/tier-2 winners.
        higher = list(tier1) + tier2_winners
        for claim in tier3_winners[:]:
            if self._intent_conflicts(claim, higher):
                tier3_losers.append(
                    (claim, "yielded to higher-tier claim on same dimension")
                )
                tier3_winners.remove(claim)
        for c in tier3_winners:
            outcomes.append(
                ArbitrationOutcome(claim=c, won=True, reason="OFFER, no conflict")
            )
        for c, reason in tier3_losers:
            outcomes.append(
                ArbitrationOutcome(claim=c, won=False, reason=reason)
            )

        # Compose the final action: tier-1 + tier-2 winners + tier-3 winners.
        all_winners = tier1 + tier2_winners + tier3_winners
        action = self._build_action(all_winners, snap)

        return ArbitrationResult(action=action, outcomes=outcomes)

    # ── Internal ──────────────────────────────────────────────────

    @staticmethod
    def _resolve_within_tier(
        claims: list[Claim],
    ) -> tuple[list[Claim], list[tuple[Claim, str]]]:
        """For same-tier claims: detect conflicts by intent dimension.

        Two PREFER claims on the same dimension (e.g. both want
        ChargeIntent on the active slot): first wins, second loses
        with a warning.

        Different dimensions don't conflict (e.g. ChargeIntent +
        DeferEVIntent both pass).
        """
        winners: list[Claim] = []
        losers: list[tuple[Claim, str]] = []

        for claim in claims:
            conflict_with = next(
                (
                    w for w in winners
                    if Arbiter._intents_conflict(claim.intent, w.intent)
                ),
                None,
            )
            if conflict_with is None:
                winners.append(claim)
            else:
                losers.append(
                    (
                        claim,
                        f"lost to {conflict_with.rule_name} "
                        f"(same dimension, earlier in evaluation order)",
                    )
                )
                logger.warning(
                    "Arbiter: %s claim from %s lost to %s on conflicting "
                    "intent type %s",
                    claim.strength.value,
                    claim.rule_name,
                    conflict_with.rule_name,
                    type(claim.intent).__name__,
                )

        return winners, losers

    @staticmethod
    def _intent_conflicts(claim: Claim, others: list[Claim]) -> bool:
        return any(
            Arbiter._intents_conflict(claim.intent, o.intent) for o in others
        )

    @staticmethod
    def _intents_conflict(a, b) -> bool:
        """Two intents conflict if they target the same dimension.

        Conservative: any two intents of the same type conflict. Different
        types only conflict if they touch the same control surface
        (e.g. ChargeIntent + DrainIntent both write slots).
        """
        if type(a) is type(b):
            return True
        slot_intents = (ChargeIntent, DrainIntent, HoldIntent, SellIntent)
        if isinstance(a, slot_intents) and isinstance(b, slot_intents):
            return True
        # EV intents conflict with each other.
        if isinstance(a, (DeferEVIntent, RestoreEVIntent)) and isinstance(
            b, (DeferEVIntent, RestoreEVIntent)
        ):
            return True
        return False

    def _build_action(self, claims: list[Claim], snap: Snapshot) -> PlannedAction:
        """Compose a PlannedAction from a set of resolved claims."""
        if not claims:
            return PlannedAction(rationale="no rules fired")

        per_claim_actions: list[PlannedAction] = []
        for c in claims:
            action = self._intent_to_action(c, snap)
            if action is not None:
                per_claim_actions.append(action)

        if not per_claim_actions:
            return PlannedAction(rationale="claims produced no writes")

        return self._builder.merge(*per_claim_actions)

    def _intent_to_action(self, claim: Claim, snap: Snapshot) -> PlannedAction | None:
        """Translate one Claim into a PlannedAction via Build constructors."""
        intent = claim.intent

        if isinstance(intent, LockdownIntent):
            return self._builder.lockdown_eps(snap)
        if isinstance(intent, ChargeIntent):
            return self._builder.charge_to(
                target_soc_pct=intent.target_soc_pct,
                by=intent.by_time,
                snap=snap,
                rate_limit_a=intent.rate_limit_a,
            )
        if isinstance(intent, DrainIntent):
            return self._builder.drain_to(
                target_soc_pct=intent.target_soc_pct,
                by=intent.by_time,
                snap=snap,
            )
        if isinstance(intent, HoldIntent):
            return self._builder.hold_at(
                soc_pct=intent.soc_pct,
                window=intent.window,
                snap=snap,
            )
        if isinstance(intent, SellIntent):
            # Map across_slot_starts to the operator's RateWindow shape.
            # For now, we just pass kwh + an empty window list — the
            # operator's sell_kwh handles the active-slot lookup.
            from ..compute import RateWindow

            windows = [
                RateWindow(start=t, end=t, rate_pence=0.0, avg_rate_pence=0.0)
                for t in intent.across_slot_starts
            ]
            return self._builder.sell_kwh(
                total_kwh=intent.kwh,
                across_slots=windows,
                snap=snap,
            )
        if isinstance(intent, DeferEVIntent):
            return self._builder.defer_ev(snap)
        if isinstance(intent, RestoreEVIntent):
            return self._builder.restore_ev(snap)
        if isinstance(intent, TeslaLimitIntent):
            return PlannedAction(
                tesla_action=TeslaAction(
                    set_charge_limit_pct=intent.charge_limit_pct
                )
            )
        logger.warning(
            "Arbiter: unknown intent type %s from rule %s",
            type(intent).__name__, claim.rule_name,
        )
        return None
