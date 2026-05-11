"""Weekly digest builder — exposes sensor.heo3_weekly_digest.

Per planner design §10:
- Sunday 23:55 local
- Aggregates the past week's tracker data
- Publishes JSON summary as state attributes
- Includes recommendations (heuristic "consider..." strings)

Tuner activity for the week is included so paddy can review what
self-tuned and whether to override.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..performance_tracker import PerformanceTracker
from .tuner import Tuner, TuningAction

logger = logging.getLogger(__name__)


# Sensor entity ID published by the digest.
DIGEST_SENSOR_ENTITY = "sensor.heo3_weekly_digest"


@dataclass
class WeeklyDigest:
    """Full breakdown of the past week's behaviour."""

    period_start: str  # ISO UTC
    period_end: str    # ISO UTC
    tick_count: int

    # Snapshot averages
    avg_battery_soc_pct: float
    avg_grid_power_w: float
    avg_solar_power_w: float
    avg_load_power_w: float

    # Forecast accuracy
    load_forecast_mean_pct_error: float
    load_forecast_rms_pct_error: float
    solar_forecast_mean_pct_error: float
    solar_forecast_rms_pct_error: float

    # Rule activations
    rule_activations: dict[str, int]

    # Apply outcomes
    total_writes_requested: int
    total_writes_succeeded: int
    total_writes_failed: int
    total_apply_duration_ms: float

    # Tuner actions over the week
    tuning_actions_this_week: list[dict]

    # Notable events
    eps_triggers: int
    saving_sessions: int
    igo_dispatches: int

    # Heuristic recommendations
    recommendations: list[str]


def build_digest(
    tracker: PerformanceTracker,
    tuner: Tuner | None = None,
    *,
    period_end: datetime | None = None,
    period_days: int = 7,
) -> WeeklyDigest:
    """Compute the digest from the tracker's recent history."""
    end = period_end or datetime.now(timezone.utc)
    start = end - timedelta(days=period_days)

    ticks = tracker.ticks_in_window(start=start, end=end)

    # Snapshot averages — only ticks with non-None values count.
    def _avg(field: str) -> float:
        values = [t[field] for t in ticks if t.get(field) is not None]
        if not values:
            return 0.0
        return sum(values) / len(values)

    # Rule activation counter
    activations = Counter()
    for t in ticks:
        for rule in t.get("active_rules", []):
            activations[rule] += 1

    # Apply outcomes
    total_req = sum(t.get("writes_requested", 0) for t in ticks)
    total_ok = sum(t.get("writes_succeeded", 0) for t in ticks)
    total_fail = sum(t.get("writes_failed", 0) for t in ticks)
    total_dur = sum(t.get("apply_duration_ms", 0) for t in ticks)

    # Notable events — count ticks where flags were on.
    eps = sum(1 for t in ticks if t.get("eps_active"))
    saving = sum(1 for t in ticks if t.get("saving_session_active"))
    igo = sum(1 for t in ticks if t.get("igo_dispatching"))

    # Tuning actions in window
    tuning_actions: list[dict] = []
    if tuner is not None:
        for action in tuner.actions_taken(since=start):
            tuning_actions.append(
                {
                    "timestamp": action.timestamp,
                    "rule": action.rule_name,
                    "parameter": action.parameter,
                    "old_value": action.old_value,
                    "new_value": action.new_value,
                    "reason": action.reason,
                }
            )

    # Recommendations
    recs = _build_recommendations(
        activations=activations,
        load_err=tracker.load_forecast_error.mean_pct_error,
        solar_err=tracker.solar_forecast_error.mean_pct_error,
        write_failed=total_fail,
        write_total=total_req,
        tick_count=len(ticks),
    )

    return WeeklyDigest(
        period_start=start.isoformat(),
        period_end=end.isoformat(),
        tick_count=len(ticks),
        avg_battery_soc_pct=_avg("battery_soc_pct"),
        avg_grid_power_w=_avg("grid_power_w"),
        avg_solar_power_w=_avg("solar_power_w"),
        avg_load_power_w=_avg("load_power_w"),
        load_forecast_mean_pct_error=tracker.load_forecast_error.mean_pct_error,
        load_forecast_rms_pct_error=tracker.load_forecast_error.rms_pct_error,
        solar_forecast_mean_pct_error=tracker.solar_forecast_error.mean_pct_error,
        solar_forecast_rms_pct_error=tracker.solar_forecast_error.rms_pct_error,
        rule_activations=dict(activations),
        total_writes_requested=total_req,
        total_writes_succeeded=total_ok,
        total_writes_failed=total_fail,
        total_apply_duration_ms=total_dur,
        tuning_actions_this_week=tuning_actions,
        eps_triggers=eps,
        saving_sessions=saving,
        igo_dispatches=igo,
        recommendations=recs,
    )


