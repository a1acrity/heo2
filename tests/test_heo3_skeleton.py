"""P1.0 skeleton smoke tests.

Confirm the package imports cleanly, the Operator class wires its
sub-components, and the type surface from §11/§14 is reachable.
The stubs themselves raise NotImplementedError — that's expected.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from heo3 import const
from heo3.adapters.inverter import InverterAdapter
from heo3.adapters.peripheral import PeripheralAdapter
from heo3.adapters.world import WorldGatherer
from heo3.build import ActionBuilder
from heo3.compute import Compute
from heo3.operator import Operator
from heo3.transport import MockTransport
from heo3.types import (
    ApplianceState,
    ApplyResult,
    EVState,
    InverterSettings,
    InverterState,
    LiveRates,
    LoadForecast,
    PlannedAction,
    PredictedRates,
    SlotPlan,
    Snapshot,
    SolarForecast,
    SystemConfig,
    SystemFlags,
    TeslaState,
)


class TestPackageImports:
    def test_domain_constant(self):
        assert const.DOMAIN == "heo3"

    def test_tick_budget_constants_present(self):
        assert const.TICK_HARD_BUDGET_S == 60.0
        assert const.TICK_WARNING_S == 30.0


class TestOperatorWiring:
    def test_operator_constructs_with_mock_transport(self):
        op = Operator(transport=MockTransport())
        assert isinstance(op.compute, Compute)
        assert isinstance(op.build, ActionBuilder)

    def test_operator_holds_three_adapters(self):
        op = Operator(transport=MockTransport())
        assert isinstance(op._inverter, InverterAdapter)
        assert isinstance(op._peripheral, PeripheralAdapter)
        assert isinstance(op._world, WorldGatherer)

    @pytest.mark.asyncio
    async def test_shutdown_disconnects_transport(self):
        transport = MockTransport()
        await transport.connect()
        op = Operator(transport=transport)
        assert transport.is_connected is True
        await op.shutdown()
        assert transport.is_connected is False

    @pytest.mark.asyncio
    async def test_snapshot_stub_raises(self):
        op = Operator(transport=MockTransport())
        with pytest.raises(NotImplementedError, match="P1.7"):
            await op.snapshot()

    @pytest.mark.asyncio
    async def test_apply_stub_raises(self):
        op = Operator(transport=MockTransport())
        with pytest.raises(NotImplementedError, match="P1.1"):
            await op.apply(PlannedAction())


class TestTypeSurface:
    """The dataclasses are reachable and constructible. Phase
    placeholders (InverterState etc.) take no args yet — they get
    fields filled in during the adapter phases."""

    def test_planned_action_default_is_no_op(self):
        action = PlannedAction()
        assert action.slots == ()
        assert action.work_mode is None
        assert action.max_charge_a is None
        assert action.tesla_action is None

    def test_planned_action_is_frozen(self):
        action = PlannedAction()
        with pytest.raises(Exception):  # FrozenInstanceError
            action.work_mode = "Selling first"  # type: ignore[misc]

    def test_slot_plan_optional_fields(self):
        slot = SlotPlan(slot_n=1)
        assert slot.start_hhmm is None
        assert slot.grid_charge is None

    def test_snapshot_construct(self):
        snap = Snapshot(
            captured_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
            local_tz=ZoneInfo("Europe/London"),
            inverter=InverterState(),
            inverter_settings=InverterSettings(),
            ev=EVState(),
            tesla=TeslaState(),
            appliances=ApplianceState(),
            rates_live=LiveRates(),
            rates_predicted=PredictedRates(),
            rates_freshness={},
            solar_forecast=SolarForecast(),
            load_forecast=LoadForecast(),
            flags=SystemFlags(),
            config=SystemConfig(),
        )
        assert snap.config.min_soc == 10
        assert snap.tesla is not None

    def test_snapshot_tesla_is_optional(self):
        snap = Snapshot(
            captured_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
            local_tz=ZoneInfo("Europe/London"),
            inverter=InverterState(),
            inverter_settings=InverterSettings(),
            ev=EVState(),
            tesla=None,
            appliances=ApplianceState(),
            rates_live=LiveRates(),
            rates_predicted=PredictedRates(),
            rates_freshness={},
            solar_forecast=SolarForecast(),
            load_forecast=LoadForecast(),
            flags=SystemFlags(),
            config=SystemConfig(),
        )
        assert snap.tesla is None

    def test_apply_result_construct(self):
        from heo3.types import VerificationResult

        result = ApplyResult(
            plan_id="test",
            requested=(),
            succeeded=(),
            failed=(),
            skipped=(),
            verification=VerificationResult(),
            duration_ms=0.0,
            captured_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
        )
        assert result.duration_ms == 0.0
