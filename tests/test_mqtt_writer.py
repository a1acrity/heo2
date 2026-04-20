# tests/test_mqtt_writer.py
"""Tests for MQTT diff-and-write logic.

The writer publishes to Solar Assistant's `/set` topics and waits for
SA's `response_message/state` topic to say "Saved" or "Error: ...".

Tests use a FakeTransport to script responses without touching real MQTT.
"""

import asyncio
from datetime import time
from unittest.mock import MagicMock

import pytest

from heo2.models import SlotConfig, ProgrammeState, SlotWrite
from heo2.mqtt_writer import (
    MqttWriter,
    MqttWriteResult,
    parse_response_message,
    format_grid_charge,
    format_time,
)


# -----------------------------------------------------------------------
# Fake transport: scripts response_message replies for publish calls.
# -----------------------------------------------------------------------

class FakeTransport:
    """Async MQTT transport stub.

    Records every publish. After each publish, optionally dispatches a
    scripted response to whoever is subscribed to response_message/state.

    Usage:
        transport = FakeTransport()
        transport.script_responses([
            "Set 'Capacity point 1' to '95': Saved.",
            "Set 'Grid charge point 2' to 'Enabled': Saved.",
        ])
    """

    def __init__(self):
        self.published: list[tuple[str, str]] = []
        self._subscribers: dict[str, list] = {}
        self._scripted: list[str | None] = []  # None means "no response"

    def script_responses(self, responses: list[str | None]) -> None:
        self._scripted = list(responses)

    async def publish(self, topic: str, payload: str) -> None:
        self.published.append((topic, payload))
        # Simulate SA receiving the publish, processing it, and replying.
        if self._scripted:
            reply = self._scripted.pop(0)
            if reply is not None:
                await self._deliver("solar_assistant/set/response_message/state", reply)

    async def _deliver(self, topic: str, payload: str) -> None:
        # Yield once so the waiting coroutine can attach its future first.
        await asyncio.sleep(0)
        for cb in self._subscribers.get(topic, []):
            result = cb(topic, payload)
            if asyncio.iscoroutine(result):
                await result

    async def subscribe(self, topic: str, callback) -> callable:
        self._subscribers.setdefault(topic, []).append(callback)
        def _unsub():
            if callback in self._subscribers.get(topic, []):
                self._subscribers[topic].remove(callback)
        return _unsub


# Helper to build a consistent baseline programme
def _baseline_programme(**overrides) -> ProgrammeState:
    slots = [
        SlotConfig(time(0, 0), time(5, 30), 100, True),
        SlotConfig(time(5, 30), time(18, 30), 100, False),
        SlotConfig(time(18, 30), time(23, 30), 20, False),
        SlotConfig(time(23, 30), time(23, 57), 100, True),
        SlotConfig(time(23, 57), time(23, 58), 20, False),
        SlotConfig(time(23, 58), time(0, 0), 20, False),
    ]
    return ProgrammeState(slots=slots, reason_log=[])


# -----------------------------------------------------------------------
# Diff tests - pure logic, no transport needed
# -----------------------------------------------------------------------

class TestDiffProgramme:
    def test_no_changes_returns_empty(self):
        current = _baseline_programme()
        new = _baseline_programme()
        writer = MqttWriter(client=MagicMock())
        assert writer.diff(current, new) == []

    def test_soc_change_detected(self):
        current = _baseline_programme()
        new = _baseline_programme()
        new.slots[0] = SlotConfig(time(0, 0), time(5, 30), 80, True)
        writer = MqttWriter(client=MagicMock())
        writes = writer.diff(current, new)
        assert len(writes) == 1
        assert writes[0].slot_number == 1
        assert writes[0].capacity_soc == 80
        assert writes[0].time_point is None
        assert writes[0].grid_charge is None

    def test_time_change_detected(self):
        current = _baseline_programme()
        new = _baseline_programme()
        new.slots[1] = SlotConfig(time(5, 30), time(19, 0), 100, False)
        writer = MqttWriter(client=MagicMock())
        writes = writer.diff(current, new)
        assert len(writes) == 1
        assert writes[0].slot_number == 2
        assert writes[0].time_point == "19:00"
        assert writes[0].capacity_soc is None

    def test_grid_charge_change_detected(self):
        current = _baseline_programme()
        new = _baseline_programme()
        new.slots[2] = SlotConfig(time(18, 30), time(23, 30), 20, True)
        writer = MqttWriter(client=MagicMock())
        writes = writer.diff(current, new)
        assert len(writes) == 1
        assert writes[0].slot_number == 3
        assert writes[0].grid_charge is True
        assert writes[0].capacity_soc is None


