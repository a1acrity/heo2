# custom_components/heo2/mqtt_writer.py
"""MQTT writer: diff-and-write with consecutive register updates. No HA imports."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from .models import ProgrammeState, SlotConfig, SlotWrite
from .const import MQTT_WRITE_TIMEOUT_SECONDS, MQTT_MAX_RETRIES

logger = logging.getLogger(__name__)


@dataclass
class MqttWriteResult:
    """Result of a write_registers operation."""
    success: bool = False
    writes_attempted: int = 0
    writes_confirmed: int = 0
    failed_slot: int | None = None
    failed_param: str | None = None
    dry_run_log: list[str] = field(default_factory=list)


class MqttWriter:
    """Diff new programme against current and write changed registers.

    Writes are consecutive: each register is published, then we wait for
    read-back confirmation before proceeding to the next.
    """

    def __init__(
        self,
        client: Any,
        base_topic: str = "solar_assistant",
        dry_run: bool = False,
    ):
        self._client = client
        self._base_topic = base_topic
        self._dry_run = dry_run

    def _topic(self, slot_num: int, param: str) -> str:
        """Build MQTT topic for a slot register."""
        return f"{self._base_topic}/inverter_1/prog{slot_num}_{param}/set"

    def _readback_topic(self, slot_num: int, param: str) -> str:
        """Build MQTT topic for reading back a slot register."""
        return f"{self._base_topic}/inverter_1/prog{slot_num}_{param}"

    def diff(self, current: ProgrammeState, new: ProgrammeState) -> list[SlotWrite]:
        """Compare two programmes and return a list of register writes needed."""
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

    async def write_registers(self, writes: list[SlotWrite]) -> MqttWriteResult:
        """Write register changes consecutively with read-back verification."""
        result = MqttWriteResult(writes_attempted=0)

        for write in writes:
            register_ops = self._build_register_ops(write)

            for topic, payload, readback_topic, expected in register_ops:
                result.writes_attempted += 1

                if self._dry_run:
                    result.dry_run_log.append(f"Would write {topic} = {payload}")
                    result.writes_confirmed += 1
                    continue

                confirmed = False
                for attempt in range(1, MQTT_MAX_RETRIES + 1):
                    await self._publish(topic, payload)
                    ok = await self._wait_for_readback(
                        readback_topic, expected, MQTT_WRITE_TIMEOUT_SECONDS
                    )
                    if ok:
                        confirmed = True
                        result.writes_confirmed += 1
                        break

                if not confirmed:
                    param = topic.split("/")[-2].split("_", 1)[1] if "/" in topic else "unknown"
                    result.success = False
                    result.failed_slot = write.slot_number
                    result.failed_param = param
                    return result

        result.success = True
        return result

    def _build_register_ops(self, write: SlotWrite) -> list[tuple[str, str, str, str]]:
        """Build (set_topic, payload, readback_topic, expected_value) tuples."""
        ops = []
        n = write.slot_number

        if write.capacity_soc is not None:
            ops.append((
                self._topic(n, "capacity"),
                str(write.capacity_soc),
                self._readback_topic(n, "capacity"),
                str(write.capacity_soc),
            ))
        if write.time_point is not None:
            ops.append((
                self._topic(n, "time"),
                write.time_point,
                self._readback_topic(n, "time"),
                write.time_point,
            ))
        if write.grid_charge is not None:
            payload = "true" if write.grid_charge else "false"
            ops.append((
                self._topic(n, "charge"),
                payload,
                self._readback_topic(n, "charge"),
                payload,
            ))
        return ops

    async def _publish(self, topic: str, payload: str) -> None:
        """Publish a message. Override in tests."""
        self._client.publish(topic, payload)

    async def _wait_for_readback(self, topic: str, expected: str, timeout: float) -> bool:
        """Wait for a read-back message matching expected value. Override in tests."""
        raise NotImplementedError("Wire up MQTT subscription in HA integration")
