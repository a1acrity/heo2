"""HA coordinator — drives the planner's tick loop.

Two trigger sources:
- 15-minute cron (always-on, primary cadence)
- State-change events on gating entities (EPS, saving session, IGO
  dispatch transitions)

Both routes converge on `tick(reason)` which:
1. Refreshes operator config via discovery (entities can register late)
2. Snapshots → runs the planner → applies → records
3. Updates HA sensors with the decision

The planner itself is injected (`Planner` Protocol) so the coordinator
doesn't depend on the rule engine. P2.0 ships with a stub planner that
returns baseline_static.

Debouncing: events within DEBOUNCE_S of each other (and within
DEBOUNCE_S of a cron tick) collapse to one tick.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Protocol

from .types import ApplyResult, PlannedAction, Snapshot

logger = logging.getLogger(__name__)


# 15-min tick aligns with rate-slot granularity.
TICK_INTERVAL = timedelta(minutes=15)

# Coalesce triggers within this window into one tick.
DEBOUNCE_S = 5.0


class Planner(Protocol):
    """Decides what to do given the current snapshot.

    The coordinator owns the tick loop; the planner owns the decision.
    """

    async def decide(self, snapshot: Snapshot) -> "Decision": ...


@dataclass(frozen=True)
class Decision:
    """Planner's output for one tick.

    Carries the PlannedAction + the audit trail. The audit (claims
    made, arbitration outcome, rationale) is what the observability
    sensors expose. P2.0's stub planner returns a Decision with empty
    audit; P2.2+ rules engine populates it.
    """

    action: PlannedAction
    rationale: str = ""
    active_rules: tuple[str, ...] = ()
    claims: tuple[dict, ...] = ()  # for observability sensor


@dataclass
class TickRecord:
    """One tick's outcome, fed to the PerformanceTracker."""

    captured_at: datetime
    reason: str
    snapshot: Snapshot
    decision: Decision
    apply_result: ApplyResult | None = None
    skipped_reason: str | None = None


class Coordinator:
    """Owns the tick loop. Sits above the planner + operator."""

    def __init__(
        self,
        *,
        operator,  # type: ignore[no-untyped-def]
        planner: Planner,
        on_tick: Callable[[TickRecord], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._operator = operator
        self._planner = planner
        self._on_tick = on_tick
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        self._last_tick_at: datetime | None = None
        self._pending_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._stopped = False

    # ── Public API ────────────────────────────────────────────────

    async def tick(self, *, reason: str = "cron") -> Decision | None:
        """Run one tick. Returns the Decision (or None if skipped).

        Lock-protected: concurrent calls serialise. Useful when an
        event-driven trigger fires during a cron tick.
        """
        if self._stopped:
            return None
        async with self._lock:
            now = self._clock()
            self._last_tick_at = now

            try:
                snap = await self._operator.snapshot()
            except Exception as exc:
                logger.exception("coordinator: snapshot failed: %s", exc)
                if self._on_tick is not None:
                    await self._on_tick(
                        TickRecord(
                            captured_at=now,
                            reason=reason,
                            snapshot=None,  # type: ignore[arg-type]
                            decision=Decision(action=PlannedAction()),
                            skipped_reason=f"snapshot failed: {exc}",
                        )
                    )
                return None

            try:
                decision = await self._planner.decide(snap)
            except Exception as exc:
                logger.exception("coordinator: planner failed: %s", exc)
                # Fail-safe: empty action (no writes).
                decision = Decision(
                    action=PlannedAction(rationale=f"planner failed: {exc}")
                )

            # Apply only if there's something to do (writes or
            # peripheral actions). Empty action returns immediately.
            try:
                result = await self._operator.apply(
                    decision.action, snapshot=snap
                )
            except Exception as exc:
                logger.exception("coordinator: apply failed: %s", exc)
                result = None

            record = TickRecord(
                captured_at=now,
                reason=reason,
                snapshot=snap,
                decision=decision,
                apply_result=result,
            )
            if self._on_tick is not None:
                try:
                    await self._on_tick(record)
                except Exception:
                    logger.exception("coordinator: on_tick callback raised")

            return decision

    async def schedule_debounced_tick(self, reason: str) -> None:
        """Trigger a tick after DEBOUNCE_S, coalescing with any
        pending trigger.

        Use this from event-driven triggers (EPS state change, saving
        session start, etc.) so a burst of state changes doesn't fire
        a burst of ticks.
        """
        if self._stopped:
            return
        if self._pending_task is not None and not self._pending_task.done():
            # Already a debounce window open — let the existing one
            # cover this trigger.
            return
        self._pending_task = asyncio.create_task(self._wait_then_tick(reason))

    async def _wait_then_tick(self, reason: str) -> None:
        await asyncio.sleep(DEBOUNCE_S)
        if self._stopped:
            return
        await self.tick(reason=reason)

    @property
    def last_tick_at(self) -> datetime | None:
        return self._last_tick_at

    async def shutdown(self) -> None:
        self._stopped = True
        if self._pending_task is not None and not self._pending_task.done():
            self._pending_task.cancel()
            try:
                await self._pending_task
            except (asyncio.CancelledError, Exception):
                pass


# ── Stub planner for P2.0 (returns baseline_static) ──────────────


class StaticBaselinePlanner:
    """Trivial planner — always returns baseline_static.

    Used by P2.0 to prove the coordinator + tick loop work end-to-end
    before the rule engine ships in P2.2+. Also serves as the
    permanent fallback when the rule engine is disabled (via
    switch.heo3_planner_enabled = off).
    """

    def __init__(self, operator) -> None:  # type: ignore[no-untyped-def]
        self._operator = operator

    async def decide(self, snapshot: Snapshot) -> Decision:
        plan = self._operator.build.baseline_static(snapshot)
        return Decision(
            action=plan,
            rationale="baseline_static (no planner enabled)",
            active_rules=("baseline_static",),
            claims=(
                {
                    "rule_name": "baseline_static",
                    "intent": "static-plan",
                    "rationale": "rule engine disabled — applying static fallback",
                    "strength": "MUST",
                },
            ),
        )
