# HEO III — System Design

> Status: **draft for review**. This document defines what HEO III is, why
> it's being built, and how it differs from HEO II. Code follows the doc;
> the doc is the source of truth. Sign off shape and decisions here BEFORE
> any implementation.
>
> Tracking issue: TBD. Targets: `custom_components/heo3/` in this repo,
> running side-by-side with HEO II until cutover. Owner: Paddy. Rebuild
> rationale: post-Phase-3 actuals (week of 2026-05-02 to 2026-05-09)
> showed the rules-engine architecture cannot natively price uncertainty;
> 2026-05-08 sold ~24 kWh at 8.7p assuming 4.95p IGO off-peak replacement
> and ended up paying 28.58p peak when the refill plan failed.

## 1. Goals and non-goals

### Goals

1. **Decisions explainable from a single source.** Every action HEO III
   takes traces back to one explicit cost objective and one set of
   constraints. No more "rule X overrode rule Y because of execution
   order; here's the matrix doc that documents it".
2. **Uncertainty priced in, not assumed away.** Forecasts are wrong some
   days. The system must reason about that — the cost of selling a kWh
   should account for the chance the refill plan fails.
3. **Mechanical control fully decoupled from planning.** The operator
   module talks to the inverter; the planning module decides what the
   operator should do. No economics in the operator, no MQTT in the
   planner.
4. **All inputs to a decision are observable.** Live audit trail per
   tick: inputs, optimisation problem, solver result, written outputs,
   verification.

### Non-goals

1. **Not a market-grade trading system.** We're optimising one battery
   against published Octopus rates over a 24–48h horizon. No
   stochastic-process modelling beyond simple forecast scenarios.
2. **Not a research vehicle.** We pick a working solver, not the most
   theoretically pure one. CVXPY or PuLP, both well-supported, both
   battle-tested.
3. **Not a full Sunsynk abstraction layer.** The operator covers the
   subset of Sunsynk controls HEO III actually uses. Anything else stays
   manual.
4. **Not a UI rewrite.** HA entities, dashboards, switches — copy
   wholesale from HEO II in spirit. Rebuilding the UI is out of scope.

## 2. World model

The load-bearing design choice: HEO III reasons about the world as a
**state-trajectory optimisation problem** over a rolling 24–48h horizon,
with explicit uncertainty handling.

### State variables (per 30-min step over the horizon)

* **`soc[t]`** — battery SOC at start of step t, fraction 0..1.
* **`p_charge[t]`** — power flowing INTO the battery (kW, ≥0).
* **`p_discharge[t]`** — power flowing OUT of the battery (kW, ≥0).
* **`p_grid_import[t]`** — power drawn from grid (kW, ≥0).
* **`p_grid_export[t]`** — power exported to grid (kW, ≥0).
* **`mode[t]`** — categorical: {`Zero export to CT`, `Selling first`}. Maps
  to inverter `work_mode`.

### Inputs (per 30-min step)

* **`r_import[t]`** — import rate p/kWh. From BottlecapDave (live, Octopus-
  published). Falls back to AgilePredict only for plan visualisation —
  never for actual writes (SPEC H4).
* **`r_export[t]`** — export rate p/kWh. Same source.
* **`pv[t]`** — solar generation forecast (kWh in step). From Solcast,
  with optional P10/P50/P90 bands for uncertainty.
* **`load[t]`** — house load forecast (kWh in step). From HEO-5 14-day
  learning model.
* **`flags[t]`** — categorical state: `eps_active`, `saving_session`,
  `ev_charging`, `igo_dispatching`, `igo_planned[]`, `defer_ev_eligible`.

### Power balance constraint (per step)

```
pv[t] + p_grid_import[t] + p_discharge[t]
  = load[t] + p_grid_export[t] + p_charge[t]
```

(Approximation; ignores in-step inverter losses except the round-trip
efficiency baked into SOC continuity below.)

### SOC continuity constraint

```
soc[t+1] = soc[t]
         + (p_charge[t] * dt * eta_charge) / capacity_kwh
         - (p_discharge[t] / eta_discharge * dt) / capacity_kwh
```

With `eta_charge ≈ 0.95`, `eta_discharge ≈ 0.95`, round-trip ≈ 0.9.
`capacity_kwh = 20.48`. `dt = 0.5` (hours per step).

### Operating limits

