"""PeripheralAdapter tests — reads + writes for zappi, Tesla, appliances."""

from __future__ import annotations

import pytest

from heo3.adapters.peripheral import (
    APPLIED,
    FAILED,
    NO_OP,
    PeripheralAdapter,
    SKIPPED_NOT_AT_HOME,
    SKIPPED_NO_CAPTURED_MODE,
    SKIPPED_NO_CONFIG,
    TeslaConfig,
    ZappiConfig,
)
from heo3.service_caller import MockServiceCaller
from heo3.state_reader import MockStateReader
from heo3.types import (
    ApplianceAction,
    EVAction,
    TeslaAction,
)


def _adapter(*, states=None, attributes=None, tesla=True, appliances=True):
    return PeripheralAdapter(
        state_reader=MockStateReader(states or {}, attributes or {}),
        service_caller=MockServiceCaller(),
        tesla_entity_prefix="natalia" if tesla else None,
        appliance_switches=(
            {"washer": "switch.washer", "dryer": "switch.dryer"}
            if appliances
            else None
        ),
    )


# ── TeslaConfig.from_vehicle ───────────────────────────────────────


class TestTeslaConfigFromVehicle:
    def test_natalia_naming(self):
        cfg = TeslaConfig.from_vehicle("natalia")
        assert cfg.charge_switch == "switch.natalia_charge"
        assert cfg.battery_level == "sensor.natalia_battery_level"
        assert cfg.charge_limit == "number.natalia_charge_limit"
        assert cfg.charge_current == "number.natalia_charge_current"
        assert cfg.located_at_home == "binary_sensor.natalia_located_at_home"


# ── EV reads ───────────────────────────────────────────────────────


class TestReadEV:
    @pytest.mark.asyncio
    async def test_full_state(self):
        adapter = _adapter(
            states={
                "select.zappi_charge_mode": "Eco+",
                "sensor.zappi_charging_state": "Charging",
                "sensor.zappi_charge_power": "3500.5",
            }
        )
        state = await adapter.read_ev()
        assert state.charging is True
        assert state.mode == "Eco+"
        assert state.charge_power_w == 3500.5

    @pytest.mark.asyncio
    async def test_not_charging(self):
        adapter = _adapter(
            states={
                "select.zappi_charge_mode": "Stopped",
                "sensor.zappi_charging_state": "Connected",
            }
        )
        state = await adapter.read_ev()
        assert state.charging is False
        assert state.mode == "Stopped"

    @pytest.mark.asyncio
    async def test_missing_state_reader_returns_empty(self):
        adapter = PeripheralAdapter()
        state = await adapter.read_ev()
        assert state.mode is None
        assert state.charge_power_w is None


# ── Tesla reads ────────────────────────────────────────────────────


class TestReadTesla:
    @pytest.mark.asyncio
    async def test_full_state(self):
        adapter = _adapter(
            states={
                "sensor.natalia_battery_level": "82.253",
                "sensor.natalia_charging": "Stopped",
                "sensor.natalia_charger_power": "0",
                "number.natalia_charge_limit": "80",
                "number.natalia_charge_current": "24",
                "binary_sensor.natalia_charge_cable": "off",
                "binary_sensor.natalia_located_at_home": "on",
            }
        )
        state = await adapter.read_tesla()
        assert state is not None
        assert state.soc_pct == 82.253
        assert state.is_charging is False
        assert state.charge_limit_pct == 80
        assert state.charge_current_a == 24
        assert state.cable_plugged is False
        assert state.located_at_home is True

    @pytest.mark.asyncio
    async def test_returns_none_when_not_configured(self):
        adapter = _adapter(tesla=False)
        assert await adapter.read_tesla() is None

    @pytest.mark.asyncio
    async def test_unknown_fields_become_none(self):
        # Car asleep — Teslemetry returns 'unknown' for live fields.
        adapter = _adapter(
            states={
                "sensor.natalia_battery_level": "82",
                "sensor.natalia_charging": "unknown",
                "sensor.natalia_charger_power": "unknown",
                "binary_sensor.natalia_located_at_home": "on",
            }
        )
        state = await adapter.read_tesla()
        assert state is not None
        assert state.soc_pct == 82.0
        assert state.is_charging is None
        assert state.charge_power_w is None
        assert state.located_at_home is True


