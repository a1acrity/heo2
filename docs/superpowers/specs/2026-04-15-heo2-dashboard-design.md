# HEO II Dashboard — Design Specification

**Date:** 2026-04-15
**Author:** Data (AI) with Paddy Sheridan
**Status:** Approved (brainstorming complete)

## 1. Architecture Overview

**Approach A: Rich Sensors + Community Cards**

HEO II exposes ~27 new Home Assistant entities that carry all the data a dashboard needs. The dashboard itself is delivered as example Lovelace YAML using community cards (ApexCharts, Mushroom, flex-table-card). Users copy the YAML into their HA dashboard config.

### Three Data Layers

| Layer | Update cadence | Source |
|-------|---------------|--------|
| **Coordinator sensors** | Every 15 min (+ event triggers) | Existing `HEO2Coordinator` |
| **Cost accumulator sensors** | Continuous (state-change listener) | New `CostTracker` class |
| **Octopus billing sensors** | Daily at 06:00 | New `OctopusBillingFetcher` class |

### Why This Approach

- No custom frontend JS — uses battle-tested community cards
- All data available to HA automations, not just the dashboard
- Users can build their own dashboards from the entities
- Follows HA conventions (entities, device grouping, `last_reset`)

---

## 2. Entity Details

All entities live under the `heo2` domain with `_attr_has_entity_name = True` and shared `DeviceInfo`, producing IDs like `sensor.heo2_solar_forecast_today` grouped under one device card.

### Group 1 — Forecast & Plan (Coordinator, every 15 min)

| Entity ID suffix | Type | Unit | State | Attributes |
|-----------------|------|------|-------|------------|
| `solar_forecast_today` | sensor | kWh | Total kWh today | `hourly: list[float]` (24 values) |
| `solar_forecast_hourly` | sensor | kWh | Current-hour kWh | `forecast: list[float]` (24 values) |
| `load_forecast_hourly` | sensor | W | Current-hour avg W | `forecast: list[float]` (24 values, in W) |
| `import_rates` | sensor | p/kWh | Current import rate | `rates: list[{start, end, rate}]` (48 half-hours) |
| `export_rates` | sensor | p/kWh | Current export rate | `rates: list[{start, end, rate}]` (48 half-hours) |
| `current_import_rate` | sensor | p/kWh | Current half-hour import rate | — |
| `current_export_rate` | sensor | p/kWh | Current half-hour export rate | — |
| `soc_trajectory` | sensor | % | Current SOC | `trajectory: list[float]` (24 values, projected SOC%) |
| `programme_slots` | sensor | — | Summary string | `slots: list[{start, end, soc, grid_charge}]` (6 slots) |
| `programme_reason` | sensor | — | Latest reason | `reasons: list[str]` (full reason log) |
| `active_rules` | sensor | — | Count of active rules | `rules: list[str]` (rule names) |

### Group 2 — Cost Accumulator (Continuous)

These use `SensorStateClass.TOTAL` with `last_reset` for HA long-term statistics.

| Entity ID suffix | Type | Unit | Reset |
|-----------------|------|------|-------|
| `daily_import_cost` | sensor | £ | Midnight |
| `daily_export_revenue` | sensor | £ | Midnight |
| `daily_solar_value` | sensor | £ | Midnight |
| `weekly_net_cost` | sensor | £ | Monday 00:00 |
| `weekly_savings_vs_flat` | sensor | £ | Monday 00:00 |

### Group 3 — Octopus Billing (Daily at 06:00, optional)

| Entity ID suffix | Type | Unit | Notes |
|-----------------|------|------|-------|
| `octopus_monthly_bill` | sensor | £ | Current month running total |
| `octopus_last_month_bill` | sensor | £ | Previous month final |

### Group 4 — ROI / Payback

| Entity ID suffix | Type | Notes |
|-----------------|------|-------|
| `system_cost` | number | Adjustable, default £16,800 |
| `additional_costs` | number | Adjustable, default £0 |
| `total_savings` | sensor | Cumulative savings (seeded £1,131.47 + accumulated) |
| `payback_progress` | sensor | % = total_savings / (system_cost + additional_costs) × 100 |
| `estimated_payback_date` | sensor | Projected date based on savings rate |

### Group 5 — Existing Entities (unchanged)

- `slot_1` through `slot_6` (6 sensors)
- `next_action` (sensor)
- `last_run` (sensor)
- `load_profile` (sensor)
- `best_{appliance}_window` (4 sensors: wash, dryer, dishwasher, ev)
- `healthy` (binary_sensor)
- `enabled` (switch)
- `min_soc` (number)

