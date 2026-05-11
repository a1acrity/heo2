"""Rule + Claim + Tunable types.

A Rule is a named, observable economic decision-maker. It evaluates
the current Snapshot and either returns a Claim (an intent it wants
to act on) or None (no opinion this tick).

Claims describe WHAT, not HOW — actual inverter writes are constructed
by the Arbiter calling operator.build constructors.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol

from ..compute import Compute
from ..types import Snapshot, TimeRange

logger = logging.getLogger(__name__)


# ── Tunable parameter ─────────────────────────────────────────────


@dataclass
class Tunable:
    """A named, bounded, mutable parameter on a rule.

    Defaults are immutable references; current value gets adjusted
    by the Tuner between ticks. Hard bounds are enforced — the Tuner
    cannot push a parameter past them.
    """

    name: str
    default: float
    lower: float
    upper: float
    description: str = ""
    current: float = field(init=False)

    def __post_init__(self) -> None:
        if not (self.lower <= self.default <= self.upper):
            raise ValueError(
                f"Tunable {self.name!r}: default {self.default} outside bounds "
                f"[{self.lower}, {self.upper}]"
            )
        self.current = self.default

    def set(self, new_value: float) -> float:
        """Set new value, clamped to bounds. Returns the clamped value."""
        clamped = max(self.lower, min(self.upper, new_value))
        if clamped != new_value:
            logger.warning(
                "Tunable %s: clamped %.3f → %.3f (bounds [%.3f, %.3f])",
                self.name, new_value, clamped, self.lower, self.upper,
            )
        self.current = clamped
        return clamped


# ── Claim types ───────────────────────────────────────────────────


class ClaimStrength(Enum):
    """How strongly a rule wants its claim to win arbitration."""

    MUST = "must"      # tier-1 only. Hard requirement.
    PREFER = "prefer"  # rule is confident.
    OFFER = "offer"    # rule is opportunistic, yields to PREFER.


# Claim intent variants. Frozen dataclasses — each is a discriminated
# variant. The Arbiter dispatches on type.


@dataclass(frozen=True)
class ChargeIntent:
    target_soc_pct: int
    by_time: datetime
    rate_limit_a: float | None = None


@dataclass(frozen=True)
class DrainIntent:
    target_soc_pct: int
    by_time: datetime


@dataclass(frozen=True)
class HoldIntent:
    soc_pct: int
    window: TimeRange


@dataclass(frozen=True)
class SellIntent:
    kwh: float
    across_slot_starts: tuple[datetime, ...]


@dataclass(frozen=True)
class LockdownIntent:
    """SPEC H3 lockdown. Tier-1 only."""
    pass


@dataclass(frozen=True)
class DeferEVIntent:
    pass


@dataclass(frozen=True)
class RestoreEVIntent:
    pass


@dataclass(frozen=True)
class TeslaLimitIntent:
    charge_limit_pct: int


# Discriminated union of all intent variants. Type-narrowed by the Arbiter.
ClaimIntent = (
    ChargeIntent
    | DrainIntent
    | HoldIntent
    | SellIntent
    | LockdownIntent
    | DeferEVIntent
    | RestoreEVIntent
    | TeslaLimitIntent
)


@dataclass(frozen=True)
class Claim:
    """A rule's attempt to influence this tick's plan.

    Exactly one intent. Rationale is mandatory + surfaces to the
    digest sensor. Strength + horizon drive arbitration.

    `expected_pence_impact`: signed estimate of £ effect over the
    horizon. Positive = saving / earning. Used for digest attribution
    (rough estimate; actual impact tracked separately).
    """

    rule_name: str
    intent: ClaimIntent
    rationale: str
    strength: ClaimStrength
    horizon: TimeRange
    expected_pence_impact: float = 0.0


# ── RuleContext ───────────────────────────────────────────────────


@dataclass(frozen=True)
class HistoricalView:
    """Last-N-days summary stats a rule can use to inform decisions.

    Filled in by RuleEngine before each tick. Empty until the
    PerformanceTracker has data.
    """

    load_forecast_mean_pct_error: float = 0.0
    solar_forecast_mean_pct_error: float = 0.0
    recent_arbitrage_pct_profitable: float = 0.0  # 0..1


@dataclass(frozen=True)
class RuleContext:
    """What a rule sees beyond the Snapshot.

    Compute helpers, current parameter values, historical signals.
    Frozen — rules never mutate the context.
    """

    compute: Compute
    parameters: dict[str, float]  # rule's CURRENT (tuned) param values
    historical: HistoricalView


# ── Rule Protocol ─────────────────────────────────────────────────


class Rule(Protocol):
    """A named, observable economic decision-maker.

    Implementations live under planner/rules/. Each rule is:
    - Pure (no I/O). Side effects happen via the Arbiter calling
      operator.build constructors on the winning claims.
    - Deterministic given (Snapshot, parameters).
    - Self-describing (name, tier, description, parameters).
    """

    @property
    def name(self) -> str: ...

    @property
    def tier(self) -> int: ...  # 1=safety, 2=mode, 3=optimisation

    @property
    def description(self) -> str: ...

    @property
    def parameters(self) -> dict[str, Tunable]: ...

    def evaluate(self, snap: Snapshot, ctx: RuleContext) -> Claim | None: ...
