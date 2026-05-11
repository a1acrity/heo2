"""HAStateReader + HAServiceCaller wrappers — minimal contract checks.

We don't pull in HA for these tests; we use a tiny stand-in for hass
(MagicMock with the bits the wrappers touch). The point is to confirm
the wrappers conform to the StateReader / ServiceCaller Protocols
and do the right delegation.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from heo3.service_caller_ha import HAServiceCaller
from heo3.state_reader_ha import HAStateReader


class _FakeHA:
    def __init__(self):
        self.states = MagicMock()
        self.services = MagicMock()
        self.services.async_call = AsyncMock()


# ── HAStateReader ──────────────────────────────────────────────────


class TestHAStateReader:
    def test_get_state_known(self):
        hass = _FakeHA()
        hass.states.get.return_value = SimpleNamespace(state="42", attributes={})
        r = HAStateReader(hass)
        assert r.get_state("sensor.x") == "42"
        hass.states.get.assert_called_with("sensor.x")

    def test_get_state_unknown_returns_none(self):
        hass = _FakeHA()
        hass.states.get.return_value = None
        r = HAStateReader(hass)
        assert r.get_state("sensor.nope") is None

    def test_get_attributes_known(self):
        hass = _FakeHA()
        hass.states.get.return_value = SimpleNamespace(
            state="x", attributes={"unit": "kWh"}
        )
        r = HAStateReader(hass)
        assert r.get_attributes("sensor.x") == {"unit": "kWh"}

    def test_get_attributes_unknown_returns_empty(self):
        hass = _FakeHA()
        hass.states.get.return_value = None
        r = HAStateReader(hass)
        assert r.get_attributes("sensor.nope") == {}


# ── HAServiceCaller ────────────────────────────────────────────────


class TestHAServiceCaller:
    @pytest.mark.asyncio
    async def test_call_passes_entity_id_in_data(self):
        hass = _FakeHA()
        c = HAServiceCaller(hass)
        await c.call("switch", "turn_on", "switch.x")
        hass.services.async_call.assert_called_once_with(
            "switch", "turn_on", {"entity_id": "switch.x"}, blocking=True
        )

    @pytest.mark.asyncio
    async def test_call_merges_extra_data(self):
        hass = _FakeHA()
        c = HAServiceCaller(hass)
        await c.call("number", "set_value", "number.x", value=80.0)
        hass.services.async_call.assert_called_once_with(
            "number",
            "set_value",
            {"entity_id": "number.x", "value": 80.0},
            blocking=True,
        )
