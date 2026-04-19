# custom_components/heo2/ha_mqtt_transport.py
"""HA-side MqttTransport implementation.

Wraps homeassistant.components.mqtt so MqttWriter stays pure.
Only imported when running inside HA - tests use FakeTransport instead.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


class HAMqttTransport:
    """Thin adapter around HA's mqtt component.

    Matches the MqttTransport protocol defined in mqtt_writer.py
    without importing from it (avoids a circular import risk).
    """

    def __init__(self, hass: Any) -> None:
        self._hass = hass

    async def publish(self, topic: str, payload: str) -> None:
        from homeassistant.components import mqtt
        await mqtt.async_publish(self._hass, topic, payload, qos=0, retain=False)

    async def subscribe(
        self,
        topic: str,
        callback: Callable[[str, str], Awaitable[None] | None],
    ) -> Callable[[], None]:
        from homeassistant.components import mqtt

        async def _msg_received(msg) -> None:
            result = callback(msg.topic, msg.payload)
            if hasattr(result, "__await__"):
                await result

        unsub = await mqtt.async_subscribe(
            self._hass, topic, _msg_received, qos=0,
        )
        return unsub
