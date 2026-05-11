# HEO III — Planner Module Design

> Status: **signed off 2026-05-11**, P2.0 ready to begin. This document defines the planner
> module. The mechanical operator layer it sits on top of is in
> `docs/HEO_III_DESIGN.md` (signed off 2026-05-10, in production
> from 2026-05-11 running `baseline_static`).
>
> Tracking issue: TBD. Owner: Paddy. Targets: `custom_components/heo3/planner/`,
> sitting alongside the operator's adapters/ + compute.py + build.py.

## 1. Why a separate planner doc

The operator design (HEO_III_DESIGN.md) was scope-disciplined to the
mechanical layer: snapshot, compute, build, apply. Zero economic
opinions. This planner doc is where the economic opinions live —
which rules fire, when, how they decide, and how they learn.

Two things shape this design more than anything else:

1. **HEO II's week-1 review (2026-05-08)**: the rules-engine pattern
   worked but had specific structural problems — the F2 race between
   SavingSession and IGODispatch, the arbitrage asymmetry (sold-low
   priced ~5p spread but cost ~20p when forecasts missed), the lack
   of native uncertainty pricing. The clean redesign here addresses
   those head-on, not as patches.
2. **Paddy's clarification (2026-05-11)**: rules must be named and
   observable when they're being used. Learning happens via
   threshold/bias auto-tuning (within safe bounds) plus a weekly
   digest sensor for human review. No auto-enable/disable of rules.

## 2. Scope

### In scope (the planner owns)

* **Rule definitions** — named, observable, ordered. Each rule decides
  whether to claim some part of the next plan and what it claims.
* **Arbitration** — when rules conflict on a slot or global, deterministic
  resolution by precedence + claim strength.
* **Coordinator / scheduler** — 15-min tick that runs the rules,
  resolves conflicts, hands the resulting `PlannedAction` to the
  operator's `apply()`. Plus event-driven ticks on EPS / saving
  session / IGO dispatch transitions.
* **Performance tracking** — per-rule activation counts and £
  attribution; forecast error tracking by category.
* **Learning (auto-tuning)** — adjust thresholds + forecast biases
  within hardcoded bounds based on the tracker's observations.
* **Weekly digest sensor** — `sensor.heo3_weekly_digest` with
  attributes summarising the week's behaviour, ready for review.

### Out of scope

* **Mechanical writes** — that's the operator's job (apply()).
* **Computing kWh / SOC / rate windows** — that's the operator's
  Compute library (§12 of the operator design).
* **Auto-enabling / disabling rules** — explicitly out per Paddy's
  clarification. Rule presence is a code change (PR), not runtime.
* **CVaR / scenario optimisation** — the rules-based architecture
  handles uncertainty via worst-case bias on key decisions, not via
  multi-scenario sampling. Revisit if the rules can't make it work.
* **Rolling-horizon optimiser** — explicitly NOT this. Rules engine
  with observability is the chosen pattern (per Paddy 2026-05-11).

### Non-goals

* **Not a re-port of HEO II.** Same rule names where the concepts
  carry forward, but the implementation is fresh — built on the
  operator's typed surface (Snapshot, PlannedAction, RateWindow)
  and free of HEO II's accumulated quirks.
* **Not a perfect optimiser.** A simple rule that's always observable
  and always-improving beats a perfect optimiser nobody can debug.

## 3. Architectural shape

```
┌──────────────────────────────────────────────────────────────────┐
│                         Coordinator                               │
│                                                                   │
│  ── Tick (15-min cron + event-driven) ──                          │
│  1. Operator.snapshot() → Snapshot                                │
│  2. RuleEngine.run(Snapshot) → Decision                           │
│       ├─ For each rule: rule.evaluate(snap) → Claim | None        │
│       ├─ Arbiter resolves Claims → PlannedAction                  │
│       └─ records per-rule activations + claims                    │
│  3. Operator.apply(action, snapshot=snap) → ApplyResult           │
│  4. PerformanceTracker.record(snap, decision, apply_result)       │
│  5. (periodically) Tuner.evaluate_and_adjust()                    │
│  6. (weekly) DigestBuilder.publish_to_sensor()                    │
└──────────────────────────────────────────────────────────────────┘
        │                        │                        │
        ▼                        ▼                        ▼
   Operator.apply         hass.states (audit         sensor.heo3_*
   (writes inverter)      trail per tick)            (digest, active rules,
                                                     last decision, ...)
```

