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
from .solcast_client import SolcastClient
from .agilepredict_client import AgilePredictClient
from .appliance_timing import ApplianceTimingCalculator, ApplianceSuggestion
from .const import DEFAULT_APPLIANCES

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
        self._solcast = SolcastClient(
            api_key=self._config.get("solcast_api_key", ""),
            resource_id=self._config.get("solcast_resource_id", ""),
        ) if self._config.get("solcast_api_key") else None
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

        solar = [0.0] * 24
        if self._solcast:
            solar = await self._solcast.fetch_forecast()

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

    def _build_import_rates(self, now) -> list:
        from datetime import timezone
        from .models import RateSlot
        from .const import DEFAULT_IGO_NIGHT_RATE_PENCE, DEFAULT_IGO_DAY_RATE_PENCE

        night_rate = self._config.get("igo_night_rate", DEFAULT_IGO_NIGHT_RATE_PENCE)
        day_rate = self._config.get("igo_day_rate", DEFAULT_IGO_DAY_RATE_PENCE)

        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        rates = [
            RateSlot(today, today.replace(hour=5, minute=30), night_rate),
            RateSlot(today.replace(hour=5, minute=30), today.replace(hour=23, minute=30), day_rate),
            RateSlot(
                today.replace(hour=23, minute=30),
                (today + timedelta(days=1)).replace(hour=5, minute=30),
                night_rate,
            ),
        ]
        return rates
