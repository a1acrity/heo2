"""operator.apply() integration tests — pre-flight + execution + result."""

from __future__ import annotations

import asyncio

import pytest

from heo3.adapters import inverter as inverter_module
from heo3.operator import Operator
from heo3.transport import MockTransport
from heo3.types import (
    InverterSettings,
    PlannedAction,
    SlotPlan,
    SlotSettings,
)


RESPONSE_TOPIC = "solar_assistant/set/response_message/state"


@pytest.fixture
def fast_retry(monkeypatch):
    monkeypatch.setattr(inverter_module, "RESPONSE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(inverter_module, "WRITE_RETRY_BACKOFF_S", 0.0)


def _baseline_settings() -> InverterSettings:
    slots = tuple(
        SlotSettings(
            start_hhmm=f"{h:02d}:00", grid_charge=False, capacity_pct=50
        )
        for h in (0, 5, 11, 16, 19, 22)
    )
    return InverterSettings(
        work_mode="Zero export to CT",
        energy_pattern="Load first",
        max_charge_a=100.0,
        max_discharge_a=100.0,
        slots=slots,
    )


async def _make_op_connected() -> tuple[Operator, MockTransport]:
    transport = MockTransport()
    await transport.connect()
    return Operator(transport=transport), transport


async def _auto_respond_with(transport: MockTransport, payloads: list[str]):
    """Replace transport.publish with one that auto-injects the next
    payload from the queue. Subsequent publishes get the next response."""
    queue = list(payloads)
    original_publish = transport.publish

    async def hooked_publish(topic: str, payload: str) -> None:
        await original_publish(topic, payload)
        if queue:
            response = queue.pop(0)
            asyncio.create_task(transport.inject(RESPONSE_TOPIC, response))

    transport.publish = hooked_publish  # type: ignore[method-assign]


class TestPreflight:
    @pytest.mark.asyncio
    async def test_disconnected_transport_returns_failure(self):
        transport = MockTransport()  # never connected
        op = Operator(transport=transport)
        result = await op.apply(PlannedAction(work_mode="Selling first"))

        assert result.succeeded == ()
        assert len(result.failed) == 1
        assert "transport not connected" in result.failed[0].reason

    @pytest.mark.asyncio
    async def test_safety_violation_returns_failure(self, fast_retry):
        op, _ = await _make_op_connected()
        result = await op.apply(PlannedAction(work_mode="Sell"))  # invalid

        assert result.succeeded == ()
        assert len(result.failed) == 1
        assert "safety:" in result.failed[0].reason
        assert "work_mode" in result.failed[0].reason


class TestExecution:
    @pytest.mark.asyncio
    async def test_empty_action_completes_with_no_writes(self, fast_retry):
        op, _ = await _make_op_connected()
        result = await op.apply(PlannedAction())

        assert result.requested == ()
        assert result.succeeded == ()
        assert result.failed == ()

    @pytest.mark.asyncio
    async def test_single_write_success(self, fast_retry):
        op, transport = await _make_op_connected()
        await _auto_respond_with(transport, ["Saved"])

        result = await op.apply(PlannedAction(work_mode="Selling first"))

        assert len(result.succeeded) == 1
        assert result.failed == ()
        assert result.succeeded[0].topic.endswith("/work_mode/set")
        assert result.verification.states[result.succeeded[0].topic] == "OK_FROM_SA"

    @pytest.mark.asyncio
    async def test_multiple_writes_in_order(self, fast_retry):
        op, transport = await _make_op_connected()
        await _auto_respond_with(transport, ["Saved"] * 4)

        action = PlannedAction(
            work_mode="Selling first",
            energy_pattern="Battery first",
            max_charge_a=80.0,
            max_discharge_a=80.0,
        )
        result = await op.apply(action)

        assert len(result.succeeded) == 4
        topics = [w.topic for w in result.succeeded]
        # Globals before currents.
        assert topics[0].endswith("/work_mode/set")
        assert topics[-1].endswith("/max_discharge_current/set")

    @pytest.mark.asyncio
    async def test_mixed_success_failure(self, fast_retry):
        op, transport = await _make_op_connected()
        # 2 writes: first succeeds, second errors.
        await _auto_respond_with(
            transport, ["Saved", "Error: Invalid value 'X' for 'Y'."]
        )

        action = PlannedAction(work_mode="Selling first", max_charge_a=80.0)
        result = await op.apply(action)

        assert len(result.succeeded) == 1
        assert len(result.failed) == 1
        assert result.succeeded[0].topic.endswith("/work_mode/set")
        assert result.failed[0].write.topic.endswith("/max_charge_current/set")


class TestDurationAndPlanId:
    @pytest.mark.asyncio
    async def test_plan_id_preserved(self, fast_retry):
        op, transport = await _make_op_connected()
        await _auto_respond_with(transport, ["Saved"])

        result = await op.apply(
            PlannedAction(work_mode="Selling first", plan_id="my-plan-123")
        )
        assert result.plan_id == "my-plan-123"

    @pytest.mark.asyncio
    async def test_plan_id_generated_when_missing(self, fast_retry):
        op, _ = await _make_op_connected()
        result = await op.apply(PlannedAction())
        assert len(result.plan_id) == 12  # uuid hex slice

    @pytest.mark.asyncio
    async def test_duration_recorded(self, fast_retry):
        op, _ = await _make_op_connected()
        result = await op.apply(PlannedAction())
        assert result.duration_ms >= 0.0
        assert result.captured_at is not None


class TestDiffPath:
    @pytest.mark.asyncio
    async def test_no_op_when_action_matches_current(self, fast_retry):
        op, transport = await _make_op_connected()
        current = _baseline_settings()
        action = PlannedAction(work_mode=current.work_mode)

        result = await op.apply(action, current_settings=current)
        assert result.requested == ()
        assert result.succeeded == ()
        assert len(transport.published) == 0
