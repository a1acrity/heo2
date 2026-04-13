# tests/test_mqtt_writer.py
"""Tests for MQTT diff-and-write logic."""

import asyncio
from datetime import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from heo2.models import SlotConfig, ProgrammeState, SlotWrite
from heo2.mqtt_writer import MqttWriter, MqttWriteResult


class TestDiffProgramme:
    def test_no_changes_returns_empty(self):
        current = ProgrammeState(
            slots=[
                SlotConfig(time(0, 0), time(5, 30), 100, True),
                SlotConfig(time(5, 30), time(18, 30), 100, False),
                SlotConfig(time(18, 30), time(23, 30), 20, False),
                SlotConfig(time(23, 30), time(23, 57), 100, True),
                SlotConfig(time(23, 57), time(23, 58), 20, False),
                SlotConfig(time(23, 58), time(0, 0), 20, False),
            ],
            reason_log=[],
        )
        new = ProgrammeState(
            slots=[s.__class__(s.start_time, s.end_time, s.capacity_soc, s.grid_charge)
                   for s in current.slots],
            reason_log=[],
        )
        writer = MqttWriter(client=MagicMock(), base_topic="solar_assistant")
        writes = writer.diff(current, new)
        assert writes == []

    def test_soc_change_detected(self):
        current = ProgrammeState(
            slots=[
                SlotConfig(time(0, 0), time(5, 30), 100, True),
                SlotConfig(time(5, 30), time(18, 30), 100, False),
                SlotConfig(time(18, 30), time(23, 30), 20, False),
                SlotConfig(time(23, 30), time(23, 57), 100, True),
                SlotConfig(time(23, 57), time(23, 58), 20, False),
                SlotConfig(time(23, 58), time(0, 0), 20, False),
            ],
            reason_log=[],
        )
        new = ProgrammeState(
            slots=[
                SlotConfig(time(0, 0), time(5, 30), 80, True),
                SlotConfig(time(5, 30), time(18, 30), 100, False),
                SlotConfig(time(18, 30), time(23, 30), 20, False),
                SlotConfig(time(23, 30), time(23, 57), 80, True),
                SlotConfig(time(23, 57), time(23, 58), 20, False),
                SlotConfig(time(23, 58), time(0, 0), 20, False),
            ],
            reason_log=[],
        )
        writer = MqttWriter(client=MagicMock(), base_topic="solar_assistant")
        writes = writer.diff(current, new)
        soc_writes = [w for w in writes if w.capacity_soc is not None]
        assert len(soc_writes) == 2
        assert soc_writes[0].slot_number == 1
        assert soc_writes[0].capacity_soc == 80

    def test_time_change_detected(self):
        current = ProgrammeState(
            slots=[
                SlotConfig(time(0, 0), time(5, 30), 100, True),
                SlotConfig(time(5, 30), time(18, 30), 100, False),
                SlotConfig(time(18, 30), time(23, 30), 20, False),
                SlotConfig(time(23, 30), time(23, 57), 100, True),
                SlotConfig(time(23, 57), time(23, 58), 20, False),
                SlotConfig(time(23, 58), time(0, 0), 20, False),
            ],
            reason_log=[],
        )
        new = ProgrammeState(
            slots=[
                SlotConfig(time(0, 0), time(6, 0), 100, True),
                SlotConfig(time(6, 0), time(18, 30), 100, False),
                SlotConfig(time(18, 30), time(23, 30), 20, False),
                SlotConfig(time(23, 30), time(23, 57), 100, True),
                SlotConfig(time(23, 57), time(23, 58), 20, False),
                SlotConfig(time(23, 58), time(0, 0), 20, False),
            ],
            reason_log=[],
        )
        writer = MqttWriter(client=MagicMock(), base_topic="solar_assistant")
        writes = writer.diff(current, new)
        time_writes = [w for w in writes if w.time_point is not None]
        assert len(time_writes) >= 1

    def test_grid_charge_change_detected(self):
        current = ProgrammeState(
            slots=[
                SlotConfig(time(0, 0), time(5, 30), 100, True),
                SlotConfig(time(5, 30), time(18, 30), 100, False),
                SlotConfig(time(18, 30), time(23, 30), 20, False),
                SlotConfig(time(23, 30), time(23, 57), 100, True),
                SlotConfig(time(23, 57), time(23, 58), 20, False),
                SlotConfig(time(23, 58), time(0, 0), 20, False),
            ],
            reason_log=[],
        )
        new = ProgrammeState(
            slots=[
                SlotConfig(time(0, 0), time(5, 30), 100, True),
                SlotConfig(time(5, 30), time(18, 30), 100, True),
                SlotConfig(time(18, 30), time(23, 30), 20, False),
                SlotConfig(time(23, 30), time(23, 57), 100, True),
                SlotConfig(time(23, 57), time(23, 58), 20, False),
                SlotConfig(time(23, 58), time(0, 0), 20, False),
            ],
            reason_log=[],
        )
        writer = MqttWriter(client=MagicMock(), base_topic="solar_assistant")
        writes = writer.diff(current, new)
        gc_writes = [w for w in writes if w.grid_charge is not None]
        assert len(gc_writes) == 1
        assert gc_writes[0].slot_number == 2
        assert gc_writes[0].grid_charge is True


