# HEO II - System Specification

> Source of truth for what HEO II should do, what data it acts on, and what it
> writes. Update this document FIRST when changing intent. Code follows spec,
> not the other way round.
>
> Tracking issue: #31. Last updated: 2026-05-02 (HEO-31 PR2: planning lifecycle + safety).

## 1. Hardware and tariff context

| Item | Value |
|---|---|
| Inverters | 2x Sunsynk 5kW, master/slave linked over RS485 |
| Batteries | 4x Sunsynk BP51.2, total 20.48 kWh nominal |
| Solar | PV array feeding inverter MPPTs |
| EPS relay | Held off by mains; releases on grid loss to switch supply to inverter EPS output |
| Tariff (import) | Octopus Intelligent Go (IGO) |
| Tariff (export) | Octopus Agile Outgoing |
| IGO peak rate | 24.8423 p/kWh (fixed) |
| IGO off-peak rate | 4.9524 p/kWh (fixed; 23:30-05:30 plus dispatched windows) |
| Agile Outgoing | Variable, 30-min slots, refreshed daily after 16:00 |
| Battery min SOC | **10%** (was 20%; reduced after new battery install) |
| Cycle budget | Soft target 2 cycles/day; partial cycles low wear |

## 2. Outputs HEO II writes to the inverter

HEO II controls these settings via MQTT direct to Solar Assistant's broker.
Master/slave RS485 link mirrors changes to inverter 2.

### Per-slot (6 timed slots, programme rewritten as needed)
1. Slot N start time (`time_point_N`)
2. Slot N grid charge enable (`grid_charge_point_N`)
3. Slot N battery SOC target (`capacity_point_N`)

### Global (changed at slot transitions, not per-slot)
4. Work mode - Selling first / Load first / Battery first
5. Energy pattern - Load first / Battery first
6. Charge rate (max watts pulled from grid)
7. Discharge rate (max watts the battery can output)
8. Zero export to CT (force zero export when set)

NOTE: items 4-8 are NOT yet implemented in MqttWriter. They are part of the
Sunsynk MQTT surface that SA exposes; we will wire them as the rules engine
needs them.

## 3. Inputs HEO II reads

### Time and battery
- `now` (timezone-aware datetime)
- `current_soc` (live battery SOC, 0-100%)
- `battery_capacity_kwh` (config; 20.48)
- `min_soc` (config; **10**)

### Rates (live, BottlecapDave Octopus integration as primary)
- `import_rate_now` - current import p/kWh
- `export_rate_now` - current export p/kWh
- `import_rates_today[]`, `export_rates_today[]` - 30-min slots
- `import_rates_tomorrow[]` - IGO is fixed
- `export_rates_tomorrow[]` - published by Octopus after 16:00. **Daily plan runs at 18:00** for 2-hour safety margin.

### Forecasts
- `solar_forecast_kwh[24]` - hourly PV today (Solcast)
- `solar_forecast_kwh_tomorrow[24]`
- `load_forecast_kwh[24]` - hourly load from HEO-5 14-day learning

### State flags / sensors
- `igo_dispatching` - in a free Octopus IGO dispatch right now
- `igo_dispatches_planned[]` - planned dispatches in next 24h (HEO-8)
- `saving_session_active`, `saving_session_window`
- `ev_charging` - Tesla currently drawing
- `ev_deferred_until` - reserved for future use (see Future Work)
- `grid_voltage` - V; if 0 grid is down
- `eps_active` - true while inverter supplies via EPS

### Active appliances
- Read flags: `ev_charging_now`, `washer_running`, `dryer_running`, `dishwasher_running`
- Write entities: HA switch IDs for each (used in EPS / power-failure mode)

## 4. Hard rules (must / must-not)

| # | Rule | Definition |
|---|------|------------|
| H1 | No peak-rate import in PLAN | The PROGRAMME never schedules a `grid_charge=true` slot covering peak hours. If forced grid import happens at peak (battery at min_soc, evening shortage), it's flagged as a planning failure but allowed - reality wins. |
| H2 | No house-battery -> EV | EV charging: battery cannot discharge to feed car. Lock SOC at current level for the duration of the charge across all slots it spans. |
| H3 | Power failure (EPS) | `grid_voltage == 0` for >5s: allow battery to drain to 0% (override min_soc 10%). Turn off EV / washer / dryer / dishwasher via HA switches. No MQTT writes (inverter is busy). |
| H4 | Live-prices-only writes | Inverter is only ever programmed using prices Octopus has actually published. Predictions (AgilePredict) for INTERNAL planning ONLY, never for what gets written. |
| H5 | Pre-write validation | Plan validated before being written (see section 6). |
| H6 | Post-write verification | Programme read back from inverter after write; mismatch flagged. |
| H7 | Cycle budget | Soft target <=2 cycles/day. Logged, alert if exceeded for 3 days running. |