# ── Appliance reads ────────────────────────────────────────────────


class TestReadAppliances:
    @pytest.mark.asyncio
    async def test_full_state(self):
        adapter = _adapter(
            states={
                "binary_sensor.washer_running": "on",
                "binary_sensor.dryer_running": "off",
            }
        )
        state = await adapter.read_appliances()
        assert state.washer_running is True
        assert state.dryer_running is False
        assert state.dishwasher_running is None  # not configured


# ── EV writes ──────────────────────────────────────────────────────


class TestApplyEV:
    @pytest.mark.asyncio
    async def test_set_mode_calls_select_service(self):
        adapter = _adapter(
            states={"select.zappi_charge_mode": "Eco"}
        )
        outcome = await adapter.apply_ev(EVAction(set_mode="Stopped"))
        assert outcome == APPLIED
        sc = adapter._service_caller
        assert sc.calls[0].domain == "select"
        assert sc.calls[0].service == "select_option"
        assert sc.calls[0].entity_id == "select.zappi_charge_mode"
        assert sc.calls[0].data == {"option": "Stopped"}

    @pytest.mark.asyncio
    async def test_invalid_mode_returns_failed(self):
        adapter = _adapter()
        outcome = await adapter.apply_ev(EVAction(set_mode="Bogus"))
        assert outcome == FAILED

    @pytest.mark.asyncio
    async def test_no_op_when_no_intent(self):
        adapter = _adapter()
        outcome = await adapter.apply_ev(EVAction())
        assert outcome == NO_OP

    @pytest.mark.asyncio
    async def test_capture_then_restore(self):
        # Currently Eco; planner sets Stopped (captures); then restores.
        adapter = _adapter(
            states={"select.zappi_charge_mode": "Eco"}
        )
        await adapter.apply_ev(EVAction(set_mode="Stopped"))
        # Now state is "Stopped" in HA (we'd update via inject normally,
        # but the captured value is what matters for restore).
        outcome = await adapter.apply_ev(EVAction(restore_previous=True))
        assert outcome == APPLIED
        sc = adapter._service_caller
        # Two calls: stop, then restore-to-Eco.
        assert sc.calls[1].data == {"option": "Eco"}

    @pytest.mark.asyncio
    async def test_restore_with_no_capture_returns_skipped(self):
        adapter = _adapter()
        outcome = await adapter.apply_ev(EVAction(restore_previous=True))
        assert outcome == SKIPPED_NO_CAPTURED_MODE

    @pytest.mark.asyncio
    async def test_no_capture_when_already_stopped(self):
        # If mode is already Stopped when we set Stopped, don't capture.
        adapter = _adapter(
            states={"select.zappi_charge_mode": "Stopped"}
        )
        await adapter.apply_ev(EVAction(set_mode="Stopped"))
        # Restore should now have nothing to restore to.
        outcome = await adapter.apply_ev(EVAction(restore_previous=True))
        assert outcome == SKIPPED_NO_CAPTURED_MODE


# ── Tesla writes ───────────────────────────────────────────────────


