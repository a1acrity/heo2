"""The Operator — single point of contact for the planner.

Composes State (Inverter/Peripheral/World adapters → Snapshot),
Compute (pure derived calculations), Build (intent constructors),
and Execute (apply). The planner (deferred) talks only to this surface.

P1.0: skeleton.
P1.1: apply() execution loop for inverter writes wired up.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import datetime, timezone

from .adapters.inverter import (
    InverterAdapter,
    VERIFY_FAILED,
    VERIFY_OK_FROM_SA,
    VERIFY_TIMEOUT,
)
from .adapters.inverter_validate import SafetyError
from .adapters.peripheral import PeripheralAdapter
from .adapters.world import WorldGatherer
from .build import ActionBuilder
from .compute import Compute
from .const import (
    DEFAULT_INVERTER_NAME,
    TICK_HARD_BUDGET_S,
    TICK_WARNING_S,
)
from .state_reader import StateReader
from .transport import Transport
from .types import (
    ApplyResult,
    FailedWrite,
    InverterSettings,
    PlannedAction,
    Snapshot,
    VerificationResult,
    Write,
)

logger = logging.getLogger(__name__)


class Operator:
    """The mechanical layer. Zero economic opinions. §3."""

    def __init__(
        self,
        *,
        transport: Transport,
        hass=None,  # type: ignore[no-untyped-def]
        state_reader: StateReader | None = None,
        inverter_name: str = DEFAULT_INVERTER_NAME,
        inverter_sensor_prefix: str | None = None,
        zappi_charge_mode_entity: str = "select.zappi_charge_mode",
        tesla_entity_prefix: str | None = None,
        appliance_switches: dict[str, str] | None = None,
    ) -> None:
        self._transport = transport
        self._hass = hass
        self._state_reader = state_reader

        self._inverter = InverterAdapter(
            transport=transport,
            inverter_name=inverter_name,
            state_reader=state_reader,
            sensor_prefix=inverter_sensor_prefix,
        )
        self._peripheral = PeripheralAdapter(
            zappi_charge_mode_entity=zappi_charge_mode_entity,
            tesla_entity_prefix=tesla_entity_prefix,
            appliance_switches=appliance_switches or {},
        )
        self._world = WorldGatherer(hass=hass)

        self._compute = Compute()
        self._build = ActionBuilder()

    # ── State ─────────────────────────────────────────────────────

    async def snapshot(self) -> Snapshot:
        """Gather complete frozen state. P1.7."""
        raise NotImplementedError("P1.7 — Snapshot integration")

    # ── Derived facts ─────────────────────────────────────────────

    @property
    def compute(self) -> Compute:
        return self._compute

    # ── Action construction ───────────────────────────────────────

    @property
    def build(self) -> ActionBuilder:
        return self._build

    # ── Execution ─────────────────────────────────────────────────

    async def apply(
        self,
        action: PlannedAction,
        *,
        current_settings: InverterSettings | None = None,
        min_soc: int = 10,
        eps_active: bool = False,
    ) -> ApplyResult:
        """Mechanically execute a planned action.

        P1.1 scope: inverter writes only. Peripheral writes wired in P1.3.
        Snapshot-derived pre-flight checks (SPEC H4 rates freshness,
        H3 EPS) wired in P1.7 once snapshot() is live.

        Pre-flight (now):
        - Transport must be connected.
        - Action must validate against §17 invariants.

        Hard cap: TICK_HARD_BUDGET_S (60s) per call (§21 resolution).
        Warns if duration exceeds TICK_WARNING_S (30s).
        """
        plan_id = action.plan_id or _generate_plan_id()
        captured_at = datetime.now(timezone.utc)
        t_start = _time.monotonic()

        # ── Pre-flight ─────────────────────────────────────────
        if not self._transport.is_connected:
            return _empty_failure_result(
                plan_id=plan_id,
                captured_at=captured_at,
                reason="transport not connected",
                duration_ms=(_time.monotonic() - t_start) * 1000.0,
            )

        try:
            writes = self._inverter.writes_for(
                action,
                current=current_settings,
                min_soc=min_soc,
                eps_active=eps_active,
            )
        except SafetyError as exc:
            logger.warning("apply() rejected by safety validation: %s", exc)
            return _empty_failure_result(
                plan_id=plan_id,
                captured_at=captured_at,
                reason=f"safety: {exc}",
                duration_ms=(_time.monotonic() - t_start) * 1000.0,
            )

        # ── Execute, with overall budget ───────────────────────
        succeeded: list[Write] = []
        failed: list[FailedWrite] = []
        verify_states: dict[str, str] = {}

        try:
            await asyncio.wait_for(
                self._execute_writes(writes, succeeded, failed, verify_states),
                timeout=TICK_HARD_BUDGET_S,
            )
        except asyncio.TimeoutError:
            logger.error(
                "apply() exceeded hard budget %ds — aborting tick",
                TICK_HARD_BUDGET_S,
            )
            # Record the writes that hadn't been attempted yet as failed.
            attempted_topics = {w.topic for w in succeeded} | {
                fw.write.topic for fw in failed
            }
            for w in writes:
                if w.topic not in attempted_topics:
                    failed.append(
                        FailedWrite(write=w, reason="aborted: tick budget exceeded")
                    )

        duration_ms = (_time.monotonic() - t_start) * 1000.0
        if duration_ms > TICK_WARNING_S * 1000.0:
            logger.warning(
                "apply() took %.1fs (warn threshold %.1fs)",
                duration_ms / 1000.0,
                TICK_WARNING_S,
            )

        return ApplyResult(
            plan_id=plan_id,
            requested=writes,
            succeeded=tuple(succeeded),
            failed=tuple(failed),
            skipped=(),  # Diff-skipped writes never enter `writes`.
            verification=VerificationResult(states=verify_states),
            duration_ms=duration_ms,
            captured_at=captured_at,
        )

    async def _execute_writes(
        self,
        writes: tuple[Write, ...],
        succeeded: list[Write],
        failed: list[FailedWrite],
        verify_states: dict[str, str],
    ) -> None:
        for write in writes:
            state = await self._inverter.publish_and_verify(write)
            verify_states[write.topic] = state
            if state == VERIFY_OK_FROM_SA:
                succeeded.append(write)
            elif state == VERIFY_FAILED:
                failed.append(FailedWrite(write=write, reason="SA returned Error"))
            elif state == VERIFY_TIMEOUT:
                failed.append(
                    FailedWrite(write=write, reason="no SA response after retries")
                )
            else:  # pragma: no cover — defensive
                failed.append(FailedWrite(write=write, reason=f"unknown: {state}"))

    async def shutdown(self) -> None:
        """Graceful close: MQTT disconnect, pending verifications cancelled."""
        await self._transport.disconnect()


# ── Helpers ────────────────────────────────────────────────────────


def _generate_plan_id() -> str:
    """Lightweight plan_id when the planner didn't supply one."""
    import uuid

    return uuid.uuid4().hex[:12]


def _empty_failure_result(
    *, plan_id: str, captured_at: datetime, reason: str, duration_ms: float
) -> ApplyResult:
    return ApplyResult(
        plan_id=plan_id,
        requested=(),
        succeeded=(),
        failed=(FailedWrite(write=Write(topic="", payload=""), reason=reason),),
        skipped=(),
        verification=VerificationResult(),
        duration_ms=duration_ms,
        captured_at=captured_at,
    )
