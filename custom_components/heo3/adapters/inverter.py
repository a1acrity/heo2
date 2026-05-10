"""Inverter Adapter — MQTT writes against Solar Assistant.

P1.1 implements writes + verification. P1.2 will add reads.

All writes target inverter_1 per SPEC §2 (inverter_2 is RS485-mirrored).
Writes are diff-only (when current state is supplied), sequenced
one-at-a-time, and verified against SA's `set/response_message/state`
channel. The FIFO-correlation hack from HEO II's writer is gone —
we publish, await, classify, then move on.

Per `reference_sa_mqtt.md`:
- Success response: `Saved`
- Failure response: `Error: <detail>`
- Response payload does NOT include the setting name. With one write
  in flight at a time, the next response IS the response for that write.

Verification states (for P1.1, no read-backs yet):
- `OK_FROM_SA` — SA returned `Saved`. Read-back verification is P1.2+.
- `FAILED` — SA returned `Error: ...`.
- `TIMEOUT` — no response within per-attempt timeout.

`SET_BUT_UNVERIFIED` and `OK` (the read-back-confirmed states from
§16) come in P1.2 once we can read the state sensors.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Iterable

from ..const import WRITE_RETRY_BACKOFF_S, WRITE_RETRY_LIMIT
from ..transport import Transport
from ..types import (
    InverterSettings,
    PlannedAction,
    SlotPlan,
    SlotSettings,
    Write,
)
from .inverter_validate import (
    SafetyError,
    snap_to_5min,
    validate_action,
)

logger = logging.getLogger(__name__)

# Per-attempt response timeout. SA usually responds in 1-3s.
RESPONSE_TIMEOUT_S = 5.0

# SA response payloads (per HEO-32 / reference_sa_mqtt.md).
RESPONSE_OK = "Saved"
RESPONSE_ERROR_PREFIX = "Error:"

VERIFY_OK_FROM_SA = "OK_FROM_SA"
VERIFY_FAILED = "FAILED"
VERIFY_TIMEOUT = "TIMEOUT"


class InverterAdapter:
    """Writes to and reads from the Sunsynk inverter via SA's MQTT broker."""

    def __init__(self, transport: Transport, inverter_name: str) -> None:
        self._transport = transport
        self._inverter_name = inverter_name

        # Single in-flight response Future. One write at a time means
        # the next response on the topic IS for that write — no FIFO
        # correlation hackery needed.
        self._response_future: asyncio.Future[str] | None = None
        self._subscribed = False

    @property
    def _set_topic_prefix(self) -> str:
        return f"solar_assistant/{self._inverter_name}"

    @property
    def _response_topic(self) -> str:
        return "solar_assistant/set/response_message/state"

    # ── Subscription ───────────────────────────────────────────────

    async def ensure_subscribed(self) -> None:
        """Subscribe to the response topic once. Idempotent — subsequent
        calls are no-ops. The handler resolves the in-flight future.
        """
        if self._subscribed:
            return
        await self._transport.subscribe(self._response_topic, self._on_response)
        self._subscribed = True

    async def _on_response(self, topic: str, payload: str) -> None:
        fut = self._response_future
        if fut is not None and not fut.done():
            fut.set_result(payload)

    # ── Translation: PlannedAction → list[Write] ──────────────────

    def writes_for(
        self,
        action: PlannedAction,
        *,
        current: InverterSettings | None = None,
        min_soc: int = 10,
        eps_active: bool = False,
    ) -> tuple[Write, ...]:
        """Translate a PlannedAction into an ordered, validated, deduped
        list of MQTT writes.

        - Validates safety invariants (§17). Raises `SafetyError` on
          violation BEFORE any writes are emitted.
        - Snaps slot times to the 5-min boundary.
        - If `current` is provided, diffs and skips no-ops.
        - Orders: work_mode → energy_pattern → per-slot writes
          (time, gc, capacity) → max current limits last (§4).
        """
        validate_action(action, min_soc=min_soc, eps_active=eps_active)

        normalised_slots = (
            tuple(_snap_slot(slot) for slot in action.slots)
            if action.slots
            else ()
        )

        writes: list[Write] = []

        # 1. Globals first: work_mode (some other settings depend on it),
        #    then energy_pattern.
        if action.work_mode is not None and (
            current is None
            or _norm_str(current.work_mode) != _norm_str(action.work_mode)
        ):
            writes.append(self._write_global("work_mode", action.work_mode))
        if action.energy_pattern is not None and (
            current is None
            or _norm_str(current.energy_pattern) != _norm_str(action.energy_pattern)
        ):
            writes.append(
                self._write_global("energy_pattern", action.energy_pattern)
            )

        # 2. Slots: per slot, time → grid_charge → capacity.
        for slot in normalised_slots:
            current_slot = (
                current.slots[slot.slot_n - 1] if current is not None else None
            )
            writes.extend(self._writes_for_slot(slot, current_slot))

        # 3. Current limits last: change while a slot is active should
        #    apply to the in-progress slot, not the next one.
        if action.max_charge_a is not None and (
            current is None
            or not _floats_equal(current.max_charge_a, action.max_charge_a)
        ):
            writes.append(
                self._write_global("max_charge_current", _fmt_amps(action.max_charge_a))
            )
        if action.max_discharge_a is not None and (
            current is None
            or not _floats_equal(current.max_discharge_a, action.max_discharge_a)
        ):
            writes.append(
                self._write_global(
                    "max_discharge_current", _fmt_amps(action.max_discharge_a)
                )
            )

        return tuple(writes)

    def _writes_for_slot(
        self, slot: SlotPlan, current: SlotSettings | None
    ) -> Iterable[Write]:
        if slot.start_hhmm is not None and (
            current is None or current.start_hhmm != slot.start_hhmm
        ):
            yield self._write_slot(slot.slot_n, "time_point", slot.start_hhmm)
        if slot.grid_charge is not None and (
            current is None or current.grid_charge != slot.grid_charge
        ):
            # SA rejects "True"/"False" — must be lowercase per HEO-32.
            yield self._write_slot(
                slot.slot_n, "grid_charge_point", "true" if slot.grid_charge else "false"
            )
        if slot.capacity_pct is not None and (
            current is None or current.capacity_pct != slot.capacity_pct
        ):
            yield self._write_slot(
                slot.slot_n, "capacity_point", str(slot.capacity_pct)
            )

    def _write_global(self, name: str, payload: str) -> Write:
        return Write(topic=f"{self._set_topic_prefix}/{name}/set", payload=payload)

    def _write_slot(self, slot_n: int, field: str, payload: str) -> Write:
        return Write(
            topic=f"{self._set_topic_prefix}/{field}_{slot_n}/set",
            payload=payload,
        )

    # ── Execution: publish one write, verify response ─────────────

    async def publish_and_verify(self, write: Write) -> str:
        """Publish one write, await SA response with retries.

        Retry policy:
        - Up to WRITE_RETRY_LIMIT (3) attempts on TIMEOUT.
        - No retry on explicit `Error: ...` responses — those are
          vocabulary mismatches that won't fix on retry (HEO-32 lesson).
        - WRITE_RETRY_BACKOFF_S (5s) between attempts.

        Returns one of: VERIFY_OK_FROM_SA, VERIFY_FAILED, VERIFY_TIMEOUT.
        """
        await self.ensure_subscribed()

        last_state = VERIFY_TIMEOUT
        for attempt in range(1, WRITE_RETRY_LIMIT + 1):
            self._response_future = asyncio.get_event_loop().create_future()
            try:
                await self._transport.publish(write.topic, write.payload)
                payload = await asyncio.wait_for(
                    self._response_future, timeout=RESPONSE_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "SA response timeout on %s (attempt %d/%d)",
                    write.topic,
                    attempt,
                    WRITE_RETRY_LIMIT,
                )
                last_state = VERIFY_TIMEOUT
                if attempt < WRITE_RETRY_LIMIT:
                    await asyncio.sleep(WRITE_RETRY_BACKOFF_S)
                continue
            finally:
                self._response_future = None

            if payload == RESPONSE_OK:
                return VERIFY_OK_FROM_SA
            if payload.startswith(RESPONSE_ERROR_PREFIX):
                logger.error(
                    "SA returned %s for %s — not retrying (vocabulary mismatch)",
                    payload,
                    write.topic,
                )
                return VERIFY_FAILED
            # Unexpected payload — treat as failure, don't retry.
            logger.error(
                "Unexpected SA response %r for %s", payload, write.topic
            )
            return VERIFY_FAILED

        return last_state


# ── Helpers ────────────────────────────────────────────────────────


def _snap_slot(slot: SlotPlan) -> SlotPlan:
    """Snap the slot's start_hhmm to a 5-min boundary if present."""
    if slot.start_hhmm is None:
        return slot
    return replace(slot, start_hhmm=snap_to_5min(slot.start_hhmm))


def _norm_str(s: str) -> str:
    return s.strip().lower()


def _floats_equal(a: float, b: float, *, tol: float = 0.5) -> bool:
    return abs(a - b) <= tol


def _fmt_amps(a: float) -> str:
    """SA accepts integer-string amps; round half-up."""
    return str(int(round(a)))
