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
entries exist (test installs), the first one is used. The operator
is fully configured at setup via discovery — no service-time
patching needed (was a workaround before discovery.py landed).
"""

from __future__ import annotations

import logging
from dataclasses import replace as _dc_replace
from typing import Any

from .adapters.peripheral import TeslaConfig, ZappiConfig
from .adapters.world import BDConfig, FlagsConfig
from .const import DOMAIN
from .discovery import discover_all

logger = logging.getLogger(__name__)


async def async_register_services(hass) -> None:  # type: ignore[no-untyped-def]
    """Register heo3.snapshot_log + heo3.apply_baseline_static."""

    async def snapshot_log(call) -> None:
        op = _get_operator(hass)
        discovered_at_setup = _get_discovered(hass)
        if op is None:
            logger.error("heo3.snapshot_log: no HEO III config entry loaded")
            return

        # Re-discover NOW — entity registry may have changed since
        # async_setup_entry (Teslemetry, SA, etc. often register
        # late). Update operator's adapters with anything new.
        discovered_now = _refresh_operator_config(hass, op)

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
            "discovered_at_setup": discovered_at_setup or {},
            "discovered_now": discovered_now,
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

        _refresh_operator_config(hass, op)

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


def _get_discovered(hass) -> dict | None:  # type: ignore[no-untyped-def]
    bucket = hass.data.get(DOMAIN, {})
    for entry_data in bucket.values():
        if isinstance(entry_data, dict) and "discovered" in entry_data:
            return entry_data["discovered"]
    return None


def _refresh_operator_config(hass, op) -> dict:  # type: ignore[no-untyped-def]
    """Re-run discovery and apply any new findings to operator adapters.

    HA integrations register entities lazily — discovery at
    async_setup_entry often misses Tesla / late-loading SA entities.
    Re-running on each service call makes the operator self-heal.
    Also handles the case where the user installs a new integration
    (e.g. adds zappi) without restarting heo3.
    """
    discovered = discover_all(hass)

    # BD meter key
    if discovered["bd_meter_key"] and op._world._bd is None:
        op._world._bd = BDConfig.from_meter_key(discovered["bd_meter_key"])

    # IGO + saving session
    cfg = op._world._flags_cfg
    needs_update = False
    if discovered["igo_dispatching_entity"] and cfg.igo_dispatching_entity is None:
        needs_update = True
    if discovered["saving_session_entity"] and cfg.saving_session_entity is None:
        needs_update = True
    if needs_update:
        op._world._flags_cfg = _dc_replace(
            cfg,
            igo_dispatching_entity=(
                discovered["igo_dispatching_entity"] or cfg.igo_dispatching_entity
            ),
            saving_session_entity=(
                discovered["saving_session_entity"] or cfg.saving_session_entity
            ),
        )

    # Tesla
    if discovered["tesla_vehicle"] and op._peripheral._tesla is None:
        op._peripheral._tesla = TeslaConfig.from_vehicle(
            discovered["tesla_vehicle"]
        )

    # Zappi (always update — entity prefix doesn't change at runtime
    # but if the user-set defaults at construction were wrong we want
    # to overwrite with what we found).
    zappi_prefix = discovered["zappi_prefix"]
    if zappi_prefix:
        op._peripheral._zappi = ZappiConfig(
            charge_mode=f"select.{zappi_prefix}_charge_mode",
            charging_state=f"sensor.{zappi_prefix}_status",
            charge_power=f"sensor.{zappi_prefix}_power_ct_internal_load",
        )

    # Inverter sensor overrides — merge in any new ones discovered.
    op._inverter._sensor_overrides.update(
        discovered["inverter_sensor_overrides"]
    )

    return discovered
