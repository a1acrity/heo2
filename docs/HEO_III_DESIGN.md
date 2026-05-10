# HEO III — Operator Module Design

> Status: **draft for review**. This document defines the operator module
> only. The planner / rules / optimiser layer is deferred to a separate
> design doc (`HEO_III_PLANNER_DESIGN.md`, TBD) once the operator is
> complete and tested.
>
> Scope discipline: the operator is the **mechanical layer**. It controls
> everything HEO III could possibly need to control on the inverter and
> its peripherals, and reads everything HEO III could possibly need to
> read from the inverter and the wider system. It has zero economic
> opinions. The same operator must be sufficient to support whatever
> planner gets built later, including a planner more sophisticated than
> the one we end up with v1.
>
> Tracking issue: TBD. Owner: Paddy. Targets: `custom_components/heo3/`
> in this repo, building alongside HEO II until cutover.

## 1. Why a separate operator module exists

Two reasons, both rooted in HEO II's pain:

1. **Decoupling from economic decisions.** HEO II's rules wrote directly
   to inverter slot fields, then a writer diffed and published. The
   coupling meant rule logic and inverter mechanics were entangled —
   the F2 bug, the writer-init race, the in-progress-slot exclusion bug
   were all consequences. A clean mechanical layer the planner talks to
   over a typed interface is the structural fix.
2. **Hooks-complete on day one.** HEO II added globals (work_mode,
   energy_pattern, max_charge_a, max_discharge_a) PR by PR over months.
   Each addition required coordinator, writer, models, and rules edits.
   HEO III's operator must expose every meaningful inverter and
   peripheral surface from the start, even if v1 of the planner doesn't
   use them all. The planner can adopt new hooks without touching the
   operator.

## 2. Scope

### In scope (the operator owns these)

* **Inverter writes** — slots + globals. Every controllable Sunsynk
  setting that matters for planning.
* **Inverter reads** — live SOC, power flows, voltage, grid state, EPS
  detection, plus read-backs of every writable setting for verification.
* **Peripheral controls** — EV (zappi) charge mode, household appliance
  switches (washer/dryer/dishwasher) used during EPS H3.
* **Peripheral reads** — EV charging state, charge mode, appliance
  running flags.
* **External state gathering** — Octopus rates (BottlecapDave), solar
  forecasts (Solcast), tariff flags (IGO dispatching, planned
  dispatches, saving sessions). Read-only collation of HA entities.
* **Verification** — write a value, await SA's response, retry on
  failure, surface mismatch.
* **Mechanical safety invariants** — 5-min granularity, slot
  contiguity, SA value vocabulary, no garbage values reach the
  inverter.

### Out of scope (the planner will own these later)

* Cost models, forecast uncertainty, optimisation.
* SOC targets, work_mode choice, when to charge / sell / drain.
* Anything that requires reasoning about rates, forecasts, or
  outcomes.

### Non-goals

* **Not a generic Sunsynk library.** Only the controls HEO III needs
  exist. New surfaces added when planner needs them.
* **Not a generic HA integration framework.** It's the right shape for
  HEO III; it's not trying to be reusable.
* **Not a real-time fast path.** 15-min coordinator tick + verification
  cycle is fast enough; we're not chasing sub-second response.

## 3. Architectural shape

```
┌─────────────────────────────────────────────────────────────────┐
│                         Operator                                 │
│  (single public class, single point of contact for planner)      │
│                                                                  │
│  ── State (mechanical I/O) ──                                    │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐              │
│  │  Inverter   │  │ Peripheral  │  │   World     │              │
│  │   Adapter   │  │   Adapter   │  │  Gatherer   │              │
│  │ MQTT W/R    │  │ HA services │  │ HA reads    │              │
│  │ verify      │  │ + reads     │  │ rates+fcst  │              │
│  └─────────────┘  └─────────────┘  └─────────────┘              │
│         │                │                │                      │
│         ▼                ▼                ▼                      │
│  ┌──────────────────────────────────────────────────┐           │
│  │              Snapshot (frozen state)              │           │
│  │  what the world IS right now                      │           │
│  └──────────────────────────────────────────────────┘           │
│                            │                                     │
│                            ▼                                     │
│  ── Derived (pure-function library over Snapshot) ──             │
│  ┌──────────────────────────────────────────────────┐           │
│  │   Compute   energy↔SOC↔kWh                        │           │
│  │             time/rate windows                     │           │
│  │             forecast aggregation                  │           │
│  │             counterfactual analysis               │           │
│  │             physics predictions                   │           │
│  └──────────────────────────────────────────────────┘           │
│                            │                                     │
│                            ▼                                     │
│  ── Construction (intent → mechanical writes) ──                 │
│  ┌──────────────────────────────────────────────────┐           │
│  │   Build     sell_kwh, charge_to, hold_at,         │           │
│  │             drain_to, defer_ev, eps_lockdown      │           │
│  │             returns: PlannedAction                │           │
│  └──────────────────────────────────────────────────┘           │
│                            │                                     │
│                            ▼                                     │
│  ── Execution (PlannedAction → inverter) ──                      │
│  ┌──────────────────────────────────────────────────┐           │
│  │   apply()   diff, write, verify, report          │           │
│  └──────────────────────────────────────────────────┘           │
└─────────────────────────────────────────────────────────────────┘
                            ▲
                            │  Planner uses everything above:
                            │  reads Snapshot, calls Compute for
                            │  derived facts, decides intent, calls
                            │  Build to construct PlannedAction,
                            │  hands it to apply().
```

Four conceptual layers. One outward-facing class. The planner expresses
intent ("sell 8 kWh in these top slots", "charge to 80% by 05:30");
the operator handles all the physics, all the topology, all the
mechanics of getting the inverter to do it. The planner never opens an
MQTT connection, never computes a discharge throttle, never does an
SOC-to-kWh conversion.

