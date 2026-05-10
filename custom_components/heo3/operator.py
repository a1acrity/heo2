"""The Operator — single point of contact for the planner.

Composes State (Inverter/Peripheral/World adapters → Snapshot),
Compute (pure derived calculations), Build (intent constructors),
and Execute (apply). The planner (deferred) talks only to this surface.

P1.0 stub: snapshot() and apply() raise NotImplementedError until the
adapters are filled in. The compute/build properties return live
instances (also stubs).
"""

from __future__ import annotations

from .adapters.inverter import InverterAdapter
from .adapters.peripheral import PeripheralAdapter
from .adapters.world import WorldGatherer
from .build import ActionBuilder
from .compute import Compute
from .const import DEFAULT_INVERTER_NAME
from .transport import Transport
from .types import ApplyResult, PlannedAction, Snapshot


class Operator:
    """The mechanical layer. Zero economic opinions. §3."""

    def __init__(
        self,
        *,
        transport: Transport,
        hass=None,  # type: ignore[no-untyped-def]
        inverter_name: str = DEFAULT_INVERTER_NAME,
        zappi_charge_mode_entity: str = "select.zappi_charge_mode",
        tesla_entity_prefix: str | None = None,
        appliance_switches: dict[str, str] | None = None,
    ) -> None:
        self._transport = transport
        self._hass = hass

        self._inverter = InverterAdapter(
            transport=transport, inverter_name=inverter_name
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
        """Gather complete frozen state: inverter + peripherals +
        world. Single call returns everything for one planner tick.
        Adapters are awaited concurrently in P1.7.
        """
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

    async def apply(self, action: PlannedAction) -> ApplyResult:
        """Mechanically execute a planned action: inverter writes,
        peripheral changes. Verifies and reports per-write outcome.
        Refuses if SPEC H4 / H3 / dry_run / disconnected (§16).
        Hard cap: 60s per call (§21 resolution).
        """
        raise NotImplementedError("P1.1 — apply() execution loop")

    async def shutdown(self) -> None:
        """Graceful close: MQTT disconnect, pending verifications cancelled."""
        await self._transport.disconnect()
