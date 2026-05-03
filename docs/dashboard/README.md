# HEO II Dashboard

Example Lovelace YAML for the HEO II integration dashboard.

## Prerequisites

Install these community cards via HACS:

- [apexcharts-card](https://github.com/RomRider/apexcharts-card) — time-series charts
- [mushroom](https://github.com/piitaya/lovelace-mushroom) — entity cards, chips, gauges
- [flex-table-card](https://github.com/custom-cards/flex-table-card) — programme slots table

## Installation

1. Install the prerequisites above via HACS
2. In your HA dashboard, click **Edit Dashboard** → **Raw configuration editor**
3. Copy the contents of the YAML files below into your dashboard views
4. Save and refresh

## Views

- `forecast-plan.yaml` — 24h energy forecast, SOC trajectory, projection summary, programme slots
- `tariffs.yaml` — current rates, 48h rate charts
- `status-modes.yaml` — alert chips, projection breakdown, EV deferral toggle, cycle budget, EPS, granularity snaps
- `roi-tracking.yaml` — daily/weekly costs, Octopus bills, payback progress

## Colour Palette

| Series | Colour | Hex |
|--------|--------|-----|
| Solar | Yellow | `#f59e0b` |
| Load | Red (dashed) | `#ef4444` |
| Battery | Cyan | `#22d3ee` |
| Grid | Purple | `#a855f7` |
| Import rate | Blue | `#3b82f6` |
| Export rate | Green | `#22c55e` |
| SOC trajectory | Teal | `#14b8a6` |
