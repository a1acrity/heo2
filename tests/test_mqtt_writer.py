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

from heo2.models import SlotConfig, ProgrammeState, SlotWrite, GlobalWrite
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

    Post-HEO-32 SA payload format: a bare "Saved" or "Error: <reason>"
    string with no setting prefix. Scripted responses must match.

    Usage:
        transport = FakeTransport()
        transport.script_responses([
            "Saved",
            "Error: No response.",
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
        """Writer maps slot[i].start_time -> time_point_{i+1} (Sunsynk
        timer convention). Changing slot 3's start to 19:00 means
        time_point_3 = 19:00."""
        current = _baseline_programme()
        new = _baseline_programme()
        # Move slot 3's start earlier - this shifts the boundary
        # between slot 2 and slot 3 from 18:30 to 19:00.
        new.slots[1] = SlotConfig(time(5, 30), time(19, 0), 100, False)
        new.slots[2] = SlotConfig(time(19, 0), time(23, 30), 20, False)
        writer = MqttWriter(client=MagicMock())
        writes = writer.diff(current, new)
        assert len(writes) == 1
        assert writes[0].slot_number == 3
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

    def test_time_point_is_slot_start_not_end(self):
        """HEO-31: regression guard. The Sunsynk timer convention is
        time_point_N = start of slot N. If a future refactor flips this
        back to slot.end_time the bug would silently shift all SOCs and
        grid_charge flags by one slot relative to their time windows.

        Slot 1 starts at 00:00 -> time_point_1 must be "00:00", NOT
        the end_time "05:30".
        """
        current = _baseline_programme()
        # Force slot 1's start to differ from current
        new = _baseline_programme()
        new.slots[0] = SlotConfig(time(0, 30), time(5, 30), 100, True)
        writer = MqttWriter(client=MagicMock())
        writes = writer.diff(current, new)
        # The diff must pick up the start change as time_point_1 = "00:30",
        # not "05:30" (which is end_time and unchanged).
        slot1_writes = [w for w in writes if w.slot_number == 1]
        assert len(slot1_writes) == 1
        assert slot1_writes[0].time_point == "00:30"


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
    def test_format_grid_charge_true_is_lowercase_true(self):
        """HEO-32: SA accepts only lowercase 'true'/'false' for
        grid_charge_point_N. Pre-HEO-32 'Enabled'/'Disabled' is
        rejected with `Error: Invalid value 'Enabled' for ...`."""
        assert format_grid_charge(True) == "true"

    def test_format_grid_charge_false_is_lowercase_false(self):
        assert format_grid_charge(False) == "false"

    def test_format_time_accepts_time_object(self):
        assert format_time(time(23, 30)) == "23:30"

    def test_format_time_passes_strings_through(self):
        assert format_time("05:30") == "05:30"


# -----------------------------------------------------------------------
# parse_response_message - pure function covering SA's current format
# -----------------------------------------------------------------------

class TestParseResponseMessage:
    def test_bare_saved_returns_success(self):
        """HEO-32: post-2026-05 SA payload is just 'Saved' - no
        'Set <name> to <value>:' prefix."""
        ok, reason = parse_response_message("Saved")
        assert ok is True
        assert reason is None

    def test_legacy_set_prefix_saved_still_recognised(self):
        """Backward compat with older SA builds that still wrote
        the verbose `Set 'X' to 'Y': Saved.` format."""
        ok, reason = parse_response_message(
            "Set 'Capacity point 1' to '97': Saved.",
        )
        assert ok is True

    def test_error_no_response_is_failure(self):
        """Live SA log shape: bare 'Error: No response' with no prefix."""
        ok, reason = parse_response_message("Error: No response.")
        assert ok is False
        assert reason == "No response"

    def test_error_invalid_value_captures_detail(self):
        """The actual error from SA when sent legacy 'Enabled'."""
        ok, reason = parse_response_message(
            "Error: Invalid value 'Enabled' for 'Grid charge point 1'. "
            "Valid values: true, false."
        )
        assert ok is False
        assert "Invalid value" in reason

    def test_legacy_set_prefix_error_still_recognised(self):
        ok, reason = parse_response_message(
            "Set 'Grid charge' to 'Disabled': Error: No response.",
        )
        assert ok is False
        assert reason == "No response"

    def test_empty_response_is_failure(self):
        ok, reason = parse_response_message("")
        assert ok is False

    def test_unrecognised_response_is_failure(self):
        ok, reason = parse_response_message("something else entirely")
        assert ok is False
        assert "unrecognised" in reason


# -----------------------------------------------------------------------
# write_registers integration - using FakeTransport
# -----------------------------------------------------------------------

class TestWriteRegisters:
    @pytest.mark.asyncio
    async def test_happy_path_capacity_write_confirmed(self):
        transport = FakeTransport()
        transport.script_responses(["Saved"])
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
    async def test_grid_charge_serialised_as_lowercase_bool(self):
        """HEO-32: SA's grid_charge_point_N expects 'true'/'false'."""
        transport = FakeTransport()
        transport.script_responses(["Saved"])
        writer = MqttWriter(transport=transport)

        await writer.write_registers([
            SlotWrite(slot_number=2, grid_charge=True),
        ])

        _topic, payload = transport.published[0]
        assert payload == "true"

    @pytest.mark.asyncio
    async def test_multiple_slots_published_in_order(self):
        transport = FakeTransport()
        transport.script_responses(["Saved", "Saved"])
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
        transport.script_responses(["Error: No response."])
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
        monkeypatch.setattr(
            "heo2.mqtt_writer.MQTT_WRITE_TIMEOUT_SECONDS", 0.05
        )
        transport = FakeTransport()
        transport.script_responses([
            None,  # first attempt: no response
            "Saved",  # second attempt: success
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
            "Error: Inverter rejected.",
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
    async def test_responses_correlated_by_fifo_order(self):
        """HEO-32: SA's bare 'Saved'/'Error:' payload doesn't carry the
        setting name, so writes are sequential and the queue pops one
        response per publish in order. A delayed first response still
        attaches to the first publish, not the second.
        """
        transport = FakeTransport()
        # Both succeed - confirms the queue feeds one response per publish.
        transport.script_responses(["Saved", "Saved"])
        writer = MqttWriter(transport=transport)

        result = await writer.write_registers([
            SlotWrite(slot_number=1, capacity_soc=80),
            SlotWrite(slot_number=2, capacity_soc=70),
        ])
        assert result.success is True
        assert result.writes_confirmed == 2

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
        transport.script_responses(["Saved"])
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
        transport.script_responses(["Error: Inverter unreachable."])
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
            "Saved",  # 1 ok
            "Error: No response.",  # 2 fails
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


class TestDiffGlobals:
    def test_no_change_when_work_mode_matches(self):
        cur = _baseline_programme()
        cur.work_mode = "Zero export to CT"
        new = _baseline_programme()
        new.work_mode = "Zero export to CT"
        writer = MqttWriter(client=MagicMock())
        assert writer.diff_globals(cur, new) == []

    def test_work_mode_change_detected(self):
        cur = _baseline_programme()
        cur.work_mode = "Zero export to CT"
        new = _baseline_programme()
        new.work_mode = "Selling first"
        writer = MqttWriter(client=MagicMock())
        out = writer.diff_globals(cur, new)
        assert len(out) == 1
        assert out[0].setting == "work_mode"
        assert out[0].value == "Selling first"

    def test_none_work_mode_on_new_does_not_trigger_write(self):
        """When the new programme leaves work_mode unset (None) we
        don't write anything - older callers and tests don't
        accidentally clobber the inverter setting."""
        cur = _baseline_programme()
        cur.work_mode = "Selling first"
        new = _baseline_programme()
        new.work_mode = None
        writer = MqttWriter(client=MagicMock())
        assert writer.diff_globals(cur, new) == []

    def test_case_insensitive_match(self):
        """Be defensive about SA vs HEO casing on the same value."""
        cur = _baseline_programme()
        cur.work_mode = "Zero Export To CT"
        new = _baseline_programme()
        new.work_mode = "zero export to ct"
        writer = MqttWriter(client=MagicMock())
        assert writer.diff_globals(cur, new) == []

    def test_energy_pattern_change_detected(self):
        cur = _baseline_programme()
        cur.work_mode = "Zero export to CT"
        cur.energy_pattern = "Load first"
        new = _baseline_programme()
        new.work_mode = "Zero export to CT"
        new.energy_pattern = "Battery first"
        writer = MqttWriter(client=MagicMock())
        out = writer.diff_globals(cur, new)
        assert len(out) == 1
        assert out[0].setting == "energy_pattern"
        assert out[0].value == "Battery first"

    def test_both_globals_change_returns_both_writes_in_order(self):
        """work_mode comes before energy_pattern (rules may depend on
        work_mode being applied first)."""
        cur = _baseline_programme()
        cur.work_mode = "Zero export to CT"
        cur.energy_pattern = "Load first"
        new = _baseline_programme()
        new.work_mode = "Selling first"
        new.energy_pattern = "Battery first"
        writer = MqttWriter(client=MagicMock())
        out = writer.diff_globals(cur, new)
        assert [w.setting for w in out] == ["work_mode", "energy_pattern"]


class TestWriteGlobals:
    @pytest.mark.asyncio
    async def test_happy_path_work_mode_publish_confirmed(self):
        transport = FakeTransport()
        transport.script_responses(["Saved"])
        writer = MqttWriter(transport=transport)

        result = await writer.write_globals([
            GlobalWrite(setting="work_mode", value="Selling first"),
        ])
        assert result.success is True
        assert result.writes_confirmed == 1
        assert transport.published[0] == (
            "solar_assistant/inverter_1/work_mode/set",
            "Selling first",
        )

    @pytest.mark.asyncio
    async def test_dry_run_logs_without_publishing(self):
        transport = FakeTransport()
        writer = MqttWriter(transport=transport, dry_run=True)
        result = await writer.write_globals([
            GlobalWrite(setting="work_mode", value="Selling first"),
        ])
        assert result.success is True
        assert result.writes_confirmed == 1
        assert len(transport.published) == 0
        assert any("work_mode" in line for line in result.dry_run_log)


class TestApplyProgrammeDiffGlobals:
    @pytest.mark.asyncio
    async def test_global_write_first_then_slots(self):
        """SPEC §2: globals (work_mode) flush before per-slot writes
        so the inverter is in the right mode when its slot caps drop."""
        transport = FakeTransport()
        transport.script_responses(["Saved", "Saved"])  # work_mode + slot
        writer = MqttWriter(transport=transport)

        cur = _baseline_programme()
        cur.work_mode = "Zero export to CT"
        new = _baseline_programme()
        new.work_mode = "Selling first"
        new.slots[0] = SlotConfig(time(0, 0), time(5, 30), 30, True)

        result, last = await apply_programme_diff(writer, cur, new)
        assert result.success is True
        assert result.writes_attempted == 2
        # Global was published first
        assert "work_mode" in transport.published[0][0]
        assert "capacity_point_1" in transport.published[1][0]
        assert last is new

    @pytest.mark.asyncio
    async def test_global_failure_aborts_before_slot_writes(self):
        transport = FakeTransport()
        transport.script_responses([
            "Error: Invalid value 'Bogus' for 'Work mode'.",
            # no second response - shouldn't get here
        ])
        writer = MqttWriter(transport=transport)

        cur = _baseline_programme()
        cur.work_mode = "Zero export to CT"
        new = _baseline_programme()
        new.work_mode = "Bogus"
        new.slots[0] = SlotConfig(time(0, 0), time(5, 30), 30, True)

        result, last = await apply_programme_diff(writer, cur, new)
        assert result.success is False
        assert result.failed_param == "work_mode"
        # slot writes never happened
        assert len(transport.published) == 1
        assert last is cur
