"""Inverter Adapter — MQTT writes + reads against Solar Assistant.

P1.0 stub: methods raise NotImplementedError. Full implementation in
P1.1 (writes) and P1.2 (reads).
"""

from __future__ import annotations

from ..transport import Transport
from ..types import InverterSettings, InverterState, PlannedAction, Write


class InverterAdapter:
    """Writes to and reads from the Sunsynk inverter via SA's MQTT broker.

    All writes target inverter_1 per SPEC §2 (inverter_2 is RS485-mirrored).
    Writes are diff-only, sequenced, and verified against SA's
    `set/response_message/state` channel.
    """

    def __init__(self, transport: Transport, inverter_name: str) -> None:
        self._transport = transport
        self._inverter_name = inverter_name

    async def read_state(self) -> InverterState:
        """P1.2."""
        raise NotImplementedError("P1.2 — Inverter Adapter reads")

    async def read_settings(self) -> InverterSettings:
        """P1.2."""
        raise NotImplementedError("P1.2 — Inverter Adapter reads")

    def writes_for(self, action: PlannedAction) -> tuple[Write, ...]:
        """Translate a PlannedAction into a sequenced list of MQTT
        writes, snapping values, validating safety invariants (§17),
        and ordering globals before slots before current limits. P1.1.
        """
        raise NotImplementedError("P1.1 — Inverter Adapter writes")

    async def publish_and_verify(self, write: Write) -> str:
        """Publish one write, await SA response, return verification
        state (OK / SET_BUT_UNVERIFIED / FAILED / PENDING). P1.1.
        """
        raise NotImplementedError("P1.1 — verification cycle")
