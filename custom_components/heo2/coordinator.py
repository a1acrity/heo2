# custom_components/heo2/coordinator.py
"""HEO II DataUpdateCoordinator — gathers inputs and runs the rule engine."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta, time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DOMAIN,
    UPDATE_INTERVAL_MINUTES,
    DEFAULT_MIN_SOC,
    DEFAULT_DAILY_PLAN_TIME,
    DEFAULT_REPLAN_SOLAR_PCT,
    DEFAULT_REPLAN_LOAD_PCT,
    DEFAULT_REPLAN_SOC_PCT,
    DEFAULT_PEAK_THRESHOLD_PENCE,
    DEFAULT_SELL_TOP_PCT,
    DEFAULT_CHEAP_CHARGE_BOTTOM_PCT,
    DEFAULT_MAX_CHARGE_KW,
    DEFAULT_MAX_DISCHARGE_KW,
    DEFAULT_CHARGE_EFFICIENCY,
    DEFAULT_DISCHARGE_EFFICIENCY,
    POST_WRITE_VERIFY_DELAY_SECONDS,
)
from .models import ProgrammeInputs, ProgrammeState, SlotConfig
from .rule_engine import RuleEngine
from .rules import default_rules
from .load_profile import LoadProfileBuilder
from .solar_forecast import solar_forecast_from_hacs
from .agilepredict_client import AgilePredictClient
from .bottlecapdave_client import (
    BottlecapDaveRates,
    merge_rate_sources,
    read_bottlecapdave_rates,
)
from .appliance_timing import ApplianceTimingCalculator, ApplianceSuggestion
from .const import DEFAULT_APPLIANCES
from .soc_trajectory import calculate_soc_trajectory
from .cost_tracker import CostAccumulator
from .octopus import OctopusBillingFetcher
from .const import DEFAULT_FLAT_RATE_PENCE
from .mqtt_writer import MqttWriter, apply_programme_diff
from .direct_mqtt_transport import DirectMqttTransport
from .inverter_state_reader import read_from_hass as read_inverter_state
from .writes_status import _compute_writes_blocked
from .plan_validator import ValidationResult, validate_plan
from .projection import Projection
from .replan_triggers import (
    BaselineSnapshot,
    capture_baseline,
    should_commit_replan,
)

logger = logging.getLogger(__name__)


class HEO2Coordinator(DataUpdateCoordinator):
    """Coordinator for HEO II: gathers inputs, runs rules, writes programme."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            logger,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self._entry = entry
        self._config = dict(entry.data)

        # Core components
        self._engine = RuleEngine(rules=default_rules())
        self._load_builder = LoadProfileBuilder(
            baseline_w=self._config.get("load_baseline_w", 1900.0)
        )
        # Solar forecast is read from HACS solcast_solar entity attributes
        # rather than via HTTP. See HEO-4 for rationale.
        self._solar_entity = self._config.get(
            "solcast_entity",
            "sensor.solcast_pv_forecast_forecast_today",
        )
        self._agilepredict = AgilePredictClient(
            base_url=self._config.get("agilepredict_url", "https://agilepredict.com"),
            region=self._config.get("agilepredict_region", "M"),
        ) if self._config.get("agilepredict_url") else None
        self._appliance_calc = ApplianceTimingCalculator()

        # MQTT writer for applying programme changes to the inverter.
        # dry_run defaults to True so nothing is written until explicitly
        # enabled via config. See HEO-27.
        self._inverter_name = self._config.get("inverter_name", "inverter_1")
        self._writer_dry_run: bool = bool(self._config.get("dry_run", True))
        # SA broker connection details. Defaults point at Paddy's install;
        # override via config entry for other deployments. No auth required
        # for SA's default broker configuration.
        self._sa_mqtt_host: str = self._config.get("sa_mqtt_host", "192.168.4.7")
        self._sa_mqtt_port: int = int(self._config.get("sa_mqtt_port", 1883))
        self._sa_mqtt_username: str | None = self._config.get("sa_mqtt_username") or None
        self._sa_mqtt_password: str | None = self._config.get("sa_mqtt_password") or None
        self._mqtt_writer: MqttWriter | None = None  # lazy-constructed on first tick
        self._mqtt_transport: DirectMqttTransport | None = None  # owned by this coordinator
        # Source of truth for "what's currently on the inverter". Seeded
        # on first tick from SA-published entities and updated on every
        # successful write.
        self._last_known_programme: ProgrammeState | None = None

        # State
        self.current_programme: ProgrammeState | None = None
        self.last_inputs: ProgrammeInputs | None = None
        self.appliance_suggestions: dict[str, ApplianceSuggestion] = {}
        self.enabled: bool = True
        self.healthy: bool = True
        # H4 (live-prices-only writes): cleared when BottlecapDave returns
        # no live rate data for the current tick. Used by writes_blocked
        # property and as a guard before MQTT writes go out. Defaults True
        # so the first tick (before _gather_inputs) doesn't spuriously
        # block; the subsequent tick with real data sets the actual value.
        self._live_rates_present: bool = True
        self._bottlecapdave_meter_key: str | None = None

        # SPEC §8 planning lifecycle. The baseline is the programme
        # currently being driven into the inverter; the rule engine
        # runs every tick for sensors/diagnostics but only swaps the
        # baseline when the daily 18:00 window fires or a trigger
        # condition deviates the world from the baseline-time snapshot.
        self._baseline: BaselineSnapshot | None = None
        # SPEC §6 H5 / §7 H6: pre-write validation + post-write verify
        # state. Reasons surface via writes_blocked_reason; the
        # projection feeds sensor.heo_ii_projection_today.
        self._plan_rejected_reason: str | None = None
        self._verify_mismatch_reason: str | None = None
        self._validation_warnings: list[str] = []
        self._last_projection: Projection | None = None
        # The programme currently on the inverter that the next tick
        # should verify. Set after a successful write; cleared after
        # verify completes. Only ever holds a programme that we
        # actively pushed (never the lazy seed).
        self._pending_verification: ProgrammeState | None = None

        # SPEC §10 tunable knobs. Defaults match the SPEC table; any
        # of these will become HA UI number entities under HEO-11.
        self._daily_plan_time: time = self._parse_time_config(
            "daily_plan_time", DEFAULT_DAILY_PLAN_TIME,
        )
        self._replan_solar_pct: float = self._config.get(
            "replan_solar_pct", DEFAULT_REPLAN_SOLAR_PCT,
        )
        self._replan_load_pct: float = self._config.get(
            "replan_load_pct", DEFAULT_REPLAN_LOAD_PCT,
        )
        self._replan_soc_pct: float = self._config.get(
            "replan_soc_pct", DEFAULT_REPLAN_SOC_PCT,
        )
        self._peak_threshold_p: float = self._config.get(
            "peak_threshold_p", DEFAULT_PEAK_THRESHOLD_PENCE,
        )
        self._sell_top_pct_default: int = self._config.get(
            "sell_top_pct_default", DEFAULT_SELL_TOP_PCT,
        )
        self._cheap_bottom_pct: int = self._config.get(
            "cheap_charge_bottom_pct", DEFAULT_CHEAP_CHARGE_BOTTOM_PCT,
        )
        self._max_charge_kw: float = self._config.get(
            "max_charge_kw", DEFAULT_MAX_CHARGE_KW,
        )
        self._max_discharge_kw: float = self._config.get(
            "max_discharge_kw", DEFAULT_MAX_DISCHARGE_KW,
        )
        self._charge_efficiency: float = self._config.get(
            "charge_efficiency", DEFAULT_CHARGE_EFFICIENCY,
        )
        self._discharge_efficiency: float = self._config.get(
            "discharge_efficiency", DEFAULT_DISCHARGE_EFFICIENCY,
        )

        # Dashboard state
        self.soc_trajectory: list[float] = [0.0] * 24
        self.cost_accumulator = CostAccumulator()
        self.octopus: OctopusBillingFetcher | None = None

        # ROI state (seeded from config)
        self._savings_to_date = self._config.get("savings_to_date", 0.0)
        self._total_accumulated_savings = 0.0

        # Octopus billing (optional)
        if self._config.get("octopus_api_key"):
            self.octopus = OctopusBillingFetcher(
                api_key=self._config["octopus_api_key"],
                mpan=self._config.get("octopus_mpan", ""),
                serial=self._config.get("octopus_serial", ""),
                product_code=self._config.get("octopus_product_code", ""),
                tariff_code=self._config.get("octopus_tariff_code", ""),
            )

    async def _async_update_data(self) -> ProgrammeState:
        """Gather inputs, run rules, decide whether to commit a fresh
        plan or hold the baseline, validate, write, verify."""
        inputs = await self._gather_inputs()
        self.last_inputs = inputs

        # Verify the previously-written programme BEFORE running new
        # rules: SA's polling will have caught up by now (15-min tick
        # spacing) so a mismatch is real rather than a timing artefact.
        await self._verify_pending_programme()

        # Always run the rule engine for sensors and diagnostics. The
        # 15-min cadence updates dashboard signals every tick even
        # when the inverter programme stays on the baseline.
        fresh = self._engine.calculate(inputs)
        self.current_programme = fresh

        # SPEC §8: decide whether this tick commits a new baseline or
        # holds the previous one. The trigger logic considers daily-
        # plan-time, IGO/saving session/grid restore transitions, and
        # quantitative deviations vs the captured baseline forecasts.
        tz = self._local_tz()
        decision = should_commit_replan(
            new_programme=fresh,
            inputs=inputs,
            baseline=self._baseline,
            tz=tz,
            daily_plan_time=self._daily_plan_time,
            replan_solar_pct=self._replan_solar_pct,
            replan_load_pct=self._replan_load_pct,
            replan_soc_pct=self._replan_soc_pct,
        )
        is_daily_plan = "daily plan" in decision.reason

        if decision.commit:
            effective = fresh
            logger.warning("HEO-31 PR2: committing new plan: %s", decision.reason)
        else:
            effective = self._baseline.programme if self._baseline else fresh
            logger.debug("HEO-31 PR2: %s", decision.reason)

        # SPEC §6 H5: pre-write validate the EFFECTIVE programme (the
        # one that will be diffed against the inverter). If it fails,
        # the baseline stays unchanged and writes_blocked surfaces the
        # rejection reason.
        validation = validate_plan(
            effective, inputs,
            peak_threshold_p=self._peak_threshold_p,
            cheap_bottom_pct=self._cheap_bottom_pct,
            max_charge_kw=self._max_charge_kw,
            max_discharge_kw=self._max_discharge_kw,
            charge_efficiency=self._charge_efficiency,
            discharge_efficiency=self._discharge_efficiency,
        )
        self._validation_warnings = validation.warnings
        self._last_projection = validation.projection

        for w in validation.warnings:
            logger.warning("HEO-31 PR2 H5 warning: %s", w)

        if not validation.passed:
            self._plan_rejected_reason = validation.reason()[len("plan rejected: "):]
            for e in validation.errors:
                logger.warning("HEO-31 PR2 H5 reject: %s", e)
            # Don't update baseline, don't write. Sensors still reflect
            # the fresh rule output for dashboard transparency.
            self._update_dashboard_secondary(inputs, fresh)
            return fresh

        self._plan_rejected_reason = None

        # Validation passed. Promote the candidate baseline only when
        # the trigger said "commit"; otherwise we stay on the existing
        # baseline (which is what we just successfully validated).
        if decision.commit:
            self._baseline = capture_baseline(
                fresh, inputs, tz=tz, is_daily_plan=is_daily_plan,
            )

        # Apply programme to inverter via MQTT if enabled.
        # Any failure is logged but does NOT abort the tick - the
        # coordinator still returns a valid programme and the next
        # tick will retry the diff against the un-updated
        # _last_known_programme.
        try:
            await self._apply_programme_to_inverter(effective)
        except Exception:
            logger.exception("HEO-27: apply_programme failed")

        self._update_dashboard_secondary(inputs, fresh)
        return fresh

    def _update_dashboard_secondary(
        self, inputs: ProgrammeInputs, fresh: ProgrammeState,
    ) -> None:
        """Calculate appliance suggestions, SOC trajectory and savings
        from the fresh rule output regardless of write outcome."""
        for name, spec in DEFAULT_APPLIANCES.items():
            self.appliance_suggestions[name] = self._appliance_calc.best_window(
                inputs=inputs,
                draw_kw=spec["draw_kw"],
                duration_hours=int(spec["duration_hours"]),
                appliance_name=name,
            )

        from datetime import datetime, timezone
        current_hour = datetime.now(timezone.utc).hour
        self.soc_trajectory = calculate_soc_trajectory(
            current_soc=inputs.current_soc,
            solar_forecast_kwh=inputs.solar_forecast_kwh,
            load_forecast_kwh=inputs.load_forecast_kwh,
            programme_slots=fresh.slots,
            battery_capacity_kwh=self._config.get("battery_capacity_kwh", 20.0),
            max_charge_kw=self._max_charge_kw,
            charge_efficiency=self._charge_efficiency,
            discharge_efficiency=self._discharge_efficiency,
            min_soc=self._config.get("min_soc", 20.0),
            max_soc=self._config.get("max_soc", 100.0),
            current_hour=current_hour,
        )

        flat_rate = self._config.get("flat_rate_pence", DEFAULT_FLAT_RATE_PENCE)
        self.cost_accumulator.calculate_savings_vs_flat(flat_rate)

    def _local_tz(self):
        """Resolve HA's local timezone, defaulting to UTC."""
        from zoneinfo import ZoneInfo
        tz_name = (
            self.hass.config.time_zone
            if self.hass and self.hass.config.time_zone
            else "UTC"
        )
        return ZoneInfo(tz_name)

    def _parse_time_config(self, key: str, default: str) -> time:
        """Parse a "HH:MM" config value into a `time` object."""
        raw = str(self._config.get(key, default))
        try:
            h, m = raw.split(":", 1)
            return time(int(h), int(m))
        except (ValueError, TypeError):
            logger.warning(
                "HEO-31 PR2: invalid %s=%r in config; falling back to %s",
                key, raw, default,
            )
            h, m = default.split(":", 1)
            return time(int(h), int(m))

    async def _verify_pending_programme(self) -> None:
        """SPEC §7 H6: read inverter state back and compare to the
        programme we wrote on the previous tick.

        On mismatch: log ERROR, set verify_mismatch_reason which drives
        writes_blocked, and leave `_last_known_programme` un-advanced
        so the next diff retries the missing fields.
        """
        if self._pending_verification is None:
            return
        observed = read_inverter_state(
            self.hass, inverter_name=self._inverter_name,
        )
        if observed is None:
            # SA entities still not populated. Defer verification one
            # more tick rather than spuriously alerting.
            logger.info(
                "HEO-31 PR2 H6: SA entities unavailable; deferring verify",
            )
            return

        mismatches = self._diff_programmes(observed, self._pending_verification)
        if mismatches:
            self._verify_mismatch_reason = (
                f"slot {mismatches[0][0]} {mismatches[0][1]} "
                f"sent={mismatches[0][2]} got={mismatches[0][3]}"
                + (f" (+{len(mismatches) - 1} more)"
                   if len(mismatches) > 1 else "")
            )
            logger.error(
                "HEO-31 PR2 H6: post-write verify mismatch: %s",
                self._verify_mismatch_reason,
            )
            # Do NOT advance _last_known_programme - the next tick's
            # diff against `observed` will spot the missing writes
            # and retry them.
            self._last_known_programme = observed
        else:
            self._verify_mismatch_reason = None
            logger.warning(
                "HEO-31 PR2 H6: post-write verify OK across all 6 slots",
            )

        self._pending_verification = None

    @staticmethod
    def _diff_programmes(
        observed: ProgrammeState, intended: ProgrammeState,
    ) -> list[tuple[int, str, str, str]]:
        """Return (slot_num, field, sent_value, observed_value) for
        every slot field that differs between intended and observed.
        Used by the post-write verify to surface concrete mismatches.
        """
        out: list[tuple[int, str, str, str]] = []
        for i in range(6):
            o = observed.slots[i]
            n = intended.slots[i]
            if o.start_time != n.start_time:
                out.append((
                    i + 1, "time_point",
                    n.start_time.strftime("%H:%M"),
                    o.start_time.strftime("%H:%M"),
                ))
            if o.capacity_soc != n.capacity_soc:
                out.append((
                    i + 1, "capacity",
                    str(n.capacity_soc), str(o.capacity_soc),
                ))
            if o.grid_charge != n.grid_charge:
                out.append((
                    i + 1, "grid_charge",
                    str(n.grid_charge), str(o.grid_charge),
                ))
        return out

    async def _apply_programme_to_inverter(
        self, new_programme: ProgrammeState,
    ) -> None:
        """Diff new_programme vs last-known inverter state and write changes.

        Lazy-seeds the writer and last-known state on the first call after
        HA startup, when SA's MQTT discovery has populated the entities.
        """
        # SPEC H4 (Live-prices-only writes): if BottlecapDave returned no
        # live rates this tick the programme may have been driven by
        # AgilePredict (forecast) or IGO fixed-rate slots, not by prices
        # Octopus has actually published. Skip the write entirely - the
        # next tick will retry once BD is back. The writes_blocked sensor
        # already reflects this state for the dashboard.
        if not self._live_rates_present:
            logger.warning(
                "HEO-14: skipping inverter write - no live BottlecapDave "
                "rates (SPEC H4); will retry next tick",
            )
            return

        # Lazy construct writer on first use. HA startup sequence can race
        # with mqtt component availability, hence deferring past __init__.
        # The DirectMqttTransport connects directly to SA's broker rather
        # than going through HA's local mosquitto + bridge, because adding
        # outbound to the bridge config kills inbound telemetry in
        # mosquitto 2.1.2. See HEO-27 discussion.
        if self._mqtt_writer is None:
            try:
                transport = DirectMqttTransport(
                    loop=asyncio.get_event_loop(),
                    host=self._sa_mqtt_host,
                    port=self._sa_mqtt_port,
                    username=self._sa_mqtt_username,
                    password=self._sa_mqtt_password,
                    client_id="heo2_writer",
                )
                await transport.connect()
            except Exception:
                logger.exception(
                    "HEO-27: DirectMqttTransport connect to %s:%d failed; "
                    "will retry on next tick",
                    self._sa_mqtt_host, self._sa_mqtt_port,
                )
                return

            self._mqtt_transport = transport
            self._mqtt_writer = MqttWriter(
                transport=transport,
                inverter_name=self._inverter_name,
                dry_run=self._writer_dry_run,
            )
            logger.warning(
                "HEO-27: MqttWriter ready via DirectMqttTransport "
                "(broker=%s:%d, inverter=%s, dry_run=%s)",
                self._sa_mqtt_host, self._sa_mqtt_port,
                self._inverter_name, self._writer_dry_run,
            )

        # Lazy seed last-known state on first tick.
        # read_inverter_state returns None if SA's MQTT-discovered
        # entities haven't been populated yet (HA startup race). In that
        # case we skip seeding and retry next tick. Writing against bogus
        # seed values would produce spurious SA log entries.
        if self._last_known_programme is None:
            seeded = read_inverter_state(
                self.hass, inverter_name=self._inverter_name,
            )
            if seeded is None:
                logger.warning(
                    "HEO-27: deferring seed - SA entities not yet populated "
                    "in HA (discovery still in progress?); retry next tick",
                )
                return  # No writer activity until we have a real baseline
            self._last_known_programme = seeded
            logger.warning(
                "HEO-27: seeded last-known programme from HA entities "
                "(slot1 cap=%d gc=%s, slot3 cap=%d)",
                self._last_known_programme.slots[0].capacity_soc,
                self._last_known_programme.slots[0].grid_charge,
                self._last_known_programme.slots[2].capacity_soc,
            )

        writes = self._mqtt_writer.diff(
            self._last_known_programme, new_programme,
        )
        if not writes:
            logger.debug("HEO-27: no diffs, nothing to write")
            return

        logger.warning(
            "HEO-27: %d slot write(s) needed (dry_run=%s)",
            len(writes), self._writer_dry_run,
        )

        result, self._last_known_programme = await apply_programme_diff(
            self._mqtt_writer,
            self._last_known_programme,
            new_programme,
        )

        if result.dry_run_log:
            for line in result.dry_run_log:
                logger.warning("HEO-27: %s", line)

        if result.success:
            logger.warning(
                "HEO-27: %d/%d writes confirmed",
                result.writes_confirmed, result.writes_attempted,
            )
            # SPEC §7 H6: queue a post-write verification for the next
            # tick. We don't sleep-and-verify inline because SA's
            # poll-then-publish cycle can take 5-10s and we don't want
            # to extend the coordinator tick. The 15-min spacing
            # between ticks is more than sufficient for SA to have
            # caught up and the entities to reflect reality.
            self._pending_verification = new_programme
        else:
            logger.warning(
                "HEO-27: write failed at slot %s param %s: %s "
                "(%d/%d confirmed before failure); will retry next tick",
                result.failed_slot, result.failed_param,
                result.failed_reason,
                result.writes_confirmed, result.writes_attempted,
            )

    async def _gather_inputs(self) -> ProgrammeInputs:
        """Build ProgrammeInputs from HA entities and external APIs."""
        from datetime import datetime, timezone, time

        now = datetime.now(timezone.utc)

        current_soc = self._read_entity_float(
            self._config.get("soc_entity", ""), default=50.0
        )
        igo_dispatching = self._read_entity_bool(
            self._config.get("igo_dispatch_entity", ""), default=False
        )
        saving_session = self._read_entity_bool(
            self._config.get("saving_session_entity", ""), default=False
        )
        ev_charging = self._read_entity_bool(
            self._config.get("ev_status_entity", ""), default=False
        )

        solar = self._read_solar_forecast(now)
        solar_tomorrow = self._read_solar_forecast(
            now, entity_override=self._config.get(
                "solcast_tomorrow_entity",
                "sensor.solcast_pv_forecast_forecast_tomorrow",
            ),
            target_offset_days=1,
        )

        # HEO-14: BottlecapDave is the PRIMARY rate source per SPEC H4.
        # AgilePredict (export) and IGO fixed-rate generator (import) are
        # fallback only - to extend coverage past BD's horizon, or as a
        # backstop when BD's entities aren't yet populated. Writes are
        # blocked when BD returns nothing (see _live_rates_present).
        bd_rates = read_bottlecapdave_rates(self.hass)
        live_import_rates = list(bd_rates.import_today) + list(bd_rates.import_tomorrow)
        live_export_rates = list(bd_rates.export_today) + list(bd_rates.export_tomorrow)

        forecast_export_rates: list = []
        if self._agilepredict:
            forecast_export_rates = await self._agilepredict.fetch_export_rates()

        igo_import_rates = self._build_import_rates(now)

        # Merged "best available" set: BD wins for any window it covers,
        # forecast/IGO-fixed fills the tail. Rules and dashboard sensors
        # consume these; the live_* subsets are reserved for H4 enforcement.
        import_rates = merge_rate_sources(live_import_rates, igo_import_rates)
        export_rates = merge_rate_sources(live_export_rates, forecast_export_rates)

        # H4 gate. Conservative AND: writes need both directions live.
        # Either being empty means a rule could be acting on forecast
        # data, which violates SPEC H4 once it reaches the inverter.
        prev_present = self._live_rates_present
        self._live_rates_present = bool(live_import_rates) and bool(live_export_rates)
        self._bottlecapdave_meter_key = (
            f"import={bd_rates.import_meter_key} export={bd_rates.export_meter_key}"
        )

        if not self._live_rates_present:
            logger.warning(
                "HEO-14: BottlecapDave incomplete (import=%d slots key=%s, "
                "export=%d slots key=%s); blocking writes per SPEC H4",
                len(live_import_rates), bd_rates.import_meter_key,
                len(live_export_rates), bd_rates.export_meter_key,
            )
        elif not prev_present:
            logger.warning(
                "HEO-14: BottlecapDave rates restored (import_key=%s %d slots, "
                "export_key=%s %d slots); writes unblocked",
                bd_rates.import_meter_key, len(live_import_rates),
                bd_rates.export_meter_key, len(live_export_rates),
            )

        load_profile = self._load_builder.build()
        load_forecast = load_profile.for_datetime(now)

        return ProgrammeInputs(
            now=now,
            current_soc=current_soc,
            battery_capacity_kwh=self._config.get("battery_capacity_kwh", 20.0),
            min_soc=self._config.get("min_soc", DEFAULT_MIN_SOC),
            import_rates=import_rates,
            export_rates=export_rates,
            solar_forecast_kwh=solar,
            load_forecast_kwh=load_forecast,
            igo_dispatching=igo_dispatching,
            saving_session=saving_session,
            saving_session_start=None,
            saving_session_end=None,
            ev_charging=ev_charging,
            grid_connected=True,
            active_appliances=[],
            appliance_expected_kwh=0.0,
            live_import_rates=live_import_rates,
            live_export_rates=live_export_rates,
            solar_forecast_kwh_tomorrow=solar_tomorrow,
        )

    def _read_entity_float(self, entity_id: str, default: float) -> float:
        if not entity_id:
            return default
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return default
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return default

    def _read_entity_bool(self, entity_id: str, default: bool) -> bool:
        if not entity_id:
            return default
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return default
        return state.state.lower() in ("on", "true", "1")

    def _read_solar_forecast(
        self,
        now,
        entity_override: str | None = None,
        target_offset_days: int = 0,
    ) -> list[float]:
        """Read solar forecast from HACS solcast_solar sensor attributes.

        Returns 24 hourly kWh values, index 0 = 00:00 local time for the
        target date. Defaults to today; pass `target_offset_days=1` to
        read tomorrow's forecast (used by rank-based pricing per SPEC §5a).

        `entity_override` selects a different Solcast sensor, e.g. the
        tomorrow-specific one. Returns zeros if the sensor is missing or
        has no detailedHourly attribute. See HEO-4 for the history: HEO II
        previously made its own Solcast HTTP calls and mis-aggregated the
        result.
        """
        from datetime import timedelta
        from zoneinfo import ZoneInfo
        tz_name = (self.hass.config.time_zone
                   if self.hass and self.hass.config.time_zone
                   else "UTC")
        tz = ZoneInfo(tz_name)
        target_date = (
            now.astimezone(tz).date() + timedelta(days=target_offset_days)
        )

        entity = entity_override or self._solar_entity
        state = self.hass.states.get(entity) if self.hass else None
        if state is None or state.state in ("unknown", "unavailable"):
            logger.warning(
                "Solar forecast entity %s not available, using zero forecast",
                entity,
            )
            return [0.0] * 24

        detailed = state.attributes.get("detailedHourly") or []
        if not detailed:
            logger.warning(
                "Solar forecast entity %s has no detailedHourly attribute",
                entity,
            )
            return [0.0] * 24

        return solar_forecast_from_hacs(detailed, target_date=target_date)

    async def async_refresh_load_profile_from_recorder(
        self, days_back: int = 14,
    ) -> int:
        """Seed LoadProfileBuilder from HA recorder history.

        Queries the last ``days_back`` days of state history for the
        configured ``load_power_entity``, aggregates by hour into kWh,
        and calls ``LoadProfileBuilder.add_day()`` for each covered date.

        Intended to be called once on startup as a fire-and-forget task
        so the integration does not block on a recorder query. Safe to
        call again later; add_day() appends samples rather than replacing,
        so repeated calls grow the median window rather than overwrite.

        Returns the number of days successfully added. Zero on any
        failure (recorder unavailable, no entity, no history).

        See HEO-5 for history. Scheduled daily refresh and Store-backed
        persistence are tracked as follow-up enhancements.
        """
        from zoneinfo import ZoneInfo
        from datetime import datetime, timedelta, timezone as _tz

        entity_id = self._config.get("load_power_entity", "")
        if not entity_id:
            logger.debug("No load_power_entity configured; skipping history learn")
            return 0

        try:
            from homeassistant.components.recorder import (
                get_instance,
                history,
            )
        except ImportError:
            logger.warning(
                "HA recorder component not available; load profile cannot learn"
            )
            return 0

        now_utc = datetime.now(_tz.utc)
        start_utc = now_utc - timedelta(days=days_back)

        try:
            recorder_instance = get_instance(self.hass)
            raw = await recorder_instance.async_add_executor_job(
                history.get_significant_states,
                self.hass,
                start_utc,
                now_utc,
                [entity_id],
            )
        except Exception as exc:  # broad: recorder errors vary
            logger.warning(
                "Recorder history fetch failed for %s: %s", entity_id, exc
            )
            return 0

        states = raw.get(entity_id) if isinstance(raw, dict) else None
        if not states:
            logger.info(
                "No recorder history for %s in last %d days; "
                "load profile stays at baseline",
                entity_id, days_back,
            )
            return 0

        from .load_history import (
            learn_days_from_samples,
            states_to_power_samples,
        )

        samples = states_to_power_samples(states)
        if not samples:
            logger.info(
                "No parseable power samples for %s; load profile stays at baseline",
                entity_id,
            )
            return 0
        tz_name = (self.hass.config.time_zone
                   if self.hass and self.hass.config.time_zone
                   else "UTC")
        tz = ZoneInfo(tz_name)

        # Detect whether this entity reports instantaneous power (W) or
        # a cumulative energy counter (kWh). Order of precedence:
        #   1. Explicit config override (load_source_type)
        #   2. state_class attribute (total_increasing -> cumulative)
        #   3. device_class attribute (energy -> cumulative, power -> watts)
        #   4. unit_of_measurement (kwh -> cumulative, w -> watts)
        # state_class and device_class are more reliable than unit because
        # MQTT-discovered sensors can publish the value before the unit
        # attribute is set, and checking only unit leads to a race at
        # startup (observed in production 2026-04-19).
        configured_type = self._config.get("load_source_type", "").lower()
        if configured_type in ("cumulative_kwh", "power_watts"):
            source_type = configured_type
            detect_reason = "config override"
        else:
            entity_state = self.hass.states.get(entity_id)
            attrs = entity_state.attributes if entity_state else {}
            state_class = str(attrs.get("state_class", "")).lower()
            device_class = str(attrs.get("device_class", "")).lower()
            unit = str(attrs.get("unit_of_measurement", "")).lower()

            if state_class in ("total_increasing", "total") or device_class == "energy":
                source_type = "cumulative_kwh"
                detect_reason = f"state_class={state_class!r} device_class={device_class!r}"
            elif unit in ("kwh", "mwh"):
                source_type = "cumulative_kwh"
                detect_reason = f"unit={unit!r}"
            elif device_class == "power" or unit in ("w", "kw"):
                source_type = "power_watts"
                detect_reason = f"device_class={device_class!r} unit={unit!r}"
            else:
                source_type = "power_watts"
                detect_reason = "default (ambiguous)"

        logger.warning(
            "HEO-5: learning from entity=%s source_type=%s (%s)",
            entity_id, source_type, detect_reason,
        )

        days = learn_days_from_samples(samples, tz, source_type=source_type)
        for d, hourly_kwh in days.items():
            # Convert date to datetime at midnight for the builder's
            # existing weekday-or-weekend branching logic.
            date_midnight = datetime(d.year, d.month, d.day, tzinfo=tz)
            self._load_builder.add_day(date_midnight, hourly_kwh)

        logger.info(
            "Load profile learned from %d samples across %d days for %s",
            len(samples), len(days), entity_id,
        )
        return len(days)

    def _build_import_rates(self, now) -> list:
        """Build IGO import rate slots relative to `now`.

        Delegates to `heo2.igo_rates.build_igo_import_rates` so the boundary
        maths is unit-tested in isolation. Local timezone comes from HA config
        to keep the night-rate window aligned with real clock time under DST.
        See docs/bugs.md HEO-1 for history.
        """
        from zoneinfo import ZoneInfo
        from .igo_rates import build_igo_import_rates
        from .const import DEFAULT_IGO_NIGHT_RATE_PENCE, DEFAULT_IGO_DAY_RATE_PENCE

        tz_name = (self.hass.config.time_zone
                   if self.hass and self.hass.config.time_zone
                   else "UTC")
        tz = ZoneInfo(tz_name)
        return build_igo_import_rates(
            now=now,
            tz=tz,
            night_start=self._config.get("igo_night_start", "23:30"),
            night_end=self._config.get("igo_night_end", "05:30"),
            night_rate_pence=self._config.get(
                "igo_night_rate", DEFAULT_IGO_NIGHT_RATE_PENCE
            ),
            day_rate_pence=self._config.get(
                "igo_day_rate", DEFAULT_IGO_DAY_RATE_PENCE
            ),
        )

    @property
    def total_savings(self) -> float:
        """Cumulative savings: seed value + accumulated from cost tracker."""
        return self._savings_to_date + self._total_accumulated_savings

    @property
    def system_cost(self) -> float:
        return self._config.get("system_cost", 16800.0)

    @property
    def additional_costs(self) -> float:
        return self._config.get("additional_costs", 0.0)

    @property
    def payback_progress(self) -> float:
        """Percentage progress towards payback (0-100)."""
        total_cost = self.system_cost + self.additional_costs
        if total_cost <= 0:
            return 100.0
        return min(100.0, (self.total_savings / total_cost) * 100.0)

    @property
    def estimated_payback_date(self) -> str | None:
        """Project payback date based on current savings rate."""
        from datetime import datetime, timezone, timedelta
        install_date_str = self._config.get("install_date", "2025-02-01")
        try:
            install_date = datetime.strptime(install_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None

        now = datetime.now(timezone.utc)
        days_elapsed = max(1, (now - install_date).days)
        daily_savings = self.total_savings / days_elapsed

        if daily_savings <= 0:
            return None

        total_cost = self.system_cost + self.additional_costs
        remaining = total_cost - self.total_savings
        if remaining <= 0:
            return "Paid back"

        days_remaining = remaining / daily_savings
        payback_date = now + timedelta(days=days_remaining)
        return payback_date.strftime("%Y-%m-%d")

    @property
    def active_rule_names(self) -> list[str]:
        """List of currently active rule names."""
        return [r.name for r in self._engine._rules if r.enabled]

    @property
    def writes_blocked(self) -> bool:
        """True when HEO II cannot currently send programme changes to
        the inverter. Drives binary_sensor.heo_ii_writes_blocked so the
        dashboard can show an alert.

        Blocked conditions:
          - dry_run is True (writes suppressed by config)
          - MqttWriter hasn't been constructed yet (early startup)
          - DirectMqttTransport exists but is not connected
          - HEO-14: BottlecapDave returned no live rates (SPEC H4)
          - HEO-31 PR2: pre-write validator rejected the plan (H5)
          - HEO-31 PR2: post-write verify saw a mismatch (H6)
        """
        blocked, _ = _compute_writes_blocked(
            dry_run=self._writer_dry_run,
            writer_constructed=self._mqtt_writer is not None,
            transport_exists=self._mqtt_transport is not None,
            transport_connected=(
                self._mqtt_transport.is_connected
                if self._mqtt_transport is not None else False
            ),
            host=self._sa_mqtt_host,
            live_rates_present=self._live_rates_present,
            plan_rejected_reason=self._plan_rejected_reason,
            verify_mismatch_reason=self._verify_mismatch_reason,
        )
        return blocked

    @property
    def writes_blocked_reason(self) -> str:
        """Short human-readable reason matching writes_blocked, for the
        binary sensor's state attributes. Returns '' when not blocked."""
        _, reason = _compute_writes_blocked(
            dry_run=self._writer_dry_run,
            writer_constructed=self._mqtt_writer is not None,
            transport_exists=self._mqtt_transport is not None,
            transport_connected=(
                self._mqtt_transport.is_connected
                if self._mqtt_transport is not None else False
            ),
            host=self._sa_mqtt_host,
            live_rates_present=self._live_rates_present,
            plan_rejected_reason=self._plan_rejected_reason,
            verify_mismatch_reason=self._verify_mismatch_reason,
        )
        return reason

    @property
    def projection_today(self) -> Projection | None:
        """Latest 24h forecast of expected return given the effective
        programme. Populated even when validation rejected the plan,
        so the dashboard always shows the projected outcome of what
        the rules just produced.
        """
        return self._last_projection

    @property
    def validation_warnings(self) -> list[str]:
        return list(self._validation_warnings)
