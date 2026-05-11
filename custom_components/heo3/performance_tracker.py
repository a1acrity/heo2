"""PerformanceTracker — per-tick records + rolling retention + forecast errors.

Records every tick's snapshot summary + decision + apply result + outcome.
Persists to HA's Store (JSON file at /config/.storage/heo3_performance_*).
30-day rolling retention.

Three jobs:
1. Per-tick recording (called from coordinator's on_tick callback).
2. Forecast error tracking — actual_load vs forecast_load + same for solar.
3. Per-rule attribution — replays Decision claims through Arbiter minus
   rule X to estimate "what would have happened without X". Used by the
   Tuner (later phase) and the weekly digest.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .coordinator import TickRecord

logger = logging.getLogger(__name__)


# Rolling retention window. Older records are pruned on each save.
RETENTION_DAYS = 30

# How many recent ticks to keep in memory (the Store has the rest).
IN_MEMORY_TICKS = 500  # ~5 days at 15-min cadence

# How often to persist (don't write on every tick — batch).
PERSIST_EVERY_N_TICKS = 4  # ~hourly at 15-min cadence


@dataclass
class TickSummary:
    """Compact per-tick record. Stored as JSON; one row per tick.

    Avoids serialising the full Snapshot (would balloon storage).
    Picks the fields that matter for outcome analysis + digest.
    """

    captured_at: str  # ISO UTC
    reason: str       # cron / eps / saving / igo

    # Snapshot summary
    battery_soc_pct: float | None = None
    grid_voltage_v: float | None = None
    grid_power_w: float | None = None
    solar_power_w: float | None = None
    load_power_w: float | None = None
    eps_active: bool = False
    igo_dispatching: bool | None = None
    saving_session_active: bool | None = None

    # Rate context
    import_current_pence: float | None = None
    export_current_pence: float | None = None

    # Forecast vs actual (filled at NEXT tick — see record_actuals_for_previous)
    solar_forecast_now_kwh_per_h: float | None = None
    load_forecast_now_kwh_per_h: float | None = None

    # Decision summary
    active_rules: list[str] = field(default_factory=list)
    rationale: str = ""
    plan_id: str = ""

    # Apply result
    writes_requested: int = 0
    writes_succeeded: int = 0
    writes_failed: int = 0
    apply_duration_ms: float = 0.0
    apply_skipped_reason: str | None = None


@dataclass
class ForecastError:
    """Rolling forecast-error metric for a category (load or solar)."""

    samples: int = 0
    sum_pct_error: float = 0.0
    sum_sq_pct_error: float = 0.0

    @property
    def mean_pct_error(self) -> float:
        return self.sum_pct_error / self.samples if self.samples else 0.0

    @property
    def rms_pct_error(self) -> float:
        if self.samples == 0:
            return 0.0
        return (self.sum_sq_pct_error / self.samples) ** 0.5

    def add(self, actual: float, forecast: float) -> None:
        if forecast <= 0:
            return
        pct = ((actual - forecast) / forecast) * 100.0
        self.samples += 1
        self.sum_pct_error += pct
        self.sum_sq_pct_error += pct * pct

    def reset(self) -> None:
        self.samples = 0
        self.sum_pct_error = 0.0
        self.sum_sq_pct_error = 0.0


# ── Storage Protocol ──────────────────────────────────────────────


class TickStore:
    """Abstraction over HA's Store. Tests pass a memory-backed double."""

    def __init__(self, hass, entry_id: str):  # type: ignore[no-untyped-def]
        from homeassistant.helpers.storage import Store

        self._store = Store(hass, version=1, key=f"heo3_performance_{entry_id}")

    async def load(self) -> list[dict]:
        data = await self._store.async_load() or {}
        return list(data.get("ticks", []))

    async def save(self, ticks: list[dict]) -> None:
        await self._store.async_save({"ticks": ticks})


