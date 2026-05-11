"""MQTT transport abstraction.

Defines the Transport Protocol the InverterAdapter writes against,
plus a MockTransport for tests. The real paho-based implementation
lands in P1.1; only the protocol shape and mock are needed for P1.0.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol


@dataclass(frozen=True)
class PublishedMessage:
    topic: str
    payload: str


class Transport(Protocol):
    """What the InverterAdapter expects from any transport."""

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def publish(self, topic: str, payload: str) -> None: ...

    async def subscribe(
        self,
        topic: str,
        handler: Callable[[str, str], Awaitable[None]],
    ) -> None: ...

    @property
    def is_connected(self) -> bool: ...


class MockTransport:
    """Records publishes; lets tests inject inbound messages.

    Use in unit tests to assert the adapter sends the right topic +
    payload sequence and to simulate SA's `set/response_message/state`
    replies. Per `feedback_dry_run.md`, do NOT test the writer via
    dry_run — use this mock instead.
    """

    def __init__(self) -> None:
        self.published: list[PublishedMessage] = []
        self._subscriptions: dict[str, list[Callable[[str, str], Awaitable[None]]]] = (
            {}
        )
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def publish(self, topic: str, payload: str) -> None:
        if not self._connected:
            raise RuntimeError("MockTransport.publish before connect()")
        self.published.append(PublishedMessage(topic=topic, payload=payload))

    async def subscribe(
        self,
        topic: str,
        handler: Callable[[str, str], Awaitable[None]],
    ) -> None:
        self._subscriptions.setdefault(topic, []).append(handler)

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── test injection ────────────────────────────────────────────

    async def inject(self, topic: str, payload: str) -> None:
        """Simulate an inbound MQTT message. Awaits all handlers."""
        for sub_topic, handlers in self._subscriptions.items():
            if _topic_matches(sub_topic, topic):
                for h in handlers:
                    await h(topic, payload)

    def clear(self) -> None:
        self.published.clear()


def _topic_matches(subscription: str, topic: str) -> bool:
    """MQTT wildcard matcher: + (single level) and # (multi-level).

    Minimal implementation — enough for the SA topics we subscribe to.
    """
    sub_parts = subscription.split("/")
    top_parts = topic.split("/")
    for i, sp in enumerate(sub_parts):
        if sp == "#":
            return True
        if i >= len(top_parts):
            return False
        if sp == "+":
            continue
        if sp != top_parts[i]:
            return False
    return len(sub_parts) == len(top_parts)
