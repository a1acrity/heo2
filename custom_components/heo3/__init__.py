"""HEO III — Energy Optimiser (operator module).

Tracking issue: https://github.com/a1acrity/heo2/issues/75
Design doc: docs/HEO_III_DESIGN.md

The integration constructs an Operator with the real PahoTransport
(direct connection to SA's broker, bypassing HA's mqtt bridge —
the bridge breaks SA telemetry on mosquitto 2.1.2). No coordinator
or scheduler yet — the operator is callable but won't tick on its
own. Manual one-shot scripts use it via hass.data[DOMAIN][entry_id].
"""

from __future__ import annotations

import asyncio
import logging

from .const import DEFAULT_MQTT_HOST, DEFAULT_MQTT_PORT, DOMAIN
from .operator import Operator
from .service_caller_ha import HAServiceCaller
from .state_reader_ha import HAStateReader
from .transport_paho import PahoTransport

# HA imports happen lazily inside async_setup_entry — keeps the
# module importable from pytest without HA installed.
CONF_HOST = "host"
CONF_PORT = "port"

logger = logging.getLogger(__name__)

PLATFORMS: list[str] = []  # Sensors/switches added in a later phase.


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

    operator = Operator(
        transport=transport,
        hass=hass,
        state_reader=HAStateReader(hass),
        service_caller=HAServiceCaller(hass),
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "operator": operator,
        "transport": transport,
    }

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