Five pieces:
- **Rule** — pure-function-ish: takes a `Snapshot`, returns a `Claim` or `None`.
- **Arbiter** — deterministic conflict resolution between Claims.
- **Coordinator** — orchestrates the tick loop, the cron, the event hooks.
- **PerformanceTracker** — records what happened, computes £ attribution.
- **Tuner** — periodically nudges thresholds + biases within bounds.

## 4. Rule shape

```python
class Rule(Protocol):
    """A named, observable economic decision-maker."""

    name: str                    # e.g. "cheap_rate_charge"
    tier: int                    # 1=safety, 2=mode, 3=optimisation
    description: str             # why this rule exists, in one line
    parameters: dict[str, Tunable]  # named knobs + their bounds

    def evaluate(self, snap: Snapshot, ctx: RuleContext) -> Claim | None:
        """Return a Claim if this rule wants to act this tick.

        ctx exposes Compute helpers + access to other rules' previous
        decisions (for telemetry, not for chaining — chaining is the
        arbiter's job)."""
```

```python
@dataclass(frozen=True)
class Claim:
    """A rule's attempt to influence this tick's plan.

    Claims describe WHAT the rule wants (not the inverter writes).
    The Arbiter resolves and the resulting PlannedAction is built
    via the operator's Build constructors. This keeps rules clean
    of mechanical concerns."""

    rule_name: str
    intent: ClaimIntent           # see ClaimIntent below
    rationale: str                # human-readable, surfaces to digest
    strength: ClaimStrength       # MUST | PREFER | OFFER
    horizon: TimeRange            # when this claim applies
    expected_pence_impact: float  # signed £ — used for tracker attribution


class ClaimStrength(Enum):
    MUST = "must"     # hard requirement. Tier-1 only. EPS lockdown, min_soc floor.
    PREFER = "prefer" # rule is confident. Wins arbitration unless another MUST.
    OFFER = "offer"   # rule is opportunistic. Loses to PREFER.


class ClaimIntent:
    """Discriminated union of what a rule can claim."""
    # Variants:
    # - ChargeIntent(target_soc_pct, by_time, rate_limit_a)
    # - DrainIntent(target_soc_pct, by_time)
    # - HoldIntent(soc_pct, window)
    # - SellIntent(kwh, across_slots)
    # - LockdownIntent()             # only EPSLockdownRule
    # - DeferEVIntent / RestoreEVIntent
    # - HoldTeslaIntent(charge_limit_pct)
    # - etc.
```

### Why claims, not direct PlannedAction

Three reasons:
1. **Auditability** — the Claim carries rationale + expected impact;
   PlannedAction is mechanical and discards that intent.
2. **Arbitration** — Claims have strength + horizon, so the Arbiter
   can resolve "rule A claims slot 5 cap=25, rule B claims slot 5 cap=10"
   by comparing claim strengths.
3. **Counterfactual analysis** — to score "what would have happened
   if rule X hadn't fired?", the tracker replays Claims minus X
   through the Arbiter. Direct PlannedActions can't be replayed
   meaningfully.

## 5. Arbiter

Deterministic. Three-pass:

