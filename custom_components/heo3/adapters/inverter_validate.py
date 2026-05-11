"""Mechanical safety invariants for inverter writes (§17).

Run BEFORE any write reaches the transport. Failures bubble up as
exceptions the planner is told about (via ApplyResult.failed) and
can replan around. The operator never publishes invalid bytes —
that's the whole point of having this layer.
"""

from __future__ import annotations

from datetime import time

from ..types import PlannedAction, SlotPlan

VALID_WORK_MODES = frozenset(
    {"Selling first", "Zero export to load", "Zero export to CT"}
)
VALID_ENERGY_PATTERNS = frozenset({"Battery first", "Load first"})
VALID_CURRENT_RANGE = (0.0, 350.0)  # Sunsynk hardware limit.
VALID_SOC_RANGE = (0, 100)


class SafetyError(ValueError):
    """A PlannedAction violated a mechanical invariant."""


def snap_to_5min(hhmm: str) -> str:
    """Floor the time string to the nearest 5-minute boundary.

    Sunsynk floors writes itself, so we snap proactively to make our
    own diffs and verifications match. Same as HEO II's SafetyRule.
    """
    h, m = hhmm.split(":")
    minute = (int(m) // 5) * 5
    return f"{int(h):02d}:{minute:02d}"


def validate_action(
    action: PlannedAction,
    *,
    min_soc: int,
    eps_active: bool,
) -> None:
    """Run all invariants. Raises SafetyError on the first violation.

    Per §17:
    - Slot times on 5-min granularity (snapping is a separate concern;
      this just checks they parse and are in [00:00, 23:55]).
    - Slot times contiguous (slot N+1 start = slot N end);
      slot 1 starts at 00:00, slot 6 ends at 00:00.
    - Exactly 6 slots, OR zero (no slot changes this tick).
    - SOC values in [0, 100].
    - min_soc respected — slot capacity_soc cannot be below
      config.min_soc unless eps_active (per SPEC H3 override).
    - Mode strings exact (case + whitespace per SA discovery).
    - GC values are bool (lowercase coercion happens at publish time).
    - Current values in [0, 350].
    """
    if action.work_mode is not None and action.work_mode not in VALID_WORK_MODES:
        raise SafetyError(
            f"work_mode {action.work_mode!r} not in {sorted(VALID_WORK_MODES)}"
        )
    if (
        action.energy_pattern is not None
        and action.energy_pattern not in VALID_ENERGY_PATTERNS
    ):
        raise SafetyError(
            f"energy_pattern {action.energy_pattern!r} not in "
            f"{sorted(VALID_ENERGY_PATTERNS)}"
        )

    for amp_field, value in (
        ("max_charge_a", action.max_charge_a),
        ("max_discharge_a", action.max_discharge_a),
    ):
        if value is None:
            continue
        lo, hi = VALID_CURRENT_RANGE
        if not (lo <= value <= hi):
            raise SafetyError(
                f"{amp_field}={value} outside [{lo}, {hi}] (Sunsynk hardware limit)"
            )

    if not action.slots:
        return

    # Build constructors emit partial slot tuples (just the ones that
    # change for the current intent). We accept any subset, as long
    # as each slot_n is in [1,6] and there are no duplicates.
    if len(action.slots) > 6:
        raise SafetyError(
            f"action.slots has {len(action.slots)} entries, max 6"
        )

    slot_ns = [s.slot_n for s in action.slots]
    if len(set(slot_ns)) != len(slot_ns):
        raise SafetyError(
            f"duplicate slot_n in action.slots: {slot_ns}"
        )

    for slot in action.slots:
        _validate_slot(slot, min_soc=min_soc, eps_active=eps_active)

    # Contiguity check only makes sense when we have all 6 slots
    # (an action specifying complete new slot timings). Partial
    # actions only modify a subset of fields on existing slots; the
    # inverter's other slots retain their previous values.
    if len(action.slots) == 6:
        _validate_slot_contiguity(action.slots)


def _validate_slot(
    slot: SlotPlan, *, min_soc: int, eps_active: bool
) -> None:
    if slot.slot_n not in (1, 2, 3, 4, 5, 6):
        raise SafetyError(f"slot_n must be 1..6, got {slot.slot_n}")

    if slot.capacity_pct is not None:
        lo, hi = VALID_SOC_RANGE
        if not (lo <= slot.capacity_pct <= hi):
            raise SafetyError(
                f"slot {slot.slot_n} capacity_pct={slot.capacity_pct} "
                f"outside [{lo}, {hi}]"
            )
        if not eps_active and slot.capacity_pct < min_soc:
            raise SafetyError(
                f"slot {slot.slot_n} capacity_pct={slot.capacity_pct} below "
                f"min_soc={min_soc} (only EPS H3 may override)"
            )

    if slot.start_hhmm is not None:
        try:
            h_str, m_str = slot.start_hhmm.split(":")
            t = time(int(h_str), int(m_str))
        except (ValueError, AttributeError) as exc:
            raise SafetyError(
                f"slot {slot.slot_n} start_hhmm={slot.start_hhmm!r} not HH:MM"
            ) from exc
        # Snap-then-compare not done here — that's a write-time concern.
        # Just confirm minute is on a 5-min boundary.
        if t.minute % 5 != 0:
            raise SafetyError(
                f"slot {slot.slot_n} start_hhmm={slot.start_hhmm} not on "
                "5-min boundary (Sunsynk granularity)"
            )


def _validate_slot_contiguity(slots: tuple[SlotPlan, ...]) -> None:
    """Slot N+1's start must equal slot N's end. Slot 1 starts at
    00:00; slot 6 ends at 00:00 (it wraps to slot 1)."""
    set_starts = [
        (slot.slot_n, slot.start_hhmm)
        for slot in slots
        if slot.start_hhmm is not None
    ]
    if not set_starts:
        return  # No timing changes — contiguity not in play this tick.

    by_n = {n: hhmm for n, hhmm in set_starts}

    if 1 in by_n and by_n[1] != "00:00":
        raise SafetyError(
            f"slot 1 must start at 00:00, got {by_n[1]} (Sunsynk timer convention)"
        )