### Public API (single source of truth for the planner)

```python
class Operator:
    # ── State ──────────────────────────────────────────
    async def snapshot(self) -> Snapshot:
        """Gather complete frozen state: inverter + peripherals +
        world. Single call returns everything for one planner tick."""

    # ── Derived facts (pure, callable any time) ────────
    @property
    def compute(self) -> Compute:
        """Stateless library of derived calculations over a
        Snapshot. Pure functions. See §12."""

    # ── Action construction (intent → writes) ──────────
    @property
    def build(self) -> ActionBuilder:
        """High-level action constructors. Inputs are domain-level
        intent; output is a fully-formed PlannedAction. See §13."""

    # ── Execution ──────────────────────────────────────
    async def apply(self, action: PlannedAction) -> ApplyResult:
        """Mechanically execute a planned action: inverter writes,
        peripheral changes. Verifies and reports per-write outcome."""

    async def shutdown(self) -> None:
        """Graceful close: MQTT disconnect, pending verifications
        cancelled."""
```

The `compute` and `build` namespaces are pure-function libraries. They
carry no state of their own — they take `Snapshot` (or relevant pieces
of it) as input. This means the planner's tests can construct a
synthetic `Snapshot` and call `compute.*` / `build.*` directly without
the operator needing to be alive.

## 4. Inverter Adapter — Writes

All inverter control. MQTT to Solar Assistant's broker
(`192.168.4.7:1883`), topics under `solar_assistant/inverter_1/...`. Per
SPEC §2: writes only ever go to inverter 1; inverter 2 is RS485-mirrored.

### Per-slot writes (×6 slots, slot N ∈ {1..6})

| Field | Topic | Value type | Notes |
|---|---|---|---|
| Slot N start time | `set/inverter/inverter_1/time_point_N` | `"HH:MM"` | 5-min granularity. Slot N's START — slot covers [start_N, start_{N+1}). Slot 1 always starts at 00:00; slot 6 always ends at 00:00. |
| Slot N grid charge | `set/inverter/inverter_1/grid_charge_point_N` | `"true"` / `"false"` | **Lowercase** per SA value vocabulary (HEO-32 incident pinned this). |
| Slot N capacity SOC | `set/inverter/inverter_1/capacity_point_N` | `"0".."100"` | Integer percent. SA accepts plain number string. |

### Global writes

| Field | Topic | Values | Notes |
|---|---|---|---|
| Work mode | `set/inverter/inverter_1/work_mode` | `"Selling first"` / `"Zero export to load"` / `"Zero export to CT"` | Case + whitespace as exposed by SA discovery. |
| Energy pattern | `set/inverter/inverter_1/energy_pattern` | `"Battery first"` / `"Load first"` | |
| Max charge current | `set/inverter/inverter_1/max_charge_current` | `"0".."350"` | Amps. Sunsynk 5kW peaks ~100A at 51.2V. **Doubles as the SOC-ceiling primitive**: there is no global `battery_max_soc` setting on the hardware; planner enforces a ceiling by writing `1` (allows PV trickle, blocks meaningful charging) or `0` (hard freeze) when current SOC reaches the desired cap. |
| Max discharge current | `set/inverter/inverter_1/max_discharge_current` | `"0".."350"` | Amps. |

### Write semantics

* **Diff-only**: a write is published only when the new value differs
  from the last-known inverter state (case-insensitive for strings,
  tolerance 0.5 for floats).
* **Atomic per-action**: a `PlannedAction` produces a list of writes;
  the operator publishes them in a defined order (work_mode first
  because some other settings depend on it; slot writes after; max
  current limits last).
* **Sequenced**: writes go out one at a time with await for SA
  response. Avoid the FIFO-correlation hack from HEO II's writer.
* **Retry on failure**: 3 retries with 5s backoff. After failure,
  surface in `ApplyResult.failed`. Coordinator decides whether to retry
  the whole tick.

## 5. Inverter Adapter — Reads

Live state via SA's MQTT-discovered HA entities (sensor.sa_inverter_1_*).
The inverter pushes telemetry every few seconds; HA caches; operator
reads HA's cache.

### Live telemetry

| Field | Entity | Units | Used for |
|---|---|---|---|
| Battery SOC | `sensor.sa_inverter_1_battery_soc` | % (0..100) | Planning (current state). |
| Battery power | `sensor.sa_inverter_1_battery_power` | W (signed: +charge, -discharge) | Verification + dashboard. |
| Battery current | `sensor.sa_inverter_1_battery_current` | A (signed) | Verification. |
| Battery voltage | `sensor.sa_inverter_1_battery_voltage` | V | Capacity sanity check. |
| Grid power | `sensor.sa_inverter_1_grid_power` | W (signed: +import, -export) | Verification + dashboard. |
| Grid voltage | `sensor.sa_inverter_1_grid_voltage` | V | EPS detection (=0 → grid down). |
| Grid frequency | `sensor.sa_inverter_1_grid_frequency` | Hz | Grid quality check (optional). |
| Solar power | `sensor.sa_inverter_1_solar_power` | W (≥0) | PV measurement. |
| Load power | `sensor.sa_inverter_1_load_power` | W (≥0) | House load measurement. |
| Inverter temperature | `sensor.sa_inverter_1_inverter_temperature` | °C | Health monitor. |
| Battery temperature | `sensor.sa_inverter_1_battery_temperature` | °C | Health monitor; clamps charge/discharge. |
| EPS active | derived from `grid_voltage == 0` for >5s | bool | SPEC H3 trigger. |

### Setting read-backs (for write verification)

Every writable setting has a corresponding read sensor (per SA
discovery). The operator reads these to verify writes landed:

