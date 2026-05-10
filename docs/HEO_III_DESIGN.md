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
┌─────────────────────────────────────────────────────────────┐
│                         Operator                             │
│  (single public class, single point of contact for planner)  │
│                                                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐          │
│  │  Inverter   │  │ Peripheral  │  │   World     │          │
│  │   Adapter   │  │   Adapter   │  │  Gatherer   │          │
│  │             │  │             │  │             │          │
│  │ MQTT write  │  │ HA service  │  │ HA entity   │          │
│  │ MQTT read   │  │ calls       │  │ reads       │          │
│  │ verify      │  │ HA entity   │  │             │          │
│  │             │  │ reads       │  │             │          │
│  └─────────────┘  └─────────────┘  └─────────────┘          │
│         │                │                │                  │
│         ▼                ▼                ▼                  │
│  ┌──────────────────────────────────────────────────┐       │
│  │              Snapshot (frozen state)              │       │
│  │  Single immutable struct, what the world IS now   │       │
│  └──────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────┘
                            ▲
                            │  (planner consumes Snapshot,
                            │   produces PlannedAction,
                            │   passes back to Operator.apply)
```

Three internal sub-modules. One outward-facing class. The planner sees
the `Operator` interface only.

### Public API (single source of truth for the planner)

```python
class Operator:
    async def snapshot(self) -> Snapshot:
        """Gather a complete frozen state: inverter + peripherals +
        world. Single call returns everything the planner needs."""

    async def apply(self, action: PlannedAction) -> ApplyResult:
        """Mechanically execute a planned action: inverter writes,
        peripheral changes. Verifies and reports per-write outcome."""

    async def shutdown(self) -> None:
        """Graceful close: MQTT disconnect, pending verifications
        cancelled."""
```

Three methods. Everything else is internal organisation.

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
| Max charge current | `set/inverter/inverter_1/max_charge_current` | `"0".."350"` | Amps. Sunsynk 5kW peaks ~100A at 51.2V. |
| Max discharge current | `set/inverter/inverter_1/max_discharge_current` | `"0".."350"` | Amps. |
| Zero export to CT enabled | `set/inverter/inverter_1/zero_export_to_ct` | `"true"` / `"false"` | **New for HEO III** — not in HEO II. Per SPEC §2 item 8. Verify SA value vocabulary via mosquitto-probe before relying on it. |
| Battery max SOC | `set/inverter/inverter_1/battery_max_soc` | `"0".."100"` | **New for HEO III** — gives planner a way to set system-level SOC ceiling without touching all 6 slots. May not exist on all firmwares; probe first. |

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
| `zero_export_to_ct` | `sensor.sa_inverter_1_zero_export_to_ct` (verify exists) |
| `battery_max_soc` | `sensor.sa_inverter_1_battery_max_soc` (verify exists) |

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

### Tesla (future hook, may not be wired v1)

| Action | Mechanism | Notes |
|---|---|---|
| Stop charging | `service: tesla.stop_charge` (or HA-native equivalent) | Currently HEO II uses zappi-only; Tesla support is a future planner feature. |

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
| Tesla charging now | (TBD entity if/when Tesla integration is added) | Future. |

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

## 12. PlannedAction — the planner's output

Equally typed. Whatever the planner produces, this is the shape:

```python
@dataclass(frozen=True)
class PlannedAction:
    # --- inverter writes ---
    slots: tuple[SlotPlan, ...]        # exactly 6 (or empty = no slot changes this tick)
    work_mode: Optional[str]
    energy_pattern: Optional[str]
    max_charge_a: Optional[float]
    max_discharge_a: Optional[float]
    zero_export_to_ct: Optional[bool]
    battery_max_soc: Optional[int]

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

## 13. ApplyResult — what happened

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

## 14. Verification + write/read cycle

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

## 15. Mechanical safety invariants

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

## 16. Configuration

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

## 17. Testing strategy

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

## 18. Build phases (operator-only)

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
* **P1.8 — Live SA validation.** Connect to the real SA broker on the
  prod HA. Mosquitto-probe to verify SA value vocabulary still matches
  what the operator sends. Confirm verification cycle works
  end-to-end. ~1 day.
* **P1.9 — Static baseline plan + cutover.** Apply the static
  baseline plan to the inverter (a one-shot script), turn off HEO II,
  let the operator run with no planner attached (it just gathers
  snapshots and reports them via dashboard). Validates the operator
  doesn't break the live system. ~1 day.

**Total: ~10-12 working days, ~2-3 weeks.**

After P1 lands and runs cleanly for a week, planner design (the other
doc) begins.

## 19. Open questions

* **`zero_export_to_ct` setting** — does SA's MQTT discovery actually
  expose this on Paddy's firmware? Mosquitto-probe required (P1.0 task).
* **`battery_max_soc` setting** — same question. May not be present on
  all firmware; operator should detect at startup and surface a warning
  if missing rather than fail.
* **Tesla integration** — out of scope for v1; operator will have a
  `TeslaAdapter` placeholder that's disabled until the user wires it.
* **HEO-5 load model** — port as-is or rewrite cleaner? Port as-is for
  P1; rewriting is a separate piece of work.
* **Single-tick latency budget** — what's the acceptable tick duration?
  At 15-min cadence, a few seconds is fine. At 1-min cadence (future),
  this matters. Decide once we see real timings.

## 20. Sign-off checklist

- [ ] Scope (§2) — every hook the planner could need is enumerated, nothing missing
- [ ] Architectural shape (§3) — operator + 3 sub-adapters + Snapshot
- [ ] Inverter writes (§4) — every Sunsynk control HEO III might need
- [ ] Inverter reads (§5) — every sensor HEO III might need
- [ ] Peripheral controls + reads (§6, §7) — EV, appliances, future hooks
- [ ] World rates + forecasts + flags (§8, §9, §10) — full external state
- [ ] Snapshot + PlannedAction shapes (§11, §12) — typed, frozen
- [ ] Verification + write/read cycle (§14)
- [ ] Mechanical safety invariants (§15)
- [ ] Config + tunables (§16)
- [ ] Testing strategy (§17) — no dry_run for tests
- [ ] Build phases (§18) — order + estimated effort
- [ ] Open questions (§19) — anything to resolve before P1.0?

After sign-off: open tracking issue, P1.0 begins.