class MemoryTickStore:
    """Test double — in-memory, no HA imports."""

    def __init__(self) -> None:
        self._ticks: list[dict] = []

    async def load(self) -> list[dict]:
        return list(self._ticks)

    async def save(self, ticks: list[dict]) -> None:
        self._ticks = list(ticks)


# ── PerformanceTracker ────────────────────────────────────────────


class PerformanceTracker:
    """Records per-tick state, computes rolling forecast errors,
    persists to HA Store with rolling retention."""

    def __init__(
        self,
        store: TickStore | MemoryTickStore,
        *,
        retention_days: int = RETENTION_DAYS,
        in_memory_limit: int = IN_MEMORY_TICKS,
        persist_every_n: int = PERSIST_EVERY_N_TICKS,
    ) -> None:
        self._store = store
        self._retention_days = retention_days
        self._in_memory_limit = in_memory_limit
        self._persist_every_n = persist_every_n

        self._ticks: deque[dict] = deque(maxlen=in_memory_limit)
        self._tick_count_since_persist = 0
        self._loaded = False

        # Rolling forecast error (recomputed on demand from ticks).
        self._load_error = ForecastError()
        self._solar_error = ForecastError()

    async def async_init(self) -> None:
        """Load any persisted history. Call once at integration setup."""
        if self._loaded:
            return
        loaded = await self._store.load()
        # Keep only what fits in memory (the rest was already pruned).
        for t in loaded[-self._in_memory_limit:]:
            self._ticks.append(t)
        self._loaded = True
        self._recompute_forecast_errors()

    async def record(self, tick: TickRecord) -> None:
        """Record a tick. Persists every N ticks to amortise the write cost."""
        summary = _build_summary(tick)
        self._ticks.append(asdict(summary))

        # Update forecast errors from any "actual vs forecast" we can
        # derive from the previous tick's snapshot vs this tick's
        # observed solar/load.
        self._update_forecast_errors_for_previous(tick)

        self._tick_count_since_persist += 1
        if self._tick_count_since_persist >= self._persist_every_n:
            await self._persist()
            self._tick_count_since_persist = 0

    async def _persist(self) -> None:
        # Prune anything older than retention.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self._retention_days)).isoformat()
        retained = [t for t in self._ticks if t.get("captured_at", "") >= cutoff]
        await self._store.save(retained)

    def _update_forecast_errors_for_previous(self, tick: TickRecord) -> None:
        """If the previous tick had a solar/load forecast, compare to
        this tick's actual.

        Approximation: 'actual' for the previous interval = average of
        previous and current power readings. Good enough for tuner
        signal; not perfect.
        """
        if len(self._ticks) < 2:
            return
        prev = self._ticks[-2]
        curr = self._ticks[-1]

        # Solar: actual_kwh_per_h ≈ avg power over the interval / 1000
        prev_solar_w = prev.get("solar_power_w")
        curr_solar_w = curr.get("solar_power_w")
        prev_solar_forecast = prev.get("solar_forecast_now_kwh_per_h")
        if (
            prev_solar_w is not None
            and curr_solar_w is not None
            and prev_solar_forecast is not None
            and prev_solar_forecast > 0
        ):
            actual_kwh_per_h = (prev_solar_w + curr_solar_w) / 2.0 / 1000.0
            self._solar_error.add(actual_kwh_per_h, prev_solar_forecast)

        # Load: same shape
        prev_load_w = prev.get("load_power_w")
        curr_load_w = curr.get("load_power_w")
        prev_load_forecast = prev.get("load_forecast_now_kwh_per_h")
        if (
            prev_load_w is not None
            and curr_load_w is not None
            and prev_load_forecast is not None
            and prev_load_forecast > 0
        ):
            actual_kwh_per_h = (prev_load_w + curr_load_w) / 2.0 / 1000.0
            self._load_error.add(actual_kwh_per_h, prev_load_forecast)

    def _recompute_forecast_errors(self) -> None:
        """Rebuild forecast error stats from the in-memory tick window.

        Called on init after load(); also useful when retention prunes.
        """
        self._load_error.reset()
        self._solar_error.reset()
        ticks = list(self._ticks)
        for prev, curr in zip(ticks, ticks[1:]):
            self._add_error_pair(prev, curr, "solar", self._solar_error)
            self._add_error_pair(prev, curr, "load", self._load_error)

    @staticmethod
    def _add_error_pair(prev: dict, curr: dict, kind: str, into: ForecastError) -> None:
        prev_w = prev.get(f"{kind}_power_w")
        curr_w = curr.get(f"{kind}_power_w")
        forecast = prev.get(f"{kind}_forecast_now_kwh_per_h")
        if prev_w is None or curr_w is None or forecast is None or forecast <= 0:
            return
        actual = (prev_w + curr_w) / 2.0 / 1000.0
        into.add(actual, forecast)

    # ── Public read API ───────────────────────────────────────────

    @property
    def load_forecast_error(self) -> ForecastError:
        return self._load_error

    @property
    def solar_forecast_error(self) -> ForecastError:
        return self._solar_error

    @property
    def tick_count(self) -> int:
        return len(self._ticks)

    def recent_ticks(self, n: int = 20) -> list[dict]:
        """Last N tick summaries. For digest building."""
        return list(self._ticks)[-n:]

    def ticks_in_window(
        self, *, start: datetime, end: datetime
    ) -> list[dict]:
        """All retained ticks with captured_at in [start, end)."""
        s, e = start.isoformat(), end.isoformat()
        return [t for t in self._ticks if s <= t.get("captured_at", "") < e]

    async def flush(self) -> None:
        """Force persistence (e.g. on shutdown)."""
        if self._tick_count_since_persist > 0:
            await self._persist()
            self._tick_count_since_persist = 0


