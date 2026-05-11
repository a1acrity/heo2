"""Coordinator tick loop tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from heo3.coordinator import (
    Coordinator,
    Decision,
    StaticBaselinePlanner,
    TickRecord,
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


def _baseline_settings() -> InverterSettings:
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


def _make_snapshot(t: datetime) -> Snapshot:
    from zoneinfo import ZoneInfo
    return Snapshot(
        captured_at=t,
        local_tz=ZoneInfo("Europe/London"),
        inverter=InverterState(),
        inverter_settings=_baseline_settings(),
        ev=EVState(),
        tesla=TeslaState(),
        appliances=ApplianceState(),
        rates_live=LiveRates(),
        rates_predicted=PredictedRates(),
        rates_freshness={"import_today": t},
        solar_forecast=SolarForecast(),
        load_forecast=LoadForecast(),
        flags=SystemFlags(),
        config=SystemConfig(),
    )


def _make_apply_result(captured: datetime) -> ApplyResult:
    return ApplyResult(
        plan_id="test",
        requested=(),
        succeeded=(),
        failed=(),
        skipped=(),
        verification=VerificationResult(),
        duration_ms=0.0,
        captured_at=captured,
    )


class _FakeOperator:
    """Minimal operator stand-in for tests."""

    def __init__(self):
        self.snapshot_calls = 0
        self.apply_calls = []
        self._captured = datetime(2026, 5, 12, 0, 0, tzinfo=timezone.utc)

    async def snapshot(self):
        self.snapshot_calls += 1
        return _make_snapshot(self._captured)

    async def apply(self, action, *, snapshot=None, **kwargs):
        self.apply_calls.append((action, snapshot))
        return _make_apply_result(self._captured)


class _RecordingPlanner:
    def __init__(self, decision: Decision | None = None):
        self.decisions = []
        self._decision = decision or Decision(action=PlannedAction(rationale="rec"))

    async def decide(self, snapshot):
        self.decisions.append(snapshot)
        return self._decision


# ── tick() ─────────────────────────────────────────────────────────


class TestTick:
    @pytest.mark.asyncio
    async def test_tick_calls_snapshot_planner_apply_in_order(self):
        op = _FakeOperator()
        planner = _RecordingPlanner()
        coord = Coordinator(operator=op, planner=planner)

        decision = await coord.tick(reason="cron")

        assert op.snapshot_calls == 1
        assert len(planner.decisions) == 1
        assert len(op.apply_calls) == 1
        assert decision is not None
        assert decision.action.rationale == "rec"

    @pytest.mark.asyncio
    async def test_tick_calls_on_tick_callback(self):
        op = _FakeOperator()
        planner = _RecordingPlanner()
        records = []

        async def on_tick(rec: TickRecord):
            records.append(rec)

        coord = Coordinator(operator=op, planner=planner, on_tick=on_tick)
        await coord.tick(reason="cron")

        assert len(records) == 1
        assert records[0].reason == "cron"
        assert records[0].snapshot is not None
        assert records[0].apply_result is not None

    @pytest.mark.asyncio
    async def test_planner_failure_falls_back_to_empty_action(self):
        op = _FakeOperator()
        planner = MagicMock()
        planner.decide = AsyncMock(side_effect=RuntimeError("kaboom"))
        coord = Coordinator(operator=op, planner=planner)

        decision = await coord.tick(reason="cron")
        assert decision is not None
        assert "planner failed" in decision.action.rationale

    @pytest.mark.asyncio
    async def test_snapshot_failure_records_skipped(self):
        op = _FakeOperator()
        op.snapshot = AsyncMock(side_effect=RuntimeError("sa-down"))
        planner = _RecordingPlanner()
        records = []

        async def on_tick(rec):
            records.append(rec)

        coord = Coordinator(operator=op, planner=planner, on_tick=on_tick)
        result = await coord.tick(reason="cron")
        assert result is None
        assert len(records) == 1
        assert records[0].skipped_reason is not None

    @pytest.mark.asyncio
    async def test_concurrent_ticks_serialise(self):
        op = _FakeOperator()
        planner = _RecordingPlanner()
        coord = Coordinator(operator=op, planner=planner)

        # Fire two ticks at once.
        await asyncio.gather(coord.tick(reason="a"), coord.tick(reason="b"))
        # Both ran (lock didn't drop one).
        assert len(planner.decisions) == 2

    @pytest.mark.asyncio
    async def test_last_tick_at_updates(self):
        op = _FakeOperator()
        planner = _RecordingPlanner()
        coord = Coordinator(operator=op, planner=planner)
        assert coord.last_tick_at is None
        await coord.tick(reason="cron")
        assert coord.last_tick_at is not None


# ── debounce ───────────────────────────────────────────────────────


class TestDebounce:
    @pytest.mark.asyncio
    async def test_burst_of_triggers_collapses_to_one_tick(self, monkeypatch):
        from heo3 import coordinator as coord_module
        monkeypatch.setattr(coord_module, "DEBOUNCE_S", 0.05)

        op = _FakeOperator()
        planner = _RecordingPlanner()
        coord = Coordinator(operator=op, planner=planner)

        # Fire 5 triggers in quick succession.
        for _ in range(5):
            await coord.schedule_debounced_tick("eps")

        # Wait for debounce window + tick.
        await asyncio.sleep(0.2)
        # Just one tick should have run.
        assert len(planner.decisions) == 1


# ── StaticBaselinePlanner ──────────────────────────────────────────


class TestStaticBaselinePlanner:
    @pytest.mark.asyncio
    async def test_returns_baseline_static_decision(self):
        op = _FakeOperator()
        # Add a fake build attribute that behaves like ActionBuilder.
        op.build = MagicMock()
        op.build.baseline_static.return_value = PlannedAction(
            rationale="baseline_static plan"
        )

        planner = StaticBaselinePlanner(op)
        snap = _make_snapshot(datetime(2026, 5, 12, 0, 0, tzinfo=timezone.utc))
        decision = await planner.decide(snap)

        assert decision.active_rules == ("baseline_static",)
        assert decision.action.rationale == "baseline_static plan"
        op.build.baseline_static.assert_called_once_with(snap)


# ── shutdown ───────────────────────────────────────────────────────


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_blocks_further_ticks(self):
        op = _FakeOperator()
        planner = _RecordingPlanner()
        coord = Coordinator(operator=op, planner=planner)
        await coord.shutdown()
        result = await coord.tick(reason="cron")
        assert result is None
        assert len(planner.decisions) == 0

    @pytest.mark.asyncio
    async def test_shutdown_cancels_pending_debounce(self, monkeypatch):
        from heo3 import coordinator as coord_module
        monkeypatch.setattr(coord_module, "DEBOUNCE_S", 1.0)

        op = _FakeOperator()
        planner = _RecordingPlanner()
        coord = Coordinator(operator=op, planner=planner)
        await coord.schedule_debounced_tick("eps")
        await coord.shutdown()
        # No tick should fire after shutdown even though debounce was scheduled.
        await asyncio.sleep(1.5)
        assert len(planner.decisions) == 0
