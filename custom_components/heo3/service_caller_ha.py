"""HAServiceCaller: HA-backed implementation of the ServiceCaller Protocol.

Conforms to service_caller.ServiceCaller. Wraps hass.services.async_call.
Lives in its own module so tests can import MockServiceCaller without
HA imports.
"""

from __future__ import annotations

from typing import Any


class HAServiceCaller:
    """Delegates to hass.services.async_call.

    Per HA convention, the entity_id is passed inside the service-call
    `data` dict alongside any other kwargs.
    """

    def __init__(self, hass) -> None:  # type: ignore[no-untyped-def]
        self._hass = hass

    async def call(
        self,
        domain: str,
        service: str,
        entity_id: str,
        **data: Any,
    ) -> None:
        await self._hass.services.async_call(
            domain,
            service,
            {"entity_id": entity_id, **data},
            blocking=True,
        )
