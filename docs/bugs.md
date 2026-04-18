# HEO II Bug Register

Diagnosed against running code at `a1acrity/heo2@50d3473` (byte-identical to production).
Tests run on Python 3.13.13. Baseline before any fix: 149 pass, 0 fail, 71% coverage.

Each entry includes a stable ID so commits and tests can reference it.

## Priority A: blockers before `dry_run=false`

### HEO-1 Timezone bug in `_build_import_rates`

**File**: `custom_components/heo2/coordinator.py:201-216`

**Problem**: `today = now.replace(hour=0, minute=0, ...)` operates on a UTC datetime,
producing UTC midnight. The night-rate window `23:30-05:30` is then built in UTC.
During BST the window is shifted by one hour: in the code it runs 23:30-05:30 UTC
which is 00:30-06:30 BST, not the true IGO window of 23:30-05:30 BST.

**Effect**: Every rule calling `rate.rate_at_time(now)` between 23:30 and 00:30 BST
gets day-rate 27.88 p when the true rate is night-rate 7 p. Also wrong at DST
transitions. Invisible to the current test suite because every test fixture uses
UTC and so is internally consistent.

**Fix**: Use `homeassistant.util.dt.now()` and build rate windows in local time,
converting to UTC only at RateSlot construction.

**Test gap**: No test asserts behaviour under a non-UTC timezone. Add a test that
freezes time at 23:45 BST, builds rates, and asserts `rate_at_time(now).rate_pence == 7.0`.

---

### HEO-2 MqttWriter readback raises NotImplementedError

**File**: `custom_components/heo2/mqtt_writer.py` (per original review — to verify in place)

**Problem**: `_wait_for_readback` raises NotImplementedError. Masked in `dry_run=true`.
First live write will throw.

**Test status**: `test_mqtt_writer.py::test_retries_on_readback_failure` and
`test_aborts_on_persistent_failure` both pass, which means they are mocking around
the NotImplementedError rather than exercising the real code path.

**Fix**: Implement MQTT subscribe-and-wait against Solar Assistant's readback topic.

---

## Priority B: Active degradation right now (even in dry_run)

### HEO-3 AgilePredict client schema wrong

**Files**:
- `custom_components/heo2/agilepredict_client.py:39,62-74`
- `tests/test_agilepredict_client.py:11-28`

**Problem**: Client calls `GET /api/rates/export` which returns HTTP 404 on Janeway's
AgilePredict instance (verified). The real endpoint is `GET /api/` returning a
different schema:

```json
[{
  "name": "2026-04-18 16:17",
  "created_at": "...",
  "prices": [
    {"date_time": "...", "agile_pred": 30.78, "agile_low": ..., "agile_high": ..., "region": "X"}
  ]
}]
```

15 regions per response (A-N, P, X). Hull's DNO region is M (NGED Yorkshire).

Tests pass because they mock a fabricated `{valid_from, valid_to, value_inc_vat}`
schema that doesn't match production.

**Effect observed**: Every 15-minute tick logs `AgilePredict fetch failed: 404`.
`sensor.heo_ii_current_export_rate = unknown`. ExportWindowRule runs with no
export rate data.

**Fix**: Rewrite `fetch_export_rates` to hit `/api/`, parse the nested schema,
filter by region (configurable, default X for unknown), convert `date_time` to
end-exclusive 30-min RateSlot objects using `agile_pred` as the pence value.

**Test gap**: Rewrite tests using the real schema. Add integration test marker
that hits `http://192.168.4.84:8001/api/` when a flag is set (skipped in CI).

---

### HEO-4 Solar forecast arithmetic error — double or quadruple counted

**File**: `custom_components/heo2/solcast_client.py:74-93`

**Problem**: Solcast returns `pv_estimate` as **average kW** over each 30-min period.
Converting to kWh requires multiplying by 0.5h. Current code does
`hourly[hour] += pv_kw` with no `* 0.5`, then sums every period received.

Additionally, the hour bucket assignment `hourly[hour] += pv_kw` ignores the
**date** of the period. Solcast returns multi-day forecasts (48+ hours on free
tier). All days are collapsed into the same 24 hourly buckets, multiplying the
reported total by N days.

**Effect observed**: `sensor.heo_ii_solar_forecast_today = 147.18` today
(18 April, cloudy). A realistic upper bound for any UK domestic PV array in
April would be around 40 kWh on a perfect day. Actual: 147 kWh, ~4x too high.
This corrupts every rule that compares demand vs generation.

Also produces the nonsense `SolarSurplus: +1234% SOC` in programme_reason.

**Fix**: Filter forecast entries to "today only" (period start >= local midnight,
< local midnight + 24h). Multiply each pv_kw by 0.5 when adding to the bucket.

