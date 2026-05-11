"""HAMqttTransport: thin adapter over HA's mqtt component.

Conforms to HEO III's `Transport` Protocol (transport.py). Use this
when HA's MQTT integration is wired to a broker that can both
read SA telemetry AND publish to it. On Paddy's install this DOESN'T
work (mosquitto 2.1.2 bridge limitation — see transport_paho.py for
why); on installs where it does, this is the cleaner option because
HA owns the MQTT lifecycle.

No active connection management — HA's mqtt integration owns the
broker connection. connect/disconnect are no-ops; is_connected
mirrors HA's mqtt integration state.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


class HAMqttTransport:
    """Thin adapter around HA's mqtt component."""

    def __init__(self, hass: Any) -> None:
        self._hass = hass
        # HA owns the connection — we treat ourselves as "connected"
        # whenever the mqtt integration is loaded.
        self._connected = True

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        """No-op — HA's mqtt integration manages the broker lifecycle."""
        self._connected = True

    async def disconnect(self) -> None:
        """No-op — HA's mqtt integration owns shutdown."""
        self._connected = False

    async def publish(self, topic: str, payload: str) -> None:
        from homeassistant.components import mqtt

        await mqtt.async_publish(self._hass, topic, payload, qos=0, retain=False)

    async def subscribe(
        self,
        topic: str,
        callback: Callable[[str, str], Awaitable[None] | None],
    ) -> None:
        from homeassistant.components import mqtt

        async def _msg_received(msg) -> None:
            result = callback(msg.topic, msg.payload)
            if hasattr(result, "__await__"):
                await result

        # HA returns an unsub callable; we don't currently track it here
        # (HEO III's Transport Protocol doesn't expose unsub). When
        # shutdown happens, HA tears down all subscriptions.
        await mqtt.async_subscribe(
            self._hass, topic, _msg_received, qos=0,
        )
