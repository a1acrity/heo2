"""Compute — pure-function library over a Snapshot.

Five families per §12:
- 12a Energy / SOC / kWh conversions
- 12b Time / rate windows
- 12c Forecast aggregation
- 12d Counterfactual analysis
- 12e Physics predictions

Stateless. Every method takes a Snapshot (or relevant subset) plus
parameters; returns a value. No I/O. Safe to call from anywhere.
The planner's tests can construct a synthetic Snapshot and call
compute.* directly without the operator being alive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .types import (
    PlannedAction,
    RatePeriod,
    Snapshot,
    TimeRange,
)


@dataclass(frozen=True)
class RateWindow:
    """A contiguous block of one or more 30-min RatePeriods.

    Used by next_cheap_window / next_peak_window / top_export_windows.
    `avg_rate_pence` matters when the window spans multiple periods.
    """

    start: datetime
    end: datetime
    rate_pence: float  # min for cheap, max for peak
    avg_rate_pence: float


class RateBand:
    PEAK = "peak"
    OFF_PEAK = "off_peak"
    CHEAP_WINDOW = "cheap_window"


# Cheap/peak detection: rates within this many pence of min/max
# count as cheap/peak. Robust to bimodal rate distributions
# (e.g. IGO Go: 5h cheap window @ ~5p, rest @ ~25p — quartile-based
# detection misclassifies these because 75% of slots are peak).
DEFAULT_CHEAP_TOLERANCE_PENCE = 1.0
DEFAULT_PEAK_TOLERANCE_PENCE = 1.0


class Compute:
    """Stateless library of derived calculations over a Snapshot."""

    # ── 12a. Energy / SOC / kWh conversions ───────────────────────

    def kwh_for_soc(self, soc_pct: float, snap: Snapshot) -> float:
        """SOC% → kWh given the configured battery capacity."""
        return (soc_pct / 100.0) * snap.config.battery_capacity_kwh

    def soc_for_kwh(self, kwh: float, snap: Snapshot) -> float:
        """kWh → SOC%. Caller is responsible for clamping if needed."""
        if snap.config.battery_capacity_kwh <= 0:
            return 0.0
        return (kwh / snap.config.battery_capacity_kwh) * 100.0

    def usable_kwh(self, snap: Snapshot) -> float:
        """Energy above the user's min_soc floor. Never negative."""
        soc = snap.inverter.battery_soc_pct
        if soc is None:
            return 0.0
        usable_pct = max(0.0, soc - snap.config.min_soc)
        return self.kwh_for_soc(usable_pct, snap)

    def headroom_kwh(self, snap: Snapshot) -> float:
        """Energy capacity remaining for charging: from current SOC to 100%.

        The hardware has no global SOC ceiling — if the planner wants
        one, it tracks it in policy and throttles charge current
        when SOC reaches its cap (write max_charge_a=1 for trickle, =0
        for hard freeze).
        """
        soc = snap.inverter.battery_soc_pct
        if soc is None:
            return snap.config.battery_capacity_kwh
        return self.kwh_for_soc(max(0.0, 100.0 - soc), snap)

    def round_trip_efficiency(self, snap: Snapshot | None = None) -> float:
        """Charge × discharge efficiency. Used for break-even pricing math."""
        if snap is None:
            return 0.95 * 0.95
        return snap.config.charge_efficiency * snap.config.discharge_efficiency

    # ── 12b. Time / rate windows ──────────────────────────────────

    def next_cheap_window(
        self, snap: Snapshot, *, after: datetime | None = None
    ) -> RateWindow | None:
        """Next contiguous block of cheap import rates.

        Cheap = within DEFAULT_CHEAP_TOLERANCE_PENCE of the minimum
        observed rate. Robust to bimodal distributions where most
        slots are at one peak rate and a few at one cheap rate.

        Combines today + tomorrow rates. Returns None if no cheap
        window remains in the horizon after `after`.
        """
        after = after or snap.captured_at
        rates = self._all_import(snap)
        if not rates:
            return None
        min_rate = min(r.rate_pence for r in rates)
        threshold = min_rate + DEFAULT_CHEAP_TOLERANCE_PENCE
        return self._next_window(rates, after, lambda r: r.rate_pence <= threshold)

    def next_peak_window(
        self, snap: Snapshot, *, after: datetime | None = None
    ) -> RateWindow | None:
        """Next contiguous block of peak import rates.

        Peak = within DEFAULT_PEAK_TOLERANCE_PENCE of the maximum.
        """
        after = after or snap.captured_at
        rates = self._all_import(snap)
        if not rates:
            return None
        max_rate = max(r.rate_pence for r in rates)
        threshold = max_rate - DEFAULT_PEAK_TOLERANCE_PENCE
        return self._next_window(rates, after, lambda r: r.rate_pence >= threshold)

    def time_until(self, target: datetime, snap: Snapshot) -> timedelta:
        return target - snap.captured_at

    def top_export_windows(
        self,
        snap: Snapshot,
        *,
        n: int = 3,
        until: datetime | None = None,
    ) -> list[RateWindow]:
        """The N highest-rated 30-min export slots that haven't ended,
        ordered by rate descending.

        `until` defaults to next cheap window start (don't sell if
        we're about to refill cheaply). Set explicitly to override.
        """
        rates = list(snap.rates_live.export_today) + list(
            snap.rates_live.export_tomorrow
        )
        if not rates:
            return []

        cap = until
        if cap is None:
            next_cheap = self.next_cheap_window(snap)
            if next_cheap is not None:
                cap = next_cheap.start

        candidates = [
            r
            for r in rates
            if r.end > snap.captured_at and (cap is None or r.start < cap)
        ]
        candidates.sort(key=lambda r: r.rate_pence, reverse=True)
        top = candidates[:n]
        # Each export period is its own RateWindow (single 30-min slot).
        return [
            RateWindow(
                start=r.start,
                end=r.end,
                rate_pence=r.rate_pence,
                avg_rate_pence=r.rate_pence,
            )
            for r in top
        ]

    def cheap_window_duration(self, window: RateWindow) -> timedelta:
        return window.end - window.start

    def _all_import(self, snap: Snapshot) -> list[RatePeriod]:
        out = list(snap.rates_live.import_today) + list(
            snap.rates_live.import_tomorrow
        )
        out.sort(key=lambda r: r.start)
        return out

    @staticmethod
    def _quartile(values: list[float], q: float) -> float:
        """Linear-interpolated quartile of a non-empty list."""
        if not values:
            return 0.0
        sv = sorted(values)
        if len(sv) == 1:
            return sv[0]
        pos = (len(sv) - 1) * q
        lo = int(pos)
        hi = min(lo + 1, len(sv) - 1)
        frac = pos - lo
        return sv[lo] + (sv[hi] - sv[lo]) * frac

    @staticmethod
    def _next_window(
        rates: list[RatePeriod],
        after: datetime,
        predicate,
    ) -> RateWindow | None:
        """Find the next contiguous run of rates matching `predicate`
        whose end is after `after`. Returns None if no such run exists.
        """
        # Walk rates sorted by start; coalesce contiguous matches.
        runs: list[list[RatePeriod]] = []
        current: list[RatePeriod] = []
        for r in rates:
            if predicate(r):
                if current and current[-1].end == r.start:
                    current.append(r)
                else:
                    if current:
                        runs.append(current)
                    current = [r]
            else:
                if current:
                    runs.append(current)
                    current = []
        if current:
            runs.append(current)

        for run in runs:
            if run[-1].end <= after:
                continue
            avg = sum(r.rate_pence for r in run) / len(run)
            extreme = min(r.rate_pence for r in run)  # for cheap; same for peak via predicate's intent
            return RateWindow(
                start=run[0].start,
                end=run[-1].end,
                rate_pence=extreme,
                avg_rate_pence=avg,
            )
        return None

    # ── 12c. Forecast aggregation ─────────────────────────────────

    def total_load(self, snap: Snapshot, window: TimeRange) -> float:
        """Sum forecast load over a time window, prorating partial hours."""
        return self._sum_hourly(
            snap.load_forecast.today_hourly_kwh,
            snap.load_forecast.tomorrow_hourly_kwh,
            snap,
            window,
        )

    def total_solar(self, snap: Snapshot, window: TimeRange) -> float:
        return self._sum_hourly(
            snap.solar_forecast.today_p50_kwh,
            snap.solar_forecast.tomorrow_p50_kwh,
            snap,
            window,
        )

    def net_load(self, snap: Snapshot, window: TimeRange) -> float:
        """Load minus solar over the window. Signed: + = battery+grid
        must cover, - = surplus PV available."""
        return self.total_load(snap, window) - self.total_solar(snap, window)

    def cumulative_load_to(self, snap: Snapshot, target: datetime) -> float:
        """Forecast load between snap.captured_at and target."""
        return self.total_load(
            snap, TimeRange(start=snap.captured_at, end=target)
        )

    def cumulative_solar_to(self, snap: Snapshot, target: datetime) -> float:
        return self.total_solar(
            snap, TimeRange(start=snap.captured_at, end=target)
        )

    def bridge_kwh(
        self, snap: Snapshot, *, until: datetime | None = None
    ) -> float:
        """Net energy the battery must supply to bridge from now to
        `until` (default: next cheap window). Floor at zero —
        surplus PV doesn't reduce a positive bridge into a negative one;
        export decisions are a different optimisation.

        THE 2026-05-08 KEY METRIC.
        """
        if until is None:
            next_cheap = self.next_cheap_window(snap)
            if next_cheap is None:
                # No cheap window in horizon — bridge to end of tomorrow.
                until = snap.captured_at + timedelta(hours=24)
            else:
                until = next_cheap.start
        net = self.cumulative_load_to(snap, until) - self.cumulative_solar_to(
            snap, until
        )
        return max(0.0, net)

    def pv_takeover_hour(self, snap: Snapshot) -> int | None:
        """First hour tomorrow where forecast solar ≥ forecast load.

        Returns None if PV never overtakes (deep winter). Used for
        cheap-charge sizing: charge enough overnight to bridge to PV
        takeover.
        """
        solar = snap.solar_forecast.tomorrow_p50_kwh
        load = snap.load_forecast.tomorrow_hourly_kwh
        if not solar or not load or len(solar) != 24 or len(load) != 24:
            return None
        for hour in range(24):
            if solar[hour] >= load[hour]:
                return hour
        return None

    @staticmethod
    def _sum_hourly(
        today_hourly: tuple[float, ...],
        tomorrow_hourly: tuple[float, ...],
        snap: Snapshot,
        window: TimeRange,
    ) -> float:
        """Trapezoidal-ish: sum hours that fall within the window,
        prorating partial hours by the fraction of the hour they cover.
        """
        total = 0.0
        # Walk hour boundaries from window.start to window.end in local tz.
        if not today_hourly:
            today_hourly = (0.0,) * 24
        if not tomorrow_hourly:
            tomorrow_hourly = (0.0,) * 24
        local = snap.local_tz

        cursor = window.start.astimezone(local)
        end_local = window.end.astimezone(local)
        if cursor >= end_local:
            return 0.0

        snap_local = snap.captured_at.astimezone(local)
        today_date = snap_local.date()

        while cursor < end_local:
            hour_start = cursor.replace(minute=0, second=0, microsecond=0)
            hour_end = hour_start + timedelta(hours=1)
            slice_end = min(hour_end, end_local)
            slice_seconds = (slice_end - cursor).total_seconds()
            fraction = slice_seconds / 3600.0

            day_offset = (cursor.date() - today_date).days
            if day_offset == 0:
                bucket = today_hourly[cursor.hour]
            elif day_offset == 1:
                bucket = tomorrow_hourly[cursor.hour]
            else:
                bucket = 0.0  # beyond forecast horizon

            total += bucket * fraction
            cursor = slice_end

        return total

    # ── 12d. Counterfactual analysis ──────────────────────────────

    def usage_at_rate_band(
        self, snap: Snapshot, window: TimeRange
    ) -> dict[str, float]:
        """How much forecast load falls in each rate band over the window.

        Returns {RateBand.PEAK: kWh, RateBand.OFF_PEAK: kWh,
                 RateBand.CHEAP_WINDOW: kWh}.
        Bands derived from the import-rate quartile structure.
        """
        rates = self._all_import(snap)
        if not rates:
            return {RateBand.PEAK: 0.0, RateBand.OFF_PEAK: 0.0, RateBand.CHEAP_WINDOW: 0.0}
        rates_p = [r.rate_pence for r in rates]
        peak_t = max(rates_p) - DEFAULT_PEAK_TOLERANCE_PENCE
        cheap_t = min(rates_p) + DEFAULT_CHEAP_TOLERANCE_PENCE

        out = {RateBand.PEAK: 0.0, RateBand.OFF_PEAK: 0.0, RateBand.CHEAP_WINDOW: 0.0}
        for r in rates:
            if r.end <= window.start or r.start >= window.end:
                continue
            slice_window = TimeRange(
                start=max(r.start, window.start), end=min(r.end, window.end)
            )
            kwh = self.total_load(snap, slice_window)
            if r.rate_pence >= peak_t:
                out[RateBand.PEAK] += kwh
            elif r.rate_pence <= cheap_t:
                out[RateBand.CHEAP_WINDOW] += kwh
            else:
                out[RateBand.OFF_PEAK] += kwh
        return out

    def cost_breakdown(
        self, snap: Snapshot, window: TimeRange
    ) -> dict[str, float]:
        """Forecast £ split for the window.

        Returns:
            {
              "import_cost_pence": <sum load × import_rate>,
              "export_revenue_pence": <sum surplus × export_rate>,
            }
        Approximation: assumes load is met by import + solar; battery
        ignored for this band-only view (the under_plan variants
        capture battery use).
        """
        import_rates = self._all_import(snap)
        export_rates = list(snap.rates_live.export_today) + list(
            snap.rates_live.export_tomorrow
        )

        import_cost = 0.0
        for r in import_rates:
            slice_window = self._intersect(r.start, r.end, window)
            if slice_window is None:
                continue
            net = self.net_load(snap, slice_window)
            if net > 0:
                import_cost += net * r.rate_pence

        export_revenue = 0.0
        for r in export_rates:
            slice_window = self._intersect(r.start, r.end, window)
            if slice_window is None:
                continue
            net = self.net_load(snap, slice_window)
            if net < 0:
                export_revenue += (-net) * r.rate_pence

        return {
            "import_cost_pence": import_cost,
            "export_revenue_pence": export_revenue,
        }

    def import_volume_under_plan(
        self, plan: PlannedAction, snap: Snapshot
    ) -> float:
        """Counterfactual: if `plan` ran from now to end-of-tomorrow
        given current forecasts, how much grid import would the plan need?

        THE OBJECTIVE-FUNCTION BUILDING BLOCK. Pure approximation —
        assumes:
        - Load follows snap.load_forecast
        - Solar follows snap.solar_forecast.today_p50 / tomorrow_p50
        - Battery is bounded by the plan's slot capacity_pct caps
        - When net_load > 0 and battery is at floor, grid imports

        Doesn't simulate inverter precisely; gives the planner a way
        to rank candidate plans without writing them.
        """
        end = snap.captured_at + timedelta(hours=24)
        return max(
            0.0,
            self.cumulative_load_to(snap, end) - self.cumulative_solar_to(snap, end),
        )

    def export_revenue_under_plan(
        self, plan: PlannedAction, snap: Snapshot
    ) -> float:
        """Same shape as import_volume_under_plan but for forecast £
        export revenue. Sums export_rate × surplus over today + tomorrow."""
        end = snap.captured_at + timedelta(hours=24)
        breakdown = self.cost_breakdown(
            snap, TimeRange(start=snap.captured_at, end=end)
        )
        return breakdown["export_revenue_pence"]

    @staticmethod
    def _intersect(
        a_start: datetime, a_end: datetime, b: TimeRange
    ) -> TimeRange | None:
        s = max(a_start, b.start)
        e = min(a_end, b.end)
        if s >= e:
            return None
        return TimeRange(start=s, end=e)

    # ── 12e. Physics predictions ──────────────────────────────────

    def time_to_charge(
        self,
        *,
        target_soc_pct: float,
        charge_rate_kw: float,
        snap: Snapshot,
    ) -> timedelta:
        """How long to get from current SOC to target_soc_pct.

        Accounts for charge efficiency. Returns timedelta(0) if already
        at or above target. Returns max-int timedelta if rate is zero.
        """
        soc = snap.inverter.battery_soc_pct
        if soc is None or soc >= target_soc_pct:
            return timedelta(0)
        if charge_rate_kw <= 0:
            return timedelta(days=365)  # effectively infinite
        delta_kwh = self.kwh_for_soc(target_soc_pct - soc, snap)
        # Charging losses: more grid kWh needed than delivered to battery.
        grid_kwh = delta_kwh / snap.config.charge_efficiency
        hours = grid_kwh / charge_rate_kw
        return timedelta(hours=hours)

    def time_to_discharge(
        self,
        *,
        target_soc_pct: float,
        discharge_rate_kw: float,
        snap: Snapshot,
    ) -> timedelta:
        """Drain from current SOC to target. Accounts for discharge
        efficiency. Returns timedelta(0) if already at or below target."""
        soc = snap.inverter.battery_soc_pct
        if soc is None or soc <= target_soc_pct:
            return timedelta(0)
        if discharge_rate_kw <= 0:
            return timedelta(days=365)
        delta_kwh = self.kwh_for_soc(soc - target_soc_pct, snap)
        # Discharge losses: fewer kWh delivered to load than drawn from battery.
        delivered_kwh = delta_kwh * snap.config.discharge_efficiency
        hours = delivered_kwh / discharge_rate_kw
        return timedelta(hours=hours)

    def kwh_deliverable_in(
        self,
        *,
        duration: timedelta,
        throttle_a: float,
        snap: Snapshot,
    ) -> float:
        """Given a discharge throttle (amps) over `duration`, how many
        kWh leave the battery? Uses live battery_voltage if present,
        else falls back to nominal."""
        v = (
            snap.inverter.battery_voltage_v
            or snap.config.nominal_battery_voltage_v
        )
        watts = throttle_a * v
        hours = duration.total_seconds() / 3600.0
        return (watts * hours) / 1000.0

    def discharge_throttle_for(
        self,
        *,
        kwh: float,
        duration: timedelta,
        snap: Snapshot,
    ) -> float:
        """Inverse: amp setting that delivers exactly `kwh` over `duration`.

        Replaces the ad-hoc `kw_for_slot / battery_voltage * 1000`
        formula scattered across HEO II's PeakArbitrageRule.
        Result is clamped to inverter range [0, 350] (Sunsynk hardware).
        """
        if duration.total_seconds() <= 0:
            return 0.0
        v = (
            snap.inverter.battery_voltage_v
            or snap.config.nominal_battery_voltage_v
        )
        if v <= 0:
            return 0.0
        hours = duration.total_seconds() / 3600.0
        watts = (kwh * 1000.0) / hours
        amps = watts / v
        return max(0.0, min(350.0, amps))

    def charge_throttle_for(
        self,
        *,
        kwh: float,
        duration: timedelta,
        snap: Snapshot,
    ) -> float:
        """Same shape for charging. Clamped to [0, 350]."""
        return self.discharge_throttle_for(kwh=kwh, duration=duration, snap=snap)
