# tests/test_direct_mqtt_transport.py
"""Tests for DirectMqttTransport.

Strategy: mock paho's client so we can simulate network-thread callbacks
from tests without real TCP connections or background threads.

The real integration risk (paho's network thread, threadsafe dispatch to
asyncio loop) is covered by:
  - in-unit: mocking paho and directly invoking the _on_connect, _on_message
    callbacks from the asyncio-test thread to exercise the dispatch logic
  - in-production: first-run deploy against SA's actual broker, verified
    by observing "Saved" responses come through
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from heo2.direct_mqtt_transport import DirectMqttTransport


def _make_fake_paho_module(connect_rc: int = 0) -> MagicMock:
    """Build a fake paho.mqtt.client module with enough API surface for tests.

    connect_rc=0 simulates successful CONNACK. Non-zero triggers failure path.
    """
    fake_mqtt = MagicMock()
    fake_mqtt.CallbackAPIVersion.VERSION2 = "v2_sentinel"

    fake_client = MagicMock()
    fake_client.publish.return_value = SimpleNamespace(rc=0)
    fake_client.subscribe.return_value = (0, 1)  # (rc, mid)
    fake_client.unsubscribe.return_value = (0, 1)
    fake_mqtt.Client.return_value = fake_client

    return fake_mqtt, fake_client


def _patch_mqtt(fake_mqtt: MagicMock):
    """Context manager patching the module-level mqtt import in
    direct_mqtt_transport with our fake."""
    return patch("heo2.direct_mqtt_transport.mqtt", fake_mqtt)


def _trigger_connack(client: MagicMock, transport: DirectMqttTransport,
                      success: bool = True) -> None:
    """Simulate paho firing on_connect after CONNACK."""
    reason_code = SimpleNamespace(is_failure=not success)
    transport._on_connect(client, None, {}, reason_code)


class TestConnect:
    @pytest.mark.asyncio
    async def test_successful_connect_sets_is_connected(self):
        """Happy path: CONNACK received, transport reports connected."""
        fake_mqtt, fake_client = _make_fake_paho_module()

        loop = asyncio.get_event_loop()
        transport = DirectMqttTransport(loop=loop)

        # Patch paho import inside connect()
        with _patch_mqtt(fake_mqtt):
            # Start connect() as a task so we can trigger CONNACK separately
            connect_task = asyncio.create_task(transport.connect())
            await asyncio.sleep(0.05)  # let executor run connect_async/loop_start

            # Simulate paho firing on_connect on its network thread
            _trigger_connack(fake_client, transport, success=True)

            await connect_task

        assert transport.is_connected is True
        fake_client.connect_async.assert_called_once_with("192.168.4.7", 1883, 60)
        fake_client.loop_start.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_timeout_raises(self):
        """If CONNACK never arrives, connect() raises TimeoutError."""
        fake_mqtt, fake_client = _make_fake_paho_module()

        loop = asyncio.get_event_loop()
        # Override timeout to short value via monkey-patching module constant
        with patch("heo2.direct_mqtt_transport.CONNECT_TIMEOUT_SECONDS", 0.1):
            transport = DirectMqttTransport(loop=loop)
            with _patch_mqtt(fake_mqtt):
                with pytest.raises(asyncio.TimeoutError):
                    await transport.connect()

        assert transport.is_connected is False
        # disconnect should have been called as cleanup
        fake_client.loop_stop.assert_called()

    @pytest.mark.asyncio
    async def test_connect_failure_reason_code_sets_not_connected(self):
        """If CONNACK has failure reason_code, we don't mark connected."""
        fake_mqtt, fake_client = _make_fake_paho_module()

        loop = asyncio.get_event_loop()
        with patch("heo2.direct_mqtt_transport.CONNECT_TIMEOUT_SECONDS", 0.1):
            transport = DirectMqttTransport(loop=loop)
            with _patch_mqtt(fake_mqtt):
                connect_task = asyncio.create_task(transport.connect())
                await asyncio.sleep(0.05)
                _trigger_connack(fake_client, transport, success=False)
                # connect() will still time out because we didn't set the event
                with pytest.raises(asyncio.TimeoutError):
                    await connect_task

        assert transport.is_connected is False


