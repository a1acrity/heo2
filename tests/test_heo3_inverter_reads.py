"""InverterAdapter read_state() / read_settings() tests."""

from __future__ import annotations

import pytest

from heo3.adapters.inverter import InverterAdapter
from heo3.state_reader import MockStateReader
from heo3.transport import MockTransport


PREFIX = "sensor.sa_inverter_1_"


def _full_telemetry_states() -> dict[str, str]:
    return {
        f"{PREFIX}battery_soc": "82",
        f"{PREFIX}battery_power": "-1500",  # discharging
        f"{PREFIX}battery_current": "-30.5",
        f"{PREFIX}battery_voltage": "51.2",
        f"{PREFIX}grid_power": "200",  # importing
        f"{PREFIX}grid_voltage": "240.5",
        f"{PREFIX}grid_frequency": "50.01",
        f"{PREFIX}solar_power": "0",
        f"{PREFIX}load_power": "1700",
        f"{PREFIX}inverter_temperature": "32.5",
        f"{PREFIX}battery_temperature": "21.0",
    }


def _full_settings_states() -> dict[str, str]:
    base = {
        f"{PREFIX}work_mode": "Selling first",
        f"{PREFIX}energy_pattern": "Battery first",
        f"{PREFIX}max_charge_current": "100",
        f"{PREFIX}max_discharge_current": "100",
    }
    for n, (start, gc, cap) in enumerate(
        [
            ("00:00", "false", "20"),
            ("05:00", "true", "80"),
            ("11:00", "false", "100"),
            ("16:00", "false", "100"),
            ("19:00", "false", "25"),
            ("22:00", "false", "10"),
        ],
        start=1,
    ):
        base[f"{PREFIX}time_point_{n}"] = start
        base[f"{PREFIX}grid_charge_point_{n}"] = gc
        base[f"{PREFIX}capacity_point_{n}"] = cap
    return base


@pytest.fixture
def reader_full():
    return MockStateReader(
        states={**_full_telemetry_states(), **_full_settings_states()}
    )


@pytest.fixture
def adapter_with(reader_full):
    return InverterAdapter(
        transport=MockTransport(),
        inverter_name="inverter_1",
        state_reader=reader_full,
    )


class TestReadState:
    @pytest.mark.asyncio
    async def test_full_telemetry_parsed(self, adapter_with):
        state = await adapter_with.read_state()
        assert state.battery_soc_pct == 82.0
        assert state.battery_power_w == -1500.0
        assert state.battery_current_a == -30.5
        assert state.battery_voltage_v == 51.2
        assert state.grid_power_w == 200.0
        assert state.grid_voltage_v == 240.5
        assert state.grid_frequency_hz == 50.01
        assert state.solar_power_w == 0.0
        assert state.load_power_w == 1700.0
        assert state.inverter_temperature_c == 32.5
        assert state.battery_temperature_c == 21.0

    @pytest.mark.asyncio
    async def test_missing_sensors_become_none(self):
        # Empty reader — every field is missing.
        adapter = InverterAdapter(
            transport=MockTransport(),
            inverter_name="inverter_1",
            state_reader=MockStateReader(),
        )
        state = await adapter.read_state()
        assert state.battery_soc_pct is None
        assert state.grid_voltage_v is None
        assert state.solar_power_w is None

    @pytest.mark.asyncio
    async def test_unavailable_state_becomes_none(self):
        reader = MockStateReader(
            {
                f"{PREFIX}battery_soc": "unavailable",
                f"{PREFIX}grid_voltage": "unknown",
            }
        )
        adapter = InverterAdapter(
            transport=MockTransport(),
            inverter_name="inverter_1",
            state_reader=reader,
        )
        state = await adapter.read_state()
        assert state.battery_soc_pct is None
        assert state.grid_voltage_v is None

    @pytest.mark.asyncio
    async def test_state_reader_required(self):
        adapter = InverterAdapter(
            transport=MockTransport(), inverter_name="inverter_1"
        )
        with pytest.raises(RuntimeError, match="state_reader"):
            await adapter.read_state()


class TestReadSettings:
    @pytest.mark.asyncio
    async def test_full_settings_parsed(self, adapter_with):
        s = await adapter_with.read_settings()
        assert s.work_mode == "Selling first"
        assert s.energy_pattern == "Battery first"
        assert s.max_charge_a == 100.0
        assert s.max_discharge_a == 100.0
        assert len(s.slots) == 6

        # Slot 1: 00:00, gc=False, cap=20
        assert s.slots[0].start_hhmm == "00:00"
        assert s.slots[0].grid_charge is False
        assert s.slots[0].capacity_pct == 20

        # Slot 2: 05:00, gc=True, cap=80
        assert s.slots[1].start_hhmm == "05:00"
        assert s.slots[1].grid_charge is True
        assert s.slots[1].capacity_pct == 80

        # Slot 6: 22:00, gc=False, cap=10
        assert s.slots[5].start_hhmm == "22:00"
        assert s.slots[5].grid_charge is False
        assert s.slots[5].capacity_pct == 10

    @pytest.mark.asyncio
    async def test_missing_globals_default_safely(self):
        # No HA states at all — defaults shouldn't crash, should produce
        # benign placeholders that diff-against-anything will trigger
        # writes for.
        adapter = InverterAdapter(
            transport=MockTransport(),
            inverter_name="inverter_1",
            state_reader=MockStateReader(),
        )
        s = await adapter.read_settings()
        assert s.work_mode == ""
        assert s.energy_pattern == ""
        assert s.max_charge_a == 0.0
        assert s.max_discharge_a == 0.0
        assert len(s.slots) == 6
        for slot in s.slots:
            assert slot.start_hhmm == "00:00"
            assert slot.grid_charge is False
            assert slot.capacity_pct == 0

    @pytest.mark.asyncio
    async def test_state_reader_required(self):
        adapter = InverterAdapter(
            transport=MockTransport(), inverter_name="inverter_1"
        )
        with pytest.raises(RuntimeError, match="state_reader"):
            await adapter.read_settings()


class TestEntityNaming:
    def test_default_sensor_prefix_uses_inverter_name(self):
        adapter = InverterAdapter(
            transport=MockTransport(),
            inverter_name="inverter_1",
            state_reader=MockStateReader(),
        )
        assert adapter._sensor_prefix == "sensor.sa_inverter_1_"

    def test_custom_prefix_overrides(self):
        adapter = InverterAdapter(
            transport=MockTransport(),
            inverter_name="inverter_1",
            state_reader=MockStateReader(),
            sensor_prefix="sensor.custom_",
        )
        assert adapter._sensor_prefix == "sensor.custom_"


class TestRoundTripWritesFor:
    """Read settings → use as diff baseline for writes_for() → no-op."""

    @pytest.mark.asyncio
    async def test_read_then_write_same_is_no_op(self, adapter_with):
        from heo3.types import PlannedAction

        current = await adapter_with.read_settings()
        # Asking for the same work_mode that's currently set.
        action = PlannedAction(work_mode=current.work_mode)
        assert adapter_with.writes_for(action, current=current) == ()
