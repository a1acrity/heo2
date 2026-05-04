# Rule field-write overlap matrix

Snapshot taken 2026-05-04 against the rule order returned by
`rules.default_rules()`. Phase 2 deliverable; pure analysis - no code
changes. Used to inform whether the strategic rule-engine rebuild is
warranted (Phase 3) and where the existing rules step on each other.

## Method

For each rule, every field it writes is recorded with the trigger
condition. Fields tracked:

- `state.work_mode` (global)
- `state.energy_pattern` (global)
- `state.max_charge_a` (global)
- `state.max_discharge_a` (global)
- `state.ev_deferral_active` (global signal flag)
- `slot.capacity_soc` (per-slot)
- `slot.grid_charge` (per-slot)
- `slot.start_time` / `slot.end_time` (per-slot, structural)

Rules listed in execution order. Later rules override earlier ones for
the same field.

## Matrix

| # | Rule | work_mode | energy_pattern | max_charge_a | max_discharge_a | ev_deferral | slot.cap_soc | slot.grid_charge | slot times |
|---|---|---|---|---|---|---|---|---|---|
| 1 | BaselineRule | "Zero export to CT" (always) | "Load first" (always) | 100.0 (always) | 100.0 (always) | - | sets all 6 slots (100/100/min/100/min/min) | sets all 6 slots (T/F/F/T/F/F) | rebuilds all 6 from off_peak_start/end + evening_start |
| 2 | CheapRateChargeRule | - | - | - | - | - | overrides every `gc=True` slot to `target_soc` (bridge calc) | - | - |
| 3 | SolarSurplusRule | - | - | - | - | - | overrides every non-GC slot overlapping 06-18 to `current_soc + surplus_soc` | - | - |
| 4 | ExportWindowRule | - (deliberately, see code comment lines 165-172) | - | - | - | - | drops every non-GC slot covering a worth-selling rate to `evening_floor_soc` | - | - |
| 5 | EveningProtectRule | - | - | - | - | - | raises any non-GC slot covering `evening_start_hour` to `min_soc + evening_demand%` | - | - |
| 6 | PeakExportArbitrageRule | "Selling first" (when inside an allocated top-priced slot AND have spare) | - | - | throttled per-slot value (1A..max_discharge_a_default) | - | - | - | - |
| 7 | SavingSessionRule | "Selling first" (when `saving_session=True`) | - | - | - | - | sets the slot containing now to `min_soc` | sets the slot containing now to `False` | - |
| 8 | IGODispatchRule | - | - | - | - | - | sets covering slot(s) to `100` (planned + active dispatch) | sets covering slot(s) to `True` | - |
| 9 | EVDeferralRule | "Selling first" (when eligible + SOC + export-rate triggers met) | - | - | - | sets `True` (same trigger) | - | - | - |
| 10 | EVChargingRule | - | - | - | - | - | raises slot containing now to `max(current_soc, min_soc)` (when ev_charging AND not igo_dispatching) | - | - |
| 11 | EPSModeRule | - | - | - | - | - | overrides EVERY slot to `0` (when eps_active) | overrides EVERY slot to `False` | - |
| 12 | SafetyRule | - | - | - | - | - | clamps every slot to `[min_soc, 100]` (or `[0, 100]` under EPS) | - | snaps every boundary to 5-min granularity; reset to default if slot count != 6 |

## Field-by-field overlap analysis

### work_mode

Writes from 4 rules: Baseline (default), PeakExportArbitrage,
SavingSession, EVDeferral. By execution order, the last one to fire
wins. The conflict matrix:

- **Baseline always sets `Zero export to CT`** as the safe default.
- **PeakExportArbitrage** flips to `Selling first` only when actively
  inside a top-priced slot and has spare. Else it leaves whatever the
  earlier rule produced (no override). Compatible with Baseline.
- **SavingSession** flips to `Selling first` when `saving_session=True`.
  Always wins over Baseline+PeakExport because it runs after both.
  PeakExport's "Selling first" is the SAME value, so the overlap is
  benign (idempotent write).