| Setting | Read entity |
|---|---|
| `time_point_N` | `sensor.sa_inverter_1_time_point_N` |
| `grid_charge_point_N` | `sensor.sa_inverter_1_grid_charge_point_N` |
| `capacity_point_N` | `sensor.sa_inverter_1_capacity_point_N` |
| `work_mode` | `sensor.sa_inverter_1_work_mode` |
| `energy_pattern` | `sensor.sa_inverter_1_energy_pattern` |
| `max_charge_current` | `sensor.sa_inverter_1_max_charge_current` |
| `max_discharge_current` | `sensor.sa_inverter_1_max_discharge_current` |

### Special handling: 5-min granularity for time_point reads

Sunsynk floors written time values to the nearest 5-minute boundary.
Writing `23:57` results in a read-back of `23:55`. The operator
**snaps writes to 5-min before publishing**, and verifies against the
snapped value. Same as HEO II's SafetyRule.

## 6. Peripheral Adapter — Controls

### EV charging (zappi)

| Action | Mechanism | Notes |
|---|---|---|
| Stop EV charging | `select.set_option` on `select.zappi_charge_mode` to `"Stopped"` | One-shot. Used by SPEC §12 EV deferral and SPEC H3 EPS. |
| Restore EV charging | `select.set_option` on same entity to previously-captured mode | Operator captures the mode before stopping; restores on transition out. |
| Set EV charging mode | `select.set_option` on `select.zappi_charge_mode` to any of {`"Eco+"`, `"Eco"`, `"Fast"`, `"Stopped"`} | General-purpose hook for future planner. |

### Household appliances (SPEC H3 EPS handling)

| Action | Mechanism | Notes |
|---|---|---|
| Turn off washer | `switch.turn_off` on `switch.washer` | EPS H3. |
| Turn off dryer | `switch.turn_off` on `switch.dryer` | EPS H3. |
| Turn off dishwasher | `switch.turn_off` on `switch.dishwasher` | EPS H3. |
| Turn on each | `switch.turn_on` on the same entity | When EPS clears or planner decides to defer to off-peak. |

### Tesla (via Teslemetry integration)

| Action | Mechanism | Notes |
|---|---|---|
| Stop / start charging | `switch.turn_off` / `switch.turn_on` on `switch.<vehicle>_charge` | Direct car command. Independent of zappi — can also stop charge by zappi-side power cut. |
| Set SOC ceiling | `number.set_value` on `number.<vehicle>_charge_limit` | Car enforces it. Range typically 50-100. Cleaner than throttling current for SOC-cap intent. |
| Throttle AC charge current | `number.set_value` on `number.<vehicle>_charge_current` | For peak-window slowdowns where stop is too coarse. |

Both the zappi and Tesla paths can stop a Tesla charge. The planner picks based on intent: zappi-stop is clean for "no power available" (EPS); Tesla-stop is the right primitive for SOC-targeted decisions ("you're at 80, that's enough"). For v1, the operator exposes both; the planner decides which lever fits.

Tesla controls only fire if `binary_sensor.<vehicle>_located_at_home` is `on` — operator gates this internally to avoid sending commands while the car is away.

### Configurability

Every peripheral entity ID is a config option, not hardcoded. Defaults
match Paddy's house (zappi, named appliances) but the operator can be
deployed elsewhere with different entities.

## 7. Peripheral Adapter — Reads

| Field | Entity | Used for |
|---|---|---|
| EV charging now | `sensor.zappi_charging_state` (or equivalent reading "Charging" / "Connected" / etc.) | SPEC H2 (no battery → EV) trigger. |
| EV charge mode | `select.zappi_charge_mode` (state) | Restore-after-deferral. |
| EV power demand | `sensor.zappi_charge_power` | Capacity awareness. |
| Washer running | `binary_sensor.washer_running` | Active appliance flag. |
| Dryer running | `binary_sensor.dryer_running` | Active appliance flag. |
| Dishwasher running | `binary_sensor.dishwasher_running` | Active appliance flag. |
| Tesla charging state | `sensor.<vehicle>_charging` | "Charging" / "Stopped" / "Disconnected" etc. |
| Tesla SOC | `sensor.<vehicle>_battery_level` | Percent. Drives planner decisions on charge-limit writes. |
| Tesla charge power | `sensor.<vehicle>_charger_power` | kW current draw. |
| Tesla charge limit | `number.<vehicle>_charge_limit` (state) | Read-back for verification of SOC-ceiling writes. |
| Tesla charge current | `number.<vehicle>_charge_current` (state) | Read-back for verification of current-throttle writes. |
| Tesla cable plugged | `binary_sensor.<vehicle>_charge_cable` | Plug detection. May be unavailable when car asleep. |
| Tesla at home | `binary_sensor.<vehicle>_located_at_home` | Gate for all Tesla writes — operator suppresses commands when off. |

## 8. World Gatherer — Rates

External state read-only. Planner needs accurate live rates plus
forecasts.

### Octopus rates (BottlecapDave integration)

| Field | Entity / source | Notes |
|---|---|---|
| Live import rate (current) | `sensor.bottlecapdave_octopus_import_current_rate` | Used for live cost, never for forward writes (SPEC H4). |
| Live import rates (today) | Attribute `import_rates_today` on BD entity | List of `{start, end, rate_pence}`. 30-min slots. |
| Live import rates (tomorrow) | Attribute `import_rates_tomorrow` | Available after 16:00 BST publish. Empty before. |
| Live export rate (current) | `sensor.bottlecapdave_octopus_export_current_rate` | |
| Live export rates (today) | Attribute `export_rates_today` | |
| Live export rates (tomorrow) | Attribute `export_rates_tomorrow` | After 16:00. |
| Tariff identifier | Attribute `tariff_code` | Audit log; differentiating tariff changes. |
| Live-data freshness | Derived: max age of any rate entity | SPEC H4 enforcement: writes blocked if stale. |

