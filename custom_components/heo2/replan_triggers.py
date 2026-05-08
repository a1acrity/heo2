# custom_components/heo2/replan_triggers.py
"""Decide whether a 15-min coordinator tick should commit a fresh
programme to the inverter or re-use the most recent baseline plan.

SPEC §8 separates two cadences:

  * **Daily plan (18:00 local)**: full re-evaluation. Tomorrow's Octopus
    rates have been published since 16:00 (2-hour safety margin); we
    compute a new programme and write it.
  * **15-min ticks** between daily plans: the rules still run for
    sensors and the dashboard, but the programme that lands on the
    inverter is the most recent baseline UNLESS one of the trigger
    conditions has fired:

      - Solar forecast deviation > replan_solar_pct from the rest-of-day
        forecast captured at last plan time
      - Load forecast deviation > replan_load_pct
      - SOC deviation > replan_soc_pct from the projected trajectory
      - New IGO dispatch announced (igo_dispatching transitioned False -> True)
      - Saving session announced (saving_session transitioned False -> True)
      - Grid restored after a previous loss (grid_connected False -> True)

This module is pure logic. The coordinator owns the baseline state and
calls `should_commit_replan()` each tick to decide whether to accept
the new programme as a baseline replacement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from .models import ProgrammeInputs, ProgrammeState


@dataclass
class BaselineSnapshot:
    """Captured state at the moment a baseline programme was committed.

    Used to detect deviation triggers on subsequent ticks. The snapshot
    fields are the inputs that drove the original plan; comparing
    "what the world looked like then" vs "what it looks like now"
    surfaces the SPEC §8 trigger conditions.
    """

    programme: ProgrammeState
    captured_at: datetime
    rest_of_day_solar_kwh: float
    rest_of_day_load_kwh: float
    soc_at_capture: float
    igo_dispatching: bool
    saving_session: bool
    grid_connected: bool
    daily_plan_date: date | None = None  # local date for which this is the 18:00 plan


@dataclass
class ReplanDecision:
    """Result of `should_commit_replan`: whether to commit the new
    programme as the baseline, and the trigger reason for logging."""

    commit: bool
    reason: str


def _rest_of_day_kwh(forecast_24: list[float], current_hour_local: int) -> float:
    """Sum the forecast kWh from the current local hour to end-of-day."""
    if not forecast_24:
        return 0.0
    return float(sum(forecast_24[current_hour_local:24]))


def _local_now(now_utc: datetime, tz: ZoneInfo | None) -> datetime:
    if tz is None:
        return now_utc
    return now_utc.astimezone(tz)


def _percent_change(current: float, baseline: float) -> float:
    """Symmetric percent change. Zero baseline maps to:
      - 0% if current is also zero
      - 100% otherwise
    Avoids divide-by-zero spam from quiet overnight hours.
    """
    if baseline <= 0:
        return 0.0 if current <= 0 else 100.0
    return abs(current - baseline) / baseline * 100.0


def _is_daily_plan_window(
    now_local: datetime,
    daily_plan_time: time,
    last_plan_date: date | None,
    window_minutes: int = 30,
) -> bool:
    """True if `now_local` is within the daily-plan window AND we have
    not yet committed a daily plan today.

    The window is `[daily_plan_time, daily_plan_time + window_minutes)`
    so a missed tick (e.g. HA restart at 18:02) still triggers the
    daily plan when we recover. After 18:30 a missed window means we
    wait until tomorrow.
    """
    today = now_local.date()
    if last_plan_date == today:
        return False
    h, m = daily_plan_time.hour, daily_plan_time.minute
    plan_dt = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
    window_end = plan_dt.replace(minute=m + window_minutes) if m + window_minutes < 60 else \
        plan_dt.replace(hour=h + 1, minute=(m + window_minutes) % 60)
    return plan_dt <= now_local < window_end


def should_commit_replan(
    *,
    new_programme: ProgrammeState,
    inputs: ProgrammeInputs,
    baseline: BaselineSnapshot | None,
    tz: ZoneInfo | None,
    daily_plan_time: time,
    replan_solar_pct: float,
    replan_load_pct: float,
    replan_soc_pct: float,
) -> ReplanDecision:
    """Decide if the new programme should replace the current baseline.

    Trigger order (first match wins, for cleaner logs):
      1. No baseline yet -> commit (first ever tick)
      2. Daily-plan window AND not already planned today -> commit
      3. Trigger condition fired -> commit
      4. Otherwise -> hold the existing baseline
    """
    if baseline is None:
        return ReplanDecision(True, "first plan: no prior baseline")

    now_local = _local_now(inputs.now, tz)
    if _is_daily_plan_window(
        now_local, daily_plan_time, baseline.daily_plan_date,
    ):
        return ReplanDecision(
            True,
            f"daily plan window ({daily_plan_time.strftime('%H:%M')}): "
            f"committing fresh programme for "
            f"{now_local.date().isoformat()}",
        )

    # IGO dispatch - announced only at the boundary, so transition matters.
    if inputs.igo_dispatching and not baseline.igo_dispatching:
        return ReplanDecision(True, "trigger: new IGO dispatch announced")

    # Saving session - same as IGO, transition-driven.
    if inputs.saving_session and not baseline.saving_session:
        return ReplanDecision(True, "trigger: saving session announced")

    # Grid loss/restore: an EPS event will have set grid_connected=False
    # somewhere; restoring it should re-plan against fresh state.
    if inputs.grid_connected and not baseline.grid_connected:
        return ReplanDecision(True, "trigger: grid restored")

    # Quantitative deviations - take the local-hour rest-of-day slice
    # of the same forecasts captured in the baseline.
    current_hour_local = now_local.hour
    new_solar = _rest_of_day_kwh(inputs.solar_forecast_kwh, current_hour_local)
    new_load = _rest_of_day_kwh(inputs.load_forecast_kwh, current_hour_local)

    solar_dev = _percent_change(new_solar, baseline.rest_of_day_solar_kwh)
    if solar_dev > replan_solar_pct:
        return ReplanDecision(
            True,
            f"trigger: solar forecast deviation {solar_dev:.0f}% "
            f"({baseline.rest_of_day_solar_kwh:.1f} -> {new_solar:.1f} kWh)"
        )

    load_dev = _percent_change(new_load, baseline.rest_of_day_load_kwh)
    if load_dev > replan_load_pct:
        return ReplanDecision(
            True,
            f"trigger: load forecast deviation {load_dev:.0f}% "
            f"({baseline.rest_of_day_load_kwh:.1f} -> {new_load:.1f} kWh)"
        )

    soc_dev = abs(inputs.current_soc - baseline.soc_at_capture)
    if soc_dev > replan_soc_pct:
        return ReplanDecision(
            True,
            f"trigger: SOC deviation {soc_dev:.0f}% "
            f"({baseline.soc_at_capture:.0f} -> {inputs.current_soc:.0f}%)"
        )

    # SPEC §2 globals (work_mode / energy_pattern / max_*_a): a runtime
    # rule activation - PeakArbitrageRule entering an allocated top-
    # priced export slot, EVDeferralRule crossing its threshold, or
    # SavingSession ending and reverting work_mode to the Baseline
    # default - changes the new programme's globals without firing any
    # of the input-deviation triggers above. Without this check the
    # new globals never land on the inverter and the rule's intent is
    # silently dropped (real production miss 2026-05-08: peak-export
    # window passed with work_mode stuck at Zero export to CT).
    globals_diff = _globals_diff(new_programme, baseline.programme)
    if globals_diff:
        return ReplanDecision(
            True,
            f"trigger: programme globals changed ({globals_diff})",
        )

    return ReplanDecision(
        False,
        f"hold baseline ({baseline.captured_at.strftime('%H:%M')}; "
        f"solar dev {solar_dev:.0f}% < {replan_solar_pct:.0f}%, "
        f"load dev {load_dev:.0f}% < {replan_load_pct:.0f}%, "
        f"SOC dev {soc_dev:.1f}% < {replan_soc_pct:.0f}%)"
    )


def _globals_diff(new: ProgrammeState, baseline: ProgrammeState) -> str | None:
    """Return a short diff description if any SPEC §2 global differs
    between `new` and `baseline`, else None. Comparison rules match
    `MqttWriter.diff_globals` so this trigger and the writer agree on
    what counts as a meaningful delta:

      * strings: case-insensitive, whitespace-trimmed equality
      * floats: tol=0.5 to suppress noisy round-trip flicker
      * None on either side is a "don't touch" signal -> ignored
    """
    def _str_equal(a, b) -> bool:
        if a is None or b is None:
            return True  # don't fire on "don't touch" sentinel
        return str(a).strip().casefold() == str(b).strip().casefold()

    def _float_equal(a, b, tol: float = 0.5) -> bool:
        if a is None or b is None:
            return True
        return abs(float(a) - float(b)) <= tol

    diffs: list[str] = []
    if not _str_equal(new.work_mode, baseline.work_mode):
        diffs.append(f"work_mode {baseline.work_mode!r}->{new.work_mode!r}")
    if not _str_equal(new.energy_pattern, baseline.energy_pattern):
        diffs.append(
            f"energy_pattern {baseline.energy_pattern!r}->{new.energy_pattern!r}"
        )
    if not _float_equal(new.max_charge_a, baseline.max_charge_a):
        diffs.append(
            f"max_charge_a {baseline.max_charge_a}->{new.max_charge_a}"
        )
    if not _float_equal(new.max_discharge_a, baseline.max_discharge_a):
        diffs.append(
            f"max_discharge_a {baseline.max_discharge_a}->{new.max_discharge_a}"
        )
    return "; ".join(diffs) if diffs else None


def capture_baseline(
    programme: ProgrammeState,
    inputs: ProgrammeInputs,
    *,
    tz: ZoneInfo | None,
    is_daily_plan: bool,
) -> BaselineSnapshot:
    """Build a BaselineSnapshot from the inputs that drove `programme`.

    `is_daily_plan` marks this baseline as the canonical 18:00 plan for
    its local date; further ticks on the same date won't fire a second
    daily-plan commit.
    """
    now_local = _local_now(inputs.now, tz)
    return BaselineSnapshot(
        programme=programme,
        captured_at=inputs.now,
        rest_of_day_solar_kwh=_rest_of_day_kwh(
            inputs.solar_forecast_kwh, now_local.hour,
        ),
        rest_of_day_load_kwh=_rest_of_day_kwh(
            inputs.load_forecast_kwh, now_local.hour,
        ),
        soc_at_capture=inputs.current_soc,
        igo_dispatching=inputs.igo_dispatching,
        saving_session=inputs.saving_session,
        grid_connected=inputs.grid_connected,
        daily_plan_date=now_local.date() if is_daily_plan else None,
    )