# -----------------------------------------------------------------------
# Topic generation - SA's real naming scheme
# -----------------------------------------------------------------------

class TestTopicGeneration:
    def test_capacity_topic_is_capacity_point_n(self):
        writer = MqttWriter(client=MagicMock())
        assert writer._set_topic("capacity", 3) == "solar_assistant/inverter_1/capacity_point_3/set"

    def test_time_topic_is_time_point_n(self):
        writer = MqttWriter(client=MagicMock())
        assert writer._set_topic("time", 1) == "solar_assistant/inverter_1/time_point_1/set"

    def test_grid_charge_topic_is_grid_charge_point_n(self):
        writer = MqttWriter(client=MagicMock())
        assert writer._set_topic("grid_charge", 6) == "solar_assistant/inverter_1/grid_charge_point_6/set"

    def test_custom_inverter_name(self):
        """A second SA instance (e.g. for inverter 2 via RS232) could
        use a different inverter_name. Writer must support that."""
        writer = MqttWriter(client=MagicMock(), inverter_name="inverter_2")
        assert writer._set_topic("capacity", 1) == "solar_assistant/inverter_2/capacity_point_1/set"

    def test_response_topic(self):
        writer = MqttWriter(client=MagicMock())
        assert writer._response_topic() == "solar_assistant/set/response_message/state"


# -----------------------------------------------------------------------
# Formatters
# -----------------------------------------------------------------------

class TestFormatters:
    def test_format_grid_charge_true_is_Enabled(self):
        assert format_grid_charge(True) == "Enabled"

    def test_format_grid_charge_false_is_Disabled(self):
        assert format_grid_charge(False) == "Disabled"

    def test_format_time_accepts_time_object(self):
        assert format_time(time(23, 30)) == "23:30"

    def test_format_time_passes_strings_through(self):
        assert format_time("05:30") == "05:30"


# -----------------------------------------------------------------------
# parse_response_message - pure function covering SA's actual formats
# -----------------------------------------------------------------------

class TestParseResponseMessage:
    def test_saved_returns_success(self):
        ok, reason = parse_response_message(
            "Set 'Capacity point 1' to '97': Saved.",
            "Capacity point 1",
        )
        assert ok is True
        assert reason is None

    def test_saved_without_trailing_period(self):
        """Defensive - some SA variants may omit the trailing dot."""
        ok, reason = parse_response_message(
            "Set 'Capacity point 1' to '97': Saved",
            "Capacity point 1",
        )
        assert ok is True

    def test_error_no_response_is_failure(self):
        """Live SA log shows this error shape verbatim."""
        ok, reason = parse_response_message(
            "Set 'Grid charge' to 'Disabled': Error: No response.",
            "Grid charge",
        )
        assert ok is False
        assert reason == "No response"

    def test_error_with_explanation_captures_detail(self):
        ok, reason = parse_response_message(
            "Set 'Work mode' to 'Selling first': Error: Value rejected by inverter.",
            "Work mode",
        )
        assert ok is False
        assert "rejected" in reason

    def test_response_for_different_setting_is_failure(self):
        """If SA responds about something else, don't claim our success."""
        ok, reason = parse_response_message(
            "Set 'Energy pattern' to 'Battery first': Saved.",
            "Capacity point 1",
        )
        assert ok is False
        assert "different setting" in reason

    def test_empty_response_is_failure(self):
        ok, reason = parse_response_message("", "Capacity point 1")
        assert ok is False

    def test_malformed_response_is_failure(self):
        ok, reason = parse_response_message(
            "something else entirely",
            "Capacity point 1",
        )
        assert ok is False
        assert "malformed" in reason


# -----------------------------------------------------------------------
# write_registers integration - using FakeTransport
# -----------------------------------------------------------------------

