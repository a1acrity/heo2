# custom_components/heo2/direct_mqtt_transport.py
"""Direct MQTT transport: connects HEO II straight to Solar Assistant's broker.

Why this exists:
  HA's mosquitto addon bridges SA's broker to HA's local broker, but the
  bridge config that enables outbound writes (topic # out 0 solar_assistant/
  solar_assistant/) breaks inbound telemetry in mosquitto 2.1.2. We
  ultimately care about inbound working so HA dashboards stay live; the
  price is that we can't use HA's local broker for writes. This transport
  bypasses the bridge entirely by connecting directly to SA's broker at
  192.168.4.7:1883 (configurable).

Threading model:
  paho-mqtt runs a network thread. Callbacks (on_connect, on_message,
  on_disconnect) fire on that thread. We dispatch them to the asyncio
  event loop using run_coroutine_threadsafe so our subscribe callbacks
  can be regular async def.

Lifecycle:
  connect() -> async, blocks until CONNACK received
  subscribe/publish -> async, safe to call after connect
  disconnect() -> clean teardown (called on HA shutdown ideally)
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Awaitable, Callable

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

# Sentinel values - module-level constants so tests can override
DEFAULT_HOST = "192.168.4.7"
DEFAULT_PORT = 1883
DEFAULT_KEEPALIVE = 60
CONNECT_TIMEOUT_SECONDS = 10.0


class DirectMqttTransport:
    """Direct-connection transport to Solar Assistant's MQTT broker.

    Implements the MqttTransport protocol (from mqtt_writer.py):
        async def publish(topic, payload) -> None
        async def subscribe(topic, callback) -> unsub_callable

    Constructor params:
        loop: asyncio event loop callbacks are dispatched to
        host, port, username, password, client_id: MQTT connection params
              (default anonymous connection to SA at 192.168.4.7:1883)

    NOT thread-safe: call from a single asyncio task. paho's own threading
    is handled internally - we just need to avoid two calls to
    connect/disconnect racing.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        username: str | None = None,
        password: str | None = None,
        client_id: str = "heo2_direct",
        keepalive: int = DEFAULT_KEEPALIVE,
    ) -> None:
        self._loop = loop
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._client_id = client_id
        self._keepalive = keepalive

        # Subscription registry: topic -> list of async callbacks.
        # We serve each incoming message to every matching subscriber.
        # Using a list rather than a single cb to support multiple subs
        # on the same topic (e.g., MqttWriter's response listener plus
        # a diagnostic sniffer).
        self._subscriptions: dict[str, list[Callable[[str, str], Any]]] = {}
        self._sub_lock = threading.Lock()

        # Paho client, constructed in connect() so the lifecycle is
        # explicit and reconstructable after disconnect.
        self._client: Any = None
        self._connected_event = asyncio.Event()
        self._connected = False


    @property
    def is_connected(self) -> bool:
        """True when CONNACK received and broker is reachable."""
        return self._connected

    async def connect(self) -> None:
        """Establish connection to SA broker. Blocks until CONNACK
        received or timeout. Raises on failure."""
        # paho-mqtt v2.x changed constructor. Prefer v2 API.
        try:
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=self._client_id,
                clean_session=True,
            )
        except AttributeError:
            # v1.x fallback - theoretical, HA ships v2 since 2024.4
            client = mqtt.Client(client_id=self._client_id, clean_session=True)

        if self._username:
            client.username_pw_set(self._username, self._password)

        # Wire callbacks. These fire on paho's network thread.
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message

        self._client = client
        self._connected_event.clear()

        logger.info(
            "DirectMqttTransport: connecting to %s:%d as client_id=%s",
            self._host, self._port, self._client_id,
        )

        # connect_async returns immediately. loop_start spawns the
        # network thread which drives the protocol and fires callbacks.
        def _start() -> None:
            client.connect_async(self._host, self._port, self._keepalive)
            client.loop_start()

        await self._loop.run_in_executor(None, _start)

        # Wait for CONNACK (set in _on_connect).
        try:
            await asyncio.wait_for(
                self._connected_event.wait(),
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error(
                "DirectMqttTransport: CONNACK not received within %.1fs, aborting",
                CONNECT_TIMEOUT_SECONDS,
            )
            await self.disconnect()
            raise


    async def disconnect(self) -> None:
        """Clean shutdown of MQTT connection. Safe to call multiple times."""
        client = self._client
        if client is None:
            return
        self._client = None
        self._connected = False

        def _stop() -> None:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                # best-effort shutdown - don't raise in disconnect path
                logger.debug("DirectMqttTransport: exception during disconnect", exc_info=True)

        await self._loop.run_in_executor(None, _stop)
        logger.info("DirectMqttTransport: disconnected from %s", self._host)

    async def publish(self, topic: str, payload: str) -> None:
        """Publish payload to topic. Matches MqttTransport protocol.

        Raises RuntimeError if not connected (lets MqttWriter handle it
        as a write failure rather than silently succeeding)."""
        client = self._client
        if client is None or not self._connected:
            raise RuntimeError(
                f"DirectMqttTransport not connected, cannot publish to {topic}"
            )

        def _publish() -> None:
            result = client.publish(topic, payload, qos=0, retain=False)
            # paho returns a MessageInfo object; .rc is the return code.
            # 0 = MQTT_ERR_SUCCESS. Other values are errors.
            if result.rc != 0:
                raise RuntimeError(
                    f"publish to {topic} failed with rc={result.rc}"
                )

        await self._loop.run_in_executor(None, _publish)


    async def subscribe(
        self,
        topic: str,
        callback: Callable[[str, str], Awaitable[None] | None],
    ) -> Callable[[], None]:
        """Subscribe to topic. callback(topic, payload) is called on
        each message. Returns a sync unsubscribe callable.

        Multiple subscribes to the same topic are supported - all
        callbacks are invoked on each matching message."""
        client = self._client
        if client is None or not self._connected:
            raise RuntimeError(
                f"DirectMqttTransport not connected, cannot subscribe to {topic}"
            )

        with self._sub_lock:
            first_sub = topic not in self._subscriptions
            self._subscriptions.setdefault(topic, []).append(callback)

        # Only send SUBSCRIBE to broker on the first subscriber for this
        # topic - otherwise we'd get duplicate messages.
        if first_sub:
            def _subscribe() -> None:
                result, _mid = client.subscribe(topic, qos=0)
                if result != 0:
                    raise RuntimeError(
                        f"subscribe to {topic} failed with rc={result}"
                    )
            await self._loop.run_in_executor(None, _subscribe)

        def unsub() -> None:
            """Remove this callback. If it was the last sub on this
            topic, also send UNSUBSCRIBE to broker."""
            with self._sub_lock:
                if topic not in self._subscriptions:
                    return
                try:
                    self._subscriptions[topic].remove(callback)
                except ValueError:
                    return
                if not self._subscriptions[topic]:
                    del self._subscriptions[topic]
                    last = True
                else:
                    last = False
            if last and self._client is not None:
                try:
                    self._client.unsubscribe(topic)
                except Exception:
                    logger.debug("unsubscribe failed for %s", topic, exc_info=True)

        return unsub


    # ---- Callbacks from paho network thread ----
    #
    # These run on paho's internal thread. They must not call asyncio
    # primitives directly - instead dispatch to the event loop with
    # call_soon_threadsafe / run_coroutine_threadsafe.

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        """Fired when CONNACK received from broker."""
        # paho v2 API: reason_code is a ReasonCode object; .is_failure exists
        # paho v1 API: reason_code is an int where 0 = success
        try:
            is_failure = reason_code.is_failure  # v2
        except AttributeError:
            is_failure = (reason_code != 0)  # v1

        if is_failure:
            logger.error(
                "DirectMqttTransport: broker rejected connection, rc=%s",
                reason_code,
            )
            # Don't set _connected. connect() will time out waiting for the
            # event and raise.
            return

        logger.info(
            "DirectMqttTransport: connected to %s:%d (flags=%s)",
            self._host, self._port, flags,
        )
        self._connected = True

        # Wake up the connect() coroutine. This MUST be threadsafe.
        self._loop.call_soon_threadsafe(self._connected_event.set)

        # If we had subscriptions before disconnect (reconnect case),
        # resubscribe. On first connect this is a no-op.
        with self._sub_lock:
            topics = list(self._subscriptions.keys())
        for t in topics:
            try:
                client.subscribe(t, qos=0)
            except Exception:
                logger.exception("DirectMqttTransport: resubscribe to %s failed", t)

    def _on_disconnect(self, client, userdata, *args, **kwargs) -> None:
        """Fired on broker disconnect or network failure.

        paho's loop_start() will attempt reconnect automatically; we just
        flag ourselves as disconnected so publish/subscribe raise cleanly.
        Signature accepts any extra args for cross-version compat (v1 and
        v2 pass different params)."""
        self._connected = False
        logger.warning(
            "DirectMqttTransport: disconnected from %s (paho will auto-reconnect)",
            self._host,
        )

    def _on_message(self, client, userdata, msg) -> None:
        """Fired on incoming PUBLISH. Dispatch to asyncio loop."""
        topic = msg.topic
        try:
            payload = msg.payload.decode("utf-8", errors="replace")
        except Exception:
            payload = ""

        with self._sub_lock:
            callbacks = list(self._subscriptions.get(topic, []))

        # Dispatch each callback to the event loop safely.
        # If callback is async, schedule the coroutine.
        # If sync, call through call_soon_threadsafe.
        for cb in callbacks:
            try:
                result = cb(topic, payload)
                if asyncio.iscoroutine(result):
                    asyncio.run_coroutine_threadsafe(result, self._loop)
            except Exception:
                logger.exception(
                    "DirectMqttTransport: callback for %s raised", topic,
                )