### IGO fixed rates (fallback for past-horizon-of-BD)

| Field | Source | Notes |
|---|---|---|
| IGO peak rate | Config (24.84p) | Constant. |
| IGO off-peak rate | Config (4.95p) | Constant. |
| IGO off-peak window | Config (23:30-05:30 local) | Constant. |

### AgilePredict (forecast — visualisation only, never written)

| Field | Source | Notes |
|---|---|---|
| Predicted import rates (next 7 days) | AgilePredict API/integration | NEVER reaches the inverter (SPEC H4). Used for daily-plan visualisation. |
| Predicted export rates (next 7 days) | AgilePredict API/integration | Same. |

## 9. World Gatherer — Forecasts

### Solar (Solcast)

| Field | Entity / source | Notes |
|---|---|---|
| Solar forecast today (hourly) | `sensor.solcast_pv_forecast_today` (attr `detailedForecast`) | 24 hourly buckets in kWh. |
| Solar forecast tomorrow (hourly) | `sensor.solcast_pv_forecast_tomorrow` | 24 hourly buckets. |
| Solar forecast P10 / P50 / P90 | Solcast attributes | For uncertainty handling. Available; not all planners use. |
| Forecast freshness | Last updated timestamp | If stale (>24h), planner is told. |

### Load (HEO-5 14-day learning model)

| Field | Source | Notes |
|---|---|---|
| Load forecast today (hourly) | HEO-5 model output | 24 hourly buckets in kWh. Learned from past 14 days, weekday/weekend split. |
| Load forecast tomorrow (hourly) | Same | |
| Day-of-week + season tag | Derived from `now` | Helps the planner pick the right learned profile. |

The HEO-5 model itself is currently part of HEO II
(`load_profile.py` + `load_history.py`). For HEO III: port as-is into
the operator's world gatherer; the planner reads the forecast like any
other input. Future improvement (out of scope): replace with a more
sophisticated model.

## 10. World Gatherer — Flags + Events

### Octopus / tariff events

| Flag | Source | Notes |
|---|---|---|
| `igo_dispatching` | `binary_sensor.octopus_energy_..._intelligent_dispatching` | True when Octopus is actively running an IGO smart-charge dispatch right now. |
| `igo_planned[]` | Attribute `planned_dispatches` on the same binary sensor | List of `{start, end, charge_kwh, source}`. Next 24h. |
| `saving_session_active` | `binary_sensor.octopus_energy_octoplus_saving_sessions` | True during an Octoplus session. |
| `saving_session_window` | Attribute on the binary sensor | `{start, end}` of current session. |
| `saving_session_price` | Attribute | Pence per kWh, typically £3+. |

### Mechanical / safety flags

| Flag | Source | Notes |
|---|---|---|
| `eps_active` | Derived: `grid_voltage == 0` for ≥5s | SPEC H3 trigger. |
| `grid_connected` | Inverse of `eps_active`. | |
| `inverter_temperature_alarm` | Derived: temperature > config threshold | Future hook. |
| `battery_temperature_alarm` | Derived: temperature outside (5°C, 50°C) | Limits charge/discharge. |

### Configuration / mode flags

| Flag | Source | Notes |
|---|---|---|
| `defer_ev_eligible` | `switch.heo3_defer_ev_when_export_high` | User-set; SPEC §12. |
| `min_soc` | `number.heo3_min_soc` | User-set; SPEC §1. |
| `cycle_budget_per_day` | `number.heo3_cycle_budget` | User-set; SPEC H7. |
| `target_end_soc` | `number.heo3_target_end_soc` | User-set; planner uncertainty knob (only used by planner; operator just exposes the value). |

## 11. Snapshot — the planner's input

Single immutable dataclass. Operator builds it from all three adapters
in one call. Frozen so the planner can't accidentally mutate.

```python
@dataclass(frozen=True)
class Snapshot:
    captured_at: datetime              # UTC, when this snapshot was built
    local_tz: ZoneInfo                 # for time-of-day comparisons

    # --- inverter live state ---
    inverter: InverterState            # all sensors above
    inverter_settings: InverterSettings  # current values of writables

    # --- peripherals ---
    ev: EVState                        # charging?, mode, power
    appliances: ApplianceState         # washer/dryer/dishwasher running flags

    # --- rates ---
    rates_live: LiveRates              # BD import/export today + tomorrow
    rates_predicted: PredictedRates    # AgilePredict (visualisation only)
    rates_freshness: dict[str, datetime]  # last update per source

    # --- forecasts ---
    solar_forecast: SolarForecast      # P50 + bands today + tomorrow
    load_forecast: LoadForecast        # HEO-5 model today + tomorrow

    # --- flags ---
    flags: SystemFlags                 # eps_active, igo_dispatching, etc.

    # --- config ---
    config: SystemConfig               # min_soc, cycle_budget, target_end_soc
```

Every nested type is a `@dataclass(frozen=True)`. Construction is one
call; reading is direct attribute access; planners never block on
operator I/O during a tick.

## 12. Compute — derived calculations (pure-function library)

Stateless library accessed via `operator.compute`. Every function
takes a `Snapshot` (or the relevant subset) plus parameters; returns
a value. No I/O, no state, no opinions. The planner uses these to
reason about facts ("how much is X?") without redoing physics.

Organised into five families.

### 12a. Energy / SOC / kWh conversions

