"""publish_and_verify cycle tests — SA response classification + retries."""

from __future__ import annotations

import asyncio

import pytest

from heo3.adapters import inverter as inverter_module
from heo3.adapters.inverter import (
    InverterAdapter,
    VERIFY_FAILED,
    VERIFY_OK_FROM_SA,
    VERIFY_TIMEOUT,
)
from heo3.transport import MockTransport
from heo3.types import Write


RESPONSE_TOPIC = "solar_assistant/set/response_message/state"


@pytest.fixture
def fast_retry(monkeypatch):
    """Shrink timeouts so retry tests run instantly."""
    monkeypatch.setattr(inverter_module, "RESPONSE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(inverter_module, "WRITE_RETRY_BACKOFF_S", 0.0)


async def _connect(adapter: InverterAdapter, transport: MockTransport) -> None:
    await transport.connect()
    await adapter.ensure_subscribed()


async def _respond_after(transport: MockTransport, payload: str, delay: float = 0.0):
    await asyncio.sleep(delay)
    await transport.inject(RESPONSE_TOPIC, payload)


class TestSaved:
    @pytest.mark.asyncio
    async def test_saved_returns_ok_from_sa(self, fast_retry):
        transport = MockTransport()
        adapter = InverterAdapter(transport, "inverter_1")
        await _connect(adapter, transport)

        asyncio.create_task(_respond_after(transport, "Saved"))
        state = await adapter.publish_and_verify(Write(topic="x", payload="y"))
        assert state == VERIFY_OK_FROM_SA
        assert len(transport.published) == 1


class TestError:
    @pytest.mark.asyncio
    async def test_error_returns_failed_no_retry(self, fast_retry):
        transport = MockTransport()
        adapter = InverterAdapter(transport, "inverter_1")
        await _connect(adapter, transport)

        asyncio.create_task(
            _respond_after(transport, "Error: Invalid value 'X' for 'Y'.")
        )
        state = await adapter.publish_and_verify(Write(topic="x", payload="y"))
        assert state == VERIFY_FAILED
        # No retries on explicit errors — only one publish should land.
        assert len(transport.published) == 1


class TestTimeout:
    @pytest.mark.asyncio
    async def test_no_response_returns_timeout_after_retries(self, fast_retry):
        transport = MockTransport()
        adapter = InverterAdapter(transport, "inverter_1")
        await _connect(adapter, transport)

        # No injected response — every attempt times out.
        state = await adapter.publish_and_verify(Write(topic="x", payload="y"))
        assert state == VERIFY_TIMEOUT
        # 3 attempts = 3 publishes.
        assert len(transport.published) == 3

    @pytest.mark.asyncio
    async def test_recovers_on_second_attempt(self, fast_retry):
        transport = MockTransport()
        adapter = InverterAdapter(transport, "inverter_1")
        await _connect(adapter, transport)

        publishes_seen = 0
        original_publish = transport.publish

        async def counted_publish(topic: str, payload: str) -> None:
            nonlocal publishes_seen
            await original_publish(topic, payload)
            publishes_seen += 1
            # Respond on the 2nd attempt only.
            if publishes_seen == 2:
                asyncio.create_task(_respond_after(transport, "Saved"))

        transport.publish = counted_publish  # type: ignore[method-assign]

        state = await adapter.publish_and_verify(Write(topic="x", payload="y"))
        assert state == VERIFY_OK_FROM_SA
        assert publishes_seen == 2  # 1st timed out, 2nd succeeded


class TestSubscription:
    @pytest.mark.asyncio
    async def test_subscribe_is_idempotent(self):
        transport = MockTransport()
        await transport.connect()
        adapter = InverterAdapter(transport, "inverter_1")

        await adapter.ensure_subscribed()
        await adapter.ensure_subscribed()
        await adapter.ensure_subscribed()

        # Only one handler registered; injecting once should not double-fire.
        # (Tested implicitly via the OK path — multiple handlers would
        # all try to set_result on the future, which raises InvalidStateError.)
        # Simpler check: the subscriptions dict has the topic once.
        assert len(transport._subscriptions[RESPONSE_TOPIC]) == 1


class TestUnexpectedPayload:
    @pytest.mark.asyncio
    async def test_unknown_payload_returns_failed(self, fast_retry):
        transport = MockTransport()
        adapter = InverterAdapter(transport, "inverter_1")
        await _connect(adapter, transport)

        asyncio.create_task(_respond_after(transport, "Garbled response"))
        state = await adapter.publish_and_verify(Write(topic="x", payload="y"))
        assert state == VERIFY_FAILED
        # No retries — unexpected payloads are treated like Errors.
        assert len(transport.published) == 1
