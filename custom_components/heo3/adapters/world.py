"""World Gatherer — read-only collation of HA entities + external sources.

Rates (BD + IGO + AgilePredict): P1.4
Forecasts (Solcast + HEO-5):     P1.5
Flags (IGO/saving/EPS/temp):      P1.6

All values are read from HA entities or external HTTP — never from
the SA broker directly. The integrations (BottlecapDave, Solcast,
octopus_energy, teslemetry) handle the upstream calls; this layer
collates their state into the operator's typed view.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from ..agilepredict_client import AgilePredictClient
from ..load_history import learn_days_from_samples
from ..load_profile import LoadProfile, LoadProfileBuilder
from ..solar_forecast import solar_forecast_from_hacs
from ..state_reader import StateReader, parse_float, parse_str
from ..state_reader import parse_bool
from ..types import (
    IGOPlannedDispatch,
    LiveRates,
    LoadForecast,
    PredictedRates,
    RatePeriod,
    SolarForecast,
    SystemFlags,
    TimeRange,
)

logger = logging.getLogger(__name__)


# Conversion: BottlecapDave publishes rates in GBP/kWh; HEO III uses pence.
GBP_TO_PENCE = 100.0


# ── Config dataclasses ────────────────────────────────────────────


@dataclass(frozen=True)
class BDConfig:
    """BottlecapDave entity IDs for one electricity meter.

    Use `from_meter_key()` to derive from the {mpan}_{serial} key.
    """

    import_current_rate: str
    export_current_rate: str
    import_day_rates: str
    import_next_day_rates: str
    export_day_rates: str
    export_next_day_rates: str

    @classmethod
    def from_meter_key(cls, key: str) -> "BDConfig":
        """Derive entity IDs from the BD `{mpan}_{serial}` key.

        Example: from_meter_key('18p5009498_2372761090617') gives
        sensor.octopus_energy_electricity_18p5009498_2372761090617_current_rate, etc.
        Export entity IDs use the same key — BD pairs import + export
        on a single meter pair.
        """
        return cls(
            import_current_rate=(
                f"sensor.octopus_energy_electricity_{key}_current_rate"
            ),
            export_current_rate=(
                f"sensor.octopus_energy_electricity_{key}_export_current_rate"
            ),
            import_day_rates=(
                f"event.octopus_energy_electricity_{key}_current_day_rates"
            ),
            import_next_day_rates=(
                f"event.octopus_energy_electricity_{key}_next_day_rates"
            ),
            export_day_rates=(
                f"event.octopus_energy_electricity_{key}_export_current_day_rates"
            ),
            export_next_day_rates=(
                f"event.octopus_energy_electricity_{key}_export_next_day_rates"
            ),
        )


@dataclass(frozen=True)
class IGOConfig:
    """IGO fixed-rate fallback constants. Per docs/SPEC.md §1, §10."""

    peak_pence: float = 24.8423
    off_peak_pence: float = 4.9524
    off_peak_start: time = time(23, 30)
    off_peak_end: time = time(5, 30)


@dataclass(frozen=True)
class SolcastConfig:
    """Solcast HACS entity IDs."""

    forecast_today: str = "sensor.solcast_pv_forecast_forecast_today"
    forecast_tomorrow: str = "sensor.solcast_pv_forecast_forecast_tomorrow"
    api_last_polled: str = "sensor.solcast_pv_forecast_api_last_polled"


@dataclass(frozen=True)
class FlagsConfig:
    """HA entity IDs the WorldGatherer uses for flag detection.

    Defaults match a typical install but every ID is overridable.
    `None` for an entity skips that flag (it stays at its default).
    """

    # Octopus IGO smart-dispatch (binary_sensor.octopus_energy_..._intelligent_dispatching)
    igo_dispatching_entity: str | None = None
    # Octopus saving sessions (binary_sensor.octopus_energy_octoplus_saving_sessions)
    saving_session_entity: str | None = None
    # Inverter sensors used for derived flags
    grid_voltage_entity: str = "sensor.sa_inverter_1_grid_voltage"
    inverter_temperature_entity: str = (
        "sensor.sa_inverter_1_inverter_temperature"
    )
    battery_temperature_entity: str = (
        "sensor.sa_inverter_1_battery_temperature"
    )
    # User-set behavioural flag (HEO III's own switch)
    defer_ev_eligible_entity: str = "switch.heo3_defer_ev_when_export_high"

    # Thresholds for derived alarms
    inverter_temperature_alarm_c: float = 65.0
    battery_temperature_min_c: float = 5.0
    battery_temperature_max_c: float = 50.0
    eps_grid_voltage_threshold_v: float = 5.0  # below this counts as "0"
    eps_debounce_s: float = 5.0


class _EPSDetector:
    """Tracks 'grid_voltage at zero for ≥ debounce_s seconds'.

    Stateful — survives across read_flags() calls within the same
    WorldGatherer instance. Reset whenever grid voltage rises above
    the threshold.
    """

    def __init__(self, debounce_s: float = 5.0, threshold_v: float = 5.0) -> None:
        self._debounce_s = debounce_s
        self._threshold_v = threshold_v
        self._first_zero_at: datetime | None = None

    def update(self, grid_voltage_v: float | None, now: datetime) -> bool:
        if grid_voltage_v is None or grid_voltage_v > self._threshold_v:
            self._first_zero_at = None
            return False
        if self._first_zero_at is None:
            self._first_zero_at = now
            return False
        elapsed = (now - self._first_zero_at).total_seconds()
        return elapsed >= self._debounce_s


@dataclass(frozen=True)
class LoadModelConfig:
    """HEO-5 load model wiring.

    `consumption_entity` is a household energy counter (state_class
    total_increasing) for the cumulative-kwh aggregator, OR a power-
    watts entity for the trapezoidal aggregator. Defaults to the SA
    load_power sensor, treated as power_watts.
    """

    consumption_entity: str = "sensor.sa_inverter_1_load_power"
    source_type: str = "power_watts"  # or "cumulative_kwh"
    learn_days: int = 14
    baseline_w: float = 1900.0


class LoadHistoryReader(Protocol):
    """Returns (timestamp, watts-or-kwh) samples for the past N days.

    Real implementation in P1.7 wraps hass.history; tests inject a
    canned list.
    """

    async def fetch(
        self, entity_id: str, days_back: int
    ) -> list[tuple[datetime, float]]: ...


class MockLoadHistoryReader:
    """Returns a pre-supplied sample list. For tests."""

    def __init__(self, samples: list[tuple[datetime, float]] | None = None) -> None:
        self._samples = list(samples or [])

    async def fetch(
        self, entity_id: str, days_back: int
    ) -> list[tuple[datetime, float]]:
        return list(self._samples)


# ── WorldGatherer ─────────────────────────────────────────────────


class WorldGatherer:
    """One pass over external HA state + external HTTP per snapshot tick."""

    def __init__(
        self,
        *,
        state_reader: StateReader | None = None,
        bd_config: BDConfig | None = None,
        igo_config: IGOConfig | None = None,
        agilepredict_client: AgilePredictClient | None = None,
        solcast_config: SolcastConfig | None = None,
        load_model_config: LoadModelConfig | None = None,
        load_history_reader: LoadHistoryReader | None = None,
        flags_config: FlagsConfig | None = None,
        local_tz: str = "Europe/London",
        hass=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self._state_reader = state_reader
        self._bd = bd_config
        self._igo = igo_config or IGOConfig()
        self._agilepredict = agilepredict_client
        self._solcast = solcast_config or SolcastConfig()
        self._load_model = load_model_config or LoadModelConfig()
        self._load_history = load_history_reader
        self._flags_cfg = flags_config or FlagsConfig()
        self._eps_detector = _EPSDetector(
            debounce_s=self._flags_cfg.eps_debounce_s,
            threshold_v=self._flags_cfg.eps_grid_voltage_threshold_v,
        )
        self._local_tz = ZoneInfo(local_tz)
        self._hass = hass

    # ── Rates (P1.4) ──────────────────────────────────────────────

    async def read_rates_live(self) -> LiveRates:
        """Read BD's current rates + today/tomorrow rate slot lists.

        Returns an empty LiveRates if BD isn't configured / entities
        are missing — the operator surfaces this via SPEC H4 freshness
        checks, not by faking values.
        """
        if self._bd is None or self._state_reader is None:
            return LiveRates()
        r = self._state_reader

        import_today = _parse_rates_attr(
            r.get_attributes(self._bd.import_day_rates).get("rates", [])
        )
        import_tomorrow = _parse_rates_attr(
            r.get_attributes(self._bd.import_next_day_rates).get("rates", [])
        )
        export_today = _parse_rates_attr(
            r.get_attributes(self._bd.export_day_rates).get("rates", [])
        )
        export_tomorrow = _parse_rates_attr(
            r.get_attributes(self._bd.export_next_day_rates).get("rates", [])
        )

        # Current rate sensors publish GBP/kWh; convert to pence.
        ic = parse_float(r.get_state(self._bd.import_current_rate))
        ec = parse_float(r.get_state(self._bd.export_current_rate))

        tariff_code = parse_str(
            r.get_attributes(self._bd.import_current_rate).get("tariff_code")
        )

        return LiveRates(
            import_current_pence=ic * GBP_TO_PENCE if ic is not None else None,
            export_current_pence=ec * GBP_TO_PENCE if ec is not None else None,
            import_today=import_today,
            import_tomorrow=import_tomorrow,
            export_today=export_today,
            export_tomorrow=export_tomorrow,
            tariff_code=tariff_code,
        )

    async def read_rates_predicted(self) -> PredictedRates:
        """AgilePredict 7-day forward export rates (visualisation only).

        Returns empty PredictedRates if AgilePredict isn't configured
        or the network call failed (the client returns [] on errors).
        """
        if self._agilepredict is None:
            return PredictedRates()
        export = await self._agilepredict.fetch_export_rates()
        # Import predictions aren't currently sourced; reserved for
        # a future improvement that gets Agile Import predictions too.
        return PredictedRates(
            import_pence=(),
            export_pence=tuple(export),
        )

    async def read_rates_freshness(self) -> dict[str, datetime]:
        """Per-source last-updated timestamp for SPEC H4 enforcement.

        Read from each BD entity's `last_updated` if exposed; otherwise
        fall back to the entity state datetime if it parses. Missing
        sources are simply absent from the returned dict.
        """
        if self._bd is None or self._state_reader is None:
            return {}
        out: dict[str, datetime] = {}
        for label, eid in (
            ("import_today", self._bd.import_day_rates),
            ("import_tomorrow", self._bd.import_next_day_rates),
            ("export_today", self._bd.export_day_rates),
            ("export_tomorrow", self._bd.export_next_day_rates),
        ):
            # BD event entities publish their state as the ISO timestamp
            # of the last refresh — convenient for freshness tracking.
            raw_state = self._state_reader.get_state(eid)
            ts = _try_parse_iso(raw_state)
            if ts is not None:
                out[label] = ts
        return out

    # ── Forecasts (P1.5) ──────────────────────────────────────────

    async def read_solar_forecast(self) -> SolarForecast:
        """Pull P10 / P50 / P90 hourly forecasts from Solcast HACS.

        Returns empty tuples when Solcast attributes are missing —
        callers should treat empty as "no forecast" not "zero solar".
        """
        if self._state_reader is None:
            return SolarForecast()
        r = self._state_reader

        today_attrs = r.get_attributes(self._solcast.forecast_today)
        tomorrow_attrs = r.get_attributes(self._solcast.forecast_tomorrow)

        today_hourly = today_attrs.get("detailedHourly", []) or []
        tomorrow_hourly = tomorrow_attrs.get("detailedHourly", []) or []

        today_local = datetime.now(self._local_tz).date()
        tomorrow_local = today_local + timedelta(days=1)

        last_polled = _try_parse_iso(r.get_state(self._solcast.api_last_polled))

        return SolarForecast(
            today_p50_kwh=tuple(
                solar_forecast_from_hacs(today_hourly, today_local, "pv_estimate")
            ),
            tomorrow_p50_kwh=tuple(
                solar_forecast_from_hacs(
                    tomorrow_hourly, tomorrow_local, "pv_estimate"
                )
            ),
            today_p10_kwh=tuple(
                solar_forecast_from_hacs(today_hourly, today_local, "pv_estimate10")
            ),
            today_p90_kwh=tuple(
                solar_forecast_from_hacs(today_hourly, today_local, "pv_estimate90")
            ),
            tomorrow_p10_kwh=tuple(
                solar_forecast_from_hacs(
                    tomorrow_hourly, tomorrow_local, "pv_estimate10"
                )
            ),
            tomorrow_p90_kwh=tuple(
                solar_forecast_from_hacs(
                    tomorrow_hourly, tomorrow_local, "pv_estimate90"
                )
            ),
            last_updated=last_polled,
        )

    async def read_load_forecast(self) -> LoadForecast:
        """Build the HEO-5 14-day median profile and return today/tomorrow.

        Returns an empty LoadForecast if no history reader is wired
        (e.g. P1.0-P1.6 standalone tests). The planner is expected to
        treat empty-tuples as "no forecast".
        """
        today_local = datetime.now(self._local_tz).date()
        tomorrow_local = today_local + timedelta(days=1)
        weekday = today_local.weekday()

        if self._load_history is None:
            return LoadForecast(
                day_of_week=weekday, is_weekend=weekday >= 5
            )

        samples = await self._load_history.fetch(
            self._load_model.consumption_entity,
            self._load_model.learn_days,
        )
        days = learn_days_from_samples(
            samples, self._local_tz, source_type=self._load_model.source_type
        )
        builder = LoadProfileBuilder(baseline_w=self._load_model.baseline_w)
        for d, hourly in days.items():
            builder.add_day(
                datetime(d.year, d.month, d.day, tzinfo=self._local_tz),
                hourly,
            )
        profile: LoadProfile = builder.build()

        today_dt = datetime(
            today_local.year, today_local.month, today_local.day,
            tzinfo=self._local_tz,
        )
        tomorrow_dt = today_dt + timedelta(days=1)

        return LoadForecast(
            today_hourly_kwh=tuple(profile.for_datetime(today_dt)),
            tomorrow_hourly_kwh=tuple(profile.for_datetime(tomorrow_dt)),
            day_of_week=weekday,
            is_weekend=weekday >= 5,
        )

    # ── Flags (P1.6) ──────────────────────────────────────────────

    async def read_flags(self, *, now: datetime | None = None) -> SystemFlags:
        """Derive operational booleans + event windows from external state.

        EPS detection requires temporal state (5s of grid_voltage at
        zero); the WorldGatherer's _EPSDetector tracks this across
        successive read_flags() calls.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        if self._state_reader is None:
            return SystemFlags()
        r = self._state_reader
        cfg = self._flags_cfg

        # IGO smart-charge dispatch
        igo_dispatching = None
        igo_planned: tuple[IGOPlannedDispatch, ...] = ()
        if cfg.igo_dispatching_entity is not None:
            igo_dispatching = parse_bool(
                r.get_state(cfg.igo_dispatching_entity)
            )
            attrs = r.get_attributes(cfg.igo_dispatching_entity)
            igo_planned = _parse_igo_planned(attrs.get("planned_dispatches", []))

        # Octoplus saving sessions
        saving_active = None
        saving_window: TimeRange | None = None
        saving_price: float | None = None
        if cfg.saving_session_entity is not None:
            saving_active = parse_bool(r.get_state(cfg.saving_session_entity))
            attrs = r.get_attributes(cfg.saving_session_entity)
            saving_window = _parse_saving_window(attrs)
            sp = attrs.get("octoplus_session_rewards_pence_per_kwh")
            if sp is None:
                sp = attrs.get("price")  # fallback if integration variant differs
            try:
                saving_price = float(sp) if sp is not None else None
            except (TypeError, ValueError):
                saving_price = None

        # EPS via temporal detector
        grid_voltage = parse_float(r.get_state(cfg.grid_voltage_entity))
        eps = self._eps_detector.update(grid_voltage, now)

        # Temperature alarms
        inv_temp = parse_float(r.get_state(cfg.inverter_temperature_entity))
        bat_temp = parse_float(r.get_state(cfg.battery_temperature_entity))
        inv_alarm = (
            None
            if inv_temp is None
            else inv_temp >= cfg.inverter_temperature_alarm_c
        )
        bat_alarm = (
            None
            if bat_temp is None
            else (
                bat_temp < cfg.battery_temperature_min_c
                or bat_temp > cfg.battery_temperature_max_c
            )
        )

        defer_ev = parse_bool(r.get_state(cfg.defer_ev_eligible_entity))

        return SystemFlags(
            igo_dispatching=igo_dispatching,
            igo_planned=igo_planned,
            saving_session_active=saving_active,
            saving_session_window=saving_window,
            saving_session_price_pence=saving_price,
            eps_active=eps,
            grid_connected=not eps,
            inverter_temperature_alarm=inv_alarm,
            battery_temperature_alarm=bat_alarm,
            defer_ev_eligible=defer_ev if defer_ev is not None else False,
        )

    # ── Convenience accessors for IGO config ──────────────────────

    @property
    def igo(self) -> IGOConfig:
        """Public access to IGO constants for the planner / Compute layer."""
        return self._igo