- **EVDeferral** also flips to `Selling first` when eligible. Runs
  after SavingSession in the execution order, so on a day where both
  conditions are met (saving session + eligible EV deferral) the
  EVDeferral path "wins" but the resulting work_mode is identical.

**Real conflict: none.** All non-Baseline writes converge on `Selling
first`. The cleanest possible refactor would merge the three into a
`SellingFirstRule` with an explicit reason chain.

### energy_pattern

Only Baseline writes (`"Load first"`). Zero overlap.

### max_charge_a / max_discharge_a

- Baseline sets both to `100.0` as defaults.
- PeakExportArbitrage overrides `max_discharge_a` with a throttled
  value (1A..100A) when actively selling.

The `max_discharge_a` overlap is intentional: PeakExportArbitrage's
throttle replaces Baseline's full-rate default to match the spare
amount. Compatible.

`max_charge_a` is written only by Baseline. Safe.

### ev_deferral_active

Only EVDeferral writes. Zero overlap.

### slot.capacity_soc - HEAVY OVERLAP

Six rules write per-slot SOC. The cascade in execution order:

1. **Baseline** lays the scaffold: 100/100/min_soc/100/min_soc/min_soc.
2. **CheapRateCharge** lowers slots 1+4 (the `gc=True` slots) from 100
   to a calculated `target_soc` based on the morning-bridge model.
3. **SolarSurplus** raises non-GC day slots (overlap 06-18) using a
   surplus calculation. CheapRateCharge already left slots 1+4 alone
   (since they're GC); SolarSurplus targets 2+3 specifically. **Tension:**
   if slot 3 (evening drain) gets raised here, EveningProtect downstream
   may not need to fire. Working as intended but indirect.
4. **ExportWindow** lowers any non-GC slot covering a worth-selling
   export rate to `evening_floor_soc` (= `min_soc + evening_demand%`).
   **Direct conflict** with SolarSurplus on slot 2/3 if a worth-selling
   window overlaps the day. Last write wins and ExportWindow runs after
   SolarSurplus, so the export drain wins. This is correct in spirit
   (sell at high prices > hold for solar absorption), but the SOC
   targets do interact: if SolarSurplus chose a high target and
   ExportWindow lowered it, the slot's "intent" is now drain-not-hold.
5. **EveningProtect** raises any non-GC slot covering `evening_start_hour`
   to `min_soc + evening_demand%`. **Direct conflict** with both
   SolarSurplus and ExportWindow on slot 3. ExportWindow's
   `evening_floor_soc` is computed using the SAME formula
   (`min_soc + evening_demand%`), so the two are typically equal -
   benign overlap. But if ExportWindow's calculation differs (e.g.
   different load horizon), EveningProtect overrides. Order matters.
6. **SavingSession** clamps the slot containing now to `min_soc`.
   Wins over everything earlier on that one slot.
7. **IGODispatch** sets covering slot(s) to 100 (always raises).
   **Direct conflict** with SavingSession when both fire on the same
   slot - IGO runs after SavingSession so dispatch wins, but a saving
   session usually runs at peak export hours which don't overlap with
   IGO dispatches (off-peak). Edge case: planned dispatch overlapping
   a saving session - IGO would win and refill the battery during the
   session, undoing the SavingSession drain. This is a real bug-in-waiting.
8. **EVCharging** raises slot containing now to `max(current_soc, min_soc)`.
   Skipped if `igo_dispatching=True`. Compatible with everything else.
9. **EPSMode** overrides EVERY slot to 0. Wins over all earlier writes
   when `eps_active`. By design.
10. **Safety** clamps every slot to `[min_soc, 100]` (or `[0, 100]`
    under EPS). Final pass.

**Hot spots:**

- Slot 3 (evening drain): potentially written by Baseline, SolarSurplus,
  ExportWindow, EveningProtect, SavingSession, EPS, Safety. Six rules
  may touch it.
- Slot 1 (overnight charge): Baseline, CheapRateCharge, IGODispatch,
  EPS, Safety.
- The slot containing "now" at any given moment is the most contested:
  PeakArbitrage doesn't touch slot SOC (only globals) but SavingSession,
  IGODispatch, and EVCharging all want a piece.

### slot.grid_charge

Three rules write:

- **Baseline**: T/F/F/T/F/F scaffold (slots 1 and 4 only).
- **SavingSession**: forces the slot containing now to `False`.
- **IGODispatch**: forces covering slots to `True`.
- **EPSMode**: forces ALL to `False`.

**Direct conflict**: SavingSession (False) vs IGODispatch (True) on the
same slot. IGO runs after SavingSession, so IGO wins - but as noted
above, a planned IGO dispatch overlapping a saving session would
counter the session's intent. Real bug exposure.

### slot times (start_time / end_time)

- Baseline rebuilds all 6 boundaries from its constructor params.
- Safety snaps every boundary to 5-min granularity and fixes
  contiguity / 00:00 anchors.

These two are the only structural writers. No overlap.

## Critical findings

### F1. work_mode coalescence opportunity

`Selling first` is set by three rules with different triggers. Each
re-runs from scratch on every tick; the rule that fires last wins. A
single `SellingModeRule` with explicit reason ranking would make the
chain easier to reason about and remove the implicit
"order-defines-winner" semantics. **Low risk refactor.**

### F2. Saving Session vs IGO Dispatch slot conflict

Concrete failure mode: an Octoplus Saving Session runs e.g. 17:00-18:00.
An IGO planned dispatch arrives for 17:30 (rare but possible - IGO
sends bonus dispatches outside the off-peak window).

- SavingSessionRule sets the 17:00-18:00 slot to cap=min_soc, gc=False.
- IGODispatchRule (runs later) sees the dispatch and sets the same
  slot to cap=100, gc=True.

Result: the inverter charges from grid during the saving session
instead of exporting to grid at £3+/kWh. **Direct revenue loss.**

The fix is execution-order awareness: either SavingSession should run
AFTER IGODispatch (so saving session wins), or IGODispatch should
guard against `inputs.saving_session=True`. Either is a single-line fix.

### F3. SolarSurplus vs ExportWindow slot 3 tension

SolarSurplus raises slot 3 (evening) when there's day surplus,
indicating "let solar fill the battery, hold for evening". ExportWindow
then lowers it if any top-priced export window overlaps the slot. The
end result is the export-window drain target wins, which is the right
economic decision, but the reason log shows two entries that look
contradictory ("raise to X" then "drop to Y").

Not a bug - the cascade is intended - but it produces unclear plan
justifications. A future refactor could compute these decisions
together and emit one consolidated reason.

### F4. Slot 3 is overweight

Six rules potentially write slot 3 in a single tick. That's the most
contested slot in the programme. Any new rule that touches the
evening window adds one more cascade layer. The three rules
(SolarSurplus, ExportWindow, EveningProtect) that all use variations
of `min_soc + evening_demand%` are the obvious consolidation target -
they're computing closely-related things via different paths.

### F5. SOC writes leak across rules silently

A rule that doesn't intend to override another rule's SOC decision
still does so by default if its trigger fires. There's no "respect
upstream higher SOC" policy except in EVCharging (which uses
`if slot.capacity_soc < hold_soc`). Most rules just assign,
overwriting strictly higher targets. Working as intended for some
cases (ExportWindow drain) but easy to introduce regressions in
others.

## What this matrix doesn't tell you

- Reads. Many rules read each other's outputs implicitly via the
  shared state (e.g. CheapRateCharge reads `slot.grid_charge` set by
  Baseline). The implicit read graph is its own analysis.
- Reason-log writes. Every rule appends to `state.reason_log` -
  noted but not enumerated.
- Test coverage of the overlaps. No test currently asserts the
  saving-session-vs-IGO precedence (F2) - that's a gap.

## Suggested next steps if Phase 3 happens

1. Fix F2 first (single line, real bug).
2. Add tests for every rule pair where one rule's output is meant to
  override the other (matrix above identifies the pairs).
3. Consider splitting "decide" from "apply": rules return a list of
  proposed writes with priorities; an arbitration layer reconciles.
  Removes the "execution-order defines precedence" implicit semantics.
4. Slot 3 consolidation: SolarSurplus + ExportWindow + EveningProtect
  share a load-budget calc. Pull it into one rule that emits a single
  decision per slot.