class TestWriteRegisters:
    @pytest.mark.asyncio
    async def test_happy_path_capacity_write_confirmed(self):
        transport = FakeTransport()
        transport.script_responses([
            "Set 'Capacity point 1' to '80': Saved.",
        ])
        writer = MqttWriter(transport=transport)

        result = await writer.write_registers([
            SlotWrite(slot_number=1, capacity_soc=80),
        ])

        assert result.success is True
        assert result.writes_attempted == 1
        assert result.writes_confirmed == 1
        assert len(transport.published) == 1
        assert transport.published[0] == (
            "solar_assistant/inverter_1/capacity_point_1/set",
            "80",
        )

    @pytest.mark.asyncio
    async def test_grid_charge_serialised_as_Enabled_or_Disabled(self):
        transport = FakeTransport()
        transport.script_responses([
            "Set 'Grid charge point 2' to 'Enabled': Saved.",
        ])
        writer = MqttWriter(transport=transport)

        await writer.write_registers([
            SlotWrite(slot_number=2, grid_charge=True),
        ])

        _topic, payload = transport.published[0]
        assert payload == "Enabled"

    @pytest.mark.asyncio
    async def test_multiple_slots_published_in_order(self):
        transport = FakeTransport()
        transport.script_responses([
            "Set 'Capacity point 1' to '80': Saved.",
            "Set 'Grid charge point 3' to 'Enabled': Saved.",
        ])
        writer = MqttWriter(transport=transport)

        result = await writer.write_registers([
            SlotWrite(slot_number=1, capacity_soc=80),
            SlotWrite(slot_number=3, grid_charge=True),
        ])

        assert result.success is True
        assert result.writes_confirmed == 2
        assert len(transport.published) == 2
        # Writes happen in the order specified
        assert "capacity_point_1" in transport.published[0][0]
        assert "grid_charge_point_3" in transport.published[1][0]


    @pytest.mark.asyncio
    async def test_error_response_fails_immediately_no_retry(self):
        """When SA says 'Error: No response', the inverter already tried
        and rejected. No point retrying - fail fast."""
        transport = FakeTransport()
        transport.script_responses([
            "Set 'Capacity point 1' to '80': Error: No response.",
        ])
        writer = MqttWriter(transport=transport)

        result = await writer.write_registers([
            SlotWrite(slot_number=1, capacity_soc=80),
        ])

        assert result.success is False
        assert result.failed_slot == 1
        assert result.failed_param == "capacity"
        assert result.failed_reason == "No response"
        # Only one publish happened - no retry on Error
        assert len(transport.published) == 1

    @pytest.mark.asyncio
    async def test_timeout_triggers_retry_then_success(self, monkeypatch):
        """If no response arrives in time, retry up to MQTT_MAX_RETRIES."""
        # Shorten the timeout so the test finishes quickly
        monkeypatch.setattr(
            "heo2.mqtt_writer.MQTT_WRITE_TIMEOUT_SECONDS", 0.05
        )
        transport = FakeTransport()
        transport.script_responses([
            None,  # first attempt: no response
            "Set 'Capacity point 1' to '80': Saved.",  # second: success
        ])
        writer = MqttWriter(transport=transport)

        result = await writer.write_registers([
            SlotWrite(slot_number=1, capacity_soc=80),
        ])

        assert result.success is True
        # Two publishes: original + one retry
        assert len(transport.published) == 2

    @pytest.mark.asyncio
    async def test_all_retries_timeout_is_failure(self, monkeypatch):
        monkeypatch.setattr(
            "heo2.mqtt_writer.MQTT_WRITE_TIMEOUT_SECONDS", 0.05
        )
        monkeypatch.setattr("heo2.mqtt_writer.MQTT_MAX_RETRIES", 3)
        transport = FakeTransport()
        transport.script_responses([None, None, None])  # all silent
        writer = MqttWriter(transport=transport)

        result = await writer.write_registers([
            SlotWrite(slot_number=1, capacity_soc=80),
        ])

        assert result.success is False
        assert result.failed_slot == 1
        assert "no response" in (result.failed_reason or "").lower()
        assert len(transport.published) == 3


    @pytest.mark.asyncio
    async def test_later_slot_not_published_if_earlier_fails(self):
        """On failure of slot 1, slot 2 must not be attempted.
        Partial writes would leave the inverter in an inconsistent state."""
        transport = FakeTransport()
        transport.script_responses([
            "Set 'Capacity point 1' to '80': Error: Inverter rejected.",
            # No second response scripted - we should never get here
        ])
        writer = MqttWriter(transport=transport)

        result = await writer.write_registers([
            SlotWrite(slot_number=1, capacity_soc=80),
            SlotWrite(slot_number=2, capacity_soc=60),
        ])

        assert result.success is False
        assert result.failed_slot == 1
        # Slot 2 never got published
        assert len(transport.published) == 1
        assert "capacity_point_1" in transport.published[0][0]

    @pytest.mark.asyncio
    async def test_dry_run_logs_without_publishing(self):
        transport = FakeTransport()
        writer = MqttWriter(transport=transport, dry_run=True)

        result = await writer.write_registers([
            SlotWrite(slot_number=1, capacity_soc=80),
        ])

        assert result.success is True
        assert result.writes_confirmed == 1
        assert len(transport.published) == 0
        assert len(result.dry_run_log) == 1
        assert "Would publish" in result.dry_run_log[0]
        assert "capacity_point_1" in result.dry_run_log[0]

    @pytest.mark.asyncio
    async def test_dry_run_empty_writes_still_succeeds(self):
        """No diffs means no writes means still a success."""
        transport = FakeTransport()
        writer = MqttWriter(transport=transport, dry_run=True)
        result = await writer.write_registers([])
        assert result.success is True
        assert result.writes_attempted == 0


