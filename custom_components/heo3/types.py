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


@dataclass(frozen=True)
class EVState:
    """Zappi state. Filled in P1.3."""


@dataclass(frozen=True)
class TeslaState:
    """Tesla state via Teslemetry. Filled in P1.3.

    Gated by `located_at_home` — operator suppresses commands when off.
    """


@dataclass(frozen=True)
class ApplianceState:
    """Washer / dryer / dishwasher running flags. Filled in P1.3."""


# ── World ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LiveRates:
    """BD import/export rates today + tomorrow. Filled in P1.4."""


@dataclass(frozen=True)
class PredictedRates:
    """AgilePredict 7-day forward (visualisation only). Filled in P1.4."""


@dataclass(frozen=True)
class SolarForecast:
    """Solcast P10/P50/P90 today + tomorrow. Filled in P1.5."""


@dataclass(frozen=True)
class LoadForecast:
    """HEO-5 model output today + tomorrow. Filled in P1.5."""


@dataclass(frozen=True)
class SystemFlags:
    """eps_active, igo_dispatching, saving_session, etc. Filled in P1.6."""


@dataclass(frozen=True)
class SystemConfig:
    """Runtime tunables: min_soc, cycle_budget, target_end_soc."""

    min_soc: int = 10
    cycle_budget: float = 1.0
    target_end_soc: int = 25


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
    """Zappi-side intent. Filled in P1.3."""


@dataclass(frozen=True)
class TeslaAction:
    """Tesla-side intent: stop/start, charge_limit, charge_current. P1.3."""


@dataclass(frozen=True)
class ApplianceAction:
    """Which appliances to turn off/on. Filled in P1.3."""


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
    """What happened during apply(). §15."""

    plan_id: str
    requested: tuple[Write, ...]
    succeeded: tuple[Write, ...]
    failed: tuple[FailedWrite, ...]
    skipped: tuple[SkippedWrite, ...]
    verification: VerificationResult
    duration_ms: float
    captured_at: datetime
