"""Tuner — daily auto-adjustment of rule parameters within hard bounds.

Per the planner design §8: simple signal-driven adjustments, NOT ML.
Each tunable has hardcoded bounds; the Tuner cannot exceed them.

Signals come from PerformanceTracker:
- Forecast errors (load + solar)
- Arbitrage outcomes
- Rule activation patterns

The Tuner does NOT enable/disable rules — that's a code change (PR).

Default OFF (gated by switch.heo3_tuner_enabled). Audit-logs every
change with old/new value + signal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..performance_tracker import PerformanceTracker
from .engine import RuleEngine
from .rule import Tunable

logger = logging.getLogger(__name__)


@dataclass
class TuningAction:
    """One parameter adjustment by the Tuner."""

    timestamp: str  # ISO UTC
    rule_name: str
    parameter: str
    old_value: float
    new_value: float
    reason: str


# Step factor for parameter adjustments — proportional control.
# new = current + STEP * (target_for_signal - current)
# Conservative: 0.3 means "move 30% of the way each tuning cycle".
STEP_FACTOR = 0.3


class Tuner:
    """Daily parameter adjuster. Reads tracker, mutates rule params."""

    def __init__(
        self,
        rule_engine: RuleEngine,
        tracker: PerformanceTracker,
        is_enabled: callable,
    ) -> None:
        self._engine = rule_engine
        self._tracker = tracker
        self._is_enabled = is_enabled
        self._actions: list[TuningAction] = []

    def actions_taken(self, *, since: datetime | None = None) -> list[TuningAction]:
        if since is None:
            return list(self._actions)
        cutoff = since.isoformat()
        return [a for a in self._actions if a.timestamp >= cutoff]

    async def evaluate_and_adjust(self) -> list[TuningAction]:
        """Run all adjustments. Returns the actions taken this cycle.

        No-op if the tuner is disabled.
        """
        if not self._is_enabled():
            logger.debug("Tuner: disabled, skipping cycle")
            return []

        actions: list[TuningAction] = []
        signals = self._collect_signals()

        # Adjustment 1: cheap_rate_charge.safety_margin_pct
        # Signal: load forecast bias. If we're systematically under-
        # forecasting load (positive bias), bump margin up.
        actions.extend(
            self._adjust_param(
                rule_name="cheap_rate_charge",
                param_name="safety_margin_pct",
                target=max(0.0, signals["load_bias_pct"] + 5.0),
                reason=(
                    f"load forecast bias {signals['load_bias_pct']:+.1f}%, "
                    "adjusting safety margin"
                ),
            )
        )

        # Adjustment 2: peak_export_arbitrage.spread_threshold_pence
        # Signal: arbitrage profitability. If recent arbitrage tends
        # to lose money (low profitable rate), raise threshold.
        prof_pct = signals["arbitrage_profitable_pct"]
        # Map: 100% profitable → threshold 5p; 0% profitable → threshold 20p
        target_threshold = 20.0 - 0.15 * prof_pct
        actions.extend(
            self._adjust_param(
                rule_name="peak_export_arbitrage",
                param_name="spread_threshold_pence",
                target=target_threshold,
                reason=(
                    f"recent arbitrage profitable rate {prof_pct:.0f}%, "
                    "adjusting spread threshold"
                ),
            )
        )

        # Adjustment 3: solar_surplus_threshold_w (cheap_rate inputs)
        # Signal: solar forecast bias. If solar consistently undershoots
        # forecast, raise the threshold (don't expect surplus).
        actions.extend(
            self._adjust_param(
                rule_name="solar_surplus",
                param_name="surplus_threshold_w",
                target=max(
                    100.0,
                    500.0 - signals["solar_bias_pct"] * 5.0,
                ),
                reason=(
                    f"solar forecast bias {signals['solar_bias_pct']:+.1f}%, "
                    "adjusting surplus threshold"
                ),
            )
        )

        self._actions.extend(actions)
        if actions:
            logger.info("Tuner: applied %d adjustments", len(actions))
        return actions

    def _adjust_param(
        self,
        *,
        rule_name: str,
        param_name: str,
        target: float,
        reason: str,
    ) -> list[TuningAction]:
        """Move a parameter STEP_FACTOR of the way toward `target`.

        Returns a list of one TuningAction (or empty if no change).
        """
        rule = self._engine.find_rule(rule_name)
        if rule is None:
            logger.debug("Tuner: rule %s not registered, skip", rule_name)
            return []
        param = rule.parameters.get(param_name)
        if param is None:
            logger.debug(
                "Tuner: rule %s has no parameter %s, skip",
                rule_name, param_name,
            )
            return []

        current = param.current
        proposed = current + STEP_FACTOR * (target - current)
        new = param.set(proposed)  # clamps to bounds

        # Skip recording if no actual change.
        if abs(new - current) < 0.01:
            return []

        action = TuningAction(
            timestamp=datetime.now(timezone.utc).isoformat(),
            rule_name=rule_name,
            parameter=param_name,
            old_value=current,
            new_value=new,
            reason=reason,
        )
        logger.info(
            "Tuner: %s.%s %.3f → %.3f (target %.3f, %s)",
            rule_name, param_name, current, new, target, reason,
        )
        return [action]

    def _collect_signals(self) -> dict[str, float]:
        """Gather signals from tracker for adjustment decisions."""
        load_err = self._tracker.load_forecast_error
        solar_err = self._tracker.solar_forecast_error
        return {
            "load_bias_pct": load_err.mean_pct_error,
            "solar_bias_pct": solar_err.mean_pct_error,
            # Arbitrage profitability — placeholder; needs richer
            # tracking once we have rule-attribution data. For now
            # default to 50% (neutral).
            "arbitrage_profitable_pct": 50.0,
        }
