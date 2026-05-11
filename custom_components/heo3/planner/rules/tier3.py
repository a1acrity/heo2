"""Tier 3 — Optimisation rules. Rate-driven, mostly PREFER + OFFER claims."""

from __future__ import annotations

from datetime import timedelta

from ...types import Snapshot, TimeRange
from ..rule import (
    ChargeIntent,
    Claim,
    ClaimStrength,
    DrainIntent,
    HoldIntent,
    Rule,
    RuleContext,
    SellIntent,
    Tunable,
)


class CheapRateChargeRule:
    """Charge from grid during cheap-rate windows.

    Asymmetry-aware: target SOC sized for worst-case (P90 load + P10
    solar) rather than P50, because cost-of-being-wrong (peak-rate
    replacement) >> upside-of-being-right (some surplus to export).

    Adds `safety_margin_pct` on top of the worst-case bridge size,
    tuned by observed forecast error.
    """

    name = "cheap_rate_charge"
    tier = 3
    description = "Charge to bridge tomorrow's load during cheap window (asymmetric)"

    def __init__(self) -> None:
        self._params = {
            "safety_margin_pct": Tunable(
                "safety_margin_pct",
                default=10.0, lower=0.0, upper=30.0,
                description="Extra %SOC above bridge_kwh estimate (asymmetric buffer)",
            ),
            "max_target_soc_pct": Tunable(
                "max_target_soc_pct",
                default=80.0, lower=50.0, upper=100.0,
                description="Cap on charge target — avoid 100% if not needed",
            ),
        }

    @property
    def parameters(self) -> dict[str, Tunable]:
        return self._params

    def evaluate(self, snap: Snapshot, ctx: RuleContext) -> Claim | None:
        from datetime import timedelta as _td

        cheap = ctx.compute.next_cheap_window(snap)
        if cheap is None:
            return None

        # Only fire if the cheap window covers the current snapshot time.
        if not (cheap.start <= snap.captured_at < cheap.end):
            return None

        # Bridge to NEXT cheap opportunity (not "now to next cheap" —
        # we're IN the cheap window now). Find next cheap after this
        # one ends; default to 24h if no further cheap horizon.
        next_cheap_after = ctx.compute.next_cheap_window(snap, after=cheap.end)
        until = (
            next_cheap_after.start if next_cheap_after is not None
            else cheap.end + _td(hours=24)
        )

        # Energy needed during the period after this cheap window ends.
        load_after = (
            ctx.compute.cumulative_load_to(snap, until)
            - ctx.compute.cumulative_load_to(snap, cheap.end)
        )
        solar_after = (
            ctx.compute.cumulative_solar_to(snap, until)
            - ctx.compute.cumulative_solar_to(snap, cheap.end)
        )
        bridge_kwh = max(0.0, load_after - solar_after)
        if bridge_kwh <= 0:
            # PV after cheap window covers the load — no need to charge.
            return None

        # Convert to SOC %. Add safety margin (asymmetric).
        target_kwh = bridge_kwh * (
            1.0 + ctx.parameters.get("safety_margin_pct", 10.0) / 100.0
        )
        target_pct = ctx.compute.soc_for_kwh(target_kwh, snap)

        # Add to current SOC to get desired target.
        current_soc = snap.inverter.battery_soc_pct or snap.config.min_soc
        desired_target = int(round(current_soc + target_pct))

        # Cap at max_target_soc_pct.
        max_target = int(ctx.parameters.get("max_target_soc_pct", 80.0))
        target = min(desired_target, max_target)

        # If we're already above the target, no-op.
        if current_soc >= target:
            return None

        return Claim(
            rule_name=self.name,
            intent=ChargeIntent(target_soc_pct=target, by_time=cheap.end),
            rationale=(
                f"cheap window active, bridge_kwh={bridge_kwh:.1f}, "
                f"target SOC={target}% (current {current_soc:.0f}%)"
            ),
            strength=ClaimStrength.PREFER,
            horizon=TimeRange(start=snap.captured_at, end=cheap.end),
            expected_pence_impact=bridge_kwh * 20.0,  # rough — saved peak rate
        )