# ── Helpers ───────────────────────────────────────────────────────


def _build_summary(tick: TickRecord) -> TickSummary:
    """Project a TickRecord onto its persistable summary."""
    snap = tick.snapshot
    decision = tick.decision
    result = tick.apply_result

    # Some snapshot fields are nullable; handle gracefully.
    solar_now = None
    load_now = None
    if snap is not None:
        # Hour-of-day forecast at the snapshot time.
        try:
            local = snap.captured_at.astimezone(snap.local_tz)
            hour = local.hour
            solar_today = snap.solar_forecast.today_p50_kwh
            load_today = snap.load_forecast.today_hourly_kwh
            if solar_today and 0 <= hour < len(solar_today):
                solar_now = solar_today[hour]
            if load_today and 0 <= hour < len(load_today):
                load_now = load_today[hour]
        except Exception:
            pass

    return TickSummary(
        captured_at=tick.captured_at.isoformat(),
        reason=tick.reason,
        battery_soc_pct=(snap.inverter.battery_soc_pct if snap else None),
        grid_voltage_v=(snap.inverter.grid_voltage_v if snap else None),
        grid_power_w=(snap.inverter.grid_power_w if snap else None),
        solar_power_w=(snap.inverter.solar_power_w if snap else None),
        load_power_w=(snap.inverter.load_power_w if snap else None),
        eps_active=(snap.flags.eps_active if snap else False),
        igo_dispatching=(snap.flags.igo_dispatching if snap else None),
        saving_session_active=(
            snap.flags.saving_session_active if snap else None
        ),
        import_current_pence=(
            snap.rates_live.import_current_pence if snap else None
        ),
        export_current_pence=(
            snap.rates_live.export_current_pence if snap else None
        ),
        solar_forecast_now_kwh_per_h=solar_now,
        load_forecast_now_kwh_per_h=load_now,
        active_rules=list(decision.active_rules),
        rationale=decision.rationale,
        plan_id=(result.plan_id if result is not None else ""),
        writes_requested=(len(result.requested) if result is not None else 0),
        writes_succeeded=(len(result.succeeded) if result is not None else 0),
        writes_failed=(len(result.failed) if result is not None else 0),
        apply_duration_ms=(result.duration_ms if result is not None else 0.0),
        apply_skipped_reason=tick.skipped_reason,
    )
