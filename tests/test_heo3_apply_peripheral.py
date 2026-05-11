"""operator.apply() peripheral dispatch — integration tests."""

from __future__ import annotations

import asyncio

import pytest

from heo3.adapters import inverter as inverter_module
from heo3.operator import Operator
from heo3.service_caller import MockServiceCaller
from heo3.state_reader import MockStateReader
from heo3.transport import MockTransport
from heo3.types import (
    ApplianceAction,
    EVAction,
    PlannedAction,
    TeslaAction,
)


@pytest.fixture
def fast_retry(monkeypatch):
    monkeypatch.setattr(inverter_module, "RESPONSE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(inverter_module, "WRITE_RETRY_BACKOFF_S", 0.0)


async def _op(states=None, *, tesla=True):
    transport = MockTransport()
    await transport.connect()
    return Operator(
        transport=transport,
        state_reader=MockStateReader(states or {}),
        service_caller=MockServiceCaller(),
        tesla_entity_prefix="natalia" if tesla else None,
        appliance_switches={"washer": "switch.washer"},
    )


class TestEVDispatch:
    @pytest.mark.asyncio
    async def test_ev_action_dispatched(self, fast_retry):
        op = await _op({"select.zappi_charge_mode": "Eco"})
        result = await op.apply(
            PlannedAction(ev_action=EVAction(set_mode="Stopped"))
        )
        assert result.peripheral_outcomes.get("ev") == "APPLIED"
        # No inverter writes were requested.
        assert result.requested == ()


class TestTeslaDispatch:
    @pytest.mark.asyncio
    async def test_tesla_action_at_home(self, fast_retry):
        op = await _op({"binary_sensor.natalia_located_at_home": "on"})
        result = await op.apply(
            PlannedAction(
                tesla_action=TeslaAction(set_charge_limit_pct=80)
            )
        )
        assert result.peripheral_outcomes.get("tesla") == "APPLIED"

    @pytest.mark.asyncio
    async def test_tesla_action_not_at_home(self, fast_retry):
        op = await _op({"binary_sensor.natalia_located_at_home": "off"})
        result = await op.apply(
            PlannedAction(
                tesla_action=TeslaAction(set_charging=False)
            )
        )
        assert result.peripheral_outcomes.get("tesla") == "SKIPPED_NOT_AT_HOME"


class TestApplianceDispatch:
    @pytest.mark.asyncio
    async def test_appliance_action_dispatched(self, fast_retry):
        op = await _op()
        result = await op.apply(
            PlannedAction(
                appliances_action=ApplianceAction(turn_off=("washer",))
            )
        )
        assert result.peripheral_outcomes.get("appliances") == "APPLIED"


class TestCombinedDispatch:
    @pytest.mark.asyncio
    async def test_inverter_plus_peripherals(self, fast_retry):
        op = await _op({"binary_sensor.natalia_located_at_home": "on"})
        # Auto-respond to the one inverter publish.
        transport = op._transport

        async def hook(topic, payload):
            await transport.inject(
                "solar_assistant/set/response_message/state", "Saved"
            )

        original_publish = transport.publish

        async def hooked_publish(topic, payload):
            await original_publish(topic, payload)
            asyncio.create_task(hook(topic, payload))

        transport.publish = hooked_publish  # type: ignore[method-assign]

        result = await op.apply(
            PlannedAction(
                work_mode="Selling first",
                tesla_action=TeslaAction(set_charge_limit_pct=80),
                appliances_action=ApplianceAction(turn_off=("washer",)),
            )
        )
        assert len(result.succeeded) == 1
        assert result.peripheral_outcomes.get("tesla") == "APPLIED"
        assert result.peripheral_outcomes.get("appliances") == "APPLIED"


class TestNoPeripheralAction:
    @pytest.mark.asyncio
    async def test_no_peripheral_actions_no_outcomes(self, fast_retry):
        op = await _op()
        result = await op.apply(PlannedAction())
        assert result.peripheral_outcomes == {}