class TestWriteRegisters:
    @pytest.mark.asyncio
    async def test_writes_consecutively(self):
        """Each register write must complete before the next starts."""
        call_order = []

        async def mock_publish(topic, payload):
            call_order.append(("pub", topic))

        async def mock_wait_readback(topic, expected, timeout):
            call_order.append(("read", topic))
            return True

        writer = MqttWriter(client=MagicMock(), base_topic="solar_assistant")
        writer._publish = mock_publish
        writer._wait_for_readback = mock_wait_readback

        writes = [
            SlotWrite(slot_number=1, capacity_soc=80),
            SlotWrite(slot_number=2, grid_charge=True),
        ]

        result = await writer.write_registers(writes)
        assert result.success is True
        assert call_order[0][0] == "pub"
        assert call_order[1][0] == "read"
        assert call_order[2][0] == "pub"
        assert call_order[3][0] == "read"

    @pytest.mark.asyncio
    async def test_retries_on_readback_failure(self):
        """Retries up to 3 times on read-back timeout."""
        attempt_count = 0

        async def mock_publish(topic, payload):
            pass

        async def mock_wait_readback(topic, expected, timeout):
            nonlocal attempt_count
            attempt_count += 1
            return attempt_count >= 3

        writer = MqttWriter(client=MagicMock(), base_topic="solar_assistant")
        writer._publish = mock_publish
        writer._wait_for_readback = mock_wait_readback

        writes = [SlotWrite(slot_number=1, capacity_soc=80)]
        result = await writer.write_registers(writes)
        assert result.success is True
        assert attempt_count == 3

    @pytest.mark.asyncio
    async def test_aborts_on_persistent_failure(self):
        """After 3 failed retries, abort remaining writes."""
        async def mock_publish(topic, payload):
            pass

        async def mock_wait_readback(topic, expected, timeout):
            return False

        writer = MqttWriter(client=MagicMock(), base_topic="solar_assistant")
        writer._publish = mock_publish
        writer._wait_for_readback = mock_wait_readback

        writes = [
            SlotWrite(slot_number=1, capacity_soc=80),
            SlotWrite(slot_number=2, capacity_soc=60),
        ]
        result = await writer.write_registers(writes)
        assert result.success is False
        assert result.failed_slot == 1

    @pytest.mark.asyncio
    async def test_dry_run_logs_without_publishing(self):
        """Dry-run mode: log what would be written, don't publish."""
        published = []

        async def mock_publish(topic, payload):
            published.append(topic)

        writer = MqttWriter(client=MagicMock(), base_topic="solar_assistant", dry_run=True)
        writer._publish = mock_publish

        writes = [SlotWrite(slot_number=1, capacity_soc=80)]
        result = await writer.write_registers(writes)
        assert result.success is True
        assert len(published) == 0
        assert len(result.dry_run_log) == 1


class TestTopicGeneration:
    def test_capacity_topic(self):
        writer = MqttWriter(client=MagicMock(), base_topic="solar_assistant")
        assert writer._topic(3, "capacity") == "solar_assistant/inverter_1/prog3_capacity/set"

    def test_time_topic(self):
        writer = MqttWriter(client=MagicMock(), base_topic="solar_assistant")
        assert writer._topic(1, "time") == "solar_assistant/inverter_1/prog1_time/set"

    def test_charge_topic(self):
        writer = MqttWriter(client=MagicMock(), base_topic="solar_assistant")
        assert writer._topic(6, "charge") == "solar_assistant/inverter_1/prog6_charge/set"
