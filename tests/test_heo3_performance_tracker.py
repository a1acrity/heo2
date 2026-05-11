"""PerformanceTracker tests — recording, retention, forecast errors."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from heo3.coordinator import Decision, TickRecord
from heo3.performance_tracker import (
    ForecastError,
    MemoryTickStore,
    PerformanceTracker,
)
from heo3.types import (
    ApplianceState,
    ApplyResult,
    EVState,
    InverterSettings,
    InverterState,
    LiveRates,
    LoadForecast,
    PlannedAction,
    PredictedRates,
    SlotSettings,
    Snapshot,
    SolarForecast,
    SystemConfig,
    SystemFlags,
    TeslaState,
    VerificationResult,
)


def _settings():
    slots = tuple(
        SlotSettings(start_hhmm=f"{h:02d}:00", grid_charge=False, capacity_pct=50)
        for h in (0, 5, 11, 16, 19, 22)
    )
    return InverterSettings(
        work_mode="Zero export to CT",
        energy_pattern="Load first",
        max_charge_a=100.0,
        max_discharge_a=100.0,
        slots=slots,
    )


def _snap(t: datetime, *, soc=80.0, solar_w=2000, load_w=600,
          solar_today=None, load_today=None):
    if solar_today is None:
        solar_today = (0,) * 7 + (1, 2, 3, 4, 5, 5, 5, 4, 3, 2, 1, 0) + (0,) * 5
    if load_today is None:
        load_today = (0.5,) * 24
    return Snapshot(
        captured_at=t,
        local_tz=ZoneInfo("Europe/London"),
        inverter=InverterState(
            battery_soc_pct=soc,
            solar_power_w=float(solar_w),
            load_power_w=float(load_w),
            grid_power_w=0.0,
        ),
        inverter_settings=_settings(),
        ev=EVState(),
        tesla=TeslaState(),
        appliances=ApplianceState(),
        rates_live=LiveRates(import_current_pence=15.0, export_current_pence=8.0),
        rates_predicted=PredictedRates(),
        rates_freshness={"import_today": t},
        solar_forecast=SolarForecast(today_p50_kwh=tuple(solar_today)),
        load_forecast=LoadForecast(today_hourly_kwh=tuple(load_today)),
        flags=SystemFlags(),
        config=SystemConfig(),
    )


def _result(t: datetime, *, requested=0, succeeded=0, failed=0):
    from heo3.types import VerificationResult, Write, FailedWrite
    return ApplyResult(
        plan_id="p1",
        requested=tuple(Write(topic="t", payload="p") for _ in range(requested)),
        succeeded=tuple(Write(topic="t", payload="p") for _ in range(succeeded)),
        failed=tuple(FailedWrite(write=Write(topic="t", payload="p"), reason="x") for _ in range(failed)),
        skipped=(),
        verification=VerificationResult(),
        duration_ms=200.0,
        captured_at=t,
    )


def _record(t: datetime, **kwargs):
    snap = _snap(t, **{k: v for k, v in kwargs.items() if k in ("soc", "solar_w", "load_w", "solar_today", "load_today")})
    return TickRecord(
        captured_at=t,
        reason="cron",
        snapshot=snap,
        decision=Decision(action=PlannedAction(rationale="x"),
                          active_rules=("static",),
                          rationale="static"),
        apply_result=_result(t, requested=2, succeeded=2),
    )


# ── ForecastError ─────────────────────────────────────────────────


class TestForecastError:
    def test_empty_returns_zero(self):
        e = ForecastError()
        assert e.mean_pct_error == 0.0
        assert e.rms_pct_error == 0.0

    def test_perfect_forecast_zero_error(self):
        e = ForecastError()
        e.add(actual=10.0, forecast=10.0)
        e.add(actual=20.0, forecast=20.0)
        assert e.mean_pct_error == 0.0
        assert e.rms_pct_error == 0.0

    def test_systematic_undershoot(self):
        # Forecast says 10, actual is 12 each time → +20% error.
        e = ForecastError()
        for _ in range(5):
            e.add(actual=12.0, forecast=10.0)
        assert e.mean_pct_error == pytest.approx(20.0)
        assert e.rms_pct_error == pytest.approx(20.0)

    def test_zero_forecast_skipped(self):
        e = ForecastError()
        e.add(actual=5.0, forecast=0.0)
        assert e.samples == 0


# ── PerformanceTracker basic recording ────────────────────────────


class TestRecording:
    @pytest.mark.asyncio
    async def test_records_tick(self):
        tracker = PerformanceTracker(MemoryTickStore(), persist_every_n=1)
        await tracker.async_init()
        t = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
        await tracker.record(_record(t))
        assert tracker.tick_count == 1
        recents = tracker.recent_ticks()
        assert recents[-1]["reason"] == "cron"

    @pytest.mark.asyncio
    async def test_summary_captures_snapshot_fields(self):
        tracker = PerformanceTracker(MemoryTickStore(), persist_every_n=1)
        await tracker.async_init()
        t = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
        await tracker.record(_record(t, soc=82.0, solar_w=1500))
        s = tracker.recent_ticks()[-1]
        assert s["battery_soc_pct"] == 82.0
        assert s["solar_power_w"] == 1500.0
        assert s["import_current_pence"] == 15.0
        assert s["plan_id"] == "p1"
        assert s["writes_succeeded"] == 2

    @pytest.mark.asyncio
    async def test_apply_result_summary(self):
        tracker = PerformanceTracker(MemoryTickStore(), persist_every_n=1)
        await tracker.async_init()
        t = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
        rec = TickRecord(
            captured_at=t, reason="cron", snapshot=_snap(t),
            decision=Decision(action=PlannedAction()),
            apply_result=_result(t, requested=10, succeeded=8, failed=2),
        )
        await tracker.record(rec)
        s = tracker.recent_ticks()[-1]
        assert s["writes_requested"] == 10
        assert s["writes_succeeded"] == 8
        assert s["writes_failed"] == 2

    @pytest.mark.asyncio
    async def test_skipped_tick_recorded(self):
        tracker = PerformanceTracker(MemoryTickStore(), persist_every_n=1)
        await tracker.async_init()
        t = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
        rec = TickRecord(
            captured_at=t, reason="cron",
            snapshot=_snap(t),
            decision=Decision(action=PlannedAction()),
            apply_result=None,
            skipped_reason="snapshot failed",
        )
        await tracker.record(rec)
        s = tracker.recent_ticks()[-1]
        assert s["apply_skipped_reason"] == "snapshot failed"


# ── Persistence + retention ───────────────────────────────────────


class TestPersistence:
    @pytest.mark.asyncio
    async def test_persists_every_n_ticks(self):
        store = MemoryTickStore()
        tracker = PerformanceTracker(store, persist_every_n=3)
        await tracker.async_init()
        t = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
        # 2 ticks: nothing persisted
        await tracker.record(_record(t))
        await tracker.record(_record(t + timedelta(minutes=15)))
        loaded = await store.load()
        assert len(loaded) == 0
        # 3rd tick: persists all 3
        await tracker.record(_record(t + timedelta(minutes=30)))
        loaded = await store.load()
        assert len(loaded) == 3

    @pytest.mark.asyncio
    async def test_retention_prunes_old_ticks(self):
        store = MemoryTickStore()
        tracker = PerformanceTracker(
            store, persist_every_n=1, retention_days=7
        )
        await tracker.async_init()
        # An old tick, well past retention.
        old = datetime.now(timezone.utc) - timedelta(days=30)
        await tracker.record(_record(old))
        # A recent tick
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        await tracker.record(_record(recent))
        loaded = await store.load()
        assert len(loaded) == 1  # only recent retained

    @pytest.mark.asyncio
    async def test_load_after_init_restores_history(self):
        store = MemoryTickStore()
        tracker1 = PerformanceTracker(store, persist_every_n=1)
        await tracker1.async_init()
        for i in range(3):
            t = datetime(2026, 5, 12, 12, i * 15, tzinfo=timezone.utc)
            await tracker1.record(_record(t))

        # Fresh tracker reads the persisted store.
        tracker2 = PerformanceTracker(store)
        await tracker2.async_init()
        assert tracker2.tick_count == 3


# ── Forecast error tracking ───────────────────────────────────────


class TestForecastErrorTracking:
    @pytest.mark.asyncio
    async def test_solar_error_computed_from_consecutive_ticks(self):
        tracker = PerformanceTracker(MemoryTickStore(), persist_every_n=10)
        await tracker.async_init()
        # Tick 1 at 12:00 BST forecast solar=5 kWh/h (hour 12 in default arr).
        # Power at 12:00 = 4500W. At 12:15 = 5500W. Avg = 5000W = 5 kWh/h.
        # Forecast = 5 → 0% error.
        t = datetime(2026, 5, 12, 11, 0, tzinfo=timezone.utc)  # 12:00 BST
        await tracker.record(_record(t, solar_w=4500))
        await tracker.record(_record(t + timedelta(minutes=15), solar_w=5500))
        e = tracker.solar_forecast_error
        assert e.samples >= 1
        assert abs(e.mean_pct_error) < 1.0  # near zero error

    @pytest.mark.asyncio
    async def test_load_error_tracks_undershoot(self):
        # Forecast load = 0.5 kWh/h (default profile flat). Actual avg
        # 1.0 kWh/h (1000W). +100% error.
        tracker = PerformanceTracker(MemoryTickStore(), persist_every_n=10)
        await tracker.async_init()
        t = datetime(2026, 5, 12, 11, 0, tzinfo=timezone.utc)
        await tracker.record(_record(t, load_w=900))
        await tracker.record(_record(t + timedelta(minutes=15), load_w=1100))
        e = tracker.load_forecast_error
        assert e.samples >= 1
        assert e.mean_pct_error == pytest.approx(100.0, abs=5.0)


# ── Time-windowed lookup ──────────────────────────────────────────


class TestWindowQuery:
    @pytest.mark.asyncio
    async def test_ticks_in_window(self):
        tracker = PerformanceTracker(MemoryTickStore(), persist_every_n=10)
        await tracker.async_init()
        base = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
        for i in range(5):
            await tracker.record(_record(base + timedelta(minutes=i * 15)))
        ticks = tracker.ticks_in_window(
            start=base + timedelta(minutes=15),
            end=base + timedelta(minutes=50),
        )
        # 12:15, 12:30, 12:45 → 3 ticks
        assert len(ticks) == 3


# ── flush ─────────────────────────────────────────────────────────


class TestFlush:
    @pytest.mark.asyncio
    async def test_flush_persists_pending(self):
        store = MemoryTickStore()
        tracker = PerformanceTracker(store, persist_every_n=10)
        await tracker.async_init()
        t = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
        await tracker.record(_record(t))  # 1 tick — no auto-persist
        loaded = await store.load()
        assert len(loaded) == 0
        await tracker.flush()
        loaded = await store.load()
        assert len(loaded) == 1