```python
class Compute:
    def kwh_for_soc(self, soc_pct: float, snap: Snapshot) -> float:
        """Convert SOC percentage to absolute kWh given live capacity."""

    def soc_for_kwh(self, kwh: float, snap: Snapshot) -> float:
        """Inverse: kWh → SOC percentage."""

    def usable_kwh(self, snap: Snapshot) -> float:
        """Energy available between current SOC and the user's min_soc
        floor. Above-floor only — never returns negative."""

    def headroom_kwh(self, snap: Snapshot) -> float:
        """Energy capacity remaining for charging: from current SOC to
        100%. The hardware has no global SOC ceiling — if the planner
        wants one, it tracks it in policy and throttles charge current
        when SOC reaches its chosen cap."""

    def round_trip_efficiency(self) -> float:
        """Charge × discharge efficiency. ~0.9. Used for
        break-even-pricing math."""
```

### 12b. Time / rate windows

```python
    def next_cheap_window(self, snap: Snapshot, *, after: datetime | None = None) -> RateWindow | None:
        """Next contiguous block of bottom-quartile import rates after
        `after` (defaults to now). Returns the window's start, end,
        and average rate. None if no cheap window in the published
        rate horizon."""

    def next_peak_window(self, snap: Snapshot, *, after: datetime | None = None) -> RateWindow | None:
        """Next contiguous block of top-quartile import rates."""

    def time_until(self, target: datetime, snap: Snapshot) -> timedelta:
        """Convenience: target - snap.captured_at."""

    def top_export_windows(self, snap: Snapshot, *, n: int = 3, until: datetime | None = None) -> list[RateWindow]:
        """The N highest-rated 30-min export slots that haven't ended,
        ordered by rate descending. `until` defaults to next cheap
        window start (don't sell if we're about to refill cheaply)."""

    def cheap_window_duration(self, window: RateWindow) -> timedelta:
        """Trivial; included for API completeness."""
```

`RateWindow` is a `dataclass(frozen=True)` with `start`, `end`,
`rate_pence`, `avg_rate_pence` (latter for multi-slot windows).

### 12c. Forecast aggregation

```python
    def total_load(self, snap: Snapshot, window: TimeRange) -> float:
        """Sum forecast load over a time window. Uses the operator's
        load model (HEO-5 14-day learned). Window may span past +
        future; past portion uses actuals if available."""

    def total_solar(self, snap: Snapshot, window: TimeRange) -> float:
        """Sum forecast PV over a window. Uses Solcast P50."""

    def net_load(self, snap: Snapshot, window: TimeRange) -> float:
        """Load - Solar. Signed: positive = battery+grid must cover,
        negative = surplus PV available."""

    def cumulative_load_to(self, snap: Snapshot, target: datetime) -> float:
        """Forecast load between snap.captured_at and target. Pro-rates
        partial hours."""

    def cumulative_solar_to(self, snap: Snapshot, target: datetime) -> float:
        """Same shape for solar."""

    def bridge_kwh(self, snap: Snapshot, *, until: datetime | None = None) -> float:
        """Net energy the battery must supply to bridge from now to
        `until` (default: next cheap window). Equals
        cumulative_load_to(until) - cumulative_solar_to(until). Floor
        at zero. THIS IS THE 2026-05-08 KEY METRIC."""

    def pv_takeover_hour(self, snap: Snapshot) -> int | None:
        """The first hour tomorrow where forecast solar ≥ forecast
        load. None if PV never overtakes (deep winter). Used by any
        cheap-charge sizing logic the planner builds."""
```

### 12d. Counterfactual analysis (visibility / dashboard)

```python
    def usage_at_rate_band(
        self, snap: Snapshot, window: TimeRange,
    ) -> dict[RateBand, float]:
        """How much of forecast load falls in each rate band (peak,
        off-peak, cheap-window). Returns {band: kWh}. Useful for
        understanding "what would a do-nothing day cost?"."""

    def cost_breakdown(
        self, snap: Snapshot, window: TimeRange,
    ) -> dict[str, float]:
        """Forecast £ split: import_cost_peak, import_cost_off_peak,
        export_revenue_top, export_revenue_other. Ground-truth-style
        accounting that the dashboard can render."""

    def import_volume_under_plan(
        self, plan: PlannedAction, snap: Snapshot,
    ) -> float:
        """Counterfactual: if THIS plan ran from now to end of
        horizon, given current forecasts, how much grid import would
        the plan need? Lets the planner compare candidate plans
        without writing them. THIS IS THE OBJECTIVE-FUNCTION
        BUILDING BLOCK for whatever decision logic the planner
        eventually uses."""

    def export_revenue_under_plan(
        self, plan: PlannedAction, snap: Snapshot,
    ) -> float:
        """Same for forecast export revenue."""
```

### 12e. Physics predictions

```python
    def time_to_charge(
        self, *, target_soc_pct: float, charge_rate_kw: float,
        snap: Snapshot,
    ) -> timedelta:
        """How long to get from current SOC to target_soc_pct at the
        given charge rate. Accounts for charge efficiency. Returns
        timedelta(0) if already at or above target."""

    def time_to_discharge(
        self, *, target_soc_pct: float, discharge_rate_kw: float,
        snap: Snapshot,
    ) -> timedelta:
        """How long to drain from current SOC to target. Accounts for
        discharge efficiency."""

    def kwh_deliverable_in(
        self, *, duration: timedelta, throttle_a: float,
        snap: Snapshot,
    ) -> float:
        """Given a discharge throttle (amps) and a slot duration,
        how many kWh leave the battery? Uses live battery voltage
        from snap so we don't drift on voltage assumptions."""

    def discharge_throttle_for(
        self, *, kwh: float, duration: timedelta, snap: Snapshot,
    ) -> float:
        """Inverse: what amp setting delivers exactly `kwh` over
        `duration`? Clamped to inverter limits. Returns the
        max_discharge_a value the operator will write. Replaces the
        ad-hoc `kw_for_slot / battery_voltage * 1000` formula
        scattered across HEO II's PeakArbitrageRule."""

    def charge_throttle_for(
        self, *, kwh: float, duration: timedelta, snap: Snapshot,
    ) -> float:
        """Same shape for charging. Useful when the planner wants to
        rate-limit a grid_charge slot."""
```