* `min_soc ≤ soc[t] ≤ 1.0` for all t. (`min_soc = 0.1`, configurable.)
* `0 ≤ p_charge[t] ≤ p_max_charge`. `p_max_charge = 5 kW` (Sunsynk
  nominal at full A).
* `0 ≤ p_discharge[t] ≤ p_max_discharge`. Same limit.
* `0 ≤ p_grid_import[t] ≤ p_inverter_max`. `p_inverter_max = 5 kW`.
* `0 ≤ p_grid_export[t] ≤ p_inverter_max`.
* **No simultaneous charge + discharge** (linearised via mode binary).
* **No simultaneous import + export** in `Zero export to CT` mode (mode
  binary controls).

### Uncertainty representation

Two options, pick one for v1:

**Option U1: Deterministic with safety margin.** Use a single forecast
(P50). Add a `safety_kwh` reserve to the SOC at the end of horizon —
forces the optimiser to land at a higher SOC than minimum, buffering
against forecast misses. Simpler. Tunable via one parameter.

**Option U2: Multi-scenario stochastic.** Solve over N forecast
scenarios (e.g., low-PV / median / high-PV from Solcast P10/P50/P90),
weight by probability, optimise expected cost. Naturally prices
uncertainty. Roughly 3x compute. Standard formulation, well-supported.

**Recommendation:** start with U1 (deterministic + safety margin) for
v1, upgrade to U2 once the rest is stable. The safety margin is the
single knob that fixes the 2026-05-08 failure pattern; U2 is a
refinement.

## 3. Decision model

### Objective function (cost to minimise)

```
total_cost =
    SUM over horizon t {
        r_import[t] * p_grid_import[t] * dt           # buy energy
      - r_export[t] * p_grid_export[t] * dt           # sell energy
    }
  + cycle_cost * total_throughput_kwh
  + boundary_cost(soc[T] vs target_end_soc)
```

* **`cycle_cost`**: small per-kWh penalty on battery throughput
  (`p_charge + p_discharge` summed over horizon) to enforce the SPEC H7
  soft cap of 2 cycles/day. ~0.5p/kWh; tunes the optimiser away from
  marginal arbitrage that would burn cycle budget.
* **`boundary_cost`**: large penalty if `soc[T]` (end of horizon) is
  below `target_end_soc`. Forces the plan to leave the battery in a
  reasonable state for the next planning window. This is the
  **uncertainty-pricing knob**: setting `target_end_soc` higher than
  strictly necessary buffers against forecast miss. The 2026-05-08
  failure was effectively `target_end_soc = min_soc` — no buffer.

### Hard constraints (no plan exists if violated)

* SPEC H1 — no `grid_charge=True` covering peak hours (28.58p slots).
* SPEC H2 — during `ev_charging`, `p_discharge[t] = 0` (battery doesn't
  feed EV).
* SPEC H3 — during `eps_active`, `min_soc → 0` (allow drain to floor).
* SPEC H4 — write only published rates; if any `r_import` or `r_export`
  for the next 6h is from forecast not BD, refuse to write (planner
  still runs for diagnostics).
