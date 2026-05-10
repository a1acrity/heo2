"""ActionBuilder — intent → PlannedAction constructors.

The planner expresses WHAT it wants ("sell 8 kWh in these top slots",
"charge to 80% by 05:30"); the constructors figure out WHICH inverter
writes achieve it. See §13 of the design.

P1.9: full implementation. Constructors are pure — they take a
Snapshot for context and return a frozen PlannedAction.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import datetime, timedelta

from .compute import Compute, RateWindow
from .types import (
    ApplianceAction,
    EVAction,
    PlannedAction,
    SlotPlan,
    Snapshot,
    TimeRange,
)

logger = logging.getLogger(__name__)


# Default appliance set for SPEC H3 EPS lockdown.
DEFAULT_EPS_APPLIANCES = ("washer", "dryer", "dishwasher")

# Baseline static plan slots (HEO II's known-good fallback).
# Slot 1: cheap-rate window (overnight charge to 80%).
# Slot 2: morning hold at 100%.
# Slot 3: midday hold at 100%.
# Slot 4: pre-peak hold at 100%.
# Slot 5: evening drain to 25%.
# Slot 6: late-night hold at 25%.
BASELINE_SLOT_TIMES = ("00:00", "05:30", "11:00", "16:00", "19:00", "23:30")
BASELINE_SLOT_CAPS = (80, 100, 100, 100, 25, 25)
BASELINE_SLOT_GC = (True, False, False, False, False, False)


class ActionBuilder:
    """High-level action constructors. §13."""

    def __init__(self, compute: Compute | None = None) -> None:
        self._compute = compute or Compute()

    # ── 13a. Energy actions ───────────────────────────────────────

    def sell_kwh(
        self,
        *,
        total_kwh: float,
        across_slots: list[RateWindow],
        snap: Snapshot,
    ) -> PlannedAction:
        """Allocate `total_kwh` across the given slots.

        For the slot covering `now` (if any): set work_mode='Selling
        first' and max_discharge_a = compute.discharge_throttle_for().
        Set the corresponding inverter slot's capacity_pct to the
        post-sell SOC.

        For future slots: stored in the plan's intent — the next tick
        re-issues sell_kwh with updated `now`.
        """
        if not across_slots or total_kwh <= 0:
            return self._empty_plan(rationale="sell_kwh: nothing to sell")

        # Equal-split allocation across slots. A future improvement
        # could weight by rate (more in the highest-paying slot).
        per_slot_kwh = total_kwh / len(across_slots)

        active = self._slot_covering_now(across_slots, snap.captured_at)
        plan = self._empty_plan(
            rationale=(
                f"sell_kwh total={total_kwh:.2f} across {len(across_slots)} slots"
            ),
            spec_h4_live_rates=True,
        )
        if active is None:
            return plan  # No slot active right now; planner re-issues next tick.

        duration = active.end - max(snap.captured_at, active.start)
        amps = self._compute.discharge_throttle_for(
            kwh=per_slot_kwh, duration=duration, snap=snap
        )
        post_sell_soc = max(
            snap.config.min_soc,
            int(
                round(
                    (snap.inverter.battery_soc_pct or 50.0)
                    - self._compute.soc_for_kwh(per_slot_kwh, snap)
                )
            ),
        )
        # Find the inverter slot that contains now.
        slots = self._slot_set_for_active_window(snap, active, post_sell_soc)
        return replace(
            plan,
            work_mode="Selling first",
            max_discharge_a=amps,
            slots=slots,
        )

    def charge_to(
        self,
        *,
        target_soc_pct: int,
        by: datetime,
        snap: Snapshot,
        rate_limit_a: float | None = None,
    ) -> PlannedAction:
        """Charge from grid to `target_soc_pct` by `by`.

        Sets the inverter slot covering [now, by) to grid_charge=True
        and capacity_pct=target_soc_pct. Optional rate_limit applies
        via max_charge_a.
        """
        if by <= snap.captured_at:
            return self._empty_plan(rationale="charge_to: by-time already passed")

        slots = self._set_capacity_and_gc_in_window(
            snap, snap.captured_at, by, target_soc_pct, gc=True
        )
        plan = self._empty_plan(
            rationale=(
                f"charge_to soc={target_soc_pct}% by={by.isoformat()}"
            ),
            spec_h4_live_rates=True,
        )
        plan = replace(plan, slots=slots)
        if rate_limit_a is not None:
            plan = replace(plan, max_charge_a=rate_limit_a)
        return plan

    def hold_at(
        self,
        *,
        soc_pct: int,
        window: TimeRange,
        snap: Snapshot,
    ) -> PlannedAction:
        """Keep battery at soc_pct over the window: cap=soc_pct, gc=False.

        PV charges naturally up to that level; load drains naturally
        down to it. Doesn't change work_mode.
        """
        slots = self._set_capacity_and_gc_in_window(
            snap, window.start, window.end, soc_pct, gc=False
        )
        return replace(
            self._empty_plan(rationale=f"hold_at soc={soc_pct}% window={window}"),
            slots=slots,
        )

    def drain_to(
        self,
        *,
        target_soc_pct: int,
        by: datetime,
        snap: Snapshot,
    ) -> PlannedAction:
        """Set slot covering [now, by) to cap=target, gc=False.

        Drain happens passively under existing work_mode (battery
        discharges as load demands).
        """
        if by <= snap.captured_at:
            return self._empty_plan(rationale="drain_to: by-time already passed")
        slots = self._set_capacity_and_gc_in_window(
            snap, snap.captured_at, by, target_soc_pct, gc=False
        )
        return replace(
            self._empty_plan(rationale=f"drain_to soc={target_soc_pct}% by={by}"),
            slots=slots,
        )

    # ── 13b. Mode actions ─────────────────────────────────────────

    def lockdown_eps(self, snap: Snapshot) -> PlannedAction:
        """SPEC H3: grid down. All slots cap=0%, gc=False. EV stop.
        Appliance switches off. Coordinator triggers on eps_active
        transition.

        Note: `min_soc` invariant is overridden because eps_active=True
        is checked at validation time.
        """
        slots = tuple(
            SlotPlan(
                slot_n=n,
                start_hhmm=BASELINE_SLOT_TIMES[n - 1],
                grid_charge=False,
                capacity_pct=0,
            )
            for n in range(1, 7)
        )
        return PlannedAction(
            slots=slots,
            ev_action=EVAction(set_mode="Stopped"),
            appliances_action=ApplianceAction(turn_off=DEFAULT_EPS_APPLIANCES),
            plan_id=_new_plan_id(),
            rationale="SPEC H3: EPS lockdown",
            source_planner_version="builder/lockdown_eps",
        )

    def baseline_static(self, snap: Snapshot) -> PlannedAction:
        """The known-good static plan: 80% overnight charge, day hold
        at 100%, evening drain to 25%, no arbitrage.

        Used by the cutover script (P1.11) and as the planner's
        fallback when it can't produce a valid plan.
        """
        slots = tuple(
            SlotPlan(
                slot_n=n,
                start_hhmm=BASELINE_SLOT_TIMES[n - 1],
                grid_charge=BASELINE_SLOT_GC[n - 1],
                capacity_pct=BASELINE_SLOT_CAPS[n - 1],
            )
            for n in range(1, 7)
        )
        return PlannedAction(
            slots=slots,
            work_mode="Zero export to CT",
            energy_pattern="Load first",
            max_charge_a=100.0,
            max_discharge_a=100.0,
            plan_id=_new_plan_id(),
            rationale="baseline static plan",
            source_planner_version="builder/baseline_static",
        )

    def restore_default(self, snap: Snapshot) -> PlannedAction:
        """Reset globals to baseline values. Used when exiting active
        arbitrage / EV-deferral / saving-session windows."""
        return PlannedAction(
            work_mode="Zero export to CT",
            energy_pattern="Load first",
            max_charge_a=100.0,
            max_discharge_a=100.0,
            plan_id=_new_plan_id(),
            rationale="restore default globals",
            source_planner_version="builder/restore_default",
        )

    # ── 13c. Peripheral actions ───────────────────────────────────

    def defer_ev(self, snap: Snapshot) -> PlannedAction:
        """SPEC §12: stop the EV. The PeripheralAdapter captures the
        current charge mode in-memory for later restore."""
        return PlannedAction(
            ev_action=EVAction(set_mode="Stopped"),
            plan_id=_new_plan_id(),
            rationale="defer EV (SPEC §12)",
            source_planner_version="builder/defer_ev",
        )

    def restore_ev(self, snap: Snapshot) -> PlannedAction:
        """Restore EV to its captured-pre-deferral charge mode."""
        return PlannedAction(
            ev_action=EVAction(restore_previous=True),
            plan_id=_new_plan_id(),
            rationale="restore EV charging",
            source_planner_version="builder/restore_ev",
        )

    # ── 13d. Composition ──────────────────────────────────────────

    def merge(self, *actions: PlannedAction) -> PlannedAction:
        """Field-by-field reconciliation of multiple PlannedActions.

        - Slot fields: union by slot_n; later actions override earlier.
        - Globals (work_mode etc): last-write-wins with a warning if
          there's a conflict.
        - Peripheral actions: must agree (or one is None) — raises
          ValueError on conflict.
        """
        if not actions:
            return PlannedAction()

        merged = PlannedAction()
        merged_slot_map: dict[int, SlotPlan] = {}

        for a in actions:
            for field_name in (
                "work_mode",
                "energy_pattern",
                "max_charge_a",
                "max_discharge_a",
            ):
                new = getattr(a, field_name)
                if new is None:
                    continue
                old = getattr(merged, field_name)
                if old is not None and old != new:
                    logger.warning(
                        "merge: %s conflict %r vs %r — using latter",
                        field_name,
                        old,
                        new,
                    )
                merged = replace(merged, **{field_name: new})

            for slot in a.slots:
                existing = merged_slot_map.get(slot.slot_n)
                merged_slot_map[slot.slot_n] = (
                    _merge_slot(existing, slot) if existing else slot
                )

            # Peripheral actions: complain on conflict.
            for periph_field in ("ev_action", "tesla_action", "appliances_action"):
                new = getattr(a, periph_field)
                if new is None:
                    continue
                old = getattr(merged, periph_field)
                if old is not None and old != new:
                    raise ValueError(
                        f"merge: peripheral {periph_field} conflict between actions"
                    )
                merged = replace(merged, **{periph_field: new})

        if merged_slot_map:
            ordered = tuple(
                merged_slot_map[n] for n in sorted(merged_slot_map)
            )
            merged = replace(merged, slots=ordered)

        return replace(
            merged,
            plan_id=_new_plan_id(),
            rationale=" + ".join(a.rationale for a in actions if a.rationale),
            source_planner_version="builder/merge",
        )

    # ── Helpers ───────────────────────────────────────────────────

    def _empty_plan(
        self,
        *,
        rationale: str = "",
        spec_h4_live_rates: bool = False,
    ) -> PlannedAction:
        return PlannedAction(
            plan_id=_new_plan_id(),
            rationale=rationale,
            source_planner_version="builder",
            spec_h4_live_rates=spec_h4_live_rates,
        )

    @staticmethod
    def _slot_covering_now(
        slots: list[RateWindow], now: datetime
    ) -> RateWindow | None:
        for s in slots:
            if s.start <= now < s.end:
                return s
        return None

    def _set_capacity_and_gc_in_window(
        self,
        snap: Snapshot,
        start: datetime,
        end: datetime,
        target_soc_pct: int,
        *,
        gc: bool,
    ) -> tuple[SlotPlan, ...]:
        """Pick the inverter slot(s) overlapping [start, end] and set
        their capacity_pct + grid_charge.

        Doesn't reshape slot timing — uses the inverter's current slot
        boundaries from snap.inverter_settings as the baseline. The
        full re-timing of slots based on rate windows is a future
        constructor (would belong with cheap-window-aligned charge).
        """
        local = snap.local_tz
        local_start = start.astimezone(local).time()
        local_end = end.astimezone(local).time()

        out: list[SlotPlan] = []
        current_slots = snap.inverter_settings.slots
        for n, slot in enumerate(current_slots, start=1):
            slot_start = _parse_hhmm(slot.start_hhmm)
            slot_end = (
                _parse_hhmm(current_slots[n].start_hhmm) if n < 6 else _parse_hhmm("00:00")
            )
            if _window_overlaps(slot_start, slot_end, local_start, local_end):
                out.append(
                    SlotPlan(
                        slot_n=n,
                        start_hhmm=slot.start_hhmm,
                        grid_charge=gc,
                        capacity_pct=target_soc_pct,
                    )
                )
        return tuple(out)

    def _slot_set_for_active_window(
        self,
        snap: Snapshot,
        window: RateWindow,
        post_sell_soc: int,
    ) -> tuple[SlotPlan, ...]:
        """Build the slot tuple for a sell window: identify the inverter
        slot containing the window's start; set its capacity_pct to
        post-sell SOC, gc=False."""
        return self._set_capacity_and_gc_in_window(
            snap, window.start, window.end, post_sell_soc, gc=False
        )


# ── Module helpers ────────────────────────────────────────────────


def _new_plan_id() -> str:
    return uuid.uuid4().hex[:12]


def _merge_slot(a: SlotPlan, b: SlotPlan) -> SlotPlan:
    """Merge two SlotPlan for the same slot_n — b's non-None fields win."""
    return SlotPlan(
        slot_n=a.slot_n,
        start_hhmm=b.start_hhmm if b.start_hhmm is not None else a.start_hhmm,
        grid_charge=b.grid_charge if b.grid_charge is not None else a.grid_charge,
        capacity_pct=(
            b.capacity_pct if b.capacity_pct is not None else a.capacity_pct
        ),
    )


def _parse_hhmm(hhmm: str):
    """Convert HH:MM to a comparable time tuple."""
    from datetime import time

    h, m = hhmm.split(":")
    return time(int(h), int(m))


def _window_overlaps(slot_start, slot_end, win_start, win_end) -> bool:
    """Check whether [slot_start, slot_end) overlaps [win_start, win_end)
    treating slot_end == 00:00 as wrapping to next day.
    """
    from datetime import time

    if slot_end == time(0, 0):
        # Slot wraps to midnight — overlaps if win starts before slot start
        # OR win starts after slot start.
        return True if win_start >= slot_start or win_end <= slot_start else (
            win_start >= slot_start
        )
    if slot_start <= slot_end:
        return win_start < slot_end and win_end > slot_start
    # slot wraps midnight
    return win_start < slot_end or win_end > slot_start