### Why pure functions, not methods on a class with state

Every Compute function is callable from a test with a synthetic
`Snapshot` and produces deterministic output. No "did the operator
warm up yet?" failure modes. Concurrency is also free — these
functions are safe to call from anywhere.

## 13. Build — action constructors (intent → PlannedAction)

`operator.build` is a higher-level layer on top of Compute. Each
constructor takes domain-level intent and returns a fully-formed
`PlannedAction` ready to hand to `apply()`. The planner expresses
WHAT it wants; the constructor figures out WHICH inverter writes
achieve it.

### 13a. Energy actions

```python
class ActionBuilder:
    def sell_kwh(
        self, *, total_kwh: float,
        across_slots: list[RateWindow],
        snap: Snapshot,
    ) -> PlannedAction:
        """Allocate `total_kwh` across the given slots (typically
        from compute.top_export_windows). For each slot:

          * If slot covers `now`, set work_mode="Selling first" and
            max_discharge_a = compute.discharge_throttle_for(
              kwh=allocation, duration=slot_duration, snap=snap)
          * Set the inverter slot's capacity_soc to the calculated
            post-sell SOC (so it stops at the right level).

        For slots in the future (next ticks will pick them up): no
        immediate inverter write needed; the daily plan / next tick
        builds on the updated state.

        Returns a PlannedAction that, applied, sells exactly
        `total_kwh` if conditions hold."""

    def charge_to(
        self, *, target_soc_pct: float, by: datetime,
        snap: Snapshot,
        rate_limit_a: float | None = None,
    ) -> PlannedAction:
        """Set the slot covering [now, by) to grid_charge=True,
        capacity_soc=target_soc_pct. If `by` is during a cheap
        window, slot timing aligns with the window. Optional rate
        limit applies via max_charge_a."""

    def hold_at(
        self, *, soc_pct: float, window: TimeRange,
        snap: Snapshot,
    ) -> PlannedAction:
        """Keep battery at `soc_pct` over `window`: set the slot
        covering window to capacity_soc=soc_pct, grid_charge=False
        (so PV charges naturally and house load drains naturally to
        that level)."""

    def drain_to(
        self, *, target_soc_pct: float, by: datetime,
        snap: Snapshot,
    ) -> PlannedAction:
        """Set slot covering [now, by) to capacity_soc=target_soc_pct,
        grid_charge=False. Doesn't change work_mode (drain happens
        passively under "Zero export to CT" if there's load)."""
```

### 13b. Mode actions

```python
    def lockdown_eps(self, snap: Snapshot) -> PlannedAction:
        """SPEC H3: grid down. All slots cap=0%, gc=False. EV stop.
        Appliance switches off. Returns the fully-baked plan.
        Coordinator triggers this on eps_active transition."""

    def baseline_static(self, snap: Snapshot) -> PlannedAction:
        """The known-good static plan: 80% overnight charge, day
        hold at 100%, evening drain to 25%, no arbitrage. Used by
        the cutover script (P1.9) and as the planner's fallback if
        it can't produce a valid plan."""

    def restore_default(self, snap: Snapshot) -> PlannedAction:
        """Reset globals to baseline values: work_mode='Zero export
        to CT', energy_pattern='Load first', max_charge_a=100,
        max_discharge_a=100. Used when exiting active arbitrage /
        EV-deferral / saving session windows."""
```

### 13c. Peripheral actions

```python
    def defer_ev(self, snap: Snapshot) -> PlannedAction:
        """SPEC §12: stop the EV. Captures current charge mode for
        later restore. Returns a PlannedAction with peripheral_action
        set; no inverter writes."""

    def restore_ev(self, snap: Snapshot) -> PlannedAction:
        """Restore EV to its captured-pre-deferral charge mode."""
```

### 13d. Composition

`PlannedAction` instances can be merged (operator-level helper). The
planner can do:

```python
plan = builder.merge(
    builder.sell_kwh(total_kwh=8, across_slots=top_3, snap=snap),
    builder.charge_to(target_soc_pct=75, by=tomorrow_5_30, snap=snap),
    builder.hold_at(soc_pct=50, window=evening, snap=snap),
)
result = await operator.apply(plan)
```

`merge` does field-by-field reconciliation:
* Slot fields: union, last-write-wins on conflicts (with a warning).
* Globals: same.
* Peripheral actions: must agree or merge raises.

### Why this layer exists at all (vs planner does it itself)

Three reasons:

1. **The planner doesn't need to know about discharge throttles, slot
   boundaries, or value vocabularies.** It knows about kWh, SOC %, and
   timestamps. The translation lives here, where it's tested once.
2. **It's the right place for "we want the same plan-shape across
   different planners".** v1 planner, v2 planner with optimiser, the
   eventual stochastic planner — all produce intent-shaped requests
   and use the same `Build` layer. The mechanical correctness of the
   plan is invariant.
3. **It makes counterfactual analysis cheap.** The planner can call
   `build.sell_kwh(...)` to get a candidate plan, then
   `compute.import_volume_under_plan(plan, snap)` to evaluate it,
   without writing anything. Loops over candidates trivially.

## 14. PlannedAction — the planner's output

Equally typed. Whatever the planner produces, this is the shape:

