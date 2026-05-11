"""Peripheral Adapter — zappi, Tesla (Teslemetry), appliances.

Implements §6 (controls) and §7 (reads) of the design.

Tesla writes are gated on `binary_sensor.<vehicle>_located_at_home`
being on — the operator silently no-ops commands when the car is
away rather than queuing them or erroring.

Zappi mode capture: when the planner sets mode=Stopped, the adapter
captures the previously-read mode so a later restore action can
write it back. Capture lives in-memory; survives across apply() calls
within the same Operator instance but not across HA restarts (P1.7
can promote to persistent storage if needed).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..service_caller import ServiceCaller
from ..state_reader import (
    StateReader,
    parse_bool,
    parse_float,
    parse_int,
    parse_str,
)
from ..types import (
    ApplianceAction,
    ApplianceState,
    EVAction,
    EVState,
    TeslaAction,
    TeslaState,
    ZAPPI_VALID_MODES,
)

logger = logging.getLogger(__name__)


# Outcome codes the apply_* methods return — the operator records
# these in ApplyResult so the planner can see what actually happened.
APPLIED = "APPLIED"
NO_OP = "NO_OP"  # action had no fields set
SKIPPED_NOT_AT_HOME = "SKIPPED_NOT_AT_HOME"
SKIPPED_NO_CONFIG = "SKIPPED_NO_CONFIG"
SKIPPED_NO_CAPTURED_MODE = "SKIPPED_NO_CAPTURED_MODE"
FAILED = "FAILED"


# ── Config dataclasses ────────────────────────────────────────────


@dataclass(frozen=True)
class ZappiConfig:
    charge_mode: str = "select.zappi_charge_mode"
    charging_state: str = "sensor.zappi_charging_state"
    charge_power: str = "sensor.zappi_charge_power"


@dataclass(frozen=True)
class TeslaConfig:
    """Entity IDs for one Tesla via Teslemetry.

    Teslemetry's naming is `<domain>.<vehicle>_<leaf>` — derive the
    full set from a vehicle short-name with `from_vehicle()`.
    """

    charge_switch: str
    battery_level: str
    charging: str
    charger_power: str
    charge_limit: str
    charge_current: str
    charge_cable: str
    located_at_home: str

    @classmethod
    def from_vehicle(cls, vehicle: str) -> "TeslaConfig":
        return cls(
            charge_switch=f"switch.{vehicle}_charge",
            battery_level=f"sensor.{vehicle}_battery_level",
            charging=f"sensor.{vehicle}_charging",
            charger_power=f"sensor.{vehicle}_charger_power",
            charge_limit=f"number.{vehicle}_charge_limit",
            charge_current=f"number.{vehicle}_charge_current",
            charge_cable=f"binary_sensor.{vehicle}_charge_cable",
            located_at_home=f"binary_sensor.{vehicle}_located_at_home",
        )


# ── Adapter ───────────────────────────────────────────────────────


class PeripheralAdapter:
    """HA service calls + reads for non-inverter equipment."""

    def __init__(
        self,
        *,
        state_reader: StateReader | None = None,
        service_caller: ServiceCaller | None = None,
        # zappi
        zappi_charge_mode_entity: str = "select.zappi_charge_mode",
        zappi_config: ZappiConfig | None = None,
        # tesla
        tesla_entity_prefix: str | None = None,
        tesla_config: TeslaConfig | None = None,
        # appliances: name → entity_id (e.g. "washer" → "switch.washer")
        # Reads use the same map; running flags assumed to live on
        # binary_sensor.<name>_running by default.
        appliance_switches: dict[str, str] | None = None,
        appliance_running_sensors: dict[str, str] | None = None,
    ) -> None:
        self._state_reader = state_reader
        self._service_caller = service_caller

        # Zappi: prefer explicit config; fall back to charge_mode entity
        # arg (legacy P1.0 shape) plus defaults.
        if zappi_config is not None:
            self._zappi = zappi_config
        else:
            self._zappi = ZappiConfig(charge_mode=zappi_charge_mode_entity)

        # Tesla: prefer explicit config; else derive from vehicle prefix;
        # else None means Tesla is not configured.
        if tesla_config is not None:
            self._tesla: TeslaConfig | None = tesla_config
        elif tesla_entity_prefix is not None:
            self._tesla = TeslaConfig.from_vehicle(tesla_entity_prefix)
        else:
            self._tesla = None

        self._appliance_switches = dict(appliance_switches or {})
        if appliance_running_sensors is not None:
            self._appliance_running = dict(appliance_running_sensors)
        else:
            # Default convention: switch.washer → binary_sensor.washer_running
            self._appliance_running = {
                name: f"binary_sensor.{name}_running"
                for name in self._appliance_switches
            }

        # In-memory captured pre-stop EV mode for restore_previous.
        self._captured_ev_mode: str | None = None

    # ── Reads ─────────────────────────────────────────────────────

    async def read_ev(self) -> EVState:
        if self._state_reader is None:
            return EVState()
        r = self._state_reader
        charging_raw = parse_str(r.get_state(self._zappi.charging_state))
        return EVState(
            charging=(
                charging_raw is not None
                and charging_raw.strip().lower() == "charging"
            ),
            mode=parse_str(r.get_state(self._zappi.charge_mode)),
            charge_power_w=parse_float(r.get_state(self._zappi.charge_power)),
        )

    async def read_tesla(self) -> TeslaState | None:
        """None if Tesla is not configured; otherwise a TeslaState
        (fields may be None individually if Teslemetry returned
        unknown — common when the car is asleep)."""
        if self._tesla is None or self._state_reader is None:
            return None
        r = self._state_reader
        cfg = self._tesla
        charging_raw = parse_str(r.get_state(cfg.charging))
        return TeslaState(
            soc_pct=parse_float(r.get_state(cfg.battery_level)),
            is_charging=(
                None
                if charging_raw is None
                else charging_raw.strip().lower() == "charging"
            ),
            charge_power_w=parse_float(r.get_state(cfg.charger_power)),
            charge_limit_pct=parse_int(r.get_state(cfg.charge_limit)),
            charge_current_a=parse_int(r.get_state(cfg.charge_current)),
            cable_plugged=parse_bool(r.get_state(cfg.charge_cable)),
            located_at_home=parse_bool(r.get_state(cfg.located_at_home)),
        )

    async def read_appliances(self) -> ApplianceState:
        if self._state_reader is None:
            return ApplianceState()
        r = self._state_reader
        return ApplianceState(
            washer_running=parse_bool(
                r.get_state(self._appliance_running.get("washer", ""))
            ),
            dryer_running=parse_bool(
                r.get_state(self._appliance_running.get("dryer", ""))
            ),
            dishwasher_running=parse_bool(
                r.get_state(self._appliance_running.get("dishwasher", ""))
            ),
        )

    # ── Writes ────────────────────────────────────────────────────

    async def apply_ev(self, action: EVAction) -> str:
        """Set zappi mode (or restore previous). Returns outcome code."""
        if self._service_caller is None:
            return SKIPPED_NO_CONFIG

        if action.restore_previous:
            if self._captured_ev_mode is None:
                logger.warning("restore_ev requested but no captured mode")
                return SKIPPED_NO_CAPTURED_MODE
            target_mode = self._captured_ev_mode
        elif action.set_mode is not None:
            if action.set_mode not in ZAPPI_VALID_MODES:
                logger.error("invalid zappi mode: %r", action.set_mode)
                return FAILED
            target_mode = action.set_mode
            # Capture pre-stop state for later restore.
            if action.set_mode == "Stopped":
                current = await self._read_current_ev_mode()
                if current is not None and current != "Stopped":
                    self._captured_ev_mode = current
        else:
            return NO_OP

        try:
            await self._service_caller.call(
                "select",
                "select_option",
                self._zappi.charge_mode,
                option=target_mode,
            )
        except Exception as exc:
            logger.error("apply_ev service call failed: %s", exc)
            return FAILED

        # If we just restored, clear the capture.
        if action.restore_previous:
            self._captured_ev_mode = None
        return APPLIED

    async def _read_current_ev_mode(self) -> str | None:
        if self._state_reader is None:
            return None
        return parse_str(self._state_reader.get_state(self._zappi.charge_mode))

    async def apply_tesla(self, action: TeslaAction) -> str:
        """Apply Tesla intent — gated on located_at_home. Returns outcome."""
        if self._tesla is None or self._service_caller is None:
            return SKIPPED_NO_CONFIG
        if (
            action.set_charging is None
            and action.set_charge_limit_pct is None
            and action.set_charge_current_a is None
        ):
            return NO_OP

        # Gate: located_at_home must be True. Missing/unknown counts as
        # "not at home" — we'd rather skip than send commands to a car
        # that might not receive them.
        if self._state_reader is None:
            return SKIPPED_NO_CONFIG
        at_home = parse_bool(
            self._state_reader.get_state(self._tesla.located_at_home)
        )
        if at_home is not True:
            logger.info(
                "Tesla command skipped: located_at_home=%s",
                at_home,
            )
            return SKIPPED_NOT_AT_HOME

        try:
            if action.set_charging is True:
                await self._service_caller.call(
                    "switch", "turn_on", self._tesla.charge_switch
                )
            elif action.set_charging is False:
                await self._service_caller.call(
                    "switch", "turn_off", self._tesla.charge_switch
                )

            if action.set_charge_limit_pct is not None:
                await self._service_caller.call(
                    "number",
                    "set_value",
                    self._tesla.charge_limit,
                    value=float(action.set_charge_limit_pct),
                )

            if action.set_charge_current_a is not None:
                await self._service_caller.call(
                    "number",
                    "set_value",
                    self._tesla.charge_current,
                    value=float(action.set_charge_current_a),
                )
        except Exception as exc:
            logger.error("apply_tesla service call failed: %s", exc)
            return FAILED

        return APPLIED

    async def apply_appliances(self, action: ApplianceAction) -> str:
        """Turn appliances on/off via the configured switches."""
        if self._service_caller is None:
            return SKIPPED_NO_CONFIG
        if not action.turn_off and not action.turn_on:
            return NO_OP

        try:
            for name in action.turn_off:
                entity = self._appliance_switches.get(name)
                if entity is None:
                    logger.warning("appliance %r not configured; skip", name)
                    continue
                await self._service_caller.call(
                    "switch", "turn_off", entity
                )
            for name in action.turn_on:
                entity = self._appliance_switches.get(name)
                if entity is None:
                    logger.warning("appliance %r not configured; skip", name)
                    continue
                await self._service_caller.call(
                    "switch", "turn_on", entity
                )
        except Exception as exc:
            logger.error("apply_appliances service call failed: %s", exc)
            return FAILED

        return APPLIED
