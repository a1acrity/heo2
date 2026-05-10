"""Abstraction over Home Assistant service calls.

The peripheral adapter writes via HA services (select.set_option,
switch.turn_on/off, number.set_value), not MQTT. Tests need to
inject a recording double; the real implementation in P1.7 wraps
hass.services.async_call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ServiceCall:
    """One recorded service invocation."""

    domain: str
    service: str
    entity_id: str
    data: dict[str, Any] = field(default_factory=dict)


class ServiceCaller(Protocol):
    """What the peripheral adapter needs from HA."""

    async def call(
        self,
        domain: str,
        service: str,
        entity_id: str,
        **data: Any,
    ) -> None: ...


class MockServiceCaller:
    """Records every call. Use in tests to assert correct service +
    target + payload. Optionally fails specific calls for error-path
    tests via `fail_on(predicate)`.
    """

    def __init__(self) -> None:
        self.calls: list[ServiceCall] = []
        self._fail_predicates: list = []

    async def call(
        self,
        domain: str,
        service: str,
        entity_id: str,
        **data: Any,
    ) -> None:
        sc = ServiceCall(
            domain=domain, service=service, entity_id=entity_id, data=dict(data)
        )
        for pred in self._fail_predicates:
            if pred(sc):
                self.calls.append(sc)
                raise RuntimeError(f"injected failure on {domain}.{service}({entity_id})")
        self.calls.append(sc)

    def fail_on(self, predicate) -> None:
        """Schedule that any subsequent call matching `predicate(sc)` raises."""
        self._fail_predicates.append(predicate)

    def clear(self) -> None:
        self.calls.clear()
        self._fail_predicates.clear()