```python
@dataclass(frozen=True)
class PlannedAction:
    # --- inverter writes ---
    slots: tuple[SlotPlan, ...]        # exactly 6 (or empty = no slot changes this tick)
    work_mode: Optional[str]
    energy_pattern: Optional[str]
    max_charge_a: Optional[float]      # also the SOC-ceiling primitive (write 1A or 0A)
    max_discharge_a: Optional[float]

    # --- peripheral actions ---
    ev_action: Optional[EVAction]      # set mode / restore previous
    appliances_action: Optional[ApplianceAction]  # which to turn off / on

    # --- audit metadata ---
    plan_id: str                       # UUID for the audit trail
    rationale: str                     # human-readable summary
    source_planner_version: str        # tracks which planner produced this

    # --- safety acks ---
    spec_h4_live_rates: bool           # planner asserts rates are live
    spec_h5_validated: bool            # planner asserts pre-validation passed
```

`Optional[...]` fields = "don't touch this dimension". The operator
diffs; absent fields produce no writes.

## 15. ApplyResult — what happened

```python
@dataclass(frozen=True)
class ApplyResult:
    plan_id: str
    requested: tuple[Write, ...]       # what the operator tried to do
    succeeded: tuple[Write, ...]
    failed: tuple[FailedWrite, ...]    # with reason
    skipped: tuple[SkippedWrite, ...]  # diff said no-op
    verification: VerificationResult   # post-write sensor read-back
    duration_ms: float
    captured_at: datetime              # operator timestamp
```

The planner uses this to detect partial failures and decide whether to
retry on the next tick or surface to the user.

## 16. Verification + write/read cycle

For every write the operator publishes, it expects SA to publish a
response on `set/response_message/state` (per SA's value vocabulary —
`Saved` / `Error: <detail>`).

### Verification states

* **`OK`** — SA returned `Saved`, the read-back sensor reflects the
  written value within tolerance.
* **`PENDING`** — write published, no SA response yet, retry timer
  running. Will retry up to 3 times with 5s backoff.
* **`SET_BUT_UNVERIFIED`** — SA returned `Saved` but the read-back
  sensor doesn't yet reflect it (HA's MQTT cache lag). Operator marks
  this as success; next tick's verification proves it landed.
* **`FAILED`** — SA returned `Error: ...`, or 3 retries elapsed without
  any response.

### Write-blocking conditions (SPEC enforcement)

The operator REFUSES to apply if any of:

* SPEC H4: `rates_freshness` shows BD data older than threshold (e.g.,
  60 min).
* SPEC H3: `eps_active` is True (grid down — don't write to MQTT,
  inverter is busy).
* `dry_run` is enabled (config flag — for testing the plan path
  without side effects).
* MQTT transport not connected.
* Verification of the previous tick failed and the user has set
  `pause_on_verification_failure` (config flag).

### Why this is different from HEO II

In HEO II, the writer used FIFO queue correlation to match SA responses
to publishes (after the HEO-32 incident showed SA's response format
changed). The operator simplifies: one write at a time, await response
or timeout, retry. The slowdown (a few seconds per global change) is
acceptable at 15-min cadence; the simplification is large.

## 17. Mechanical safety invariants

The operator enforces these BEFORE writing. Failures bubble up as
`PlannedAction` rejections (the planner is told and can replan).

* **Slot times on 5-min granularity.** Snap before publish.
* **Slot times contiguous.** `slot[N+1].start == slot[N].end`. Slot 0
  starts at 00:00, slot 5 ends at 00:00.
* **Exactly 6 slots.** No more, no less.
* **SOC values in [0, 100].** Reject otherwise.
* **min_soc respected** — slot capacity_soc cannot be below
  `config.min_soc` unless `eps_active` (per SPEC H3 override).
* **Mode strings exact.** `"Selling first"` etc. — case + whitespace
  must match SA's discovery output. Compare via canonicalised strings.
* **GC values lowercase.** `"true"` / `"false"`. (HEO-32 incident.)
* **Current values in [0, 350]** (Sunsynk hardware limit).

## 18. Configuration

Operator config is one HA `config_entry` plus number/select entities
the user can tune at runtime.

### Initial setup (config_flow)

* SA MQTT broker host + port + credentials.
* Inverter name (default: `inverter_1`).
* Entity IDs for each peripheral (zappi, washer, dryer, dishwasher).
* Path for storage (state cache, replan baseline).

### Runtime tunables (HA number / select entities)

* `number.heo3_min_soc` — battery floor.
* `number.heo3_cycle_budget` — daily cycle soft cap.
* `number.heo3_target_end_soc` — uncertainty buffer (planner uses).
* `switch.heo3_dry_run` — operator skip writes (test mode).
* `switch.heo3_pause_on_verification_failure` — pause writes on
  any verification miss until user clears.

## 19. Testing strategy

### Unit tests (no MQTT, no HA)

* **Mock MQTT transport** that records publishes and lets tests inject
  responses. Assert the right topics + payloads are produced.
* Each adapter (Inverter / Peripheral / World) tested in isolation.
* `Operator.apply()` tested end-to-end with mock transport: build a
  `PlannedAction`, assert the right `Write` sequence, simulate
  responses, verify `ApplyResult`.

### Integration tests (live MQTT, mock HA)

* **mosquitto-probe fixtures** — capture real SA topic-format snapshots
  (HEO-32 incident shows these change). Replay against the operator.
* `tests/integration/test_sa_live.py` — opt-in tests that connect to
  the live SA broker. Run manually before each release.

### Replay tests

* Capture a day's worth of HA state changes (rates, forecasts, flags)
  as a fixture.
* Replay through the operator's world gatherer; assert the snapshot
  matches an expected schema.

### NOT testing dry_run

Per `feedback_dry_run.md`: dry_run skips MQTT publish and hides writer
bugs. Tests use **mock transport**, not dry_run. Dry_run is a runtime
flag for the user, not a testing tool.

