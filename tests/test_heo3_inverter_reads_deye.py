"""InverterAdapter Deye-Sunsynk read-back path tests."""

from __future__ import annotations

import pytest

from heo3.adapters.inverter import InverterAdapter
from heo3.state_reader import MockStateReader
from heo3.transport import MockTransport


PREFIX = "deye_sunsynk_sol_ark_"


def _full_deye_states():
    states = {
        f"select.{PREFIX}work_mode": "Selling first",
        f"select.{PREFIX}energy_pattern": "Battery first",
        f"number.{PREFIX}max_charge_current": "100",
        f"number.{PREFIX}max_discharge_current": "100",
    }
    for n, (start, gc, cap) in enumerate(
        [
            ("00:00", "on", "80"),
            ("05:30", "off", "100"),
            ("11:00", "off", "100"),
            ("16:00", "off", "100"),
            ("19:00", "off", "25"),
            ("23:30", "off", "25"),
        ],
        start=1,
    ):
        states[f"select.{PREFIX}time_point_{n}"] = start
        states[f"switch.{PREFIX}grid_charge_point_{n}"] = gc
        states[f"number.{PREFIX}capacity_point_{n}"] = cap
    return states


class TestDeyeReadPath:
    @pytest.mark.asyncio
    async def test_full_deye_settings_parsed(self):
        adapter = InverterAdapter(
            transport=MockTransport(),
            inverter_name="inverter_1",
            state_reader=MockStateReader(_full_deye_states()),
            deye_settings_prefix=PREFIX,
        )
        s = await adapter.read_settings()
        assert s.work_mode == "Selling first"
        assert s.energy_pattern == "Battery first"
        assert s.max_charge_a == 100.0
        assert s.max_discharge_a == 100.0
        assert s.slots[0].start_hhmm == "00:00"
        assert s.slots[0].grid_charge is True
        assert s.slots[0].capacity_pct == 80
        assert s.slots[5].start_hhmm == "23:30"
        assert s.slots[5].capacity_pct == 25

    @pytest.mark.asyncio
    async def test_falls_back_to_sa_when_deye_work_mode_missing(self):
        # Deye prefix configured but its work_mode select returns None.
        sa_prefix = "sensor.sa_inverter_1_"
        sa_states = {
            f"{sa_prefix}work_mode": "Zero export to CT",
            f"{sa_prefix}energy_pattern": "Load first",
            f"{sa_prefix}max_charge_current": "50",
            f"{sa_prefix}max_discharge_current": "50",
            **{
                f"{sa_prefix}time_point_{n}": "00:00" for n in range(1, 7)
            },
            **{
                f"{sa_prefix}grid_charge_point_{n}": "false" for n in range(1, 7)
            },
            **{
                f"{sa_prefix}capacity_point_{n}": "10" for n in range(1, 7)
            },
        }
        adapter = InverterAdapter(
            transport=MockTransport(),
            inverter_name="inverter_1",
            state_reader=MockStateReader(sa_states),
            deye_settings_prefix=PREFIX,  # Deye configured but no entities
        )
        s = await adapter.read_settings()
        # Came from SA fallback.
        assert s.work_mode == "Zero export to CT"
        assert s.max_charge_a == 50.0

    @pytest.mark.asyncio
    async def test_no_deye_prefix_uses_sa_directly(self):
        sa_prefix = "sensor.sa_inverter_1_"
        sa_states = {
            f"{sa_prefix}work_mode": "Zero export to CT",
            f"{sa_prefix}energy_pattern": "Load first",
            f"{sa_prefix}max_charge_current": "50",
            f"{sa_prefix}max_discharge_current": "50",
            **{
                f"{sa_prefix}time_point_{n}": "00:00" for n in range(1, 7)
            },
            **{
                f"{sa_prefix}grid_charge_point_{n}": "false" for n in range(1, 7)
            },
            **{
                f"{sa_prefix}capacity_point_{n}": "10" for n in range(1, 7)
            },
        }
        adapter = InverterAdapter(
            transport=MockTransport(),
            inverter_name="inverter_1",
            state_reader=MockStateReader(sa_states),
            # no deye prefix
        )
        s = await adapter.read_settings()
        assert s.work_mode == "Zero export to CT"