# -----------------------------------------------------------------------
# apply_programme_diff - the pure helper used by the coordinator
# -----------------------------------------------------------------------

from heo2.mqtt_writer import apply_programme_diff


class TestApplyProgrammeDiff:
    @pytest.mark.asyncio
    async def test_no_diff_returns_last_known_unchanged(self):
        """When new matches last_known, no writes are attempted and
        last_known is returned as-is (not replaced with a new object)."""
        transport = FakeTransport()
        writer = MqttWriter(transport=transport)
        programme = _baseline_programme()

        result, returned_last_known = await apply_programme_diff(
            writer, programme, programme,
        )

        assert result.success is True
        assert result.writes_attempted == 0
        assert result.writes_confirmed == 0
        assert len(transport.published) == 0
        assert returned_last_known is programme

    @pytest.mark.asyncio
    async def test_success_advances_last_known_to_new(self):
        """After a successful write, last_known should reflect the new
        programme so the next tick sees no diff."""
        transport = FakeTransport()
        transport.script_responses([
            "Set 'Capacity point 1' to '80': Saved.",
        ])
        writer = MqttWriter(transport=transport)

        current = _baseline_programme()
        new = _baseline_programme()
        new.slots[0] = SlotConfig(time(0, 0), time(5, 30), 80, True)

        result, returned_last_known = await apply_programme_diff(
            writer, current, new,
        )

        assert result.success is True
        assert returned_last_known is new
        assert returned_last_known.slots[0].capacity_soc == 80


    @pytest.mark.asyncio
    async def test_failure_keeps_last_known_unchanged(self):
        """If SA rejects the write, last_known must NOT advance.
        Next tick should then retry the same diff."""
        transport = FakeTransport()
        transport.script_responses([
            "Set 'Capacity point 1' to '80': Error: Inverter unreachable.",
        ])
        writer = MqttWriter(transport=transport)

        current = _baseline_programme()
        new = _baseline_programme()
        new.slots[0] = SlotConfig(time(0, 0), time(5, 30), 80, True)

        result, returned_last_known = await apply_programme_diff(
            writer, current, new,
        )

        assert result.success is False
        assert returned_last_known is current
        # Caller still has the OLD programme; next tick will re-diff.
        assert returned_last_known.slots[0].capacity_soc == 100

    @pytest.mark.asyncio
    async def test_dry_run_advances_last_known(self):
        """In dry_run mode, advance last_known anyway so we don't spam
        the log with 'Would publish' lines every tick. The coordinator
        is simulating the world where the writes happened."""
        transport = FakeTransport()
        writer = MqttWriter(transport=transport, dry_run=True)

        current = _baseline_programme()
        new = _baseline_programme()
        new.slots[0] = SlotConfig(time(0, 0), time(5, 30), 80, True)

        result, returned_last_known = await apply_programme_diff(
            writer, current, new,
        )

        assert result.success is True
        assert len(result.dry_run_log) >= 1
        assert len(transport.published) == 0
        # dry_run advances last_known to simulate the writes
        assert returned_last_known is new


    @pytest.mark.asyncio
    async def test_partial_failure_does_not_partially_commit(self):
        """If write 2 of 3 fails, last_known stays at the pre-write state.
        This is conservative but correct: there's no safe way to represent
        'slot 1 updated, slot 2 failed, slot 3 never tried' as a single
        ProgrammeState. Next tick re-diffs from the original state and
        retries everything."""
        transport = FakeTransport()
        transport.script_responses([
            "Set 'Capacity point 1' to '80': Saved.",  # 1 ok
            "Set 'Capacity point 2' to '60': Error: No response.",  # 2 fails
            # slot 3 never attempted
        ])
        writer = MqttWriter(transport=transport)

        current = _baseline_programme()
        new = _baseline_programme()
        new.slots[0] = SlotConfig(time(0, 0), time(5, 30), 80, True)
        new.slots[1] = SlotConfig(time(5, 30), time(18, 30), 60, False)
        new.slots[2] = SlotConfig(time(18, 30), time(23, 30), 40, False)

        result, returned_last_known = await apply_programme_diff(
            writer, current, new,
        )

        assert result.success is False
        assert result.failed_slot == 2
        assert result.writes_confirmed == 1
        # last_known still reflects the PRE-write world
        assert returned_last_known is current
        assert returned_last_known.slots[0].capacity_soc == 100  # unchanged
