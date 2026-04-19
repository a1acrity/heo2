# custom_components/heo2/coordinator.py
"""HEO II DataUpdateCoordinator — gathers inputs and runs the rule engine."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, UPDATE_INTERVAL_MINUTES, DEFAULT_MIN_SOC
from .models import ProgrammeInputs, ProgrammeState
from .rule_engine import RuleEngine
from .rules import default_rules
from .load_profile import LoadProfileBuilder
from .solar_forecast import solar_forecast_from_hacs
from .agilepredict_client import AgilePredictClient
from .appliance_timing import ApplianceTimingCalculator, ApplianceSuggestion
from .const import DEFAULT_APPLIANCES
from .soc_trajectory import calculate_soc_trajectory
from .cost_tracker import CostAccumulator
from .octopus import OctopusBillingFetcher
from .const import DEFAULT_FLAT_RATE_PENCE

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
        ) if self._config.get("agilepredict_url") else None
        self._appliance_calc = ApplianceTimingCalculator()

        # State
        self.current_programme: ProgrammeState | None = None
        self.last_inputs: ProgrammeInputs | None = None
        self.appliance_suggestions: dict[str, ApplianceSuggestion] = {}
        self.enabled: bool = True
        self.healthy: bool = True

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
        """Gather inputs, run rules, return new programme."""
        inputs = await self._gather_inputs()
        self.last_inputs = inputs

        programme = self._engine.calculate(inputs)
        self.current_programme = programme

        # Calculate appliance timing suggestions
        for name, spec in DEFAULT_APPLIANCES.items():
            self.appliance_suggestions[name] = self._appliance_calc.best_window(
                inputs=inputs,
                draw_kw=spec["draw_kw"],
                duration_hours=int(spec["duration_hours"]),
                appliance_name=name,
            )

        # Calculate SOC trajectory for dashboard
        from datetime import datetime, timezone
        current_hour = datetime.now(timezone.utc).hour
        self.soc_trajectory = calculate_soc_trajectory(
            current_soc=inputs.current_soc,
            solar_forecast_kwh=inputs.solar_forecast_kwh,
            load_forecast_kwh=inputs.load_forecast_kwh,
            programme_slots=programme.slots,
            battery_capacity_kwh=self._config.get("battery_capacity_kwh", 20.0),
            max_charge_kw=self._config.get("max_charge_kw", 5.0),
            charge_efficiency=self._config.get("charge_efficiency", 0.95),
            discharge_efficiency=self._config.get("discharge_efficiency", 0.95),
            min_soc=self._config.get("min_soc", 20.0),
            max_soc=self._config.get("max_soc", 100.0),
            current_hour=current_hour,
        )

        # Update savings vs flat rate
        flat_rate = self._config.get("flat_rate_pence", DEFAULT_FLAT_RATE_PENCE)
        self.cost_accumulator.calculate_savings_vs_flat(flat_rate)

        return programme

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

        export_rates = []
        if self._agilepredict:
            export_rates = await self._agilepredict.fetch_export_rates()

        load_profile = self._load_builder.build()
        load_forecast = load_profile.for_datetime(now)

        import_rates = self._build_import_rates(now)

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

    def _read_solar_forecast(self, now) -> list[float]:
        """Read solar forecast from HACS solcast_solar sensor attributes.

        Returns 24 hourly kWh values, index 0 = 00:00 local time for today.
        Returns zeros if the sensor is missing or has no detailedHourly
        attribute. See HEO-4 for the history: HEO II previously made its
        own Solcast HTTP calls and mis-aggregated the result.
        """
        from zoneinfo import ZoneInfo
        tz_name = (self.hass.config.time_zone
                   if self.hass and self.hass.config.time_zone
                   else "UTC")
        tz = ZoneInfo(tz_name)
        target_date = now.astimezone(tz).date()

        state = self.hass.states.get(self._solar_entity) if self.hass else None
        if state is None or state.state in ("unknown", "unavailable"):
            logger.warning(
                "Solar forecast entity %s not available, using zero forecast",
                self._solar_entity,
            )
            return [0.0] * 24

        detailed = state.attributes.get("detailedHourly") or []
        if not detailed:
            logger.warning(
                "Solar forecast entity %s has no detailedHourly attribute",
                self._solar_entity,
            )
            return [0.0] * 24

        return solar_forecast_from_hacs(detailed, target_date=target_date)

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
