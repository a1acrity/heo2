"""HAStateReader: HA-backed implementation of the StateReader Protocol.

Conforms to state_reader.StateReader. Just delegates to hass.states.
Lives in its own module so tests can import state_reader.MockStateReader
without dragging in HA imports.
"""

from __future__ import annotations

from typing import Any


class HAStateReader:
    """Delegates to hass.states.get(). Returns None for unknown entities."""

    def __init__(self, hass) -> None:  # type: ignore[no-untyped-def]
        self._hass = hass

    def get_state(self, entity_id: str) -> str | None:
        s = self._hass.states.get(entity_id)
        return s.state if s else None

    def get_attributes(self, entity_id: str) -> dict[str, Any]:
        s = self._hass.states.get(entity_id)
        return dict(s.attributes) if s else {}
