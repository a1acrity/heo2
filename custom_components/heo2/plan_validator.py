# custom_components/heo2/plan_validator.py
"""Pre-write plan validation per SPEC §6 / hard rule H5.

Runs after the rule engine has produced a programme but BEFORE the
coordinator hands it to the MQTT writer. Checks both:

  1. Structural invariants (already enforced by SafetyRule, re-asserted
     here so a SafetyRule bug can never silently leak a bad plan).
  2. Sanity rules - the SPEC §6 "do not write a plan that is going to
     cost money on purpose" guards.

Hard failures cause the coordinator to keep the previous plan and set
`binary_sensor.heo_ii_writes_blocked` ON with the rejection reason.
Warnings are logged but do not block writes.

Pure logic, no Home Assistant imports. The projection is computed
alongside (it informs both the dashboard sensor and the
peak-rate-import warning).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

from .models import ProgrammeInputs, ProgrammeState, RateSlot, SlotConfig
from .projection import Projection, project_day
from .rank_pricing import bottom_n_pct, filter_today


@dataclass
class ValidationResult:
    """Outcome of `validate_plan`. Errors block the write; warnings don't.

    `projection` is always populated (even on validation failure) so the
    dashboard projection sensor reflects the rejected plan's expected
    behaviour and the user can see why it was rejected.
    """

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    projection: Projection | None = None

    @property
    def passed(self) -> bool:
        return not self.errors

    def reason(self) -> str:
        """One-line summary suitable for `writes_blocked` reason."""
        if not self.errors:
            return ""
        if len(self.errors) == 1:
            return f"plan rejected: {self.errors[0]}"
        return f"plan rejected: {self.errors[0]} (+{len(self.errors) - 1} more)"


def _slots_overlapping_rates(
    programme: ProgrammeState, rate_slots: list[RateSlot],
) -> list[tuple[int, RateSlot]]:
    """Return (slot_index, rate_slot) pairs where the programme slot's
    time window overlaps the rate slot's time window.

    Programme slots are time-of-day; rate slots are absolute datetimes.
    For overlap detection we project the rate slot onto its local
    time-of-day. A rate slot crossing midnight is split into two
    notional half-slots.
    """
    pairs: list[tuple[int, RateSlot]] = []
    for r in rate_slots:
        # Rate slots from BD/IGO are 30-min wide; the start time is
        # enough to identify which programme slot it lands in.
        local_clock = r.start.time()
        for i, slot in enumerate(programme.slots):
            if slot.contains_time(local_clock):
                pairs.append((i, r))
                break
    return pairs


def _check_no_grid_charge_in_peak(
    programme: ProgrammeState,
    import_rates: list[RateSlot],
    peak_threshold_p: float,
) -> list[str]:
    """SPEC §6 sanity check: no `grid_charge=True` slot's time range may
    overlap a peak-rate import window.

    H1 hard rule: peak imports are NEVER scheduled (forced peak imports
    that arise from reality are flagged separately as warnings).
    """
    errors: list[str] = []
    peak_rates = [r for r in import_rates if r.rate_pence >= peak_threshold_p]
    if not peak_rates:
        return errors

    for slot_idx, rate in _slots_overlapping_rates(programme, peak_rates):
        slot = programme.slots[slot_idx]
        if slot.grid_charge:
            errors.append(
                f"H1 violation: slot {slot_idx + 1} "
                f"({slot.start_time.strftime('%H:%M')}-"
                f"{slot.end_time.strftime('%H:%M')}) has grid_charge=True "
                f"covering peak rate {rate.rate_pence:.2f}p at "
                f"{rate.start.strftime('%H:%M')}"
            )
            # One error per slot is enough; further peak slots in the
            # same programme slot would just be noise.
            break
    return errors


def _check_static(programme: ProgrammeState, min_soc: float) -> list[str]:
    """Re-assertion of SafetyRule invariants. Should be a no-op in
    practice; if any of these fire there is a bug in the rule pipeline.
    """
    errors: list[str] = []
    if len(programme.slots) != 6:
        errors.append(
            f"structural: expected 6 slots, got {len(programme.slots)}"
        )
        return errors  # subsequent checks assume 6 slots
    if programme.slots[0].start_time != time(0, 0):
        errors.append(
            f"structural: slot 1 must start 00:00, got "
            f"{programme.slots[0].start_time.strftime('%H:%M')}"
        )
    if programme.slots[-1].end_time != time(0, 0):
        errors.append(
            f"structural: slot 6 must end 00:00, got "
            f"{programme.slots[-1].end_time.strftime('%H:%M')}"
        )
    for i in range(5):
        if programme.slots[i].end_time != programme.slots[i + 1].start_time:
            errors.append(
                f"structural: slot {i + 2} starts "
                f"{programme.slots[i + 1].start_time.strftime('%H:%M')} but "
                f"slot {i + 1} ends "
                f"{programme.slots[i].end_time.strftime('%H:%M')}"
            )
            break
    for i, slot in enumerate(programme.slots):
        if not (min_soc <= slot.capacity_soc <= 100):
            errors.append(
                f"structural: slot {i + 1} SOC {slot.capacity_soc}% "
                f"outside [{min_soc:.0f}, 100]"
            )
            break
    return errors


def _check_cheap_window_covered(
    programme: ProgrammeState,
    import_rates: list[RateSlot],
    inputs: ProgrammeInputs,
    bottom_n_pct_threshold: int,
) -> list[str]:
    """Soft SPEC §6 check: at least one `grid_charge=True` slot should
    cover the IGO cheap window.

    Returns warnings (not errors) - the user may legitimately skip
    cheap-rate charging on a sunny summer day where the battery will
    refill from PV. The rule engine's CheapRateCharge rule already
    handles that decision; this guard catches accidental misconfigs
    that would silently leave the cheap window uncovered.
    """
    today_import = filter_today(import_rates, inputs.now)
    cheap = bottom_n_pct(today_import, bottom_n_pct_threshold)
    if not cheap:
        return []

    pairs = _slots_overlapping_rates(programme, cheap)
    covered = any(programme.slots[i].grid_charge for i, _ in pairs)
    if covered:
        return []

    cheap_start = min(c.start.strftime("%H:%M") for c in cheap)
    cheap_end = max(c.end.strftime("%H:%M") for c in cheap)
    return [
        f"no grid_charge=True slot covers the cheap window "
        f"({cheap_start}-{cheap_end}, "
        f"{len(cheap)} slots @ avg "
        f"{sum(c.rate_pence for c in cheap) / len(cheap):.2f}p)"
    ]


def validate_plan(
    programme: ProgrammeState,
    inputs: ProgrammeInputs,
    *,
    peak_threshold_p: float = 24.0,
    cheap_bottom_pct: int = 25,
    max_charge_kw: float = 5.0,
    max_discharge_kw: float = 5.0,
    charge_efficiency: float = 0.95,
    discharge_efficiency: float = 0.95,
) -> ValidationResult:
    """Run all SPEC §6 pre-write checks and a 24-hour projection.

    `errors` block the write (coordinator keeps the previous plan).
    `warnings` are logged but allow the write to proceed.
    `projection` is populated regardless so the dashboard can show
    the expected return for both accepted and rejected plans.
    """
    errors: list[str] = []
    warnings: list[str] = []

    errors.extend(_check_static(programme, min_soc=inputs.min_soc))

    if not errors:
        errors.extend(_check_no_grid_charge_in_peak(
            programme, inputs.import_rates, peak_threshold_p,
        ))

    warnings.extend(_check_cheap_window_covered(
        programme, inputs.import_rates, inputs, cheap_bottom_pct,
    ))

    projection = project_day(
        programme,
        inputs,
        battery_capacity_kwh=inputs.battery_capacity_kwh,
        max_charge_kw=max_charge_kw,
        max_discharge_kw=max_discharge_kw,
        charge_efficiency=charge_efficiency,
        discharge_efficiency=discharge_efficiency,
        peak_threshold_p=peak_threshold_p,
    )

    if projection.peak_import_kwh > 0.001:
        # H1 says reality wins for forced peak imports - it's a warning
        # surfaced on the dashboard, not a reject. The plan-time GC=True
        # check above catches the deliberate version of this mistake.
        warnings.append(
            f"projection forecasts {projection.peak_import_kwh:.2f} kWh "
            f"of peak-rate import (cost "
            f"{projection.peak_import_pence / 100.0:.2f}); "
            f"likely battery floor reached during peak hours"
        )

    return ValidationResult(
        errors=errors, warnings=warnings, projection=projection,
    )