**Test gap**: No test feeds multi-day data, so the double-count is invisible.
Existing tests feed hand-crafted single-day fixtures. Add a test that feeds a
48-hour Solcast response and asserts the 24 hourly buckets cover only "today".

---

### HEO-5 LoadProfileBuilder never learns

**Files**:
- `custom_components/heo2/load_profile.py` (correct in isolation)
- `custom_components/heo2/coordinator.py:45-47,144-146` (never calls add_day)

**Problem**: Coordinator instantiates `LoadProfileBuilder(baseline_w=1900.0)` on
startup, calls `build()` each tick, but never calls `add_day()`. So `weekday` and
`weekend` are always `[1.9] * 24`, and `sensor.heo_ii_load_profile.state == "weekend"`
today because `for_datetime(now)` returns the baseline flat profile either way.

**Effect observed**: daily demand reported as 45.6 kWh (exactly 1.9 × 24) across
every rule. CheapRateChargeRule concludes "worth charging -101.6 kWh" and sets
target 20% instead of charging fully.

**Fix**: Two parts.
1. On startup, query HA recorder (`history.state_changes_during_period`) for the
   last 7-14 days of `load_power_entity`, aggregate by hour, call `add_day()`.
2. Persist the builder via HA's Store helper so restart keeps learnings.

**Test gap**: Tests cover add_day() and build() in isolation but never assert
that the coordinator calls add_day. Add a test that instantiates the coordinator
with a mock hass providing history data and asserts add_day was called.

---

## Priority C: Still active, lower impact

### HEO-6 EveningProtectRule hour-boundary off-by-one

**File**: `custom_components/heo2/rules/evening_protect.py`

**Status**: Original review flagged `slot_end_mins=1110 vs evening_mins=1080`
(18:30 vs 18:00). Need to re-read code to confirm and reproduce with a failing
test. Adding to fix queue but not yet re-verified in this audit.

---

### HEO-7 ExportWindowRule hour arithmetic lossy

**File**: `custom_components/heo2/rules/export_window.py`

**Status**: Original review flagged exclusive-end-hour and ignored sub-hour
minute boundaries. Verify with targeted test.

---

### HEO-8 IGO dispatches only reacted to, not planned ahead

**File**: `custom_components/heo2/rules/igo_dispatch.py`

**Problem**: Rule only modifies the current slot when `igo_dispatching=True`.
BottlecapDave's integration exposes planned dispatches at
`binary_sensor.octopus_energy_..._intelligent_dispatching.attributes.planned_dispatches`
as a list of upcoming windows. HEO II could plan future slots to maximise SOC
before each dispatch.

**Fix**: Extend rule input to accept list of planned dispatches; adjust future
slots' SOC targets to account for them.

---

## Priority D: Configuration drift (not code bugs)

### HEO-9 Three entity IDs misconfigured in config entry

**Location**: `.storage/core.config_entries` entry for HEO II

**Problem**: Three fields point at the IGO dispatching binary sensor instead of
their correct entities:

- `saving_session_entity` = should be `binary_sensor.octopus_energy_a_8e04cfcf_octoplus_saving_sessions`
- `tapo_dryer_entity` = should be a smart-plug switch, or empty
- `tapo_wash_entity` = should be a smart-plug switch, or empty

**Fix**: Correct via HA UI (HEO II integration → Configure), or patch storage
directly. UI route is safer.

---

### HEO-10 Saving Sessions never triggers a rule

**Problem**: `saving_session_entity` is wired (even if wrongly) but no rule in
`rules/` reads `inputs.saving_session` except `ev_charging.py` which uses it as
a flag not a price signal. Octoplus Saving Sessions pay £3+/kWh — missing these
is expensive.

**Fix**: Add a `SavingSessionRule` that, on saving session active, overrides the
current slot to drain to `min_soc` regardless of Agile price.

---

## Priority E: Observability & UX

### HEO-11 Rule parameters baked into constructors

Rule thresholds (evening_start_hour, off_peak times, max_target_soc,
degradation cost) are constructor args with no HA UI. Should be number/select
entities.

### HEO-12 `reason_log` not persisted

Lives in memory per coordinator tick. Can't review history of why a slot was set.
Should write to HA Store or a dedicated log sensor with trimmed ring buffer.

### HEO-13 MQTT writer hardcoded to inverter_1

Slave inverter has no MQTT control path. Requires Solar Assistant's own topic
tree extended to inverter_2, plus a second physical RS485 link (spare USB adapter
available).

---

## Resolved or obsolete

- ~~Battery capacity default 20 kWh~~: already changed to 10.0 in config entry (close enough to 10.24 actual).
- ~~AgilePredict service down~~: it's up; bug is in the client code, not the service.
- ~~178 orphaned tmp files in .storage~~: all zero-byte, safe to delete with backup.