**Pass 1: tier-1 (safety) MUSTs.** EPSLockdownRule, MinSOCFloorRule.
Their claims are absolute and the Arbiter records what they enforced.
If a tier-1 MUST conflicts with itself (shouldn't happen), it's a bug
that crashes the planner with a clear error.

**Pass 2: tier-2 (mode) claims.** SavingSession, IGODispatch, EPSMode,
EVDeferral. These respond to external events. Within tier-2:
- Time-windowed claims merge by union (saving session 17:00-18:00
  + IGO dispatch 02:00-03:00 don't conflict).
- Same-slot conflicts: PREFER beats OFFER. PREFER vs PREFER on the
  same slot logs a warning + uses claim_order (rule list position)
  as deterministic tie-break.

**Pass 3: tier-3 (optimisation) claims.** CheapCharge, EveningProtect,
PeakExportArbitrage, SolarSurplus. Same conflict resolution as tier-2.
But MAY NOT override anything tier-1 or tier-2 already set.

Output: a `Decision` containing:
- The chosen Claims per slot/global
- The PlannedAction built from them (via operator.build.merge of
  per-claim PlannedActions)
- The full audit: every Claim that was made (winning + losing) with
  its rule, rationale, and arbitration outcome

This audit is what the observability layer surfaces.

## 6. The starting rule set

Designed fresh, informed by HEO II's lessons. Tier ordering = arbitration
precedence (lower tier wins).

### Tier 1 — Safety (always evaluated, always MUST)

| Rule | Triggers when | Claims |
|---|---|---|
| **EPSLockdownRule** | `flags.eps_active` is True | LockdownIntent (slots cap=0%, gc=False, EV stop, appliances off). Single MUST that overrides everything. |
| **MinSOCFloorRule** | always | HoldIntent on the active slot with cap ≥ `config.min_soc`. Prevents any other rule from draining below floor (unless eps_active overrides). |

### Tier 2 — Modes (event-driven)

| Rule | Triggers when | Claims |
|---|---|---|
| **SavingSessionRule** | `flags.saving_session_active` is True | SellIntent over the session window: drain to min_soc + buffer. PREFER strength because the £/kWh signal (~£3) dominates regular peak rates. |
| **IGODispatchRule** | `flags.igo_dispatching` OR scheduled `igo_planned[]` covers the active slot | ChargeIntent during the dispatch (gc=True, target=80% by dispatch end). PREFER strength. Avoids HEO II's F2 race by checking saving_session BEFORE claiming (no double-write). |
| **EVDeferralRule** | `defer_ev_eligible` AND `top_export_window` is now AND `ev_charging` | DeferEVIntent (zappi → Stopped). PREFER. Restore-on-window-end is a separate claim that fires when the export window passes. |

### Tier 3 — Optimisation (rate-driven)

| Rule | Triggers when | Claims |
|---|---|---|
| **CheapRateChargeRule** | active slot covers a `next_cheap_window` | ChargeIntent (gc=True) to a target SOC. Target = `bridge_kwh(until=next_pv_takeover)` rounded up. PREFER. **Asymmetry-aware**: target sized for worst-case (P90 load + P10 solar), not P50. |
| **PeakExportArbitrageRule** | active slot is a top-N export window AND `usable_kwh > 0` AND `expected_spread > threshold` | SellIntent for a fraction of usable_kwh. **Asymmetry-aware**: spread = export_rate - WORST_CASE_REPLACEMENT_RATE (next peak import rate, not next cheap). PREFER if spread > threshold; OFFER otherwise. |
| **SolarSurplusRule** | `solar_power > load_power + headroom` AND `headroom_kwh > 0` | HoldIntent allowing PV to charge battery. OFFER. |
| **EveningDrainRule** | active slot is in evening window (e.g. 19:00-23:30) | DrainIntent to `target_end_soc` (default 25%) by 23:30. OFFER — defaults beat doing nothing but yield to other rules. |

That's 9 rules total: tier-1 (2) + tier-2 (3) + tier-3 (4). Smaller than HEO II (which had ~13). Differences from HEO II:

- **No BaselineRule** — the operator's `build.baseline_static()` is the planner's *fallback when zero rules fire*, not a rule itself.
- **No SeparatePeakArbitrage / EveningProtect split** — replaced by asymmetry-aware PeakExportArbitrage that ALREADY considers worst-case replacement.
- **MinSOCFloorRule is its own thing** — was implicit in HEO II; explicit here so it's observable when blocking another rule.

## 7. Tunable parameters + bounds

Each rule has named parameters. Auto-tuning (per §8) can adjust them
within bounds. Bounds are HARD — the tuner cannot exceed them.

| Rule | Parameter | Default | Bounds | Tuning signal |
|---|---|---|---|---|
| MinSOCFloorRule | `floor_pct` | 10 | [5, 25] | manual (this is a safety knob) |
| SavingSessionRule | `drain_buffer_pct` | 5 | [0, 15] | actual delivered vs forecast over recent sessions |
| IGODispatchRule | `target_soc_pct` | 80 | [50, 100] | how much was delivered vs allocated |
| CheapRateChargeRule | `safety_margin_pct` | 10 | [0, 30] | actual vs forecast load shortfall over week |
| CheapRateChargeRule | `solar_p10_factor` | 1.0 | [0.7, 1.0] | actual solar vs P10 forecast (under-shoot ratio) |
| PeakExportArbitrageRule | `spread_threshold_pence` | 8.0 | [3.0, 30.0] | actual P&L of past arbitrage decisions |
| PeakExportArbitrageRule | `worst_case_replacement_quantile` | 0.9 | [0.5, 1.0] | "did we end up paying more than we sold for" rate |
| EveningDrainRule | `target_end_soc` | 25 | [10, 50] | overnight surplus vs cheap-charge target |

Forecast biases (separate from rules — applied at Compute layer):
- `load_forecast_bias_pct` — added to load forecast. Default 0, bounds [-20, +30]. Tuned from rolling 7-day actual vs forecast.
- `solar_forecast_bias_pct` — applied to solar P50. Default 0, bounds [-30, +10] (asymmetric: more willing to assume less solar than more).

## 8. Learning loop

A `Tuner` runs daily at 03:00 local (low-activity window, after
overnight charge concluded). For each tunable:

1. Query the PerformanceTracker for the relevant signal over the
   past 7 days.
2. Compute a proposed adjustment (small step, hardcoded max delta).
3. Clamp to bounds.
4. If the adjustment crosses a configured "significant change"
   threshold, write to a "pending" channel (logged + sensor
   attribute) for paddy to review, but DO apply it.
5. Audit log every change with: timestamp, parameter, old value,
   new value, signal that drove the change.

Adjustment rules (deliberately simple — no ML):

```
load_forecast_bias_pct:
  signal = mean_pct_error(actual_load, forecast_load) over 7 days
  proposed = current_bias + 0.3 * (signal - current_bias)
  clamped to [-20, +30]

PeakExportArbitrage.spread_threshold_pence:
  signal_a = avg_spread_of_arbitrage_decisions_that_lost_money
  signal_b = avg_spread_of_arbitrage_decisions_that_made_money
  if signal_a is high (lots of losses): nudge threshold up by 0.5p
  if signal_b is far above current threshold: nudge threshold down by 0.3p
  clamped to [3.0, 30.0]
```

NOT in scope (per Paddy's clarification): the tuner does NOT enable
or disable rules. Rule presence is a PR / code change.

## 9. Coordinator + scheduling

```python
class Coordinator:
    """Owns the tick loop. Sits above the rule engine + operator."""

    async def tick(self, *, reason: str = "cron") -> Decision:
        snap = await self._operator.snapshot()
        decision = self._engine.run(snap)
        result = await self._operator.apply(decision.action, snapshot=snap)
        self._tracker.record(snap, decision, result, reason)
        return decision
```

**Cadence:**
- **15-min cron tick** — primary. Aligns with HEO II's pattern; matches the
  granularity of rate slots.
- **Event-driven ticks** (immediate, debounced 5s):
  - `flags.eps_active` transitions to True → tick (lockdown)
  - `flags.eps_active` transitions to False → tick (restore)
  - `flags.saving_session_active` transitions to True → tick
  - `flags.igo_dispatching` transitions to True → tick

The HA integration's `async_setup_entry` registers a state-change
listener on the gating entities + a cron callback via
`async_track_time_interval`. Coordinator dedupes near-simultaneous
triggers (5s debounce window) so an event during a cron tick
doesn't double-fire.

**Pre-flight gates** (already implemented in operator §16, just
trigger them):
- `transport.is_connected` — coordinator skips tick + warns if no.
- SPEC H4 (rates fresh) — operator's apply() handles this.
- SPEC H3 (eps_active) — coordinator routes EPS to lockdown action,
  bypassing the regular rule engine.

## 10. Observability surface

Every tick produces an audit trail. Surfaced via three sensors:

### `sensor.heo3_active_rules`
- **state**: comma-joined list of rules that won arbitration
- **attributes**:
  - `last_tick_at`, `tick_reason` (cron / eps / saving / igo)
  - `claims_made`: list of (rule_name, intent_summary, strength,
    arbitration_outcome) — winning AND losing
  - `decision_rationale`: human-readable summary built from winning
    claims' rationale strings

### `sensor.heo3_last_decision`
- **state**: short summary ("charge to 80% by 05:30 (CheapRateCharge)")
- **attributes**: full Decision JSON

### `sensor.heo3_weekly_digest`
- Updated every Sunday at 23:55 local (post evening drain).
- **state**: ok / partial / error (any tracker errors that week)
- **attributes**:
  - `period_start`, `period_end`
  - `total_pence_saved` (vs do-nothing baseline)
  - `total_pence_baseline` (what static baseline would have cost)
  - `rule_activations`: `{rule_name: count}`
  - `rule_attribution_pence`: `{rule_name: estimated_£_impact}`
  - `forecast_errors`:
    - `load_mean_pct_error`, `load_rms_pct_error`
    - `solar_mean_pct_error`, `solar_rms_pct_error`
  - `tuning_actions`: list of (parameter, old, new, reason) over the week
  - `notable_events`: EPS triggers, saving sessions, IGO dispatches
  - `recommendations`: list of "consider..." strings (e.g. "rule X fired
    only 3 times this week — confirm its conditions still match reality")

Per-tick logbook entry under domain `heo3` so it shows up in the
HA logbook view.

## 11. PerformanceTracker

Sits between the Coordinator and the digest. Three jobs:

1. **Record per-tick state**: snapshot summary, decision, apply
   result, actual outcome (battery SOC change, grid energy ±,
   £ in/out at then-current rates).
2. **Attribute outcomes to rules**: for each tick, compute the
   counterfactual ("what would the plan have been without rule X?")
   by replaying claims through the arbiter minus X. Difference
   in apply()'s expected impact = X's attribution.
3. **Track forecast errors**: actual_load vs forecast_load and
   actual_solar vs forecast_solar over rolling windows. Used by
   the Tuner.

Storage: HA `Store` (JSON file) keyed by entry_id. Rolling 30 days
retained. Older summaries pruned.

## 12. Rule context

Rules need helpers without knowing about adapters / the tuner /
each other:

```python
@dataclass(frozen=True)
class RuleContext:
    compute: Compute              # operator's Compute instance
    parameters: dict[str, float]  # this rule's CURRENT (tuned) params
    historical: HistoricalView    # last-7-days summary stats
    rate_window_helpers: ...      # convenience: top_export_windows, etc.
```

Rules MUST:
- Be deterministic given (Snapshot, parameters).
- Not call I/O. Side effects come from the operator's apply().
- Not mutate any input.

Rules MAY:
- Use `historical` to inform decisions (e.g. "if I fired 5 times
  yesterday and it always lost money, decline").

Rule activations are inherently observable because they return Claims.
The `rationale` field is mandatory and surfaces to the digest.

## 13. Pre-planner work needed

These ship before the planner can run:

1. **HA coordinator infrastructure** — `async_track_time_interval`
   for the 15-min tick + `async_track_state_change_event` for the
   event-driven triggers. Estimated 0.5 day.
2. **PerformanceTracker storage** — HA `Store` for rolling 30-day
   summaries. Estimated 1 day (incl tests).
3. **Profile + reduce SA write latency** — current ~6s/write means
   tick takes 60s+. Investigate whether we can publish writes in
   parallel + correlate responses by FIFO (HEO II's pattern). If
   we can get to ~2s/write, full re-apply is ~20s. ~1-2 days.
4. **Read-back via Deye-Sunsynk integration** — replace the SA
   mirror entities (which we proved are stale-prone) with the
   `number.deye_sunsynk_sol_ark_*` entities for verification.
   ~0.5 day.

These come BEFORE rule implementation. Rules need a working tick
loop + reliable telemetry to stand on.

## 14. Build phases (planner)

* **P2.0 — Coordinator + tick loop.** 15-min cron + event listeners
  + dedupe. Empty rule engine returns baseline_static. ~1 day.
* **P2.1 — Performance tracker.** Per-tick storage, 30-day rolling.
  Forecast error tracking. ~1.5 days.
* **P2.2 — Rule + Arbiter framework.** Protocol, Claim type,
  arbiter logic, RuleContext. No actual rules yet — just the
  scaffolding + observability sensors. ~1.5 days.
* **P2.3 — Tier 1 rules.** EPSLockdownRule, MinSOCFloorRule.
  Both have hard tests because they can't be wrong. ~1 day.
* **P2.4 — Tier 2 rules.** SavingSession, IGODispatch, EPSReady,
  EVDeferral. Each rule + its tests. ~3 days.
* **P2.5 — Tier 3 rules.** CheapRateCharge, PeakExportArbitrage,
  SolarSurplus, EveningDrain. The asymmetry-aware ones get extra
  test coverage. ~3 days.
* **P2.6 — Tuner.** Daily 03:00 cron. Threshold + bias adjustments
  with bounds + audit log. ~1.5 days.
* **P2.7 — Weekly digest builder.** Sunday 23:55 cron. Sensor
  publication. Recommendation strings. ~1 day.
* **P2.8 — Live cutover.** Switch from baseline_static to the
  rule-engine-driven coordinator. Watch first week closely.
  ~0.5 day deploy + monitoring.

**Total: ~13-15 working days, ~3 weeks.** Plus ~3-4 days of
pre-planner work in §13 = ~4 weeks elapsed.

## 15. Open questions

* **Counterfactual attribution accuracy.** "What would have happened
  without rule X" is approximate — the arbiter without X might
  produce a different Claim from another rule. Worth a test that
  validates attribution sums approximately match "total saved
  vs baseline".
* **Tuner step sizes.** The 0.3 step factor in §8 is a guess.
  Worth A/B testing with synthetic histories before letting it
  loose on real data. Could start with manual nudges + log only,
  enable auto-apply after watching for a month.
* **Event debounce window.** 5s might be too aggressive if HA's
  event bus is congested. Watch in production.
* **Recommendation string generation.** The digest's "consider..."
  recommendations are heuristic. The set should grow organically
  as paddy reads digests and identifies patterns worth flagging.

## 16. Sign-off checklist

Signed off 2026-05-11.

- [x] Why-this-doc + scope (§1, §2)
- [x] Architectural shape (§3)
- [x] Rule shape: Claim + Arbiter contracts (§4, §5)
- [x] Starting rule set (§6) — 9 rules, all named, all with one-line description
- [x] Tunable parameters + bounds (§7) — every rule documented
- [x] Learning loop (§8) — what tunes, how, where audit logs land
- [x] Coordinator + scheduling (§9) — cron + event triggers
- [x] Observability sensors (§10) — active_rules / last_decision / weekly_digest
- [x] PerformanceTracker (§11) — what's stored, for how long
- [x] Rule context (§12) — what helpers rules can call
- [x] Pre-planner work (§13) — tick infrastructure, latency profiling, read-back
- [x] Build phases (§14) — P2.0 through P2.8, ~3 weeks
- [x] Open questions (§15) — none blocking; will iterate during build
