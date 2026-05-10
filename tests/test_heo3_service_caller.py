"""MockServiceCaller contract tests."""

from __future__ import annotations

import pytest

from heo3.service_caller import MockServiceCaller, ServiceCall


class TestRecording:
    @pytest.mark.asyncio
    async def test_records_call(self):
        c = MockServiceCaller()
        await c.call("switch", "turn_on", "switch.x")
        assert len(c.calls) == 1
        assert c.calls[0] == ServiceCall(
            domain="switch",
            service="turn_on",
            entity_id="switch.x",
            data={},
        )

    @pytest.mark.asyncio
    async def test_records_data(self):
        c = MockServiceCaller()
        await c.call("number", "set_value", "number.x", value=80.0)
        assert c.calls[0].data == {"value": 80.0}

    @pytest.mark.asyncio
    async def test_clear(self):
        c = MockServiceCaller()
        await c.call("a", "b", "c")
        c.clear()
        assert c.calls == []


class TestFailureInjection:
    @pytest.mark.asyncio
    async def test_fail_on_match_raises(self):
        c = MockServiceCaller()
        c.fail_on(lambda sc: sc.entity_id == "switch.fragile")
        with pytest.raises(RuntimeError, match="injected failure"):
            await c.call("switch", "turn_on", "switch.fragile")
        # Call still recorded.
        assert c.calls[0].entity_id == "switch.fragile"

    @pytest.mark.asyncio
    async def test_fail_on_no_match_passes(self):
        c = MockServiceCaller()
        c.fail_on(lambda sc: sc.entity_id == "switch.fragile")
        await c.call("switch", "turn_on", "switch.fine")  # no exception
        assert len(c.calls) == 1