## 5. Soft objectives (priority order)

When trade-offs arise, decisions follow this order:

1. **Avoid peak-rate import** (H1, repeated as priority)
2. **Avoid grid use generally** - cover load from battery+solar where economical
3. **Sell during the top-ranked windows of today** - see section 5a
4. **Recover PV investment as fast as possible** - within 1-3 above
5. **Saving Sessions** - drain to min_soc as quickly as inverter allows. £3+/kWh windows.

## 5a. Rank-based pricing (replaces fixed thresholds)

Both selling and cheap-rate charging use **rank within today's published rates** rather than fixed pence thresholds. This adapts automatically to:
- Day-to-day variation in Agile prices
- Seasonal differences (winter vs summer rate distributions)
- Tariff changes without code edits

### Selling (export decision)

```
top_export_windows = top N% of export_rates_today by p/kWh
where N is calibrated by available battery surplus:
  - High SOC + high tomorrow forecast: N = 50% (sell aggressively)
  - Medium SOC: N = 30%
  - Low SOC OR low tomorrow forecast: N = 15% (only the very best)
```

A window is "worth selling in" if it's in `top_export_windows` AND
`export_rate * round_trip_efficiency > replacement_cost` where
replacement_cost = next available IGO off-peak rate (typically 4.95p).

### Charging from grid (cheap-rate decision)

```
bottom_import_windows = bottom 25% of import_rates_today (or all IGO off-peak)
charge target = enough to cover (load - solar) until next bottom window
```

For IGO this is straightforward (off-peak windows are explicitly cheap).
For variable tariffs the same logic generalises.

### Why rank not absolute