## 20. Build phases (operator-only)

* **P1.0 — Skeleton.** Package layout, `Operator` class with stub
  methods, mock transport, config_flow shell. ~1 day.
* **P1.1 — Inverter Adapter writes.** Slots + globals + verification
  cycle. Tests with mock transport assert correct topics + payloads.
  ~2 days.
* **P1.2 — Inverter Adapter reads.** Live state collation. Tests
  feeding mock HA states. ~1 day.
* **P1.3 — Peripheral Adapter.** EV + appliances + reads. Tests
  asserting service-call shape. ~1 day.
* **P1.4 — World Gatherer rates.** BD + IGO + AgilePredict reads,
  freshness tracking. ~1-2 days.
* **P1.5 — World Gatherer forecasts.** Solcast + load model wiring.
  Mostly porting from HEO II. ~1 day.
* **P1.6 — World Gatherer flags.** IGO dispatch, saving session,
  EPS detection. ~1 day.
* **P1.7 — Snapshot integration.** Compose adapters into one
  `Snapshot`. End-to-end test. ~1 day.
* **P1.8 — Compute library.** Pure-function family in §12. Each
  family in own file with focused unit tests against synthetic
  Snapshots. Energy/SOC/kWh + time/rate + forecast aggregation +
  counterfactual + physics. ~3 days.
* **P1.9 — Build (action constructors).** Each constructor in §13
  with tests asserting the produced PlannedAction matches expected
  writes for representative inputs. ~2 days.
* **P1.10 — Live SA validation.** Connect to the real SA broker on
  the prod HA. Mosquitto-probe to verify SA value vocabulary still
  matches what the operator sends. Confirm verification cycle works
  end-to-end. ~1 day.
* **P1.11 — Static baseline plan + cutover.** Apply the static
  baseline plan to the inverter (use `build.baseline_static()` +
  `apply()` from a one-shot script), turn off HEO II, let the
  operator run with no planner attached (it gathers snapshots,
  exposes Compute results via dashboard sensors, applies
  `build.lockdown_eps()` on grid-loss transitions). Validates the
  operator doesn't break the live system. ~1 day.

**Total: ~14-17 working days, ~3-4 weeks.** Compute + Build add ~5
days vs the previous estimate but absorb the planner's hardest work.

After P1 lands and runs cleanly for a week, planner design begins —
and starts from a much better place because the operator already
exposes `compute.bridge_kwh`, `compute.import_volume_under_plan`,
`build.sell_kwh`, etc. The planner just decides intent.

## 21. Open questions

*(none currently — all resolved during design review)*

### Resolved

* **`zero_export_to_ct` as a separate setting** (resolved 2026-05-10):
  doesn't exist. "Zero export to CT" is a value of `work_mode`, not a
  separate boolean. Removed from §4.
* **`battery_max_soc` setting** (resolved 2026-05-10): hardware doesn't
  expose any global SOC ceiling. Confirmed by HA states scrape — no
  such entity exists on either SA or the Deye-Sunsynk integration. The
  ceiling primitive is `max_charge_current`: write `1` for soft cap
  (PV trickle still allowed) or `0` for hard freeze. Planner owns the
  policy; operator just exposes the existing current-throttle write.
* **Tesla integration** (resolved 2026-05-10): in scope for v1 via the
  Teslemetry HA integration, which exposes the full vehicle surface
  (72 entities for Paddy's car, "Natalia"). Operator's TeslaAdapter
  uses `switch.<vehicle>_charge` for stop/start, `number.<vehicle>_charge_limit`
  for SOC ceiling, `number.<vehicle>_charge_current` for AC throttle.
  All writes gated by `binary_sensor.<vehicle>_located_at_home`. See
  §6 / §7 for full surface. Both zappi and Tesla paths can stop a
  Tesla charge — planner picks based on intent.
* **HEO-5 load model: port or rewrite** (resolved 2026-05-10): port
  as-is for P1 (~443 lines, ~1 day). Solar forecast (Solcast) is
  accurate in production per Paddy; load model accuracy will be
  measured once the planner is running. The operator will expose
  forecast-vs-actual error sensors so a future rewrite decision is
  data-driven.
* **Single-tick latency budget** (resolved 2026-05-10): hard cap of
  60 seconds on `apply()`, with a 30-second warning threshold. At
  15-min cadence this is generous (~67x margin). The cap exists to
  prevent a stuck verification cycle from overlapping the next tick.
  Revisit if cadence ever drops to 1-min. Acceptance test in P1.10
  asserts a representative apply() completes well under 30s.

## 22. Sign-off checklist

- [ ] Scope (§2) — every hook the planner could need is enumerated, nothing missing
- [ ] Architectural shape (§3) — Operator + adapters + Compute + Build + apply
- [ ] Inverter writes (§4) — every Sunsynk control HEO III might need
- [ ] Inverter reads (§5) — every sensor HEO III might need
- [ ] Peripheral controls + reads (§6, §7) — EV, appliances, future hooks
- [ ] World rates + forecasts + flags (§8, §9, §10) — full external state
- [ ] Snapshot shape (§11) — typed, frozen
- [ ] Compute library (§12) — every derived calculation a planner might need
- [ ] Build constructors (§13) — every high-level intent → PlannedAction mapping
- [ ] PlannedAction shape (§14) — typed, frozen
- [ ] ApplyResult (§15)
- [ ] Verification + write/read cycle (§16)
- [ ] Mechanical safety invariants (§17)
- [ ] Config + tunables (§18)
- [ ] Testing strategy (§19) — no dry_run for tests
- [ ] Build phases (§20) — P1.0 through P1.11, estimated 14-17 days
- [ ] Open questions (§21) — anything to resolve before P1.0?

After sign-off: open tracking issue, P1.0 begins.