class PeakExportArbitrageRule:
    """Sell battery during top export windows.

    THE asymmetry-aware rule. Computes spread as:
        export_rate - WORST_CASE_REPLACEMENT_RATE
    where WORST_CASE_REPLACEMENT is the next peak import rate (not
    the next cheap rate). Only fires if spread > threshold.

    The 2026-05-08 lesson: HEO II's PeakArbitrage assumed cheap
    replacement (~5p), so any spread looked attractive. When forecasts
    missed (load higher than predicted), replacement actually came
    at peak rate (~25p), wiping out the arbitrage gain.
    """

    name = "peak_export_arbitrage"
    tier = 3
    description = "Sell during top export windows; spread vs worst-case replacement"

    def __init__(self) -> None:
        self._params = {
            "spread_threshold_pence": Tunable(
                "spread_threshold_pence",
                default=8.0, lower=3.0, upper=30.0,
                description="Min spread (export - peak_import) to fire",
            ),
            "sell_fraction": Tunable(
                "sell_fraction",
                default=0.5, lower=0.1, upper=1.0,
                description="Fraction of usable_kwh to sell per qualifying window",
            ),
        }

    @property
    def parameters(self) -> dict[str, Tunable]:
        return self._params

    def evaluate(self, snap: Snapshot, ctx: RuleContext) -> Claim | None:
        # Skip if we're below floor (MinSOCFloor's job to enforce).
        usable = ctx.compute.usable_kwh(snap)
        if usable <= 0:
            return None

        top_windows = ctx.compute.top_export_windows(snap, n=3)
        if not top_windows:
            return None

        # Find the active window (if any).
        active = next(
            (w for w in top_windows if w.start <= snap.captured_at < w.end),
            None,
        )
        if active is None:
            return None

        # Worst-case replacement = next peak import rate.
        next_peak = ctx.compute.next_peak_window(snap)
        replacement_rate = (
            next_peak.avg_rate_pence if next_peak else 30.0  # paranoid fallback
        )
        spread = active.rate_pence - replacement_rate

        threshold = ctx.parameters.get("spread_threshold_pence", 8.0)
        if spread < threshold:
            return None

        sell_fraction = ctx.parameters.get("sell_fraction", 0.5)
        kwh_to_sell = usable * sell_fraction

        return Claim(
            rule_name=self.name,
            intent=SellIntent(
                kwh=kwh_to_sell,
                across_slot_starts=(active.start,),
            ),
            rationale=(
                f"export {active.rate_pence:.1f}p > peak-replacement "
                f"{replacement_rate:.1f}p, spread {spread:.1f}p "
                f"(threshold {threshold:.1f}p), sell {kwh_to_sell:.1f} kWh"
            ),
            strength=ClaimStrength.PREFER,
            horizon=TimeRange(start=active.start, end=active.end),
            expected_pence_impact=kwh_to_sell * spread,
        )


class SolarSurplusRule:
    """Hold high SOC when PV is running so surplus charges battery.

    OFFER strength: yields to anything that wants the slot for a
    different purpose (e.g. SavingSession draining for £3/kWh).
    """

    name = "solar_surplus"
    tier = 3
    description = "Allow PV surplus to charge battery when generating"

    def __init__(self) -> None:
        self._params = {
            "surplus_threshold_w": Tunable(
                "surplus_threshold_w",
                default=500.0, lower=100.0, upper=2000.0,
                description="PV must exceed load by this many W to fire",
            ),
        }

    @property
    def parameters(self) -> dict[str, Tunable]:
        return self._params

    def evaluate(self, snap: Snapshot, ctx: RuleContext) -> Claim | None:
        solar = snap.inverter.solar_power_w
        load = snap.inverter.load_power_w
        if solar is None or load is None:
            return None

        surplus = solar - load
        threshold = ctx.parameters.get("surplus_threshold_w", 500.0)
        if surplus < threshold:
            return None

        # Need headroom for the surplus to land somewhere.
        headroom = ctx.compute.headroom_kwh(snap)
        if headroom <= 0:
            return None

        # Hold target = 100% (let PV fill).
        window = TimeRange(
            start=snap.captured_at,
            end=snap.captured_at + timedelta(hours=1),
        )
        return Claim(
            rule_name=self.name,
            intent=HoldIntent(soc_pct=100, window=window),
            rationale=(
                f"PV surplus {surplus:.0f}W (headroom {headroom:.1f} kWh) — "
                f"hold at 100% to absorb"
            ),
            strength=ClaimStrength.OFFER,
            horizon=window,
            expected_pence_impact=0.0,  # consumed via reduced grid import
        )


class EveningDrainRule:
    """Drain battery to target_end_soc by 23:30 to make room for cheap charge.

    OFFER strength: defaults beat doing nothing but yield to other
    rules. Active during evening window (default 19:00-23:30 local).
    """

    name = "evening_drain"
    tier = 3
    description = "Drain battery to target_end_soc by overnight cheap-charge start"

    def __init__(self) -> None:
        self._params = {
            "target_end_soc": Tunable(
                "target_end_soc",
                default=25.0, lower=10.0, upper=50.0,
                description="Target SOC at end of drain window",
            ),
            "drain_start_hour": Tunable(
                "drain_start_hour",
                default=19.0, lower=16.0, upper=22.0,
                description="Local hour to start drain",
            ),
            "drain_end_hour": Tunable(
                "drain_end_hour",
                default=23.5, lower=22.0, upper=24.0,
                description="Local hour for drain to complete",
            ),
        }

    @property
    def parameters(self) -> dict[str, Tunable]:
        return self._params

    def evaluate(self, snap: Snapshot, ctx: RuleContext) -> Claim | None:
        local = snap.captured_at.astimezone(snap.local_tz)
        hour_now = local.hour + local.minute / 60.0

        start_h = ctx.parameters.get("drain_start_hour", 19.0)
        end_h = ctx.parameters.get("drain_end_hour", 23.5)
        if hour_now < start_h or hour_now >= end_h:
            return None

        target = int(ctx.parameters.get("target_end_soc", 25.0))
        current_soc = snap.inverter.battery_soc_pct
        if current_soc is not None and current_soc <= target:
            # Already at or below target — no-op.
            return None

        # Compute end-time = today's end_h local.
        end_local = local.replace(
            hour=int(end_h), minute=int((end_h % 1) * 60),
            second=0, microsecond=0,
        )
        if end_local < local:
            end_local = end_local + timedelta(days=1)
        end_utc = end_local.astimezone(snap.captured_at.tzinfo)

        return Claim(
            rule_name=self.name,
            intent=DrainIntent(target_soc_pct=target, by_time=end_utc),
            rationale=(
                f"evening window, drain to {target}% by "
                f"{end_local.strftime('%H:%M')}"
            ),
            strength=ClaimStrength.OFFER,
            horizon=TimeRange(start=snap.captured_at, end=end_utc),
            expected_pence_impact=0.0,
        )
