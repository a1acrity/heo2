"""HEO III — Energy Optimiser (operator + planner module).

Tracking issues:
- Operator: https://github.com/a1acrity/heo2/issues/75
- Planner:  https://github.com/a1acrity/heo2/issues/90

Design docs:
- Operator: docs/HEO_III_DESIGN.md
- Planner:  docs/HEO_III_PLANNER_DESIGN.md

The integration constructs:
- PahoTransport (paho-MQTT direct to SA's broker)
- HAStateReader / HAServiceCaller (HA-backed adapters)
- Operator with auto-discovered config (BD / IGO / Tesla / zappi /
  appliances / inverter sensor overrides / Deye-Sunsynk read-back)
- PerformanceTracker (rolling 30-day per-tick records)
- Coordinator (15-min cron + event-driven debounced ticks)
- Planner (rule engine when enabled, StaticBaselinePlanner otherwise)
- Two HA switches: heo3_planner_enabled (default ON) +
  heo3_tuner_enabled (default OFF)

Coordinator runs autonomously once setup completes.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from .adapters.peripheral import TeslaConfig, ZappiConfig
from .adapters.world import BDConfig, FlagsConfig
from .build import ActionBuilder
from .compute import Compute
from .const import DEFAULT_MQTT_HOST, DEFAULT_MQTT_PORT, DOMAIN
from .coordinator import Coordinator, StaticBaselinePlanner, TickRecord
from .discovery import discover_all
from .operator import Operator
from .performance_tracker import PerformanceTracker, TickStore
from .planner.arbiter import Arbiter
from .planner.digest import build_digest, publish_digest_sensor
from .planner.engine import RuleEngine, SwitchablePlanner
from .planner.rule import HistoricalView
from .planner.rules import ALL_RULES
from .planner.tuner import Tuner
from .service_caller_ha import HAServiceCaller
from .services import async_register_services
from .state_reader_ha import HAStateReader
from .switch import is_planner_enabled, is_tuner_enabled
from .transport_paho import PahoTransport

# HA imports happen lazily inside async_setup_entry — keeps the
# module importable from pytest without HA installed.
CONF_HOST = "host"
CONF_PORT = "port"

logger = logging.getLogger(__name__)

PLATFORMS: list[str] = ["switch"]

DEFAULT_APPLIANCES = {
    "washer": "switch.washer",
    "dryer": "switch.dryer",
    "dishwasher": "switch.dishwasher",
}

# Cron tick cadence for the coordinator.
TICK_INTERVAL = timedelta(minutes=15)

# Entities the coordinator watches for state-change-triggered ticks.
EVENT_TRIGGER_ENTITIES = (
    "binary_sensor.octopus_energy_00000000_0009_4000_8020_000000032ba2_intelligent_dispatching",
    "binary_sensor.octopus_energy_a_8e04cfcf_octoplus_saving_sessions",
    # EPS detection lives on the inverter grid voltage sensor.
    "sensor.sa_inverter_1_grid_voltage",
)


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
        deye_settings_prefix=discovered["deye_prefix"],
    )

    # Performance tracker
    tracker = PerformanceTracker(TickStore(hass, entry.entry_id))
    await tracker.async_init()

    # Planner: rule engine wrapped in a SwitchablePlanner so the user
    # can toggle between rule engine and static fallback via
    # switch.heo3_planner_enabled (default ON).
    rules = [cls() for cls in ALL_RULES]
    rule_engine = RuleEngine(
        rules,
        compute=operator.compute,
        arbiter=Arbiter(operator.build),
        get_historical=lambda: HistoricalView(
            load_forecast_mean_pct_error=tracker.load_forecast_error.mean_pct_error,
            solar_forecast_mean_pct_error=tracker.solar_forecast_error.mean_pct_error,
        ),
    )
    fallback = StaticBaselinePlanner(operator)
    planner = SwitchablePlanner(
        rule_engine, fallback,
        is_enabled=lambda: is_planner_enabled(hass),
    )

    # Tuner — daily 03:00 cron. Default OFF.
    tuner = Tuner(
        rule_engine, tracker,
        is_enabled=lambda: is_tuner_enabled(hass),
    )

    # Coordinator + on_tick callback that feeds tracker + sensors.
    async def on_tick(record: TickRecord) -> None:
        try:
            await tracker.record(record)
        except Exception:
            logger.exception("HEO III: tracker.record failed")
        try:
            _publish_decision_sensors(hass, record)
        except Exception:
            logger.exception("HEO III: decision sensor publish failed")

    coordinator = Coordinator(
        operator=operator, planner=planner, on_tick=on_tick
    )

    # HA cron + event triggers
    from homeassistant.helpers.event import (
        async_track_state_change_event,
        async_track_time_change,
        async_track_time_interval,
    )

    async def _cron_callback(_now) -> None:
        await coordinator.tick(reason="cron")

    cancel_cron = async_track_time_interval(
        hass, _cron_callback, TICK_INTERVAL
    )

    async def _state_change_callback(event) -> None:
        eid = event.data.get("entity_id", "?")
        await coordinator.schedule_debounced_tick(reason=f"event:{eid}")

    cancel_state = async_track_state_change_event(
        hass, list(EVENT_TRIGGER_ENTITIES), _state_change_callback
    )

    # Daily 03:00 local Tuner cron (post-overnight-charge).
    async def _tuner_callback(_now) -> None:
        try:
            await tuner.evaluate_and_adjust()
        except Exception:
            logger.exception("HEO III: tuner.evaluate_and_adjust failed")

    cancel_tuner_cron = async_track_time_change(
        hass, _tuner_callback, hour=3, minute=0, second=0
    )

    # Weekly Sunday 23:55 local digest cron.
    async def _digest_callback(_now) -> None:
        try:
            digest = build_digest(tracker, tuner=tuner)
            publish_digest_sensor(hass, digest)
            logger.info(
                "HEO III: weekly digest published (%d ticks, %d recs)",
                digest.tick_count, len(digest.recommendations),
            )
        except Exception:
            logger.exception("HEO III: digest build failed")

    # async_track_time_change supports a 'weekday' filter via attached
    # logic; simpler: fire every day 23:55, only act on Sundays inside
    # the callback. (HA's native API doesn't accept a weekday filter.)
    async def _maybe_weekly_digest(now) -> None:
        # weekday(): Monday=0, Sunday=6
        if now.weekday() == 6:
            await _digest_callback(now)

    cancel_digest_cron = async_track_time_change(
        hass, _maybe_weekly_digest, hour=23, minute=55, second=0
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "operator": operator,
        "transport": transport,
        "tracker": tracker,
        "coordinator": coordinator,
        "planner": planner,
        "rule_engine": rule_engine,
        "tuner": tuner,
        "discovered": discovered,
        "cancel_cron": cancel_cron,
        "cancel_state": cancel_state,
        "cancel_tuner_cron": cancel_tuner_cron,
        "cancel_digest_cron": cancel_digest_cron,
    }

    await async_register_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Run an initial tick on startup so the system has a snapshot
    # + applied state right away (don't wait 15 min for the first cron).
    # Use create_task so HA setup doesn't block on snapshot/apply latency.
    async def _initial_tick():
        # Brief delay so HA finishes loading other integrations first.
        await asyncio.sleep(30)
        await coordinator.tick(reason="initial")

    asyncio.create_task(_initial_tick())

    logger.info(
        "HEO III setup_entry: operator + coordinator + tracker wired, "
        "transport connected to %s:%d, cron every %s, watching %d entities",
        host, port, TICK_INTERVAL, len(EVENT_TRIGGER_ENTITIES),
    )
    return True


async def async_unload_entry(hass, entry) -> bool:  # type: ignore[no-untyped-def]
    """Tear down."""
    bucket = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if not bucket:
        return True

    # Stop the cron + state-change subscriptions.
    for cancel_key in (
        "cancel_cron", "cancel_state",
        "cancel_tuner_cron", "cancel_digest_cron",
    ):
        cancel = bucket.get(cancel_key)
        if cancel is not None:
            try:
                cancel()
            except Exception:
                logger.exception("HEO III: %s callback raised", cancel_key)

    # Stop coordinator (cancels any pending debounced tick).
    coordinator = bucket.get("coordinator")
    if coordinator is not None:
        try:
            await coordinator.shutdown()
        except Exception:
            logger.exception("HEO III: coordinator shutdown raised")

    # Flush tracker so we don't lose pending records.
    tracker = bucket.get("tracker")
    if tracker is not None:
        try:
            await tracker.flush()
        except Exception:
            logger.exception("HEO III: tracker flush raised")

    # Operator handles transport disconnect.
    operator = bucket.get("operator")
    if operator is not None:
        try:
            await operator.shutdown()
        except Exception:
            logger.exception("HEO III: operator shutdown raised")

    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return True


def _publish_decision_sensors(hass, record: TickRecord) -> None:  # type: ignore[no-untyped-def]
    """Update HA state for sensor.heo3_active_rules + .heo3_last_decision."""
    decision = record.decision
    snap = record.snapshot
    result = record.apply_result

    # Active rules sensor — state is comma-joined names; attrs include
    # full audit.
    hass.states.async_set(
        "sensor.heo3_active_rules",
        ",".join(decision.active_rules) or "(none)",
        attributes={
            "tick_at": record.captured_at.isoformat(),
            "tick_reason": record.reason,
            "claims": list(decision.claims),
            "rationale": decision.rationale,
            "skipped_reason": record.skipped_reason,
        },
    )

    # Last decision sensor
    failed_reasons = (
        [fw.reason for fw in result.failed]
        if result is not None else []
    )
    hass.states.async_set(
        "sensor.heo3_last_decision",
        decision.rationale[:200] if decision.rationale else "no-op",
        attributes={
            "tick_at": record.captured_at.isoformat(),
            "tick_reason": record.reason,
            "plan_id": result.plan_id if result is not None else None,
            "writes_requested": (
                len(result.requested) if result is not None else 0
            ),
            "writes_succeeded": (
                len(result.succeeded) if result is not None else 0
            ),
            "writes_failed": (
                len(result.failed) if result is not None else 0
            ),
            "failed_reasons": failed_reasons,
            "duration_ms": (
                result.duration_ms if result is not None else 0.0
            ),
            "battery_soc_pct": (
                snap.inverter.battery_soc_pct if snap is not None else None
            ),
        },
    )
