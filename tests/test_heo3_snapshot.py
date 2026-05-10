"""Operator.snapshot() integration tests.

Confirms that snapshot() composes all three adapters concurrently
into a frozen Snapshot with every field populated.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from heo3.adapters.peripheral import TeslaConfig
from heo3.adapters.world import (
    BDConfig,
    FlagsConfig,
    LoadModelConfig,
    MockLoadHistoryReader,
    SolcastConfig,
)
from heo3.operator import Operator
from heo3.service_caller import MockServiceCaller
from heo3.state_reader import MockStateReader
from heo3.transport import MockTransport


METER_KEY = "18p5009498_2372761090617"
PREFIX = "sensor.sa_inverter_1_"


def _full_states():
    """A populated state dict covering inverter + peripherals + world."""
    bd = BDConfig.from_meter_key(METER_KEY)
    cfg = FlagsConfig(
        igo_dispatching_entity="binary_sensor.octopus_intelligent_dispatching",
        saving_session_entity="binary_sensor.octoplus_saving_sessions",
    )
    states = {
        # Inverter telemetry
        f"{PREFIX}battery_soc": "82",
        f"{PREFIX}battery_power": "-1500",
        f"{PREFIX}grid_voltage": "240.5",
        f"{PREFIX}grid_power": "200",
        # Inverter settings
        f"{PREFIX}work_mode": "Selling first",
        f"{PREFIX}energy_pattern": "Battery first",
        f"{PREFIX}max_charge_current": "100",
        f"{PREFIX}max_discharge_current": "100",
        # All 6 slots
        **{
            f"{PREFIX}time_point_{n}": t
            for n, t in zip(range(1, 7), ["00:00", "05:00", "11:00", "16:00", "19:00", "22:00"])
        },
        **{
            f"{PREFIX}grid_charge_point_{n}": "false" for n in range(1, 7)
        },
        **{
            f"{PREFIX}capacity_point_{n}": str(c)
            for n, c in zip(range(1, 7), [20, 80, 100, 100, 25, 10])
        },
        # Peripherals
        "select.zappi_charge_mode": "Eco",
        "binary_sensor.natalia_located_at_home": "on",
        "sensor.natalia_battery_level": "82",
        # Rates current
        bd.import_current_rate: "0.285844",
        bd.export_current_rate: "0.1134",
        # Freshness (event entity state)
        bd.import_day_rates: datetime.now(timezone.utc).isoformat(),
        bd.export_day_rates: datetime.now(timezone.utc).isoformat(),
        # Flags
        cfg.igo_dispatching_entity: "off",
        cfg.saving_session_entity: "off",
        # Config tunables
        "number.heo3_min_soc": "12",
        "number.heo3_cycle_budget": "1.5",
        "number.heo3_target_end_soc": "30",
    }
    attrs = {
        bd.import_day_rates: {
            "rates": [
                {
                    "start": "2026-05-10T00:00:00+01:00",
                    "end": "2026-05-10T00:30:00+01:00",
                    "value_inc_vat": 0.05,
                }
            ]
        },
    }
    return states, attrs, bd, cfg


async def _make_op():
    transport = MockTransport()
    await transport.connect()
    states, attrs, bd, fcfg = _full_states()
    return Operator(
        transport=transport,
        state_reader=MockStateReader(states, attrs),
        service_caller=MockServiceCaller(),
        bd_config=bd,
        flags_config=fcfg,
        solcast_config=SolcastConfig(),
        load_model_config=LoadModelConfig(),
        load_history_reader=MockLoadHistoryReader([]),
        tesla_entity_prefix="natalia",
    )


class TestSnapshotComposition:
    @pytest.mark.asyncio
    async def test_snapshot_runs_all_reads(self):
        op = await _make_op()
        snap = await op.snapshot()

        # Inverter live state populated
        assert snap.inverter.battery_soc_pct == 82.0
        assert snap.inverter.grid_voltage_v == 240.5

        # Inverter settings populated
        assert snap.inverter_settings.work_mode == "Selling first"
        assert len(snap.inverter_settings.slots) == 6
        assert snap.inverter_settings.slots[0].capacity_pct == 20

        # Peripherals
        assert snap.ev.mode == "Eco"
        assert snap.tesla is not None
        assert snap.tesla.soc_pct == 82.0
        assert snap.tesla.located_at_home is True

        # Rates
        assert snap.rates_live.import_current_pence == pytest.approx(28.5844)
        assert len(snap.rates_live.import_today) == 1

        # Freshness recorded
        assert "import_today" in snap.rates_freshness

        # Flags
        assert snap.flags.igo_dispatching is False
        assert snap.flags.eps_active is False

        # Config from HA tunables
        assert snap.config.min_soc == 12
        assert snap.config.cycle_budget == 1.5
        assert snap.config.target_end_soc == 30

    @pytest.mark.asyncio
    async def test_snapshot_frozen(self):
        op = await _make_op()
        snap = await op.snapshot()
        with pytest.raises(Exception):  # FrozenInstanceError
            snap.captured_at = datetime.now(timezone.utc)  # type: ignore[misc]

    @pytest.mark.asyncio
    async def test_captured_at_is_utc(self):
        op = await _make_op()
        snap = await op.snapshot()
        assert snap.captured_at.tzinfo is not None
        assert snap.captured_at.tzinfo.utcoffset(snap.captured_at).total_seconds() == 0

    @pytest.mark.asyncio
    async def test_default_config_used_when_entities_missing(self):
        # Operator with no state for the config entities falls back to
        # SystemConfig defaults.
        transport = MockTransport()
        await transport.connect()
        op = Operator(
            transport=transport,
            state_reader=MockStateReader({}),  # no config entities
            service_caller=MockServiceCaller(),
            load_history_reader=MockLoadHistoryReader([]),
        )
        snap = await op.snapshot()
        assert snap.config.min_soc == 10  # SystemConfig default


# ── apply() pre-flight gates wired from snapshot ──────────────────


class TestApplyWithSnapshot:
    @pytest.mark.asyncio
    async def test_eps_active_blocks_writes(self):
        op = await _make_op()
        snap = await op.snapshot()
        # Forge an EPS-active snapshot (replace flags via dataclass replace).
        from dataclasses import replace
        from heo3.types import SystemFlags

        eps_flags = SystemFlags(eps_active=True, grid_connected=False)
        snap_eps = replace(snap, flags=eps_flags)

        from heo3.types import PlannedAction

        result = await op.apply(
            PlannedAction(work_mode="Selling first"),
            snapshot=snap_eps,
        )
        assert result.succeeded == ()
        assert "SPEC H3" in result.failed[0].reason

    @pytest.mark.asyncio
    async def test_stale_rates_block_writes(self):
        op = await _make_op()
        snap = await op.snapshot()
        # Forge stale freshness (10 hours old).
        from dataclasses import replace

        old = datetime.now(timezone.utc) - timedelta(hours=10)
        snap_stale = replace(
            snap, rates_freshness={"import_today": old}
        )

        from heo3.types import PlannedAction

        result = await op.apply(
            PlannedAction(work_mode="Selling first"),
            snapshot=snap_stale,
        )
        assert result.succeeded == ()
        assert "SPEC H4" in result.failed[0].reason

    @pytest.mark.asyncio
    async def test_fresh_rates_allow_writes(self):
        # With fresh snapshot, apply() reaches the safety/diff path.
        # Empty action means no writes — successful empty result.
        op = await _make_op()
        snap = await op.snapshot()
        from heo3.types import PlannedAction

        result = await op.apply(PlannedAction(), snapshot=snap)
        assert result.failed == ()