class TestPublish:
    @pytest.mark.asyncio
    async def test_publish_before_connect_raises(self):
        """publish on disconnected transport must raise, not silently succeed."""
        loop = asyncio.get_event_loop()
        transport = DirectMqttTransport(loop=loop)

        with pytest.raises(RuntimeError, match="not connected"):
            await transport.publish("some/topic", "payload")

    @pytest.mark.asyncio
    async def test_publish_happy_path(self):
        """Once connected, publish calls paho client.publish with right args."""
        fake_mqtt, fake_client = _make_fake_paho_module()
        loop = asyncio.get_event_loop()
        transport = DirectMqttTransport(loop=loop)

        with _patch_mqtt(fake_mqtt):
            task = asyncio.create_task(transport.connect())
            await asyncio.sleep(0.05)
            _trigger_connack(fake_client, transport, success=True)
            await task

            await transport.publish("solar_assistant/inverter_1/capacity_point_1/set", "100")

        fake_client.publish.assert_called_once_with(
            "solar_assistant/inverter_1/capacity_point_1/set", "100",
            qos=0, retain=False,
        )

    @pytest.mark.asyncio
    async def test_publish_nonzero_rc_raises(self):
        """If paho publish returns non-zero rc (e.g. disconnected mid-call),
        we raise so MqttWriter can count it as a write failure."""
        fake_mqtt, fake_client = _make_fake_paho_module()
        fake_client.publish.return_value = SimpleNamespace(rc=4)  # MQTT_ERR_NO_CONN

        loop = asyncio.get_event_loop()
        transport = DirectMqttTransport(loop=loop)

        with _patch_mqtt(fake_mqtt):
            task = asyncio.create_task(transport.connect())
            await asyncio.sleep(0.05)
            _trigger_connack(fake_client, transport, success=True)
            await task

            with pytest.raises(RuntimeError, match="rc=4"):
                await transport.publish("topic", "payload")


class TestSubscribe:
    @pytest.mark.asyncio
    async def test_subscribe_calls_paho_subscribe(self):
        """First subscribe to a topic sends SUBSCRIBE to broker."""
        fake_mqtt, fake_client = _make_fake_paho_module()
        loop = asyncio.get_event_loop()
        transport = DirectMqttTransport(loop=loop)

        with _patch_mqtt(fake_mqtt):
            task = asyncio.create_task(transport.connect())
            await asyncio.sleep(0.05)
            _trigger_connack(fake_client, transport, success=True)
            await task

            async def cb(topic, payload):
                pass

            await transport.subscribe("solar_assistant/set/response_message/state", cb)

        fake_client.subscribe.assert_called_once_with(
            "solar_assistant/set/response_message/state", qos=0,
        )

    @pytest.mark.asyncio
    async def test_incoming_message_dispatches_to_callback(self):
        """Simulate paho on_message firing; verify our callback runs."""
        fake_mqtt, fake_client = _make_fake_paho_module()
        loop = asyncio.get_event_loop()
        transport = DirectMqttTransport(loop=loop)

        received: list[tuple[str, str]] = []

        async def cb(topic, payload):
            received.append((topic, payload))

        with _patch_mqtt(fake_mqtt):
            task = asyncio.create_task(transport.connect())
            await asyncio.sleep(0.05)
            _trigger_connack(fake_client, transport, success=True)
            await task
            await transport.subscribe("topic/a", cb)

            # simulate incoming message from paho network thread
            fake_msg = SimpleNamespace(topic="topic/a", payload=b"payload_bytes")
            transport._on_message(fake_client, None, fake_msg)

            # Let the scheduled coroutine run
            await asyncio.sleep(0.05)

        assert received == [("topic/a", "payload_bytes")]


    @pytest.mark.asyncio
    async def test_unsubscribe_removes_callback_and_sends_unsub_on_last(self):
        """Unsub-callable removes the cb. When it's the last for a topic,
        UNSUBSCRIBE is sent to broker."""
        fake_mqtt, fake_client = _make_fake_paho_module()
        loop = asyncio.get_event_loop()
        transport = DirectMqttTransport(loop=loop)

        with _patch_mqtt(fake_mqtt):
            task = asyncio.create_task(transport.connect())
            await asyncio.sleep(0.05)
            _trigger_connack(fake_client, transport, success=True)
            await task

            received: list = []
            async def cb(t, p): received.append((t, p))

            unsub = await transport.subscribe("topic/a", cb)
            unsub()

            # Broker-level unsubscribe should fire because it was last sub
            fake_client.unsubscribe.assert_called_once_with("topic/a")

            # Incoming message after unsub should NOT call our callback
            fake_msg = SimpleNamespace(topic="topic/a", payload=b"after_unsub")
            transport._on_message(fake_client, None, fake_msg)
            await asyncio.sleep(0.05)

        assert received == []  # callback was never invoked

    @pytest.mark.asyncio
    async def test_multiple_subs_same_topic_single_broker_sub(self):
        """Two subscribers on the same topic should only send one SUBSCRIBE
        to the broker, but both callbacks should fire on messages."""
        fake_mqtt, fake_client = _make_fake_paho_module()
        loop = asyncio.get_event_loop()
        transport = DirectMqttTransport(loop=loop)

        with _patch_mqtt(fake_mqtt):
            task = asyncio.create_task(transport.connect())
            await asyncio.sleep(0.05)
            _trigger_connack(fake_client, transport, success=True)
            await task

            calls_a: list = []
            calls_b: list = []
            async def cb_a(t, p): calls_a.append(p)
            async def cb_b(t, p): calls_b.append(p)

            await transport.subscribe("topic/x", cb_a)
            await transport.subscribe("topic/x", cb_b)

            # Only ONE broker SUBSCRIBE call
            assert fake_client.subscribe.call_count == 1

            # Both callbacks fire on a message
            fake_msg = SimpleNamespace(topic="topic/x", payload=b"hello")
            transport._on_message(fake_client, None, fake_msg)
            await asyncio.sleep(0.05)

        assert calls_a == ["hello"]
        assert calls_b == ["hello"]

