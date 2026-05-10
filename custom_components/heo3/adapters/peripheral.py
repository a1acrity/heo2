"""Peripheral Adapter — zappi, Tesla (Teslemetry), appliances.

P1.0 stub. Full implementation in P1.3.
"""

from __future__ import annotations

from ..types import (
    ApplianceState,
    EVAction,
    EVState,
    PlannedAction,
    TeslaAction,
    TeslaState,
)


class PeripheralAdapter:
    """HA service calls + reads for non-inverter equipment.

    Tesla writes are gated on `binary_sensor.<vehicle>_located_at_home`
    being `on` — the operator suppresses commands when the car is away
    rather than letting them queue or error.
    """

    def __init__(
        self,
        zappi_charge_mode_entity: str,
        tesla_entity_prefix: str | None,
        appliance_switches: dict[str, str],
    ) -> None:
        self._zappi_charge_mode_entity = zappi_charge_mode_entity
        self._tesla_entity_prefix = tesla_entity_prefix
        self._appliance_switches = appliance_switches

    async def read_ev(self) -> EVState:
        raise NotImplementedError("P1.3 — Peripheral Adapter reads")

    async def read_tesla(self) -> TeslaState | None:
        """Returns None if Tesla is not configured."""
        raise NotImplementedError("P1.3 — Peripheral Adapter reads")

    async def read_appliances(self) -> ApplianceState:
        raise NotImplementedError("P1.3 — Peripheral Adapter reads")

    async def apply_ev(self, action: EVAction) -> None:
        raise NotImplementedError("P1.3 — Peripheral Adapter writes")

    async def apply_tesla(self, action: TeslaAction) -> None:
        """No-op if car is not at home (gating per design §6/§7)."""
        raise NotImplementedError("P1.3 — Peripheral Adapter writes")

    async def apply_appliances(self, action: PlannedAction) -> None:
        raise NotImplementedError("P1.3 — Peripheral Adapter writes")