* SPEC H7 — running cycle count over last 3 days < `cycle_budget * 3`
  (alert only, doesn't block plans).

### Soft objectives (encoded as cost terms)

* SPEC §5 priority 1 (avoid peak import) — already implicit in
  objective (peak `r_import` is high, optimiser avoids).
* SPEC §5 priority 2 (avoid grid use) — implicit (any positive
  `p_grid_import` adds cost).
* SPEC §5 priority 3 (sell during top windows) — implicit (high
  `r_export` slots have higher revenue).
* SPEC §5 priority 4 (PV ROI) — implicit (every kWh of PV displaces
  imports, no separate term needed).
* SPEC §5 priority 5 (Saving Sessions) — when `saving_session = True`
  in the flags, override `r_export[t]` for the session window with the
  session price (£3+/kWh) so the optimiser naturally drains.

### Solver

* **CVXPY** for the convex relaxation, **CBC** (via PuLP) or **SCIP**
  for the MILP when mode binaries are active.
* For one battery, 24h horizon at 30-min granularity = 48 timesteps.
  Variables: ~250. Solves in <1s on the HA host.
* If solver time becomes a bottleneck, drop to deterministic (U1) only
  and skip mode binaries by selecting work_mode heuristically post-
  solve.

### Mapping the 48-step trajectory back to 6 inverter slots

The Sunsynk has 6 hard slots; the optimiser produces 48 steps. We need
to compress.

**Strategy:** the optimiser is run as a 48-step problem to find the
ideal trajectory. Then a separate **slot-mapping** step finds the 6
slot boundaries (subject to 5-min granularity and `start[0] = 00:00`,
`end[5] = 00:00`) that minimise the L2 error between the 6-slot
piecewise-constant SOC schedule and the 48-step ideal. Tractable as a
small dynamic-programming search over candidate boundaries.

Result: 6 slots × `{start_time, end_time, capacity_soc, grid_charge}`
+ globals `{work_mode, energy_pattern, max_charge_a, max_discharge_a}`.
Same shape the operator already accepts — no disruption to write path.

## 4. Worked example: how HEO III handles 2026-05-08

### Inputs at 18:00 daily-plan time (2026-05-08)

* SOC: 80%
* Capacity: 20.48 kWh, min_soc = 10% (= 2.05 kWh floor)
* Load forecast 18:00-23:30: 4.5 kWh (evening peak)
* Load forecast 23:30-05:30: 1.0 kWh (overnight)
* Load forecast 05:30-09:00: 2.0 kWh (morning before PV takeover)
* PV forecast tomorrow: 53.1 kWh (Solcast P50)
* Tomorrow load total: ~22 kWh
* Export rates 18:00-23:30: 11-13p, top 3 around 11.66-13.28p
* Export rates 17:30-19:30 (current peak): around 11-13p
* Import rates: peak 28.58p (06:00-23:30), off-peak 4.95p (23:30-06:00)

### What HEO II actually did (2026-05-08, ground truth)

PeakArbitrage's `worth_selling` test: `11.66 × 0.9 = 10.5p > 4.95p`
replacement → sell. Allocated 8 kWh of "spare" to top 3-4 export slots.
Drained battery from 51% to 10% (floor) by 22:00. Overnight cheap charge
sized for 0.2 kWh morning bridge — landed at 20% by 05:30. Hit floor
mid-morning before PV took over. Imported at peak rate ~5p worth.

### What HEO III does

The optimiser gets the same inputs but with `target_end_soc = 50%`
(safety margin) and the cycle_cost term active.

Setting up the cost function:

* Selling at 11.66p between 21:30-22:00 nets 11.66p × 2.5 kWh × 0.5h =
  ~14.6p revenue per slot.
* Replacement at peak (28.58p) if the plan miscalculates: 28.58p × 2.5
  kWh = 71.5p cost. Asymmetry baked in.
* `boundary_cost(soc[T] < 50%)`: penalty grows steeply below 50% at
  end-of-horizon to enforce the safety reserve.

Solver output:

* Drain limited to ~30% SOC by midnight (above the 50% target_end means
  the optimiser leaves margin even with cycle cost favouring deeper
  drain).
* Sells ~6 kWh across top 2 export slots (not 4 — marginal profit on
  3rd/4th slot is below the implicit cycle + uncertainty cost).
* Overnight charge target: 75% (not 20%) — bridges next-day morning
  load with a margin against PV being later than P50.
* Result: 0 peak imports overnight or morning. ~£2.50 export revenue
  (vs HEO II's £4.29). Net day position basically equivalent or
  slightly better, with the catastrophe-tail risk removed.

The crucial difference: in HEO II, `worth_selling` was a per-rule
local decision against off-peak replacement. In HEO III, every kWh
sold is weighed against the optimiser's full forecast-aware cost
function — including the explicit risk of the refill failing.

## 5. Failure-mode tests

The doc must articulate how the design handles plausible failure
modes beyond 2026-05-08. Each becomes a regression test scenario in
`tests/heo3/test_scenarios/`.

### S1: Heavy unpredicted evening load (oven, dishwasher, AC)

* Forecast: 4.5 kWh evening load. Actual: 7 kWh.
* HEO II behaviour: PeakArbitrage allocated assuming 4.5; battery hits
  floor at ~21:00, peak imports for the rest of the evening.
* HEO III behaviour: `boundary_cost` keeps the plan at higher
  end-of-horizon SOC; even with 50% over-run, battery stays well
  above floor until cheap window. Once cheap charge arrives, replan
  on Wednesday's tick incorporates the new load reality.

### S2: PV under-delivers (cloudy day, forecast missed)

* Forecast: 53 kWh PV. Actual: 25 kWh.
* HEO II behaviour: CheapRateCharge sized overnight target tight
  (53 kWh PV will refill); battery starts day at 20%; PV doesn't
  refill enough; evening drain hits floor; peak imports.
* HEO III behaviour: `target_end_soc` keeps overnight charge target
  at 75% (not 20%). Even with PV miss, evening doesn't strand.
  Cycle cost may grow but no peak imports.

### S3: Saving session + IGO dispatch overlap

* Saving session 17:00-18:00, IGO dispatch 17:30-18:30.
* HEO II behaviour: F2 bug pre-2026-05-08 (IGO refilled session
  battery). Fixed by guard.
* HEO III behaviour: both contributors emit constraints. Saving
  session emits "during [17:00,18:00], `mode = Selling first` and
  `r_export = £3+/kWh`". IGO dispatch emits "during [17:30,18:30],
  prefer `p_charge` if cheap rate available". The optimiser
  reconciles natively — sells through the session, charges in the
  remaining 30 min after.

### S4: Grid loss during a planned export

* `eps_active = True` mid-tick.
* HEO II behaviour: EPSModeRule overrides all slots cap=0, gc=False.
* HEO III behaviour: hard constraint `eps_active → min_soc = 0,
  p_grid_export = p_grid_import = 0`. Optimiser produces the EPS
  plan; operator writes it; coordinator turns off EV / appliance
  switches per SPEC H3.

### S5: BD outage (no live rates)

* BottlecapDave returns no rates this tick.
* HEO II behaviour: `_live_rates_present = False`, writes blocked.
* HEO III behaviour: same. Operator refuses to apply if SPEC H4
  guard fails. Planner still runs for visualisation / dashboard.

## 6. Operator module

`custom_components/heo3/operator/`. Owns the inverter — full mechanical
control, no opinions.

### Public API

```python
class Operator:
    async def read_state() -> InverterState
    async def apply(plan: PlannedState) -> ApplyResult
    def snapshot() -> InverterState  # cached, last-known
```

### `InverterState` shape

```python
@dataclass(frozen=True)
class InverterState:
    soc_pct: float                  # live, 0..100
    slots: tuple[SlotState, ...]    # 6 slots, current
    work_mode: str
    energy_pattern: str
    max_charge_a: float
    max_discharge_a: float
    grid_voltage: float
    eps_active: bool
    captured_at: datetime
```

### `PlannedState` shape

Same as the rules module produces — 6 slots + globals. Pre-validated.

### `ApplyResult` shape

```python
@dataclass(frozen=True)
class ApplyResult:
    requested_writes: tuple[Write, ...]
    successful: tuple[Write, ...]
    failed: tuple[Write, ...]      # with reason
    verification: VerificationResult
```

### Internal scope

* MQTT transport (port `direct_mqtt_transport.py` from HEO II).
* Topic mapping (port from HEO II `mqtt_writer.py`).
* SA value vocabulary (lowercase true/false, exact mode strings).
* 5-min granularity enforcement on writes.
* Slot boundary continuity (00:00 → 00:00 wrap, contiguous, exactly 6).
* Diff-only writes (don't republish unchanged values).
* Write verification (publish, await response, retry policy).
* Live state cache (operator updates on inverter state pushes).

### Out of scope

Anything that involves an opinion: SOC targets, when to sell, what
work_mode means semantically. The operator is told "set slot 3 cap to
50%, gc to False"; it writes. It doesn't validate "is 50% a sensible
target".

## 7. Rules / Contributors module

`custom_components/heo3/contributors/` (renamed from "rules" because
they don't WRITE; they CONTRIBUTE constraints + cost terms to the
optimisation).

### Contributor interface

```python
class Contributor(Protocol):
    name: str
    enabled: bool

    def contribute(
        self, world: WorldState, problem: OptimisationProblem,
    ) -> ContributionReport:
        """Append constraints and/or cost terms to `problem`. Return
        a structured report describing what was added — used by the
        introspection / audit trail."""
```

### Contribution types

* **HardConstraint**: `(time_window, expression)`. E.g.,
  `("17:00-18:00", "mode == Selling first")`. Cannot be violated; if
  conflicting hards, problem is infeasible and writes are blocked
  (with diagnostic).
* **CostTerm**: `(time_window, weighted_objective)`. E.g.,
  `("18:00-23:30", "+1.0 * p_grid_import * 28.58")`. Adds to the
  total objective.
* **VariableBound**: `(time_window, variable, lo, hi)`. E.g.,
  `("19:00-21:00", "soc", 0.5, None)` — keeps SOC ≥ 50% during a
  user-defined "high-load" window.

### Worked example contributors

* **EPSContributor**: when `world.flags.eps_active`, adds hard
  constraints `min_soc = 0`, `p_grid_export = 0`, `p_grid_import = 0`.
* **SavingSessionContributor**: when `world.flags.saving_session`,
  overrides `r_export` in the session window with the session price,
  forces `mode == Selling first` for the duration.
* **EVDeferralContributor**: when triggers met, adds cost term
  steering `p_discharge` toward export rather than EV charging.
* **CycleBudgetContributor**: adds cost term proportional to total
  charge throughput (the soft H7 cap).
* **EndOfHorizonContributor**: adds the `boundary_cost` term for
  `soc[T] < target_end_soc`. THIS IS THE 2026-05-08 FIX.

### Introspection (the bit Paddy specifically asked for)

```python
class ContributorRegistry:
    def all_contributions(world: WorldState) -> list[ContributionReport]
    def conflict_report(world: WorldState) -> ConflictReport
    def per_field_provenance(plan: PlannedState) -> dict[str, list[str]]
```

`all_contributions` runs every contributor against `world`, returns
the structured list of constraints/cost terms each emitted. Pure
function. Callable for any test scenario.

`conflict_report` walks the contributions and detects:
* Hard-vs-hard conflicts (infeasibility).
* Hard constraint that the cost terms would otherwise violate
  (active-binding flag).
* Time-window overlaps with semantically incompatible directives.

`per_field_provenance` after a solve, maps each output field
(slot[i].capacity_soc, work_mode, etc.) back to the contributors
whose constraints/costs were active. Surfaces in the dashboard:
"why is slot 3 at 50%?" → "EndOfHorizon (target_end_soc) + cycle
cost + base cost".

### Per-contributor enable/disable

Each contributor exposes a HA switch entity
(`switch.heo3_contributor_<name>`) so we can disable individual
contributors at runtime for debugging.

## 8. Coordinator + lifecycle

`custom_components/heo3/coordinator.py`. Wires it all together at the
HA tick cadence.

### Tick flow (15 min)

1. Gather `WorldState` from HA entities (rates, SOC, forecasts, flags).
2. Run all `Contributor.contribute(world, problem)`.
3. Solve `problem`. Record full audit trail.
4. If solver succeeds and SPEC H4/H5 pass, hand `PlannedState` to
   operator; else block writes with diagnostic.
5. Operator diffs against last-known inverter state, writes deltas,
   verifies.
6. Update HA dashboard sensors with audit, plan, projected outcome.

### Daily plan (18:00 BST)

Same flow but with the full 48-step horizon (covers tomorrow's
24h). The daily plan re-baselines the 15-min ticks for the day.

### Replan triggers (between daily plans)

* World state divergence above threshold (forecast / SOC / flag
  transitions) — same shape as HEO II `replan_triggers.py`.
* Contributor change (e.g., saving session announced).
* Solver-output globals changed vs last commit (the bug pattern
  HEO II PR #73 fixed; here it's natively built into the design).

## 9. Migration plan

### Build phases

* **P1 — Operator module + tests.** Pure mechanical layer. Mock
  MQTT transport for tests; live transport against SA broker for
  integration. ~2-3 days.
* **P2 — World state gathering.** Port BD / Solcast / HA entity
  reading from HEO II's coordinator. ~1 day.
* **P3 — Optimiser core.** Formulate problem in CVXPY/PuLP; solve;
  trajectory-to-slot mapping. Standalone tests with synthetic
  inputs. ~3-5 days.
* **P4 — Contributors.** Port each economic decision as a
  Contributor. ~2-3 days.
* **P5 — Coordinator + dashboard sensors.** ~1-2 days.
* **P6 — Replay validation.** Replay 2026-05-08 inputs and 5+
  other historical days; verify outputs are sensible. ~2-3 days.
* **P7 — Shadow mode.** Run HEO III alongside HEO II (HEO II
  active, HEO III computes but doesn't write). Compare plans for
  3-5 days. ~ongoing.
* **P8 — Cutover.** Disable HEO II, enable HEO III writes. ~1
  day for the switchover, monitor for a week.

**Total estimated effort: 12-19 working days, ~3-4 weeks.**

### Static baseline plan (cutover gap)

Between "HEO II off" (Paddy's stated intent) and "HEO III ready",
inverter must run on a known-good static schedule. Suggested:

| Slot | Time | Cap | gc |
|---|---|---|---|
| 1 | 00:00-05:30 | 80% | True |
| 2 | 05:30-18:00 | 100% | False |
| 3 | 18:00-23:30 | 25% | False |
| 4 | 23:30-23:55 | 80% | True |
| 5 | 23:55-23:55 | 10% | False |
| 6 | 23:55-00:00 | 10% | False |

Globals: `work_mode = Zero export to CT`, `energy_pattern = Load
first`, `max_charge_a = max_discharge_a = 100`. No arbitrage; just
charge cheap, hold for evening, drain to 25% reserve, refill cheap.

Apply once via a one-shot script (`scripts/apply_static_baseline.py`)
before disabling HEO II. Inverter runs this until HEO III takes over.

### Cutover criteria (P8 → live)

HEO III is OK to take over when, on replay/shadow data:

* Zero unplanned peak-rate imports across the validation window.
* Net cost ≤ HEO II's actual net cost (or within 5%).
* Cycle budget respected.
* No solver infeasibilities or unhandled exceptions.
* All SPEC hard rules (H1-H7) verified by tests.

### HEO II retirement

Once HEO III has run cleanly for 2+ weeks, archive HEO II:

* Move `custom_components/heo2/` to `custom_components/heo2.archived/`
  (keeps git history; signals do-not-deploy).
* Remove HEO II from `manifest.json` deployment targets.
* Keep tests for reference; gate them with `@pytest.mark.archived`.

## 10. Open questions

* **Forecast uncertainty: how much margin?** `target_end_soc` value
  for U1 (deterministic + safety margin) — calibrate against
  historical forecast errors. 50% is a reasonable starting guess
  but should be tuned.
* **Cycle cost weight.** What p/kWh penalty discourages marginal
  cycling without suppressing useful arbitrage? Empirical;
  starts at 0.5p/kWh, refine.
* **Solver choice.** CVXPY (cleaner formulation, may need MILP
  extension for mode binaries) vs PuLP (handles MILP natively but
  more verbose). Both run fine on HA hardware. Decide during P3
  prototyping.
* **Rolling horizon vs daily plan.** Daily plan at 18:00 with full
  24-48h horizon, or every-tick re-solve over 24h sliding window?
  Daily plan + 15-min adjustments is closer to HEO II semantics;
  pure rolling-horizon is more responsive but more compute.
  Recommend: full solve daily, fast incremental adjust intra-day
  (warm-start from prior solution).
* **Tomorrow's prices.** Octopus publishes after 16:00 BST. Daily
  plan at 18:00 has them. Mid-day re-solves don't have tomorrow —
  use AgilePredict for visualisation only (SPEC H4 forbids writing
  forecast prices). Means mid-day plans have a shorter
  effective horizon.

## 11. What this doc does NOT yet specify

Deferred until P3+ implementation reveals concrete needs:

* Exact CVXPY/PuLP formulation (variables, constraints in code).
* HA dashboard entity layout (port from HEO II as starting point).
* Test scaffolding (fixture shapes, replay format).
* Specific Sunsynk register mapping for any new globals.
* Configuration entity layout (`number.heo3_target_end_soc` etc.).

These are implementation details, not design decisions.

---

## Sign-off checklist

- [ ] Goals + non-goals (§1) accurate
- [ ] World model (§2) — uncertainty handling: Option U1 first?
- [ ] Decision model (§3) — cost function shape + cycle cost
- [ ] 2026-05-08 walkthrough (§4) — predicted behaviour matches intent
- [ ] Operator interface (§6) shape signed off
- [ ] Contributor interface (§7) shape signed off
- [ ] Migration plan + static baseline (§9) accepted
- [ ] Open questions (§10) — anything to resolve before P1?

After sign-off: tracking issue in GitHub, P1 begins.
