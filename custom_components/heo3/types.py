"""HEO III type definitions.

Snapshot, PlannedAction, ApplyResult and the nested dataclasses they
compose. Top-level shapes are defined here per §11/§14 of the design;
nested types are placeholders until the adapter phases (P1.1-P1.6)
fill them in. Frozen so the planner cannot accidentally mutate state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo


# ── Inverter ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InverterState:
    """Live telemetry snapshot — what the inverter is doing right now.

    All fields nullable: HA may return `unknown` / `unavailable`
    on cold boot or transient comms issues. The planner is
    expected to handle missing values, not the operator.

    Per design §5. EPS detection (grid_voltage == 0 for ≥5s) lives
    in `SystemFlags` because it requires temporal state — see P1.6.
    """

    battery_soc_pct: float | None = None
    battery_power_w: float | None = None  # signed: + charge, - discharge
    battery_current_a: float | None = None  # signed
    battery_voltage_v: float | None = None
    grid_power_w: float | None = None  # signed: + import, - export
    grid_voltage_v: float | None = None
    grid_frequency_hz: float | None = None
    solar_power_w: float | None = None  # ≥0
    load_power_w: float | None = None  # ≥0
    inverter_temperature_c: float | None = None
    battery_temperature_c: float | None = None


@dataclass(frozen=True)
class SlotSettings:
    """Current values of one timer slot (1..6). Per SPEC §2 / §17:
    `start_hhmm` is on a 5-min boundary; `grid_charge` is True/False;
    `capacity_pct` is 0..100.
    """

    start_hhmm: str
    grid_charge: bool
    capacity_pct: int


@dataclass(frozen=True)
class InverterSettings:
    """Current values of writable inverter settings.

    Used as the diff baseline for `InverterAdapter.writes_for()`. A
    write is only published when the new value differs from the value
    here (case-insensitive for strings, tolerance 0.5 for floats).
    """

    work_mode: str
    energy_pattern: str
    max_charge_a: float
    max_discharge_a: float
    slots: tuple[
        SlotSettings,
        SlotSettings,
        SlotSettings,
        SlotSettings,
        SlotSettings,
        SlotSettings,
    ]


# ── Peripherals ─────────────────────────────────────────────────────


ZAPPI_VALID_MODES = ("Stopped", "Eco", "Eco+", "Fast")


@dataclass(frozen=True)
class EVState:
    """Zappi snapshot. All fields nullable — entities may be unavailable."""

    charging: bool | None = None  # True if currently delivering power
    mode: str | None = None  # one of ZAPPI_VALID_MODES
    charge_power_w: float | None = None


@dataclass(frozen=True)
class TeslaState:
    """Tesla state via Teslemetry.

    Gated by `located_at_home` — operator suppresses commands when off.
    All fields nullable — Teslemetry returns 'unknown' when the car
    is asleep (which is most of the time).
    """

    soc_pct: float | None = None
    is_charging: bool | None = None  # derived from sensor.<vehicle>_charging
    charge_power_w: float | None = None
    charge_limit_pct: int | None = None  # car-side SOC ceiling
    charge_current_a: int | None = None  # car-side AC draw
    cable_plugged: bool | None = None
    located_at_home: bool | None = None


@dataclass(frozen=True)
class ApplianceState:
    """Running flags for the SPEC H3 EPS load-shedding set."""

    washer_running: bool | None = None
    dryer_running: bool | None = None
    dishwasher_running: bool | None = None


# ── World ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RatePeriod:
    """One half-hour rate period.

    `start` and `end` are timezone-aware datetimes (typically UTC after
    parsing); `rate_pence` is in pence/kWh (BD publishes GBP/kWh —
    we convert at the boundary).
    """

    start: datetime
    end: datetime
    rate_pence: float


@dataclass(frozen=True)
class LiveRates:
    """Octopus rates from BottlecapDave, plus IGO fixed-rate fallback.

    Per SPEC H4: live rates only ever feed inverter writes. AgilePredict
    forecasts (`PredictedRates`) NEVER reach the inverter — they're for
    daily-plan visualisation only.

    Fields are nullable / empty when the BD entity hasn't been
    discovered or HA is still warming up.
    """

    import_current_pence: float | None = None
    export_current_pence: float | None = None
    import_today: tuple["RatePeriod", ...] = ()
    import_tomorrow: tuple["RatePeriod", ...] = ()
    export_today: tuple["RatePeriod", ...] = ()
    export_tomorrow: tuple["RatePeriod", ...] = ()
    tariff_code: str | None = None  # for audit log / tariff change detection


@dataclass(frozen=True)
class PredictedRates:
    """AgilePredict 7-day forward (visualisation only).

    NEVER reaches the inverter (SPEC H4). Used by Compute for daily-
    plan rendering.
    """

    import_pence: tuple["RatePeriod", ...] = ()
    export_pence: tuple["RatePeriod", ...] = ()


@dataclass(frozen=True)
class SolarForecast:
    """Solcast hourly forecast — today + tomorrow, P10/P50/P90.

    Each tuple is 24 floats (kWh per hour). Empty tuples mean the
    Solcast attribute wasn't available; callers should treat this as
    "no forecast" rather than "zero solar".
    """

    today_p50_kwh: tuple[float, ...] = ()
    tomorrow_p50_kwh: tuple[float, ...] = ()
    today_p10_kwh: tuple[float, ...] = ()
    today_p90_kwh: tuple[float, ...] = ()
    tomorrow_p10_kwh: tuple[float, ...] = ()
    tomorrow_p90_kwh: tuple[float, ...] = ()
    last_updated: datetime | None = None


@dataclass(frozen=True)
class LoadForecast:
    """HEO-5 14-day learning model output.

    24 hourly kWh values for today and tomorrow. Weekday vs weekend
    profiles are distinct; the planner uses `is_weekend` to select.
    """

    today_hourly_kwh: tuple[float, ...] = ()
    tomorrow_hourly_kwh: tuple[float, ...] = ()
    day_of_week: int = 0  # 0=Monday
    is_weekend: bool = False


@dataclass(frozen=True)
class TimeRange:
    """A start-end window. Used for saving sessions, off-peak windows, etc."""

    start: datetime
    end: datetime


@dataclass(frozen=True)
class IGOPlannedDispatch:
    """One Octopus IGO smart-charge dispatch on the planned schedule."""

    start: datetime
    end: datetime
    charge_kwh: float | None = None
    source: str | None = None


@dataclass(frozen=True)
class SystemFlags:
    """Operational flags derived from external state.

    Per §10. Numeric tunables (min_soc, cycle_budget, target_end_soc)
    live in `SystemConfig`; this dataclass is for derived booleans
    and event windows the planner reasons about each tick.
    """

    # Octopus / tariff events
    igo_dispatching: bool | None = None
    igo_planned: tuple[IGOPlannedDispatch, ...] = ()
    saving_session_active: bool | None = None
    saving_session_window: TimeRange | None = None
    saving_session_price_pence: float | None = None

    # Mechanical / safety
    eps_active: bool = False  # derived: grid_voltage == 0 for ≥5s
    grid_connected: bool = True  # inverse of eps_active
    inverter_temperature_alarm: bool | None = None
    battery_temperature_alarm: bool | None = None

    # User-set behavioural flags
    defer_ev_eligible: bool = False


@dataclass(frozen=True)
class SystemConfig:
    """Runtime tunables (planner uncertainty knobs) + battery params.

    Defaults match Paddy's install (4 × Sunsynk BP51.2 = 20.48 kWh).
    The first three are user-set via HA number entities; the last
    three are hardware constants the planner uses for SOC↔kWh math.
    """

    # Planner uncertainty knobs (HA number entities)
    min_soc: int = 10
    cycle_budget: float = 1.0
    target_end_soc: int = 25

    # Battery hardware constants (used by Compute)
    battery_capacity_kwh: float = 20.48
    nominal_battery_voltage_v: float = 51.2
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95


# ── Snapshot ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Snapshot:
    """Frozen state — what the world IS right now. §11."""

    captured_at: datetime
    local_tz: ZoneInfo

    inverter: InverterState
    inverter_settings: InverterSettings

    ev: EVState
    tesla: TeslaState | None
    appliances: ApplianceState

    rates_live: LiveRates
    rates_predicted: PredictedRates
    rates_freshness: dict[str, datetime]

    solar_forecast: SolarForecast
    load_forecast: LoadForecast

    flags: SystemFlags

    config: SystemConfig


# ── PlannedAction ───────────────────────────────────────────────────


@dataclass(frozen=True)
class SlotPlan:
    """One inverter timer slot (1..6). Filled in P1.1."""

    slot_n: int
    start_hhmm: str | None = None
    grid_charge: bool | None = None
    capacity_pct: int | None = None


@dataclass(frozen=True)
class EVAction:
    """Zappi-side intent.

    `set_mode`: write a specific mode (one of ZAPPI_VALID_MODES) to
    `select.zappi_charge_mode`.
    `restore_previous`: use the previously-captured mode (operator
    captures whenever the planner sets mode=Stopped).
    """

    set_mode: str | None = None
    restore_previous: bool = False


@dataclass(frozen=True)
class TeslaAction:
    """Tesla-side intent.

    All fields optional. `None` = don't touch this dimension.
    `set_charging`: True flips charge switch on, False flips off.
    `set_charge_limit_pct`: write the car's SOC ceiling (50-100).
    `set_charge_current_a`: write AC draw amps.

    All writes are silently no-op'd if `located_at_home` is False at
    apply time — the operator surfaces this in the outcome.
    """

    set_charging: bool | None = None
    set_charge_limit_pct: int | None = None
    set_charge_current_a: int | None = None


@dataclass(frozen=True)
class ApplianceAction:
    """Per-appliance turn off/on.

    Identifiers in `turn_off` and `turn_on` correspond to keys in
    PeripheralAdapter's `appliance_switches` config — they're not
    HA entity IDs themselves.
    """

    turn_off: tuple[str, ...] = ()
    turn_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannedAction:
    """The planner's output. §14.

    `Optional` fields = "don't touch this dimension". The operator
    diffs; absent fields produce no writes.
    """

    slots: tuple[SlotPlan, ...] = ()
    work_mode: str | None = None
    energy_pattern: str | None = None
    max_charge_a: float | None = None
    max_discharge_a: float | None = None

    ev_action: EVAction | None = None
    tesla_action: TeslaAction | None = None
    appliances_action: ApplianceAction | None = None

    plan_id: str = ""
    rationale: str = ""
    source_planner_version: str = ""

    spec_h4_live_rates: bool = False
    spec_h5_validated: bool = False


# ── ApplyResult ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Write:
    """One MQTT publish (or HA service call). Filled in P1.1."""

    topic: str
    payload: str


@dataclass(frozen=True)
class FailedWrite:
    """A write that did not land. Filled in P1.1."""

    write: Write
    reason: str


@dataclass(frozen=True)
class SkippedWrite:
    """Diff said the write was a no-op. Filled in P1.1."""

    write: Write


@dataclass(frozen=True)
class VerificationResult:
    """Per-write verification outcome. Filled in P1.1."""

    states: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ApplyResult:
    """What happened during apply(). §15.

    `peripheral_outcomes` carries the result of peripheral writes
    (zappi, Tesla, appliances) by adapter name → outcome code. Codes
    come from `adapters.peripheral`: APPLIED / NO_OP / SKIPPED_NOT_
    AT_HOME / SKIPPED_NO_CONFIG / SKIPPED_NO_CAPTURED_MODE / FAILED.
    """

    plan_id: str
    requested: tuple[Write, ...]
    succeeded: tuple[Write, ...]
    failed: tuple[FailedWrite, ...]
    skipped: tuple[SkippedWrite, ...]
    verification: VerificationResult
    duration_ms: float
    captured_at: datetime
    peripheral_outcomes: dict[str, str] = field(default_factory=dict)
