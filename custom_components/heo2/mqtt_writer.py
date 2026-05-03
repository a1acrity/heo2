# custom_components/heo2/mqtt_writer.py
"""MQTT writer: diff-and-write with consecutive register updates.

Pure logic, no HA imports. Transport is injected via an MqttTransport
protocol so this module can be unit-tested without HA.

The writer implements the Solar Assistant set-write protocol:
  1. Publish a value to solar_assistant/inverter_1/<setting>/set
  2. SA applies it to the inverter over RS485
  3. SA publishes a response on solar_assistant/set/response_message/state
     e.g. "Set 'Capacity point 1' to '97': Saved."
     or   "Set 'Grid charge' to 'Disabled': Error: No response."
  4. We parse "Saved" (success) / "Error" (failure) from that response

See HEO-2 and the SA MQTT docs: https://solar-assistant.io/help/integration/mqtt
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import time
from typing import Any, Awaitable, Callable, Protocol

from .models import GlobalWrite, ProgrammeState, SlotConfig, SlotWrite
from .const import MQTT_WRITE_TIMEOUT_SECONDS, MQTT_MAX_RETRIES

logger = logging.getLogger(__name__)


# Human-readable SA setting name used inside the response_message text.
# SA capitalises and spaces out the name ("Capacity point 1", not
# "capacity_point_1"). We need this to parse responses.
_SETTING_DISPLAY_NAME = {
    "capacity": "Capacity point {n}",
    "time": "Time point {n}",
    "grid_charge": "Grid charge point {n}",
}

# SA's MQTT topic stem for each settable parameter.
_SETTING_TOPIC_STEM = {
    "capacity": "capacity_point_{n}",
    "time": "time_point_{n}",
    "grid_charge": "grid_charge_point_{n}",
}


class MqttTransport(Protocol):
    """Minimal async MQTT transport the writer needs.

    HA wiring will wrap homeassistant.components.mqtt to satisfy this.
    Tests wire up a fake.
    """

    async def publish(self, topic: str, payload: str) -> None: ...

    async def subscribe(
        self,
        topic: str,
        callback: Callable[[str, str], Awaitable[None] | None],
    ) -> Callable[[], None]:
        """Subscribe to `topic`. `callback(topic, payload)` is called on
        each incoming message. Returns an unsubscribe callable."""
        ...


@dataclass
class MqttWriteResult:
    """Result of a write_registers operation."""
    success: bool = False
    writes_attempted: int = 0
    writes_confirmed: int = 0
    failed_slot: int | None = None
    failed_param: str | None = None
    failed_reason: str | None = None
    dry_run_log: list[str] = field(default_factory=list)


def format_time(t: time | str) -> str:
    """SA expects HH:MM. Accept a time or a string."""
    if isinstance(t, time):
        return t.strftime("%H:%M")
    return str(t)


def format_grid_charge(enabled: bool) -> str:
    """SA's grid_charge_point_N set-topic accepts 'true' / 'false'.

    Pre-HEO-32 (2026-04 and earlier) SA used 'Enabled' / 'Disabled' here.
    The current SA build rejects those with `Error: Invalid value
    'Enabled' for 'Grid charge point N'. Valid values: true, false.`
    so any HEO II writer talking to a current SA must emit lowercase
    booleans.
    """
    return "true" if enabled else "false"


def parse_response_message(response: str) -> tuple[bool, str | None]:
    """Parse an SA response_message payload.

    Returns (success, error_detail_or_none). `success` is True iff the
    response is "Saved" (with optional trailing punctuation/whitespace).
    Anything starting "Error:" is an explicit failure with the error
    text returned as the detail.

    Current SA format observed 2026-05-02:
        Saved
        Error: Invalid value 'Enabled' for 'Grid charge point 1'. ...
        Error: No response.

    Pre-2026-05 SA prefixed every response with `Set 'Name' to 'Value': `.
    The payload no longer carries the setting name, so callers must
    correlate responses to writes by FIFO ordering rather than by
    parsing the response payload (handled by `_publish_and_confirm`).
    """
    if not response:
        return False, "empty response"

    text = response.strip().rstrip(".")
    if text == "Saved" or text.endswith(": Saved"):
        return True, None

    marker = "Error:"
    if marker in text:
        detail = text[text.index(marker) + len(marker):].strip().rstrip(".")
        return False, detail or "unspecified error"
    return False, f"unrecognised response: {response!r}"


class MqttWriter:
    """Diff new programme against current and write changed registers
    via Solar Assistant's MQTT set-topics.

    Writes are consecutive. After each publish we wait for SA's
    response_message with a timeout. A "Saved" response short-circuits
    the wait. An "Error:" response fails the write immediately (no
    retry, since SA already tried and failed to reach the inverter).
    A timeout (no response at all) triggers a retry.
    """

    def __init__(
        self,
        transport: MqttTransport | None = None,
        base_topic: str = "solar_assistant",
        inverter_name: str = "inverter_1",
        dry_run: bool = False,
        *,
        client: Any = None,  # legacy name, accepted for migration
    ):
        # Accept either `transport` or `client` for backward compatibility
        # with older tests that passed a MagicMock().
        self._transport = transport if transport is not None else client
        self._base_topic = base_topic
        self._inverter = inverter_name
        self._dry_run = dry_run

    # --- topic builders -------------------------------------------------

    def _set_topic(self, param: str, slot_num: int) -> str:
        stem = _SETTING_TOPIC_STEM[param].format(n=slot_num)
        return f"{self._base_topic}/{self._inverter}/{stem}/set"

    def _response_topic(self) -> str:
        return f"{self._base_topic}/set/response_message/state"

    def _setting_display(self, param: str, slot_num: int) -> str:
        return _SETTING_DISPLAY_NAME[param].format(n=slot_num)


    # --- diff -----------------------------------------------------------

    def diff_globals(
        self, current: ProgrammeState, new: ProgrammeState,
    ) -> list[GlobalWrite]:
        """Diff the SPEC §2 global settings between two programmes.

        `None` on the new side means "don't touch", so older callers
        that don't set the field never trigger a write. Equality check
        ignores case and trailing whitespace to be defensive about SA
        renderings.

        Currently wired: `work_mode`, `energy_pattern`. Future PRs can
        extend the same pattern to charge/discharge rate and zero-
        export-to-CT. Order matters: work_mode is applied first because
        downstream rules may depend on it (e.g. cannot export under
        Zero-Export work_mode regardless of energy_pattern).
        """
        out: list[GlobalWrite] = []

        def _check(field_name: str, topic_name: str) -> None:
            new_val = getattr(new, field_name)
            if new_val is None:
                return
            cur_val = getattr(current, field_name) or ""
            if str(cur_val).strip().casefold() != new_val.strip().casefold():
                out.append(GlobalWrite(
                    setting=topic_name, value=new_val.strip(),
                ))

        _check("work_mode", "work_mode")
        _check("energy_pattern", "energy_pattern")
        return out

    def diff(self, current: ProgrammeState, new: ProgrammeState) -> list[SlotWrite]:
        """Compare two programmes and return a list of register writes.

        `time_point_N` on the inverter is slot N's START time (Sunsynk
        timer convention - verified against the SA UI). So we write
        `slot.start_time` to time_point_N. Pre-2026-05-02 this used
        `slot.end_time`, which produced an off-by-one shift relative
        to the intended time windows (HEO-31 fix).
        """
        writes: list[SlotWrite] = []

        for i in range(6):
            cur = current.slots[i]
            nw = new.slots[i]
            slot_num = i + 1

            cap = nw.capacity_soc if nw.capacity_soc != cur.capacity_soc else None
            cur_time = cur.start_time.strftime("%H:%M")
            new_time = nw.start_time.strftime("%H:%M")
            tp = new_time if new_time != cur_time else None
            gc = nw.grid_charge if nw.grid_charge != cur.grid_charge else None

            if cap is not None or tp is not None or gc is not None:
                writes.append(SlotWrite(
                    slot_number=slot_num,
                    capacity_soc=cap,
                    time_point=tp,
                    grid_charge=gc,
                ))

        return writes

    # --- per-slot op enumeration ---------------------------------------

    def _ops_for_slot(self, write: SlotWrite) -> list[tuple[str, str, str, str]]:
        """Return (param_key, set_topic, payload, setting_display) tuples
        for each actual change in `write`."""
        ops: list[tuple[str, str, str, str]] = []
        n = write.slot_number

        if write.capacity_soc is not None:
            ops.append((
                "capacity",
                self._set_topic("capacity", n),
                str(write.capacity_soc),
                self._setting_display("capacity", n),
            ))
        if write.time_point is not None:
            ops.append((
                "time",
                self._set_topic("time", n),
                format_time(write.time_point),
                self._setting_display("time", n),
            ))
        if write.grid_charge is not None:
            ops.append((
                "grid_charge",
                self._set_topic("grid_charge", n),
                format_grid_charge(write.grid_charge),
                self._setting_display("grid_charge", n),
            ))
        return ops


    # --- the actual write orchestration --------------------------------

    def _global_set_topic(self, setting: str) -> str:
        return f"{self._base_topic}/{self._inverter}/{setting}/set"

    async def write_globals(
        self, writes: list[GlobalWrite],
    ) -> MqttWriteResult:
        """Sequentially publish each GlobalWrite to its `<setting>/set`
        topic and wait for SA's bare 'Saved' / 'Error: ...' response.
        Same FIFO-queue correlation as `write_registers` (HEO-32).

        Sequential by design: SA confirms one set at a time and we
        want to fail fast on the first error rather than firing all
        globals and trying to figure out which one SA rejected.
        """
        result = MqttWriteResult(writes_attempted=0)

        if self._dry_run:
            for w in writes:
                result.writes_attempted += 1
                result.dry_run_log.append(
                    f"Would publish {self._global_set_topic(w.setting)} "
                    f"= {w.value}"
                )
                result.writes_confirmed += 1
            result.success = True
            return result

        if not writes:
            result.success = True
            return result

        self._response_queue: asyncio.Queue[str] = asyncio.Queue()

        async def _on_response(topic: str, payload: str) -> None:
            await self._response_queue.put(payload)

        unsubscribe = await self._transport.subscribe(
            self._response_topic(), _on_response,
        )
        try:
            while not self._response_queue.empty():
                self._response_queue.get_nowait()

            for w in writes:
                topic = self._global_set_topic(w.setting)
                result.writes_attempted += 1
                ok, reason = await self._publish_and_confirm(
                    topic, w.value, w.setting,
                )
                if ok:
                    result.writes_confirmed += 1
                else:
                    result.success = False
                    result.failed_param = w.setting
                    result.failed_reason = reason
                    return result
            result.success = True
            return result
        finally:
            unsubscribe()

    async def write_registers(self, writes: list[SlotWrite]) -> MqttWriteResult:
        """Publish each setting change and wait for SA to confirm each
        one via response_message. Returns on first unrecoverable failure
        or when all writes are confirmed.

        SA's response payload no longer includes the setting name (just
        "Saved" or "Error: ..."), so we correlate responses to writes
        by strict FIFO. We subscribe once per batch, push every incoming
        response onto an asyncio.Queue, and `_publish_and_confirm` pops
        exactly one item per publish. Writes are sequential by design
        (see HEO-32) so the next response is always for the most recent
        publish.
        """
        result = MqttWriteResult(writes_attempted=0)

        if self._dry_run:
            for write in writes:
                for _, topic, payload, _ in self._ops_for_slot(write):
                    result.writes_attempted += 1
                    result.dry_run_log.append(f"Would publish {topic} = {payload}")
                    result.writes_confirmed += 1
            result.success = True
            return result

        self._response_queue: asyncio.Queue[str] = asyncio.Queue()

        async def _on_response(topic: str, payload: str) -> None:
            await self._response_queue.put(payload)

        unsubscribe = await self._transport.subscribe(
            self._response_topic(), _on_response,
        )
        try:
            # Drain any responses that may have queued up before our
            # write loop starts (retained messages, slow connect handshake).
            while not self._response_queue.empty():
                self._response_queue.get_nowait()

            for write in writes:
                for param, topic, payload, setting_display in self._ops_for_slot(write):
                    result.writes_attempted += 1
                    ok, reason = await self._publish_and_confirm(
                        topic, payload, setting_display,
                    )
                    if ok:
                        result.writes_confirmed += 1
                    else:
                        result.success = False
                        result.failed_slot = write.slot_number
                        result.failed_param = param
                        result.failed_reason = reason
                        return result
            result.success = True
            return result
        finally:
            unsubscribe()


    async def _publish_and_confirm(
        self, topic: str, payload: str, setting_display: str,
    ) -> tuple[bool, str | None]:
        """Publish once and pop the next response off the FIFO queue.

        Retries on timeout (no response) but NOT on explicit Error
        responses - SA already attempted the write and reported failure
        from the inverter end.

        Returns (success, reason_if_failed). The setting_display name is
        included in log lines for diagnosis but is no longer used to
        match responses to publishes (HEO-32: SA simplified the payload).
        """
        last_reason: str | None = None

        for attempt in range(1, MQTT_MAX_RETRIES + 1):
            try:
                await self._transport.publish(topic, payload)
            except Exception as exc:
                last_reason = f"publish failed: {exc}"
                logger.warning(
                    "HEO-2: %s attempt %d/%d publish error on %s: %s",
                    setting_display, attempt, MQTT_MAX_RETRIES, topic, exc,
                )
                continue

            try:
                response = await asyncio.wait_for(
                    self._response_queue.get(),
                    timeout=MQTT_WRITE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                last_reason = f"no response within {MQTT_WRITE_TIMEOUT_SECONDS}s"
                logger.warning(
                    "HEO-2: %s attempt %d/%d timeout on %s",
                    setting_display, attempt, MQTT_MAX_RETRIES, topic,
                )
                continue

            ok, detail = parse_response_message(response)
            if ok:
                logger.info(
                    "HEO-2: %s confirmed (attempt %d): %r",
                    setting_display, attempt, response,
                )
                return True, None
            logger.warning(
                "HEO-2: %s rejected by SA: %s (raw=%r)",
                setting_display, detail, response,
            )
            return False, detail

        return False, last_reason


async def apply_programme_diff(
    writer: "MqttWriter",
    last_known: "ProgrammeState",
    new_programme: "ProgrammeState",
) -> tuple["MqttWriteResult", "ProgrammeState"]:
    """Diff new_programme against last_known and apply via writer.

    Returns (result, effective_last_known) where effective_last_known is:
      - new_programme if the write succeeded (or was dry-run)
      - last_known unchanged if the write failed (caller should retry
        on next tick; partial successes are NOT committed)

    Behaviour on dry_run: writer reports success with writes_confirmed == N
    and dry_run_log populated. We still advance last_known because in
    dry_run mode we're simulating a world where the writes happened, so
    the next tick should see no diff. This prevents re-logging the same
    "Would publish" lines every 15 minutes.

    Pure async, no HA imports. Callable from the coordinator with real
    HAMqttTransport-backed writer, or from tests with FakeTransport.
    """
    # Globals first so per-slot writes happen under the intended
    # work_mode (e.g. SavingSession switches to "Selling first"; we
    # want the inverter in selling mode before its slot caps drop).
    global_writes = writer.diff_globals(last_known, new_programme)
    slot_writes = writer.diff(last_known, new_programme)

    if not global_writes and not slot_writes:
        result = MqttWriteResult(success=True, writes_attempted=0, writes_confirmed=0)
        return result, last_known

    combined = MqttWriteResult(success=True)
    if global_writes:
        g_result = await writer.write_globals(global_writes)
        combined.writes_attempted += g_result.writes_attempted
        combined.writes_confirmed += g_result.writes_confirmed
        combined.dry_run_log.extend(g_result.dry_run_log)
        if not g_result.success:
            combined.success = False
            combined.failed_param = g_result.failed_param
            combined.failed_reason = g_result.failed_reason
            return combined, last_known

    if slot_writes:
        s_result = await writer.write_registers(slot_writes)
        combined.writes_attempted += s_result.writes_attempted
        combined.writes_confirmed += s_result.writes_confirmed
        combined.dry_run_log.extend(s_result.dry_run_log)
        if not s_result.success:
            combined.success = False
            combined.failed_slot = s_result.failed_slot
            combined.failed_param = s_result.failed_param
            combined.failed_reason = s_result.failed_reason
            return combined, last_known

    return combined, new_programme
