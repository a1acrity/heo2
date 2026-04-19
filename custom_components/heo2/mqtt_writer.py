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

from .models import ProgrammeState, SlotConfig, SlotWrite
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
    """SA expects 'Enabled' / 'Disabled' as the setting value."""
    return "Enabled" if enabled else "Disabled"


def parse_response_message(
    response: str, expected_setting: str,
) -> tuple[bool, str | None]:
    """Parse an SA response_message payload.

    Returns (success, error_detail_or_none). `success` is True iff the
    response is for the expected setting and ends with ": Saved."
    Any other outcome (Error, different setting, malformed) is False.

    SA format observed in production:
        Set 'Capacity point 1' to '97': Saved.
        Set 'Grid charge' to 'Disabled': Error: No response.
    """
    if not response or "Set '" not in response:
        return False, f"malformed response: {response!r}"

    if expected_setting not in response:
        # Response is for some other concurrent write; not ours
        return False, f"response for different setting: {response!r}"

    if response.rstrip(".").endswith("Saved"):
        return True, None

    # Everything else is an error; extract the detail after "Error:"
    marker = "Error:"
    if marker in response:
        detail = response[response.index(marker) + len(marker):].strip().rstrip(".")
        return False, detail
    return False, f"unknown failure: {response!r}"


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

    def diff(self, current: ProgrammeState, new: ProgrammeState) -> list[SlotWrite]:
        """Compare two programmes and return a list of register writes."""
        writes: list[SlotWrite] = []

        for i in range(6):
            cur = current.slots[i]
            nw = new.slots[i]
            slot_num = i + 1

            cap = nw.capacity_soc if nw.capacity_soc != cur.capacity_soc else None
            cur_time = cur.end_time.strftime("%H:%M")
            new_time = nw.end_time.strftime("%H:%M")
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

    async def write_registers(self, writes: list[SlotWrite]) -> MqttWriteResult:
        """Publish each setting change and wait for SA to confirm each
        one via response_message. Returns on first unrecoverable failure
        or when all writes are confirmed."""
        result = MqttWriteResult(writes_attempted=0)

        if self._dry_run:
            for write in writes:
                for _, topic, payload, _ in self._ops_for_slot(write):
                    result.writes_attempted += 1
                    result.dry_run_log.append(f"Would publish {topic} = {payload}")
                    result.writes_confirmed += 1
            result.success = True
            return result

        # Subscribe to the response topic ONCE for the whole batch. Each
        # write temporarily attaches its own future to the subscription
        # handler via the shared self._response_futures dict.
        self._response_futures: dict[str, asyncio.Future[tuple[bool, str | None]]] = {}

        async def _on_response(topic: str, payload: str) -> None:
            for setting_display, fut in list(self._response_futures.items()):
                if fut.done():
                    continue
                if setting_display in payload:
                    ok, detail = parse_response_message(payload, setting_display)
                    if not fut.done():
                        fut.set_result((ok, detail))

        unsubscribe = await self._transport.subscribe(
            self._response_topic(), _on_response,
        )
        try:
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
        """Publish once and wait for response. Retry on timeout only
        (not on Error responses, since SA already tried and reported
        failure from the inverter end).

        Returns (success, reason_if_failed).
        """
        last_reason: str | None = None

        for attempt in range(1, MQTT_MAX_RETRIES + 1):
            fut: asyncio.Future[tuple[bool, str | None]] = asyncio.get_event_loop().create_future()
            self._response_futures[setting_display] = fut

            try:
                await self._transport.publish(topic, payload)

                try:
                    ok, detail = await asyncio.wait_for(
                        fut, timeout=MQTT_WRITE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    last_reason = f"no response within {MQTT_WRITE_TIMEOUT_SECONDS}s"
                    logger.warning(
                        "HEO-2: %s attempt %d/%d timeout on %s",
                        setting_display, attempt, MQTT_MAX_RETRIES, topic,
                    )
                    continue

                if ok:
                    logger.info(
                        "HEO-2: %s confirmed (attempt %d)",
                        setting_display, attempt,
                    )
                    return True, None
                else:
                    # Explicit Error from SA. No retry.
                    logger.warning(
                        "HEO-2: %s rejected by SA: %s",
                        setting_display, detail,
                    )
                    return False, detail
            finally:
                self._response_futures.pop(setting_display, None)

        return False, last_reason
