"""HEO III service registrations for diagnostics + cutover.

Two services exposed via standard HA service-call:

  service: heo3.snapshot_log
    Reads everything (no inverter side-effects). Stores a JSON
    summary on `sensor.heo3_last_snapshot` (and logs at INFO).
    Use to verify reads work before any inverter write.

  service: heo3.apply_baseline_static
    Builds and applies the static baseline plan. Stores a JSON
    summary on `sensor.heo3_last_apply`. THIS WRITES TO THE INVERTER.

Both use the operator on hass.data[DOMAIN][<entry_id>]. If multiple
entries exist (test installs), the first one is used.

State sensors are set via hass.states.async_set rather than registered
as a real platform — this keeps the diagnostic surface trivial to
read via REST without adding a sensor.py.
"""

from __future__ import annotations

import json
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

        summary = {
            "captured_at": snap.captured_at.isoformat(),
            "battery_soc_pct": snap.inverter.battery_soc_pct,
            "work_mode": snap.inverter_settings.work_mode,
            "energy_pattern": snap.inverter_settings.energy_pattern,
            "max_charge_a": snap.inverter_settings.max_charge_a,
            "max_discharge_a": snap.inverter_settings.max_discharge_a,
            "grid_voltage_v": snap.inverter.grid_voltage_v,
            "solar_power_w": snap.inverter.solar_power_w,
            "load_power_w": snap.inverter.load_power_w,
            "ev_mode": snap.ev.mode,
            "tesla_at_home": (
                snap.tesla.located_at_home if snap.tesla else None
            ),
            "tesla_soc": snap.tesla.soc_pct if snap.tesla else None,
            "eps_active": snap.flags.eps_active,
            "igo_dispatching": snap.flags.igo_dispatching,
            "saving_session_active": snap.flags.saving_session_active,
            "import_current_pence": snap.rates_live.import_current_pence,
            "export_current_pence": snap.rates_live.export_current_pence,
            "import_today_count": len(snap.rates_live.import_today),
            "import_tomorrow_count": len(snap.rates_live.import_tomorrow),
            "solar_today_total_kwh": round(
                sum(snap.solar_forecast.today_p50_kwh), 2
            ),
            "solar_tomorrow_total_kwh": round(
                sum(snap.solar_forecast.tomorrow_p50_kwh), 2
            ),
            "load_today_total_kwh": round(
                sum(snap.load_forecast.today_hourly_kwh), 2
            ),
            "min_soc": snap.config.min_soc,
            "slots_current": [
                {
                    "n": i + 1,
                    "start": s.start_hhmm,
                    "gc": s.grid_charge,
                    "cap": s.capacity_pct,
                }
                for i, s in enumerate(snap.inverter_settings.slots)
            ],
        }
        hass.states.async_set(
            "sensor.heo3_last_snapshot",
            "ok",
            attributes=summary,
        )
        logger.info("heo3.snapshot_log OK: SOC=%s%%", snap.inverter.battery_soc_pct)

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

        summary = {
            "plan_id": result.plan_id,
            "rationale": plan.rationale,
            "captured_at": result.captured_at.isoformat(),
            "duration_ms": result.duration_ms,
            "requested_count": len(result.requested),
            "succeeded_count": len(result.succeeded),
            "failed_count": len(result.failed),
            "succeeded": [
                {"topic": w.topic, "payload": w.payload}
                for w in result.succeeded
            ],
            "failed": [
                {
                    "topic": fw.write.topic,
                    "payload": fw.write.payload,
                    "reason": fw.reason,
                }
                for fw in result.failed
            ],
            "verification": result.verification.states,
        }
        state_value = "ok" if not result.failed else "partial_failure"
        if not result.succeeded and not result.failed:
            state_value = "no_op"
        hass.states.async_set(
            "sensor.heo3_last_apply",
            state_value,
            attributes=summary,
        )
        logger.info(
            "heo3.apply_baseline_static: %s requested=%d succeeded=%d failed=%d",
            state_value,
            len(result.requested),
            len(result.succeeded),
            len(result.failed),
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
