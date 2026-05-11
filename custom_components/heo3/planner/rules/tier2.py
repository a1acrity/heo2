"""Tier 2 — Mode rules. Event-driven, PREFER claims."""

from __future__ import annotations

from datetime import timedelta

from ...types import Snapshot, TimeRange
from ..rule import (
    ChargeIntent,
    Claim,
    ClaimStrength,
    DeferEVIntent,
    DrainIntent,
    Rule,
    RuleContext,
    Tunable,
)


class SavingSessionRule:
    """Octoplus saving session active — drain to floor + buffer.

    The £/kWh signal during a saving session (~£3) dominates regular
    peak rates. We drain hard during the session window to maximise
    export, leaving just `min_soc + drain_buffer_pct` headroom.
    """

    name = "saving_session"
    tier = 2
    description = "Drain battery during Octoplus saving session for maximum export"

    def __init__(self) -> None:
        self._params = {
            "drain_buffer_pct": Tunable(
                "drain_buffer_pct",
                default=5.0, lower=0.0, upper=15.0,
                description="Headroom above min_soc to leave during session",
            ),
        }

    @property
    def parameters(self) -> dict[str, Tunable]:
        return self._params

    def evaluate(self, snap: Snapshot, ctx: RuleContext) -> Claim | None:
        if not snap.flags.saving_session_active:
            return None

        window = snap.flags.saving_session_window
        if window is None:
            return None

        # Skip if we're not actually in the window yet.
        if not (window.start <= snap.captured_at < window.end):
            return None

        floor = snap.config.min_soc
        buffer = int(ctx.parameters.get("drain_buffer_pct", 5.0))
        target = floor + buffer

        return Claim(
            rule_name=self.name,
            intent=DrainIntent(target_soc_pct=target, by_time=window.end),
            rationale=(
                f"saving session active, drain to {target}% by "
                f"{window.end.isoformat(timespec='minutes')}"
            ),
            strength=ClaimStrength.PREFER,
            horizon=window,
            expected_pence_impact=300.0,  # rough — £3/kWh × ~1 kWh assumption
        )


class IGODispatchRule:
    """Octopus IGO smart-charge dispatch active — charge during the dispatch window.

    Avoids HEO II's F2 race by checking saving_session first; if
    a saving session is active, we yield (saving session is paying us
    to export, not charging from grid).
    """

    name = "igo_dispatch"
    tier = 2
    description = "Charge during Octopus IGO smart dispatch window"

    def __init__(self) -> None:
        self._params = {
            "target_soc_pct": Tunable(
                "target_soc_pct",
                default=80.0, lower=50.0, upper=100.0,
                description="Target SOC by end of dispatch",
            ),
        }

    @property
    def parameters(self) -> dict[str, Tunable]:
        return self._params

    def evaluate(self, snap: Snapshot, ctx: RuleContext) -> Claim | None:
        # F2 race fix: if saving session is active, don't claim — that
        # rule wants the opposite (drain hard).
        if snap.flags.saving_session_active:
            return None

        if not snap.flags.igo_dispatching:
            return None

        # Find the active dispatch in planned[].
        active = next(
            (
                d for d in snap.flags.igo_planned
                if d.start <= snap.captured_at < d.end
            ),
            None,
        )
        if active is None:
            # IGO says dispatching but no slot covers now — be cautious.
            # Use a 1h horizon as a default.
            end = snap.captured_at + timedelta(hours=1)
        else:
            end = active.end

        target = int(ctx.parameters.get("target_soc_pct", 80.0))
        return Claim(
            rule_name=self.name,
            intent=ChargeIntent(target_soc_pct=target, by_time=end),
            rationale=(
                f"IGO dispatching, charge to {target}% by "
                f"{end.isoformat(timespec='minutes')}"
            ),
            strength=ClaimStrength.PREFER,
            horizon=TimeRange(start=snap.captured_at, end=end),
            expected_pence_impact=0.0,  # cost-saving via cheap dispatch
        )


class EVDeferralRule:
    """SPEC §12: stop the EV during top export windows.

    Fires when:
    - User has enabled defer_ev (switch.heo3_defer_ev_when_export_high)
    - Current slot is one of the top-N export windows
    - EV is currently charging (otherwise no-op)

    Returns DeferEVIntent. RestoreEV happens via a separate logic
    when this rule's conditions stop holding (handled implicitly by
    the next tick's evaluate not firing).
    """

    name = "ev_deferral"
    tier = 2
    description = "Stop EV charging during top export windows to maximise sell revenue"

    def __init__(self) -> None:
        self._params = {
            "top_export_windows_n": Tunable(
                "top_export_windows_n",
                default=3.0, lower=1.0, upper=10.0,
                description="N top-rated export windows to consider",
            ),
        }

    @property
    def parameters(self) -> dict[str, Tunable]:
        return self._params

    def evaluate(self, snap: Snapshot, ctx: RuleContext) -> Claim | None:
        if not snap.flags.defer_ev_eligible:
            return None
        if not snap.ev.charging:
            return None

        n = int(ctx.parameters.get("top_export_windows_n", 3.0))
        top_windows = ctx.compute.top_export_windows(snap, n=n)
        in_top = any(
            w.start <= snap.captured_at < w.end for w in top_windows
        )
        if not in_top:
            return None

        # Use the active window's end as the deferral horizon.
        active_window = next(
            (w for w in top_windows if w.start <= snap.captured_at < w.end),
            None,
        )
        end = (
            active_window.end if active_window
            else snap.captured_at + timedelta(hours=1)
        )

        return Claim(
            rule_name=self.name,
            intent=DeferEVIntent(),
            rationale="top export window active — stop EV to maximise export",
            strength=ClaimStrength.PREFER,
            horizon=TimeRange(start=snap.captured_at, end=end),
            expected_pence_impact=20.0,  # rough — depends on export rate
        )