**Total new entities:** 23 (11 forecast/plan + 5 cost + 2 octopus + 3 ROI sensors + 2 ROI number entities)

---

## 3. Data Flow

### Pipeline 1 — Coordinator (every 15 min)

```
HEO2Coordinator._async_update_data()
├── Read SOC entity state
├── Read rate data (import_rates, export_rates from AgilePredict/IGO)
├── Read solar forecast (Solcast API)
├── Build load forecast (HA recorder historical + appliance awareness)
├── Run rule engine → ProgrammeState
├── Calculate SOC trajectory (new: forward simulation)
│   └── current SOC + hourly (solar - load) + programme grid_charge flags
│       → 24 floats (projected SOC% per hour)
└── Update all Group 1 sensor states
```

**SOC trajectory calculation:**
- Start from current SOC
- For each hour ahead (0–23), apply: solar_kwh × charge_efficiency - load_kwh / discharge_efficiency
- If programme slot has grid_charge=True and SOC < target, add charge at max_charge_kw
- Clamp to [min_soc, max_soc]
- Result: 24-element list of projected SOC percentages

### Pipeline 2 — Cost Accumulator (continuous)

```
CostTracker
├── async_track_state_change(load_power_entity)
├── async_track_state_change(pv_power_entity)  [if configured]
├── On each state change:
│   ├── Calculate energy: W × Δt → kWh
│   ├── Look up current import/export rate
│   ├── Accumulate: import_cost += kWh × import_rate / 100
│   ├── Accumulate: export_revenue += kWh × export_rate / 100
│   ├── Accumulate: solar_value += solar_kWh × import_rate / 100
│   └── Update sensor states
├── Daily reset at midnight (set last_reset, zero accumulators)
└── Weekly reset Monday 00:00 (weekly sensors only)
```

### Pipeline 3 — Octopus Billing (daily at 06:00)

```
OctopusBillingFetcher
├── Triggered by async_track_time_change(hour=6, minute=0)
├── GET /v1/electricity-meter-points/{mpan}/meters/{serial}/consumption/
│   └── ?period_from=start_of_month&group_by=day
├── Multiply each day's kWh × actual half-hourly rates
├── Sum → octopus_monthly_bill
├── On 1st of month: snapshot previous → octopus_last_month_bill
└── Update sensor states
```

**Octopus API details:**
- Base URL: `https://api.octopus.energy/v1/`
- Auth: HTTP Basic (API key as username, no password)
- Consumption data lags ~24 hours
- Rate fetched from: `/v1/products/{product_code}/electricity-tariffs/{tariff_code}/standard-unit-rates/`

---

## 4. Config Flow Changes

### New Step 7: Octopus (optional)

Added between current step 6 (services) and entry creation. The current `async_step_services` will chain to `async_step_octopus` instead of calling `async_create_entry`.

```python
async def async_step_octopus(self, user_input=None):
    if user_input is not None:
        self._data.update(user_input)
        return await self.async_step_payback()
    return self.async_show_form(
        step_id="octopus",
        data_schema=vol.Schema({
            vol.Optional("octopus_api_key", default=""): str,
            vol.Optional("octopus_account_number", default=""): str,
            vol.Optional("octopus_mpan", default=""): str,
        }),
    )
```

### New Step 8: Payback Seed Values

```python
async def async_step_payback(self, user_input=None):
    if user_input is not None:
        self._data.update(user_input)
        return self.async_create_entry(title="HEO II", data=self._data)
    return self.async_show_form(
        step_id="payback",
        data_schema=vol.Schema({
            vol.Required("system_cost", default=16800.0): vol.Coerce(float),
            vol.Required("additional_costs", default=0.0): vol.Coerce(float),
            vol.Required("savings_to_date", default=1131.47): vol.Coerce(float),
            vol.Required("install_date", default="2025-02-01"): str,
        }),
    )
```

### strings.json additions

```json
"octopus": {
    "title": "Octopus Energy (Optional)",
    "description": "Connect to Octopus Energy for billing data. Leave blank to skip.",
    "data": {
        "octopus_api_key": "Octopus API key",
        "octopus_account_number": "Account number",
        "octopus_mpan": "Electricity MPAN"
    }
},
"payback": {
    "title": "Payback Tracking",
    "description": "Seed values for ROI calculation.",
    "data": {
        "system_cost": "Total system cost (£)",
        "additional_costs": "Additional costs (£)",
        "savings_to_date": "Savings already banked (£)",
        "install_date": "Installation date (YYYY-MM-DD)"
    }
}
```

---

## 5. Dashboard YAML Structure

### Delivery

