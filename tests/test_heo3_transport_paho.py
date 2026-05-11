"""PahoTransport tests — mocked paho client, no real network.

Strategy ported from heo2/test_direct_mqtt_transport.py — exercises
the threadsafe-dispatch logic by directly invoking on_connect /
on_message from the test thread.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from heo3.transport_paho import PahoTransport


def _fake_paho() -> tuple[MagicMock, MagicMock]:
    """Build a fake paho.mqtt.client module that PahoTransport can drive."""
    fake_mqtt = MagicMock()
    fake_mqtt.CallbackAPIVersion.VERSION2 = "v2_sentinel"

    fake_client = MagicMock()
    fake_client.publish.return_value = SimpleNamespace(rc=0)
    fake_client.subscribe.return_value = (0, 1)  # (rc, mid)
    fake_client.unsubscribe.return_value = (0, 1)
    fake_mqtt.Client.return_value = fake_client
    return fake_mqtt, fake_client


def _connack(client: MagicMock, transport: PahoTransport, success: bool = True):
    """Simulate paho's _on_connect callback firing from the network thread."""
    rc = MagicMock()
    rc.is_failure = not success
    transport._on_connect(client, None, {}, rc)


# ── Transport Protocol conformance ────────────────────────────────


class TestProtocolShape:
    def test_required_methods_exist(self):
        loop = asyncio.new_event_loop()
        try:
            t = PahoTransport(loop=loop)
            assert hasattr(t, "connect") and callable(t.connect)
            assert hasattr(t, "disconnect") and callable(t.disconnect)
            assert hasattr(t, "publish") and callable(t.publish)
            assert hasattr(t, "subscribe") and callable(t.subscribe)
            assert hasattr(t, "is_connected")
            assert isinstance(t.is_connected, bool)
        finally:
            loop.close()


# ── connect ────────────────────────────────────────────────────────


class TestConnect:
    @pytest.mark.asyncio
    async def test_successful_connack(self):
        fake_mqtt, fake_client = _fake_paho()
        with patch("heo3.transport_paho.mqtt", fake_mqtt):
            t = PahoTransport(loop=asyncio.get_event_loop(), host="x", port=1883)
            connect_task = asyncio.create_task(t.connect())
            # Give the executor time to call connect_async + loop_start.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            _connack(fake_client, t, success=True)
            await connect_task
            assert t.is_connected is True

    @pytest.mark.asyncio
    async def test_failed_connack_disconnects(self):
        fake_mqtt, fake_client = _fake_paho()
        with patch("heo3.transport_paho.mqtt", fake_mqtt):
            t = PahoTransport(loop=asyncio.get_event_loop(), host="x", port=1883)
            # No connack will be triggered → connect() times out.
            from heo3 import transport_paho

            with patch.object(transport_paho, "CONNECT_TIMEOUT_SECONDS", 0.1):
                with pytest.raises(asyncio.TimeoutError):
                    await t.connect()


# ── publish / subscribe ────────────────────────────────────────────


class TestPublishSubscribe:
    @pytest.mark.asyncio
    async def test_publish_calls_paho(self):
        fake_mqtt, fake_client = _fake_paho()
        with patch("heo3.transport_paho.mqtt", fake_mqtt):
            t = PahoTransport(loop=asyncio.get_event_loop(), host="x")
            connect_task = asyncio.create_task(t.connect())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            _connack(fake_client, t)
            await connect_task

            await t.publish("foo/bar", "value")
            fake_client.publish.assert_called_with(
                "foo/bar", "value", qos=0, retain=False
            )

    @pytest.mark.asyncio
    async def test_publish_when_disconnected_raises(self):
        fake_mqtt, fake_client = _fake_paho()
        with patch("heo3.transport_paho.mqtt", fake_mqtt):
            t = PahoTransport(loop=asyncio.get_event_loop())
            with pytest.raises(RuntimeError, match="not connected"):
                await t.publish("x", "y")

    @pytest.mark.asyncio
    async def test_subscribe_dispatches_on_message(self):
        fake_mqtt, fake_client = _fake_paho()
        with patch("heo3.transport_paho.mqtt", fake_mqtt):
            t = PahoTransport(loop=asyncio.get_event_loop())
            connect_task = asyncio.create_task(t.connect())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            _connack(fake_client, t)
            await connect_task

            received: list[tuple[str, str]] = []

            async def handler(topic: str, payload: str) -> None:
                received.append((topic, payload))

            await t.subscribe("response/state", handler)

            # Simulate paho delivering a message.
            msg = SimpleNamespace(topic="response/state", payload=b"Saved")
            t._on_message(fake_client, None, msg)
            # _on_message dispatches via run_coroutine_threadsafe; yield.
            await asyncio.sleep(0.01)
            assert received == [("response/state", "Saved")]
