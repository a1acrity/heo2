"""ActionBuilder — intent → PlannedAction constructors.

The planner expresses WHAT it wants ("sell 8 kWh in these top slots",
"charge to 80% by 05:30"); the constructors figure out WHICH inverter
writes achieve it. See §13 of the design.

P1.0 stub: methods raise NotImplementedError. Full implementation in P1.9.
"""

from __future__ import annotations

from datetime import datetime

from .compute import RateWindow, TimeRange
from .types import PlannedAction, Snapshot


class ActionBuilder:
    """High-level action constructors. P1.9."""

    # ── 13a. Energy actions ───────────────────────────────────────

    def sell_kwh(
        self,
        *,
        total_kwh: float,
        across_slots: list[RateWindow],
        snap: Snapshot,
    ) -> PlannedAction:
        raise NotImplementedError("P1.9 — ActionBuilder.sell_kwh")

    def charge_to(
        self,
        *,
        target_soc_pct: float,
        by: datetime,
        snap: Snapshot,
        rate_limit_a: float | None = None,
    ) -> PlannedAction:
        raise NotImplementedError("P1.9 — ActionBuilder.charge_to")

    def hold_at(
        self,
        *,
        soc_pct: float,
        window: TimeRange,
        snap: Snapshot,
    ) -> PlannedAction:
        raise NotImplementedError("P1.9 — ActionBuilder.hold_at")

    def drain_to(
        self,
        *,
        target_soc_pct: float,
        by: datetime,
        snap: Snapshot,
    ) -> PlannedAction:
        raise NotImplementedError("P1.9 — ActionBuilder.drain_to")

    # ── 13b. Mode actions ─────────────────────────────────────────

    def lockdown_eps(self, snap: Snapshot) -> PlannedAction:
        """SPEC H3: grid down. All slots cap=0%, gc=False. EV stop.
        Appliance switches off. Coordinator triggers this on
        eps_active transition.
        """
        raise NotImplementedError("P1.9 — ActionBuilder.lockdown_eps")

    def baseline_static(self, snap: Snapshot) -> PlannedAction:
        """The known-good static plan: 80% overnight charge, day hold
        at 100%, evening drain to 25%, no arbitrage. Used by the
        cutover script (P1.11) and as the planner's fallback.
        """
        raise NotImplementedError("P1.9 — ActionBuilder.baseline_static")

    def restore_default(self, snap: Snapshot) -> PlannedAction:
        raise NotImplementedError("P1.9 — ActionBuilder.restore_default")

    # ── 13c. Peripheral actions ───────────────────────────────────

    def defer_ev(self, snap: Snapshot) -> PlannedAction:
        raise NotImplementedError("P1.9 — ActionBuilder.defer_ev")

    def restore_ev(self, snap: Snapshot) -> PlannedAction:
        raise NotImplementedError("P1.9 — ActionBuilder.restore_ev")

    # ── 13d. Composition ──────────────────────────────────────────

    def merge(self, *actions: PlannedAction) -> PlannedAction:
        """Field-by-field reconciliation:
        - Slot fields: union, last-write-wins on conflicts (with warning).
        - Globals: same.
        - Peripheral actions: must agree or merge raises.
        """
        raise NotImplementedError("P1.9 — ActionBuilder.merge")