Example YAML files in `docs/dashboard/`:

```
docs/dashboard/
├── README.md              # Prerequisites, install instructions
├── forecast-plan.yaml     # View 1
├── tariffs.yaml           # View 2
└── roi-tracking.yaml      # View 3
```

### Prerequisites (HACS community cards)

- `apexcharts-card` — time-series charts
- `mushroom` — info chips, entity cards
- `flex-table-card` — programme slots table

### View 1: Forecast & Plan

**24-hour energy chart** (ApexCharts):
- Solar: area fill, colour `#f59e0b` (yellow)
- Load: dashed line, colour `#ef4444` (red)
- Battery: bars, colour `#22d3ee` (cyan)
- Grid: bars, colour `#a855f7` (purple)
- Data source: `sensor.heo2_solar_forecast_hourly` (attribute `forecast`), `sensor.heo2_load_forecast_hourly` (attribute `forecast`)

**SOC trajectory chart** (ApexCharts):
- Line chart, 0–100% y-axis
- Data source: `sensor.heo2_soc_trajectory` (attribute `trajectory`)
- Overlay: rate period shading using `sensor.heo2_import_rates` attribute

**Programme table** (flex-table-card):
- 6 rows from `sensor.heo2_programme_slots` attribute `slots`
- Columns: Slot #, Start, End, SOC Target, Grid Charge
- Grid charge column: green tick / red cross

**Info chips** (Mushroom):
- Active rules count + list
- Next action
- Programme reason (latest)

### View 2: Tariffs

**Current rate cards** (Mushroom entity cards):
- `sensor.heo2_current_import_rate` — large value display
- `sensor.heo2_current_export_rate` — large value display

**48-hour rate charts** (ApexCharts, step-line):
- Import rates from `sensor.heo2_import_rates` attribute `rates`
- Export rates from `sensor.heo2_export_rates` attribute `rates`
- Highlight current half-hour

**Tariff selector:**
- Simple entity card for any user-configured tariff input_select (documented in README, not created by integration)

### View 3: ROI Tracking

**Daily/weekly cost cards** (Mushroom entity cards):
- `sensor.heo2_daily_import_cost`
- `sensor.heo2_daily_export_revenue`
- `sensor.heo2_daily_solar_value`
- `sensor.heo2_weekly_net_cost`
- `sensor.heo2_weekly_savings_vs_flat`

**Octopus monthly bills** (conditional cards, only shown if Octopus configured):
- `sensor.heo2_octopus_monthly_bill`
- `sensor.heo2_octopus_last_month_bill`

**Payback progress** (Mushroom gauge card):
- `sensor.heo2_payback_progress` — 0–100% gauge
- `sensor.heo2_estimated_payback_date` — text below gauge
- `sensor.heo2_total_savings` — running total display

**Adjustable inputs:**
- `number.heo2_system_cost` — slider/input box
- `number.heo2_additional_costs` — slider/input box

---

## 6. Colour Palette

Consistent across all charts, matching original HEO:

| Series | Colour | Hex |
|--------|--------|-----|
| Solar | Yellow | `#f59e0b` |
| Load | Red (dashed) | `#ef4444` |
| Battery | Cyan | `#22d3ee` |
| Grid | Purple | `#a855f7` |
| Import rate | Blue | `#3b82f6` |
| Export rate | Green | `#22c55e` |
| SOC trajectory | Teal | `#14b8a6` |

---

## 7. New Classes Summary

| Class | File | Responsibility |
|-------|------|---------------|
| `CostTracker` | `cost_tracker.py` | Subscribes to power entity state changes, accumulates energy × rate, resets daily/weekly |
| `OctopusBillingFetcher` | `octopus.py` | Daily fetch from Octopus API, calculates monthly bill from consumption × rates |
| `SOCTrajectoryCalculator` | In coordinator or `models.py` | Forward simulation: current SOC + forecasts + programme → 24-hour SOC projection |

---

## 8. Testing Strategy

- **Unit tests:** CostTracker accumulation logic, SOC trajectory calculation, Octopus API response parsing
- **Integration tests:** Config flow steps 7–8, sensor state updates from coordinator data
- **Mock fixtures:** Fake Octopus API responses, fake state changes for CostTracker
- **Dashboard:** Manual validation — YAML files are documentation, not testable code

---

## 9. Out of Scope

- Custom Lovelace card JS (using community cards only)
- Agile vs IGO comparison table (dropped per Paddy's request)
- Real-time WebSocket streaming (HA polling is sufficient at 15-min intervals)
- Historical chart data beyond what HA long-term statistics provides
- Backtesting (nice-to-have, not this spec)