def _build_recommendations(
    *,
    activations: Counter,
    load_err: float,
    solar_err: float,
    write_failed: int,
    write_total: int,
    tick_count: int = 0,
) -> list[str]:
    """Heuristic 'consider...' strings.

    The set should grow organically as paddy reads digests and
    identifies patterns worth flagging.
    """
    recs: list[str] = []

    # Forecast errors
    if abs(load_err) > 20:
        recs.append(
            f"Load forecast mean error {load_err:+.1f}% — consider reviewing "
            "the HEO-5 load model or enabling tuner."
        )
    if abs(solar_err) > 20:
        recs.append(
            f"Solar forecast mean error {solar_err:+.1f}% — Solcast may need "
            "recalibration or check for shading."
        )

    # Write failures
    if write_total > 0:
        fail_pct = (write_failed / write_total) * 100
        if fail_pct > 5:
            recs.append(
                f"Inverter write failures {fail_pct:.1f}% — investigate SA "
                "broker latency or vocabulary mismatches."
            )

    # Inactive rules — only flag if we had ticks (otherwise it's
    # just an empty period, nothing to recommend).
    if tick_count > 0:
        expected_rules = (
            "min_soc_floor",
            "cheap_rate_charge",
            "evening_drain",
        )
        for rule in expected_rules:
            if activations.get(rule, 0) == 0:
                recs.append(
                    f"Rule {rule!r} did not fire all week — confirm its "
                    "conditions still match reality."
                )

    if not recs:
        recs.append("System operating within expected parameters.")
    return recs


def publish_digest_sensor(hass, digest: WeeklyDigest) -> None:  # type: ignore[no-untyped-def]
    """Push the digest onto sensor.heo3_weekly_digest."""
    state = (
        f"{digest.tick_count} ticks; "
        f"{digest.total_writes_succeeded}/{digest.total_writes_requested} writes ok"
    )
    hass.states.async_set(
        DIGEST_SENSOR_ENTITY,
        state,
        attributes=_digest_to_attrs(digest),
    )


def _digest_to_attrs(digest: WeeklyDigest) -> dict[str, Any]:
    """Project to a flat dict for the sensor's attributes."""
    return {
        "period_start": digest.period_start,
        "period_end": digest.period_end,
        "tick_count": digest.tick_count,
        "avg_battery_soc_pct": round(digest.avg_battery_soc_pct, 1),
        "avg_grid_power_w": round(digest.avg_grid_power_w, 0),
        "avg_solar_power_w": round(digest.avg_solar_power_w, 0),
        "avg_load_power_w": round(digest.avg_load_power_w, 0),
        "load_forecast_mean_pct_error": round(digest.load_forecast_mean_pct_error, 1),
        "load_forecast_rms_pct_error": round(digest.load_forecast_rms_pct_error, 1),
        "solar_forecast_mean_pct_error": round(digest.solar_forecast_mean_pct_error, 1),
        "solar_forecast_rms_pct_error": round(digest.solar_forecast_rms_pct_error, 1),
        "rule_activations": digest.rule_activations,
        "total_writes_requested": digest.total_writes_requested,
        "total_writes_succeeded": digest.total_writes_succeeded,
        "total_writes_failed": digest.total_writes_failed,
        "tuning_actions_this_week": digest.tuning_actions_this_week,
        "eps_triggers": digest.eps_triggers,
        "saving_sessions": digest.saving_sessions,
        "igo_dispatches": digest.igo_dispatches,
        "recommendations": digest.recommendations,
    }