# ── Helpers ───────────────────────────────────────────────────────


def _parse_rates_attr(raw: Any) -> tuple[RatePeriod, ...]:
    """Convert BD's `attributes.rates` list into RatePeriod tuple.

    BD's rate dict shape:
      {
        "start": "2026-04-30T23:30:00+01:00",
        "end":   "2026-05-01T00:00:00+01:00",
        "value_inc_vat": 0.04952,        # GBP/kWh
        "is_capped": False,
        "is_intelligent_adjusted": False,
      }
    """
    if not isinstance(raw, list):
        return ()
    out: list[RatePeriod] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        start = _try_parse_iso(entry.get("start"))
        end = _try_parse_iso(entry.get("end"))
        gbp = entry.get("value_inc_vat")
        if start is None or end is None or gbp is None:
            continue
        try:
            pence = float(gbp) * GBP_TO_PENCE
        except (TypeError, ValueError):
            continue
        out.append(RatePeriod(start=start, end=end, rate_pence=pence))
    out.sort(key=lambda p: p.start)
    return tuple(out)


def _parse_igo_planned(raw: Any) -> tuple[IGOPlannedDispatch, ...]:
    """Parse the `planned_dispatches` attribute on the IGO binary_sensor."""
    if not isinstance(raw, list):
        return ()
    out: list[IGOPlannedDispatch] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        start = _try_parse_iso(entry.get("start"))
        end = _try_parse_iso(entry.get("end"))
        if start is None or end is None:
            continue
        kwh = entry.get("charge_in_kwh")
        if kwh is None:
            kwh = entry.get("charge_kwh")
        try:
            kwh_val = float(kwh) if kwh is not None else None
        except (TypeError, ValueError):
            kwh_val = None
        source = entry.get("source")
        out.append(
            IGOPlannedDispatch(
                start=start,
                end=end,
                charge_kwh=kwh_val,
                source=str(source) if source is not None else None,
            )
        )
    return tuple(out)


def _parse_saving_window(attrs: dict) -> TimeRange | None:
    """Octoplus saving session window — try a few attribute name variants."""
    # The integration exposes the active session in different shapes
    # depending on version. Try common ones.
    for start_key, end_key in (
        ("current_session_start", "current_session_end"),
        ("octoplus_session_start", "octoplus_session_end"),
        ("start", "end"),
    ):
        s = _try_parse_iso(attrs.get(start_key))
        e = _try_parse_iso(attrs.get(end_key))
        if s is not None and e is not None:
            return TimeRange(start=s, end=e)
    return None


def _try_parse_iso(raw: Any) -> datetime | None:
    if raw is None:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt
