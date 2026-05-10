"""Abstraction over Home Assistant state reads.

The adapters need to query HA entity states and attributes; tests
need to inject synthetic state without spinning up HA. This module
defines the Protocol both share and provides a Mock implementation
for tests. The real implementation lives in `state_reader_ha.py`
(P1.7 wiring) and just delegates to `hass.states.get()`.
"""

from __future__ import annotations

from typing import Any, Protocol


class StateReader(Protocol):
    """What the adapters need to query HA."""

    def get_state(self, entity_id: str) -> str | None: ...

    def get_attributes(self, entity_id: str) -> dict[str, Any]: ...


class MockStateReader:
    """Test double — pre-populated states + attributes.

    Use `set_state(eid, state, attributes=...)` to inject. Returns
    None (state) / {} (attributes) for unknown entities so adapters
    behave the same way they would in HA when an entity is absent.
    """

    def __init__(
        self,
        states: dict[str, str] | None = None,
        attributes: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._states: dict[str, str] = dict(states or {})
        self._attributes: dict[str, dict[str, Any]] = dict(attributes or {})

    def get_state(self, entity_id: str) -> str | None:
        return self._states.get(entity_id)

    def get_attributes(self, entity_id: str) -> dict[str, Any]:
        return dict(self._attributes.get(entity_id, {}))

    def set_state(
        self,
        entity_id: str,
        state: str,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        self._states[entity_id] = state
        if attributes is not None:
            self._attributes[entity_id] = dict(attributes)

    def remove(self, entity_id: str) -> None:
        self._states.pop(entity_id, None)
        self._attributes.pop(entity_id, None)


# ── Helpers for parsing HA state strings ──────────────────────────────


_NULL_STATES = frozenset({"unknown", "unavailable", "none", ""})


def parse_float(state: str | None) -> float | None:
    """HA states are strings. Return None on missing / unknown / unparseable."""
    if state is None:
        return None
    if state.strip().lower() in _NULL_STATES:
        return None
    try:
        return float(state)
    except (ValueError, TypeError):
        return None


def parse_int(state: str | None) -> int | None:
    f = parse_float(state)
    return None if f is None else int(f)


def parse_bool(state: str | None) -> bool | None:
    """HA exposes booleans as `on`/`off` (binary_sensor) or `true`/`false`
    (some MQTT discovery numbers). Returns None on unknown/missing.
    """
    if state is None:
        return None
    s = state.strip().lower()
    if s in ("on", "true", "1", "yes"):
        return True
    if s in ("off", "false", "0", "no"):
        return False
    return None


def parse_str(state: str | None) -> str | None:
    if state is None:
        return None
    if state.strip().lower() in _NULL_STATES:
        return None
    return state
