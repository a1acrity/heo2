"""HEO III — Energy Optimiser (operator module).

Tracking issue: https://github.com/a1acrity/heo2/issues/75
Design doc: docs/HEO_III_DESIGN.md

The integration constructs an Operator with:
- PahoTransport (paho-MQTT direct to SA's broker)
- HAStateReader / HAServiceCaller (HA-backed adapters)
- BD / IGO / Saving-session / Tesla / Zappi / appliance entity IDs
  AUTO-DISCOVERED from the live HA registry (see discovery.py)

No coordinator or scheduler yet — the operator is callable but
won't tick on its own. Manual one-shot scripts use it via
hass.data[DOMAIN][entry_id].
"""

from __future__ import annotations

import asyncio
import logging

from .adapters.peripheral import TeslaConfig, ZappiConfig
from .adapters.world import BDConfig, FlagsConfig
from .const import DEFAULT_MQTT_HOST, DEFAULT_MQTT_PORT, DOMAIN
from .discovery import discover_all
from .operator import Operator
from .service_caller_ha import HAServiceCaller
from .services import async_register_services
from .state_reader_ha import HAStateReader
from .transport_paho import PahoTransport

# HA imports happen lazily inside async_setup_entry — keeps the
# module importable from pytest without HA installed.
CONF_HOST = "host"
CONF_PORT = "port"

logger = logging.getLogger(__name__)

PLATFORMS: list[str] = []  # Sensors/switches added in a later phase.

# Default appliance switches. Stays as built-in for now (the only
# discoverable hint is "switch.<name>" naming, which is too loose to
# auto-detect reliably). Extend the config flow when the planner
# needs different sets per install.
DEFAULT_APPLIANCES = {
    "washer": "switch.washer",
    "dryer": "switch.dryer",
    "dishwasher": "switch.dishwasher",
}


async def async_setup_entry(hass, entry) -> bool:  # type: ignore[no-untyped-def]
    """Set up HEO III from a config entry."""
    host = entry.data.get(CONF_HOST, DEFAULT_MQTT_HOST)
    port = entry.data.get(CONF_PORT, DEFAULT_MQTT_PORT)

    transport = PahoTransport(
        loop=asyncio.get_running_loop(), host=host, port=port
    )
    try:
        await transport.connect()
    except Exception as exc:
        logger.error(
            "HEO III: failed to connect to SA broker %s:%d — %s",
            host, port, exc,
        )
        raise

    discovered = discover_all(hass)
    logger.info("HEO III discovery: %s", discovered)

    bd_config = (
        BDConfig.from_meter_key(discovered["bd_meter_key"])
        if discovered["bd_meter_key"]
        else None
    )
    flags_config = FlagsConfig(
        igo_dispatching_entity=discovered["igo_dispatching_entity"],
        saving_session_entity=discovered["saving_session_entity"],
    )
    tesla_config = (
        TeslaConfig.from_vehicle(discovered["tesla_vehicle"])
        if discovered["tesla_vehicle"]
        else None
    )
    zappi_prefix = discovered["zappi_prefix"]
    zappi_config = (
        ZappiConfig(
            charge_mode=f"select.{zappi_prefix}_charge_mode",
            charging_state=f"sensor.{zappi_prefix}_status",
            charge_power=f"sensor.{zappi_prefix}_power_ct_internal_load",
        )
        if zappi_prefix
        else None
    )

    operator = Operator(
        transport=transport,
        hass=hass,
        state_reader=HAStateReader(hass),
        service_caller=HAServiceCaller(hass),
        bd_config=bd_config,
        flags_config=flags_config,
        tesla_config=tesla_config,
        zappi_config=zappi_config,
        appliance_switches=DEFAULT_APPLIANCES,
        inverter_sensor_overrides=discovered["inverter_sensor_overrides"],
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "operator": operator,
        "transport": transport,
        "discovered": discovered,
    }

    await async_register_services(hass)

    logger.info(
        "HEO III setup_entry: operator wired, transport connected to %s:%d",
        host, port,
    )
    return True


async def async_unload_entry(hass, entry) -> bool:  # type: ignore[no-untyped-def]
    """Tear down."""
    bucket = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if bucket:
        operator = bucket.get("operator")
        if operator is not None:
            try:
                await operator.shutdown()
            except Exception:
                logger.exception("HEO III: shutdown raised")
    return True
