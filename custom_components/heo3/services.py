"""HEO III service registrations for diagnostics + cutover.

Two services exposed via standard HA service-call:

  service: heo3.snapshot_log
    Reads everything (no inverter side-effects). Logs a 1-line
    summary at INFO level. Use to verify reads work before any
    write attempt.

  service: heo3.apply_baseline_static
    Builds and applies the static baseline plan. Logs the
    ApplyResult summary. THIS WRITES TO THE INVERTER.

Both use the operator on hass.data[DOMAIN][<entry_id>]. If multiple
entries exist (test installs), the first one is used.
"""

from __future__ import annotations

import logging
from typing import Any

from .adapters.peripheral import TeslaConfig
from .adapters.world import BDConfig, FlagsConfig
from .const import DOMAIN

logger = logging.getLogger(__name__)

# Paddy's house defaults — patched onto the operator at service time
# until the config flow learns to ask for them.
DEFAULT_BD_METER_KEY = "18p5009498_2372761090617"
DEFAULT_IGO_DISPATCH_ENTITY = (
    "binary_sensor.octopus_energy_00000000_0009_4000_8020_000000032ba2"
    "_intelligent_dispatching"
)
DEFAULT_SAVING_SESSION_ENTITY = (
    "binary_sensor.octopus_energy_a_8e04cfcf_octoplus_saving_sessions"
)
DEFAULT_TESLA_VEHICLE = "natalia"
DEFAULT_APPLIANCES = {
    "washer": "switch.washer",
    "dryer": "switch.dryer",
    "dishwasher": "switch.dishwasher",
}


async def async_register_services(hass) -> None:  # type: ignore[no-untyped-def]
    """Register heo3.snapshot_log + heo3.apply_baseline_static."""

    async def snapshot_log(call) -> None:
        op = _get_operator(hass)
        if op is None:
            logger.error("heo3.snapshot_log: no HEO III config entry loaded")
            return

        _patch_house_config(op)
        try:
            snap = await op.snapshot()
        except Exception as exc:
            logger.exception("heo3.snapshot_log failed: %s", exc)
            return

        logger.info(
            "heo3.snapshot: SOC=%s%%, work_mode=%r, grid_v=%sV, "
            "ev_mode=%r, tesla_at_home=%s, eps=%s, "
            "import_today_slots=%d, solar_today_kwh=%.2f, "
            "load_today_kwh=%.2f",
            snap.inverter.battery_soc_pct,
            snap.inverter_settings.work_mode,
            snap.inverter.grid_voltage_v,
            snap.ev.mode,
            snap.tesla.located_at_home if snap.tesla else None,
            snap.flags.eps_active,
            len(snap.rates_live.import_today),
            sum(snap.solar_forecast.today_p50_kwh),
            sum(snap.load_forecast.today_hourly_kwh),
        )

    async def apply_baseline_static(call) -> None:
        op = _get_operator(hass)
        if op is None:
            logger.error("heo3.apply_baseline_static: no config entry loaded")
            return

        _patch_house_config(op)
        try:
            snap = await op.snapshot()
            plan = op.build.baseline_static(snap)
            result = await op.apply(plan, snapshot=snap)
        except Exception as exc:
            logger.exception("heo3.apply_baseline_static failed: %s", exc)
            return

        logger.info(
            "heo3.apply_baseline_static: plan=%s, requested=%d, "
            "succeeded=%d, failed=%d, duration=%.0fms",
            result.plan_id,
            len(result.requested),
            len(result.succeeded),
            len(result.failed),
            result.duration_ms,
        )
        for fw in result.failed:
            logger.error(
                "heo3.apply_baseline_static FAILED write: %s = %r (reason: %s)",
                fw.write.topic,
                fw.write.payload,
                fw.reason,
            )
        for w in result.succeeded:
            logger.info(
                "heo3.apply_baseline_static OK write: %s = %r",
                w.topic,
                w.payload,
            )

    hass.services.async_register(DOMAIN, "snapshot_log", snapshot_log)
    hass.services.async_register(
        DOMAIN, "apply_baseline_static", apply_baseline_static
    )


def _get_operator(hass) -> Any | None:  # type: ignore[no-untyped-def]
    """Pick the first heo3 operator from hass.data."""
    bucket = hass.data.get(DOMAIN, {})
    for entry_data in bucket.values():
        if isinstance(entry_data, dict) and "operator" in entry_data:
            return entry_data["operator"]
    return None


def _patch_house_config(op) -> None:
    """Monkey-patch BD/Flags/Tesla/appliance config onto the operator
    until the config flow learns to ask for them.

    Idempotent — patching the same operator repeatedly is safe.
    """
    if op._world._bd is None:
        op._world._bd = BDConfig.from_meter_key(DEFAULT_BD_METER_KEY)

    if op._world._flags_cfg.igo_dispatching_entity is None:
        from dataclasses import replace

        op._world._flags_cfg = replace(
            op._world._flags_cfg,
            igo_dispatching_entity=DEFAULT_IGO_DISPATCH_ENTITY,
            saving_session_entity=DEFAULT_SAVING_SESSION_ENTITY,
        )

    if op._peripheral._tesla is None:
        op._peripheral._tesla = TeslaConfig.from_vehicle(DEFAULT_TESLA_VEHICLE)

    if not op._peripheral._appliance_switches:
        op._peripheral._appliance_switches = dict(DEFAULT_APPLIANCES)
        op._peripheral._appliance_running = {
            name: f"binary_sensor.{name}_running"
            for name in DEFAULT_APPLIANCES
        }