class TestApplyTesla:
    @pytest.mark.asyncio
    async def test_stop_charging_at_home(self):
        adapter = _adapter(
            states={"binary_sensor.natalia_located_at_home": "on"}
        )
        outcome = await adapter.apply_tesla(TeslaAction(set_charging=False))
        assert outcome == APPLIED
        sc = adapter._service_caller
        assert sc.calls[0].domain == "switch"
        assert sc.calls[0].service == "turn_off"
        assert sc.calls[0].entity_id == "switch.natalia_charge"

    @pytest.mark.asyncio
    async def test_start_charging_at_home(self):
        adapter = _adapter(
            states={"binary_sensor.natalia_located_at_home": "on"}
        )
        outcome = await adapter.apply_tesla(TeslaAction(set_charging=True))
        assert outcome == APPLIED
        sc = adapter._service_caller
        assert sc.calls[0].service == "turn_on"

    @pytest.mark.asyncio
    async def test_set_charge_limit(self):
        adapter = _adapter(
            states={"binary_sensor.natalia_located_at_home": "on"}
        )
        outcome = await adapter.apply_tesla(
            TeslaAction(set_charge_limit_pct=85)
        )
        assert outcome == APPLIED
        sc = adapter._service_caller
        assert sc.calls[0].domain == "number"
        assert sc.calls[0].service == "set_value"
        assert sc.calls[0].entity_id == "number.natalia_charge_limit"
        assert sc.calls[0].data == {"value": 85.0}

    @pytest.mark.asyncio
    async def test_set_charge_current(self):
        adapter = _adapter(
            states={"binary_sensor.natalia_located_at_home": "on"}
        )
        outcome = await adapter.apply_tesla(
            TeslaAction(set_charge_current_a=16)
        )
        assert outcome == APPLIED
        sc = adapter._service_caller
        assert sc.calls[0].entity_id == "number.natalia_charge_current"
        assert sc.calls[0].data == {"value": 16.0}

    @pytest.mark.asyncio
    async def test_combined_action_applies_all(self):
        adapter = _adapter(
            states={"binary_sensor.natalia_located_at_home": "on"}
        )
        outcome = await adapter.apply_tesla(
            TeslaAction(
                set_charging=True,
                set_charge_limit_pct=75,
                set_charge_current_a=20,
            )
        )
        assert outcome == APPLIED
        sc = adapter._service_caller
        assert len(sc.calls) == 3

    @pytest.mark.asyncio
    async def test_skipped_when_not_at_home(self):
        adapter = _adapter(
            states={"binary_sensor.natalia_located_at_home": "off"}
        )
        outcome = await adapter.apply_tesla(TeslaAction(set_charging=False))
        assert outcome == SKIPPED_NOT_AT_HOME
        assert adapter._service_caller.calls == []

    @pytest.mark.asyncio
    async def test_skipped_when_at_home_unknown(self):
        # Missing/unknown counts as not-at-home.
        adapter = _adapter(
            states={"binary_sensor.natalia_located_at_home": "unknown"}
        )
        outcome = await adapter.apply_tesla(TeslaAction(set_charging=False))
        assert outcome == SKIPPED_NOT_AT_HOME

    @pytest.mark.asyncio
    async def test_no_op_when_no_intent(self):
        adapter = _adapter(
            states={"binary_sensor.natalia_located_at_home": "on"}
        )
        outcome = await adapter.apply_tesla(TeslaAction())
        assert outcome == NO_OP

    @pytest.mark.asyncio
    async def test_skipped_when_tesla_not_configured(self):
        adapter = _adapter(tesla=False)
        outcome = await adapter.apply_tesla(TeslaAction(set_charging=False))
        assert outcome == SKIPPED_NO_CONFIG


# ── Appliance writes ───────────────────────────────────────────────


class TestApplyAppliances:
    @pytest.mark.asyncio
    async def test_turn_off(self):
        adapter = _adapter()
        outcome = await adapter.apply_appliances(
            ApplianceAction(turn_off=("washer",))
        )
        assert outcome == APPLIED
        sc = adapter._service_caller
        assert sc.calls[0].service == "turn_off"
        assert sc.calls[0].entity_id == "switch.washer"

    @pytest.mark.asyncio
    async def test_turn_on(self):
        adapter = _adapter()
        outcome = await adapter.apply_appliances(
            ApplianceAction(turn_on=("dryer",))
        )
        assert outcome == APPLIED
        sc = adapter._service_caller
        assert sc.calls[0].service == "turn_on"
        assert sc.calls[0].entity_id == "switch.dryer"

    @pytest.mark.asyncio
    async def test_unknown_appliance_skipped_silently(self):
        adapter = _adapter()
        outcome = await adapter.apply_appliances(
            ApplianceAction(turn_off=("nonexistent",))
        )
        assert outcome == APPLIED  # adapter completes; just logs the skip
        assert adapter._service_caller.calls == []

    @pytest.mark.asyncio
    async def test_no_op_when_empty(self):
        adapter = _adapter()
        outcome = await adapter.apply_appliances(ApplianceAction())
        assert outcome == NO_OP