A fixed 6p threshold is meaningless when winter Agile prices range 0.03p-17p
(median ~5p, so 6p triggers half the time and you'd trade away your reserve)
versus summer when 6p is the bottom of the distribution. Rank adapts
without code changes.

## 6. Pre-write validation (H5 detail)

Implemented in `custom_components/heo2/plan_validator.py`. Runs after
the rule engine has produced a programme and BEFORE the coordinator
hands it to the MQTT writer.

### Static checks (re-asserted; SafetyRule fixes them in-place)
- Exactly 6 slots
- Slot 1 starts 00:00, slot 6 ends 00:00
- Contiguous time boundaries
- All SOCs in [min_soc, 100]

### Sanity checks (hard - reject the plan)
- **H1**: No `grid_charge=true` slot's time window may overlap any
  import rate slot at >= `peak_threshold_p` (24.0p default).

### Sanity checks (soft - log warning, allow write)
- No `grid_charge=true` slot covers the bottom-25% cheap window
  (the rule engine's CheapRateCharge rule is normally responsible
  for this; missing coverage is sometimes legitimate in summer).
- Projection forecasts > 0 kWh peak-rate import (battery hits floor
  during peak hours). H1 says reality wins for forced peak imports;
  the warning is informational so the user can investigate.

### Projection report
A 1-line summary written to `sensor.heo_ii_projection_today` every
tick (whether the plan was accepted or rejected) so the projected
outcome is always visible:

> Expected return today: +£X.YZ - sells N kWh @ avg Mp, grid imports P kWh @ avg Qp, ZERO peak-rate import

When validation fails, the plan is rejected and the previous baseline
stays on the inverter. Logged at WARNING. Sensor
`binary_sensor.heo_ii_writes_blocked` goes ON with reason `H5: <error>`.

## 7. Post-write verification (H6 detail)

Implemented in the coordinator. The check is **deferred to the next
tick** rather than running inline at the end of the writing tick:

1. After a successful per-slot write batch, the new programme is
   stored in `_pending_verification`.
2. The next 15-min tick begins by reading back the 6-slot programme
   from `sensor.sa_inverter_1_*` entities and diffing each field
   against `_pending_verification`.
3. If any field mismatches: log ERROR, set
   `binary_sensor.heo_ii_writes_blocked` ON with reason
   `H6: slot N <field> sent=A got=B`, and reset
   `_last_known_programme` to the observed state so the next diff
   targets the correct delta.
4. If all match: clear the pending state silently.

Why next-tick rather than inline? SA's MQTT polling cadence updates
the discovered HA entities every few seconds. An inline read would
race with that cadence; the 15-min tick spacing gives plenty of
margin without padding the current tick with an artificial sleep.

This is on top of the per-write "Saved" readback that MqttWriter
already does (HEO-2). Per-write confirms SA accepted the publish;
post-write programme verify confirms the inverter's actual state
matches what we sent.

## 8. Planning cadence

### Daily plan (18:00 local)
- Full re-evaluation
- Uses tomorrow's Octopus rates (available since 16:00, 2-hour margin)
- Sets the next 24h programme
- Generates projection report (section 6)
- Writes inverter

### Tick refinement (every 15 min between 18:00 plans)
Same rules run, but only ALLOWED to override the 18:00 plan if a "trigger
condition" has fired:

- Solar deviation > X% from forecast for rest-of-day (X TBD)
- Load forecast deviation > X% (running ahead/behind)
- SOC deviation > 10% from projected SOC trajectory
- New IGO dispatch announced
- Saving Session announced
- Grid loss / restore

Otherwise, the 15-min tick reads state and updates dashboard sensors but
does NOT rewrite the inverter. This addresses the "constant rewrite"
problem.

## 9. Operating modes

| Mode | Trigger | Behaviour |
|---|---|---|
| Normal | default | Rules per section 5; plan at 18:00; refine on triggers |
| EPS / power failure | `eps_active` | min_soc -> 0%; turn off EV/washer/dryer/dishwasher; no MQTT writes; dashboard banner |
| Saving Session | `saving_session_active` | Drain to min_soc as fast as inverter allows (Selling first + max discharge); resume normal at session end |
| EV charging | `ev_charging` | Apply H2 (no battery -> car); affected slots SOC locked at present level |
| Winter low-PV | implicit (daily PV forecast < daily load) | Charge target HIGHER (covers more of evening); EveningProtect floor higher; sell ONLY in top 15% of export windows (preserve cycles for own use) |

## 10. Configuration knobs (HA UI tunable - HEO-11)

| Knob | Default | Purpose |
|---|---|---|
| `min_soc` | 10 | Battery floor |
| `max_target_soc` | 100 | Battery ceiling |
| `peak_threshold_p` | 24.0 | Peak-rate detection (covers IGO peak with margin) |
| `igo_off_peak_p` | 4.95 | IGO cheap rate |
| `sell_top_pct_default` | 30 | Default rank for "worth selling" |
| `sell_top_pct_low_soc` | 15 | Rank when SOC low or forecast poor |
| `sell_top_pct_high_soc` | 50 | Rank when SOC high and tomorrow good |
| `cycle_budget` | 2.0 | Cycles/day soft target |
| `daily_plan_time` | 18:00 | When the once-a-day full plan runs |
| `replan_solar_pct` | 25 | Solar deviation triggering replan |
| `replan_load_pct` | 25 | Load deviation triggering replan |
| `replan_soc_pct` | 10 | SOC trajectory deviation triggering replan |

## 11. Out of scope (for now)

- Heating control (separate skill exists)
- Vehicle-to-Grid (V2G) - Tesla doesn't expose
- Battery health beyond cycle counting
- Grid frequency response markets

## 12. Future work (deferred)

### EV charge deferral
Mechanism for the user to defer the car charge so surplus PV is sold rather
than sent to the car. Triggered when:
- Battery is full
- Export prices are at the top of today's range
- User has flagged "car not needed tomorrow"

Requires:
- Tesla integration suppression mechanism (TBD)
- Dashboard prompt UX
- Logic for "ridiculous low export" auto-charge fallback

Tracked separately from main rules redesign. Will be added once core spec
implementation is solid.

## 13. Related issues

- #16 HEO-14: BottlecapDave primary for live rates (foundation for H4)
- #11 HEO-11: Rule parameters as HA UI entities (covers section 10)
- #10 HEO-10: Saving Sessions trigger (covers mode in section 9)
- #8 HEO-8: Planned IGO dispatches (covers section 3 input)
- #7 HEO-7: ExportWindowRule hour arithmetic (covers section 5a granularity)
- #6 HEO-6: EveningProtect off-by-one
