"""Compute — pure-function library over a Snapshot.

Five families per §12 of the design:
- Energy / SOC / kWh conversions
- Time / rate windows
- Forecast aggregation
- Counterfactual analysis (visibility / dashboard)
- Physics predictions

Stateless. Every method takes a Snapshot (or relevant subset) plus
parameters; returns a value. No I/O. Safe to call from anywhere.

P1.0 stub: methods raise NotImplementedError. Full implementation
in P1.8.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .types import PlannedAction, Snapshot


@dataclass(frozen=True)
class TimeRange:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class RateWindow:
    start: datetime
    end: datetime
    rate_pence: float
    avg_rate_pence: float


class RateBand:
    PEAK = "peak"
    OFF_PEAK = "off_peak"
    CHEAP_WINDOW = "cheap_window"


class Compute:
    """Stateless library of derived calculations. P1.8."""

    # ── 12a. Energy / SOC / kWh conversions ───────────────────────

    def kwh_for_soc(self, soc_pct: float, snap: Snapshot) -> float:
        raise NotImplementedError("P1.8 — Compute.kwh_for_soc")

    def soc_for_kwh(self, kwh: float, snap: Snapshot) -> float:
        raise NotImplementedError("P1.8 — Compute.soc_for_kwh")

    def usable_kwh(self, snap: Snapshot) -> float:
        raise NotImplementedError("P1.8 — Compute.usable_kwh")

    def headroom_kwh(self, snap: Snapshot) -> float:
        """Energy capacity remaining for charging: from current SOC to
        100%. The hardware has no global SOC ceiling — if the planner
        wants one, it tracks it in policy and throttles charge current
        when SOC reaches its chosen cap (write max_charge_a=1 for
        trickle, =0 for hard freeze).
        """
        raise NotImplementedError("P1.8 — Compute.headroom_kwh")

    def round_trip_efficiency(self) -> float:
        raise NotImplementedError("P1.8 — Compute.round_trip_efficiency")

    # ── 12b. Time / rate windows ──────────────────────────────────

    def next_cheap_window(
        self, snap: Snapshot, *, after: datetime | None = None
    ) -> RateWindow | None:
        raise NotImplementedError("P1.8 — Compute.next_cheap_window")

    def next_peak_window(
        self, snap: Snapshot, *, after: datetime | None = None
    ) -> RateWindow | None:
        raise NotImplementedError("P1.8 — Compute.next_peak_window")

    def time_until(self, target: datetime, snap: Snapshot) -> timedelta:
        raise NotImplementedError("P1.8 — Compute.time_until")

    def top_export_windows(
        self,
        snap: Snapshot,
        *,
        n: int = 3,
        until: datetime | None = None,
    ) -> list[RateWindow]:
        raise NotImplementedError("P1.8 — Compute.top_export_windows")

    def cheap_window_duration(self, window: RateWindow) -> timedelta:
        raise NotImplementedError("P1.8 — Compute.cheap_window_duration")

    # ── 12c. Forecast aggregation ─────────────────────────────────

    def total_load(self, snap: Snapshot, window: TimeRange) -> float:
        raise NotImplementedError("P1.8 — Compute.total_load")

    def total_solar(self, snap: Snapshot, window: TimeRange) -> float:
        raise NotImplementedError("P1.8 — Compute.total_solar")

    def net_load(self, snap: Snapshot, window: TimeRange) -> float:
        raise NotImplementedError("P1.8 — Compute.net_load")

    def cumulative_load_to(self, snap: Snapshot, target: datetime) -> float:
        raise NotImplementedError("P1.8 — Compute.cumulative_load_to")

    def cumulative_solar_to(self, snap: Snapshot, target: datetime) -> float:
        raise NotImplementedError("P1.8 — Compute.cumulative_solar_to")

    def bridge_kwh(
        self, snap: Snapshot, *, until: datetime | None = None
    ) -> float:
        """Net energy the battery must supply to bridge from now to
        `until` (default: next cheap window). Floor at zero.
        THE 2026-05-08 KEY METRIC.
        """
        raise NotImplementedError("P1.8 — Compute.bridge_kwh")

    def pv_takeover_hour(self, snap: Snapshot) -> int | None:
        raise NotImplementedError("P1.8 — Compute.pv_takeover_hour")

    # ── 12d. Counterfactual analysis ──────────────────────────────

    def usage_at_rate_band(
        self, snap: Snapshot, window: TimeRange
    ) -> dict[str, float]:
        raise NotImplementedError("P1.8 — Compute.usage_at_rate_band")

    def cost_breakdown(
        self, snap: Snapshot, window: TimeRange
    ) -> dict[str, float]:
        raise NotImplementedError("P1.8 — Compute.cost_breakdown")

    def import_volume_under_plan(
        self, plan: PlannedAction, snap: Snapshot
    ) -> float:
        """Counterfactual: if THIS plan ran from now to end of horizon
        given current forecasts, how much grid import would the plan
        need? Lets the planner compare candidate plans without writing
        them. THE OBJECTIVE-FUNCTION BUILDING BLOCK.
        """
        raise NotImplementedError("P1.8 — Compute.import_volume_under_plan")

    def export_revenue_under_plan(
        self, plan: PlannedAction, snap: Snapshot
    ) -> float:
        raise NotImplementedError("P1.8 — Compute.export_revenue_under_plan")

    # ── 12e. Physics predictions ──────────────────────────────────

    def time_to_charge(
        self,
        *,
        target_soc_pct: float,
        charge_rate_kw: float,
        snap: Snapshot,
    ) -> timedelta:
        raise NotImplementedError("P1.8 — Compute.time_to_charge")

    def time_to_discharge(
        self,
        *,
        target_soc_pct: float,
        discharge_rate_kw: float,
        snap: Snapshot,
    ) -> timedelta:
        raise NotImplementedError("P1.8 — Compute.time_to_discharge")

    def kwh_deliverable_in(
        self,
        *,
        duration: timedelta,
        throttle_a: float,
        snap: Snapshot,
    ) -> float:
        raise NotImplementedError("P1.8 — Compute.kwh_deliverable_in")

    def discharge_throttle_for(
        self,
        *,
        kwh: float,
        duration: timedelta,
        snap: Snapshot,
    ) -> float:
        """Inverse of kwh_deliverable_in. Replaces the ad-hoc
        `kw_for_slot / battery_voltage * 1000` formula scattered across
        HEO II's PeakArbitrageRule.
        """
        raise NotImplementedError("P1.8 — Compute.discharge_throttle_for")

    def charge_throttle_for(
        self,
        *,
        kwh: float,
        duration: timedelta,
        snap: Snapshot,
    ) -> float:
        raise NotImplementedError("P1.8 — Compute.charge_throttle_for")
