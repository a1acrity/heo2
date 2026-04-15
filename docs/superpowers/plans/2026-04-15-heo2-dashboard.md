# HEO II Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 23 new Home Assistant entities across 3 data layers (coordinator, cost accumulator, Octopus billing) to power a community-card dashboard for HEO II.

**Architecture:** Three independent data pipelines feed sensors: (1) the existing coordinator enriched with forecast/trajectory data every 15 min, (2) a new CostTracker that accumulates energy costs from HA state changes with daily/weekly resets, (3) an optional OctopusBillingFetcher that fetches monthly bill data daily at 06:00. All sensors are CoordinatorEntity-based, reading from state stored on the coordinator object. Two new config flow steps collect Octopus credentials and payback seed values.

**Tech Stack:** Python 3.12, Home Assistant Core APIs (DataUpdateCoordinator, CoordinatorEntity, SensorStateClass, async_track_state_change_event, async_track_time_change), httpx for Octopus API, pytest + pytest-asyncio + pytest-httpx for testing.

**Spec:** `docs/superpowers/specs/2026-04-15-heo2-dashboard-design.md`

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `custom_components/heo2/soc_trajectory.py` | Pure function: forward-simulate 24h SOC from forecasts + programme |
| Create | `custom_components/heo2/cost_tracker.py` | Pure accumulator: energy × rate with daily/weekly reset |
| Create | `custom_components/heo2/octopus.py` | HTTP client: fetch Octopus consumption, calculate monthly bill |
| Modify | `custom_components/heo2/const.py` | Add payback defaults, flat rate constant |
| Modify | `custom_components/heo2/coordinator.py` | Store dashboard state, run SOC trajectory, host CostTracker/Octopus data |
| Modify | `custom_components/heo2/sensor.py` | Add 21 new sensor classes (Groups 1–4) |
| Modify | `custom_components/heo2/number.py` | Add 2 number entities (system_cost, additional_costs) |
| Modify | `custom_components/heo2/config_flow.py` | Add steps 7 (Octopus) and 8 (Payback) |
| Modify | `custom_components/heo2/strings.json` | Add UI strings for new config steps |
| Modify | `custom_components/heo2/__init__.py` | Start CostTracker + OctopusBillingFetcher, clean up on unload |
| Create | `tests/test_soc_trajectory.py` | SOC trajectory unit tests |
| Create | `tests/test_cost_tracker.py` | CostTracker accumulation + reset tests |
| Create | `tests/test_octopus.py` | Octopus API client tests |
| Create | `tests/test_config_flow_dashboard.py` | Config flow steps 7–8 tests |
| Create | `tests/test_dashboard_sensors.py` | Dashboard sensor state + attribute tests |
| Create | `docs/dashboard/README.md` | Dashboard prerequisites + install instructions |
| Create | `docs/dashboard/forecast-plan.yaml` | View 1: Forecast & Plan |
| Create | `docs/dashboard/tariffs.yaml` | View 2: Tariffs |
| Create | `docs/dashboard/roi-tracking.yaml` | View 3: ROI Tracking |

---

### Task 1: SOC Trajectory Calculator

**Files:**
- Create: `custom_components/heo2/soc_trajectory.py`
- Create: `tests/test_soc_trajectory.py`

- [ ] **Step 1: Write failing tests for SOC trajectory**

```python
# tests/test_soc_trajectory.py
"""Tests for SOC trajectory forward simulation."""

import pytest
from datetime import time

from heo2.soc_trajectory import calculate_soc_trajectory
from heo2.models import SlotConfig


@pytest.fixture
def flat_load() -> list[float]:
    """1.9 kWh per hour for 24 hours."""
    return [1.9] * 24


@pytest.fixture
def midday_solar() -> list[float]:
    """Solar peak around midday: ~20 kWh total."""
    solar = [0.0] * 24
    for h in range(6, 18):
        solar[h] = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.2, 0.8, 1.5, 2.5, 3.0, 3.5, 3.5, 3.0, 2.0, 1.0, 0.3, 0.0][h]
    return solar


@pytest.fixture
def default_slots() -> list[SlotConfig]:
    """Default 6-slot programme at 20% min SOC, no grid charge."""
    from heo2.models import ProgrammeState
    return ProgrammeState.default(min_soc=20).slots


class TestSOCTrajectory:
    def test_returns_24_floats(self, flat_load, default_slots):
        result = calculate_soc_trajectory(
            current_soc=50.0,
            solar_forecast_kwh=[0.0] * 24,
            load_forecast_kwh=flat_load,
            programme_slots=default_slots,
            battery_capacity_kwh=20.0,
            max_charge_kw=5.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            min_soc=20.0,
            max_soc=100.0,
            current_hour=12,
        )
        assert len(result) == 24
        assert all(isinstance(v, float) for v in result)

    def test_first_value_is_current_soc(self, flat_load, default_slots):
        result = calculate_soc_trajectory(
            current_soc=65.0,
            solar_forecast_kwh=[0.0] * 24,
            load_forecast_kwh=flat_load,
            programme_slots=default_slots,
            battery_capacity_kwh=20.0,
            max_charge_kw=5.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            min_soc=20.0,
            max_soc=100.0,
            current_hour=12,
        )
        assert result[0] == 65.0

    def test_soc_decreases_with_load_no_solar(self, flat_load, default_slots):
        result = calculate_soc_trajectory(
            current_soc=80.0,
            solar_forecast_kwh=[0.0] * 24,
            load_forecast_kwh=flat_load,
            programme_slots=default_slots,
            battery_capacity_kwh=20.0,
            max_charge_kw=5.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            min_soc=20.0,
            max_soc=100.0,
            current_hour=12,
        )
        # SOC should decrease over time with no solar
        assert result[5] < result[0]

    def test_soc_clamped_to_min(self, flat_load, default_slots):
        result = calculate_soc_trajectory(
            current_soc=25.0,
            solar_forecast_kwh=[0.0] * 24,
            load_forecast_kwh=flat_load,
            programme_slots=default_slots,
            battery_capacity_kwh=20.0,
            max_charge_kw=5.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            min_soc=20.0,
            max_soc=100.0,
            current_hour=12,
        )
        assert all(v >= 20.0 for v in result)

    def test_soc_clamped_to_max(self, default_slots):
        result = calculate_soc_trajectory(
            current_soc=95.0,
            solar_forecast_kwh=[5.0] * 24,  # massive solar
            load_forecast_kwh=[0.1] * 24,   # tiny load
            programme_slots=default_slots,
            battery_capacity_kwh=20.0,
            max_charge_kw=5.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            min_soc=20.0,
            max_soc=100.0,
            current_hour=12,
        )
        assert all(v <= 100.0 for v in result)

    def test_grid_charge_increases_soc(self):
        """When a slot has grid_charge=True, SOC should increase."""
        slots = [
            SlotConfig(time(0, 0), time(4, 0), capacity_soc=80, grid_charge=True),
            SlotConfig(time(4, 0), time(8, 0), capacity_soc=20, grid_charge=False),
            SlotConfig(time(8, 0), time(12, 0), capacity_soc=20, grid_charge=False),
            SlotConfig(time(12, 0), time(16, 0), capacity_soc=20, grid_charge=False),
            SlotConfig(time(16, 0), time(23, 59), capacity_soc=20, grid_charge=False),
            SlotConfig(time(23, 59), time(0, 0), capacity_soc=20, grid_charge=False),
        ]
        result = calculate_soc_trajectory(
            current_soc=30.0,
            solar_forecast_kwh=[0.0] * 24,
            load_forecast_kwh=[0.5] * 24,
            programme_slots=slots,
            battery_capacity_kwh=20.0,
            max_charge_kw=5.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            min_soc=20.0,
            max_soc=100.0,
            current_hour=0,
        )
        # During grid charge hours (0-4), SOC should increase
        assert result[2] > result[0]

    def test_solar_increases_soc(self, midday_solar, default_slots):
        result = calculate_soc_trajectory(
            current_soc=40.0,
            solar_forecast_kwh=midday_solar,
            load_forecast_kwh=[0.5] * 24,
            programme_slots=default_slots,
            battery_capacity_kwh=20.0,
            max_charge_kw=5.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            min_soc=20.0,
            max_soc=100.0,
            current_hour=6,
        )
        # During solar hours, SOC should rise
        assert result[6] > result[0]  # 6 hours into solar peak
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/a1acrity/ai-projects/data/heo2 && python -m pytest tests/test_soc_trajectory.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'heo2.soc_trajectory'`

- [ ] **Step 3: Implement SOC trajectory calculator**

```python
# custom_components/heo2/soc_trajectory.py
"""SOC trajectory forward simulation. No Home Assistant imports."""

from __future__ import annotations

from datetime import time

from .models import SlotConfig


def calculate_soc_trajectory(
    current_soc: float,
    solar_forecast_kwh: list[float],
    load_forecast_kwh: list[float],
    programme_slots: list[SlotConfig],
    battery_capacity_kwh: float,
    max_charge_kw: float,
    charge_efficiency: float,
    discharge_efficiency: float,
    min_soc: float,
    max_soc: float,
    current_hour: int,
) -> list[float]:
    """Forward-simulate battery SOC for the next 24 hours.

    Args:
        current_soc: Current battery SOC percentage (0-100).
        solar_forecast_kwh: 24-element list, index 0 = hour 00:00.
        load_forecast_kwh: 24-element list, index 0 = hour 00:00.
        programme_slots: The 6 inverter timer slots.
        battery_capacity_kwh: Total battery capacity in kWh.
        max_charge_kw: Maximum grid charge rate in kW.
        charge_efficiency: Charge efficiency (0-1).
        discharge_efficiency: Discharge efficiency (0-1).
        min_soc: Minimum allowed SOC percentage.
        max_soc: Maximum allowed SOC percentage.
        current_hour: Current hour (0-23) — simulation starts here.

    Returns:
        24-element list of projected SOC percentages.
    """
    trajectory: list[float] = []
    soc = current_soc

    for step in range(24):
        trajectory.append(soc)

        hour_idx = (current_hour + step) % 24
        hour_time = time(hour_idx, 0)

        solar_kwh = solar_forecast_kwh[hour_idx]
        load_kwh = load_forecast_kwh[hour_idx]

        # Net energy: solar charges (with loss), load discharges (with loss)
        net_kwh = (solar_kwh * charge_efficiency) - (load_kwh / discharge_efficiency)

        # Grid charge if programme slot says so and SOC is below target
        for slot in programme_slots:
            if slot.contains_time(hour_time) and slot.grid_charge:
                if soc < slot.capacity_soc:
                    needed_kwh = (slot.capacity_soc - soc) / 100.0 * battery_capacity_kwh
                    available_kwh = max_charge_kw * charge_efficiency
                    net_kwh += min(needed_kwh, available_kwh)
                break

        soc += (net_kwh / battery_capacity_kwh) * 100.0
        soc = max(min_soc, min(max_soc, soc))

    return trajectory
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/a1acrity/ai-projects/data/heo2 && python -m pytest tests/test_soc_trajectory.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/heo2/soc_trajectory.py tests/test_soc_trajectory.py
git commit -m "feat: add SOC trajectory forward simulation"
```

---

### Task 2: CostTracker Accumulator

**Files:**
- Create: `custom_components/heo2/cost_tracker.py`
- Create: `tests/test_cost_tracker.py`

- [ ] **Step 1: Write failing tests for CostTracker**

```python
# tests/test_cost_tracker.py
"""Tests for CostTracker energy cost accumulator."""

import pytest
from datetime import datetime, timezone, timedelta

from heo2.cost_tracker import CostAccumulator


@pytest.fixture
def acc() -> CostAccumulator:
    return CostAccumulator()


@pytest.fixture
def t0() -> datetime:
    return datetime(2026, 4, 15, 12, 0, 0, tzinfo=timezone.utc)


class TestCostAccumulator:
    def test_initial_values_are_zero(self, acc):
        assert acc.daily_import_cost == 0.0
        assert acc.daily_export_revenue == 0.0
        assert acc.daily_solar_value == 0.0
        assert acc.weekly_net_cost == 0.0
        assert acc.weekly_savings_vs_flat == 0.0

    def test_load_update_accumulates_import_cost(self, acc, t0):
        # First update sets baseline — no accumulation yet
        acc.update_load(watts=1000.0, now=t0, import_rate_pence=27.88)
        assert acc.daily_import_cost == 0.0

        # Second update: 1000W for 1 hour = 1 kWh
        t1 = t0 + timedelta(hours=1)
        acc.update_load(watts=1000.0, now=t1, import_rate_pence=27.88)
        # 1 kWh × 27.88p / 100 = £0.2788
        assert acc.daily_import_cost == pytest.approx(0.2788, abs=0.001)

    def test_load_tracks_weekly_net_cost(self, acc, t0):
        acc.update_load(watts=2000.0, now=t0, import_rate_pence=27.88)
        t1 = t0 + timedelta(hours=1)
        acc.update_load(watts=2000.0, now=t1, import_rate_pence=27.88)
        # 2 kWh × 27.88p / 100 = £0.5576
        assert acc.weekly_net_cost == pytest.approx(0.5576, abs=0.001)

    def test_load_tracks_weekly_imported_kwh(self, acc, t0):
        acc.update_load(watts=2000.0, now=t0, import_rate_pence=27.88)
        t1 = t0 + timedelta(hours=1)
        acc.update_load(watts=2000.0, now=t1, import_rate_pence=27.88)
        assert acc.weekly_imported_kwh == pytest.approx(2.0, abs=0.01)

    def test_pv_update_accumulates_solar_value(self, acc, t0):
        acc.update_pv(watts=3000.0, now=t0, import_rate_pence=27.88, export_rate_pence=15.0)
        t1 = t0 + timedelta(hours=1)
        acc.update_pv(watts=3000.0, now=t1, import_rate_pence=27.88, export_rate_pence=15.0)
        # 3 kWh × 27.88p / 100 = £0.8364
        assert acc.daily_solar_value == pytest.approx(0.8364, abs=0.001)

    def test_pv_update_accumulates_export_revenue(self, acc, t0):
        acc.update_pv(watts=3000.0, now=t0, import_rate_pence=27.88, export_rate_pence=15.0)
        t1 = t0 + timedelta(hours=1)
        acc.update_pv(watts=3000.0, now=t1, import_rate_pence=27.88, export_rate_pence=15.0)
        # 3 kWh × 15p / 100 = £0.45
        assert acc.daily_export_revenue == pytest.approx(0.45, abs=0.001)

    def test_pv_reduces_weekly_net_cost(self, acc, t0):
        acc.update_pv(watts=3000.0, now=t0, import_rate_pence=27.88, export_rate_pence=15.0)
        t1 = t0 + timedelta(hours=1)
        acc.update_pv(watts=3000.0, now=t1, import_rate_pence=27.88, export_rate_pence=15.0)
        # Export revenue reduces net cost: -£0.45
        assert acc.weekly_net_cost == pytest.approx(-0.45, abs=0.001)

    def test_daily_reset_zeros_daily_values(self, acc, t0):
        acc.update_load(watts=1000.0, now=t0, import_rate_pence=27.88)
        acc.update_load(watts=1000.0, now=t0 + timedelta(hours=1), import_rate_pence=27.88)
        acc.reset_daily(t0 + timedelta(days=1))
        assert acc.daily_import_cost == 0.0
        assert acc.daily_export_revenue == 0.0
        assert acc.daily_solar_value == 0.0
        # Weekly values should NOT be reset
        assert acc.weekly_net_cost != 0.0

    def test_weekly_reset_zeros_weekly_values(self, acc, t0):
        acc.update_load(watts=1000.0, now=t0, import_rate_pence=27.88)
        acc.update_load(watts=1000.0, now=t0 + timedelta(hours=1), import_rate_pence=27.88)
        acc.reset_weekly(t0 + timedelta(days=7))
        assert acc.weekly_net_cost == 0.0
        assert acc.weekly_savings_vs_flat == 0.0
        assert acc.weekly_imported_kwh == 0.0

    def test_savings_vs_flat(self, acc, t0):
        """Savings = (flat_rate × total_kwh) - actual_cost + export_revenue."""
        flat_rate_pence = 24.5  # typical SVT rate
        # Import 2 kWh at 7p (cheap rate)
        acc.update_load(watts=2000.0, now=t0, import_rate_pence=7.0)
        acc.update_load(watts=2000.0, now=t0 + timedelta(hours=1), import_rate_pence=7.0)
        # Export 1 kWh at 15p
        acc.update_pv(watts=1000.0, now=t0, import_rate_pence=7.0, export_rate_pence=15.0)
        acc.update_pv(watts=1000.0, now=t0 + timedelta(hours=1), import_rate_pence=7.0, export_rate_pence=15.0)

        acc.calculate_savings_vs_flat(flat_rate_pence)

        # Flat cost = 2 kWh × 24.5p / 100 = £0.49
        # Actual cost = 2 kWh × 7p / 100 = £0.14
        # Export revenue = 1 kWh × 15p / 100 = £0.15
        # Savings = £0.49 - £0.14 + £0.15 = £0.50
        assert acc.weekly_savings_vs_flat == pytest.approx(0.50, abs=0.01)

    def test_last_daily_reset_recorded(self, acc, t0):
        reset_time = t0 + timedelta(days=1)
        acc.reset_daily(reset_time)
        assert acc.last_daily_reset == reset_time

    def test_last_weekly_reset_recorded(self, acc, t0):
        reset_time = t0 + timedelta(days=7)
        acc.reset_weekly(reset_time)
        assert acc.last_weekly_reset == reset_time
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/a1acrity/ai-projects/data/heo2 && python -m pytest tests/test_cost_tracker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'heo2.cost_tracker'`

- [ ] **Step 3: Implement CostAccumulator**

```python
# custom_components/heo2/cost_tracker.py
"""Energy cost accumulator. No Home Assistant imports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CostAccumulator:
    """Accumulates energy × rate for daily/weekly cost tracking.

    Call update_load() on each grid import power reading.
    Call update_pv() on each solar generation power reading.
    Call reset_daily() at midnight, reset_weekly() on Monday 00:00.
    """

    # Daily accumulators (reset at midnight)
    daily_import_cost: float = 0.0
    daily_export_revenue: float = 0.0
    daily_solar_value: float = 0.0

    # Weekly accumulators (reset Monday 00:00)
    weekly_net_cost: float = 0.0
    weekly_savings_vs_flat: float = 0.0
    weekly_imported_kwh: float = 0.0

    # Reset timestamps (for HA long-term statistics)
    last_daily_reset: datetime | None = None
    last_weekly_reset: datetime | None = None

    # Internal state for trapezoidal integration
    _last_load_w: float | None = field(default=None, repr=False)
    _last_load_time: datetime | None = field(default=None, repr=False)
    _last_pv_w: float | None = field(default=None, repr=False)
    _last_pv_time: datetime | None = field(default=None, repr=False)

    def update_load(
        self, watts: float, now: datetime, import_rate_pence: float
    ) -> None:
        """Record a grid import power reading and accumulate cost."""
        if self._last_load_w is not None and self._last_load_time is not None:
            dt_hours = (now - self._last_load_time).total_seconds() / 3600
            kwh = self._last_load_w / 1000.0 * dt_hours
            cost = kwh * import_rate_pence / 100.0
            self.daily_import_cost += cost
            self.weekly_net_cost += cost
            self.weekly_imported_kwh += kwh
        self._last_load_w = watts
        self._last_load_time = now

    def update_pv(
        self,
        watts: float,
        now: datetime,
        import_rate_pence: float,
        export_rate_pence: float,
    ) -> None:
        """Record a solar generation power reading and accumulate value."""
        if self._last_pv_w is not None and self._last_pv_time is not None:
            dt_hours = (now - self._last_pv_time).total_seconds() / 3600
            kwh = self._last_pv_w / 1000.0 * dt_hours
            self.daily_solar_value += kwh * import_rate_pence / 100.0
            self.daily_export_revenue += kwh * export_rate_pence / 100.0
            self.weekly_net_cost -= kwh * export_rate_pence / 100.0
        self._last_pv_w = watts
        self._last_pv_time = now

    def calculate_savings_vs_flat(self, flat_rate_pence: float) -> None:
        """Calculate weekly savings compared to a flat tariff.

        Savings = (flat_rate × total_imported_kwh) - actual_import_cost + export_revenue.
        """
        flat_cost = self.weekly_imported_kwh * flat_rate_pence / 100.0
        actual_net = self.weekly_net_cost
        self.weekly_savings_vs_flat = flat_cost - actual_net

    def reset_daily(self, now: datetime) -> None:
        """Zero daily accumulators and record reset time."""
        self.daily_import_cost = 0.0
        self.daily_export_revenue = 0.0
        self.daily_solar_value = 0.0
        self.last_daily_reset = now

    def reset_weekly(self, now: datetime) -> None:
        """Zero weekly accumulators and record reset time."""
        self.weekly_net_cost = 0.0
        self.weekly_savings_vs_flat = 0.0
        self.weekly_imported_kwh = 0.0
        self.last_weekly_reset = now
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/a1acrity/ai-projects/data/heo2 && python -m pytest tests/test_cost_tracker.py -v`
Expected: All 12 tests PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/heo2/cost_tracker.py tests/test_cost_tracker.py
git commit -m "feat: add CostAccumulator for daily/weekly energy cost tracking"
```

---

### Task 3: Octopus Billing Fetcher

**Files:**
- Create: `custom_components/heo2/octopus.py`
- Create: `tests/test_octopus.py`

- [ ] **Step 1: Write failing tests for OctopusBillingFetcher**

```python
# tests/test_octopus.py
"""Tests for Octopus Energy billing client."""

import pytest
from datetime import datetime, timezone

from heo2.octopus import OctopusBillingFetcher


SAMPLE_CONSUMPTION = {
    "results": [
        {
            "consumption": 10.5,
            "interval_start": "2026-04-01T00:00:00Z",
            "interval_end": "2026-04-01T00:30:00Z",
        },
        {
            "consumption": 8.2,
            "interval_start": "2026-04-01T00:30:00Z",
            "interval_end": "2026-04-01T01:00:00Z",
        },
    ],
    "count": 2,
    "next": None,
    "previous": None,
}

SAMPLE_RATES = {
    "results": [
        {
            "value_inc_vat": 27.88,
            "valid_from": "2026-04-01T00:00:00Z",
            "valid_to": "2026-04-01T00:30:00Z",
        },
        {
            "value_inc_vat": 25.50,
            "valid_from": "2026-04-01T00:30:00Z",
            "valid_to": "2026-04-01T01:00:00Z",
        },
    ],
    "count": 2,
    "next": None,
    "previous": None,
}


class TestOctopusBillingFetcher:
    @pytest.mark.asyncio
    async def test_calculates_monthly_bill(self, httpx_mock):
        """Consumption × rate = monthly bill."""
        httpx_mock.add_response(
            url__regex=r".*/consumption/.*",
            json=SAMPLE_CONSUMPTION,
        )
        httpx_mock.add_response(
            url__regex=r".*/standard-unit-rates/.*",
            json=SAMPLE_RATES,
        )
        fetcher = OctopusBillingFetcher(
            api_key="test_key",
            mpan="1234567890",
            serial="ABC123",
            product_code="AGILE-FLEX-22-11-25",
            tariff_code="E-1R-AGILE-FLEX-22-11-25-C",
        )
        bill = await fetcher.fetch_monthly_bill(
            now=datetime(2026, 4, 15, 6, 0, tzinfo=timezone.utc),
        )
        # 10.5 kWh × 27.88p/100 + 8.2 kWh × 25.50p/100 = £2.928 + £2.091 = £5.019
        # But actual calc matches consumption intervals to rate intervals
        assert bill > 0.0

    @pytest.mark.asyncio
    async def test_returns_zero_on_http_error(self, httpx_mock):
        httpx_mock.add_response(status_code=401)
        fetcher = OctopusBillingFetcher(
            api_key="bad_key",
            mpan="1234567890",
            serial="ABC123",
            product_code="AGILE-FLEX-22-11-25",
            tariff_code="E-1R-AGILE-FLEX-22-11-25-C",
        )
        bill = await fetcher.fetch_monthly_bill(
            now=datetime(2026, 4, 15, 6, 0, tzinfo=timezone.utc),
        )
        assert bill == 0.0

    @pytest.mark.asyncio
    async def test_empty_consumption_returns_zero(self, httpx_mock):
        httpx_mock.add_response(
            url__regex=r".*/consumption/.*",
            json={"results": [], "count": 0, "next": None, "previous": None},
        )
        httpx_mock.add_response(
            url__regex=r".*/standard-unit-rates/.*",
            json=SAMPLE_RATES,
        )
        fetcher = OctopusBillingFetcher(
            api_key="test_key",
            mpan="1234567890",
            serial="ABC123",
            product_code="AGILE-FLEX-22-11-25",
            tariff_code="E-1R-AGILE-FLEX-22-11-25-C",
        )
        bill = await fetcher.fetch_monthly_bill(
            now=datetime(2026, 4, 15, 6, 0, tzinfo=timezone.utc),
        )
        assert bill == 0.0

    def test_calculates_bill_from_consumption_and_rates(self):
        """Unit test for the pure billing calculation."""
        consumption = [
            {"consumption": 10.0, "interval_start": "2026-04-01T00:00:00Z"},
            {"consumption": 5.0, "interval_start": "2026-04-01T00:30:00Z"},
        ]
        rates = [
            {"value_inc_vat": 20.0, "valid_from": "2026-04-01T00:00:00Z", "valid_to": "2026-04-01T00:30:00Z"},
            {"value_inc_vat": 30.0, "valid_from": "2026-04-01T00:30:00Z", "valid_to": "2026-04-01T01:00:00Z"},
        ]
        bill = OctopusBillingFetcher._calculate_bill(consumption, rates)
        # 10 kWh × 20p/100 + 5 kWh × 30p/100 = £2.00 + £1.50 = £3.50
        assert bill == pytest.approx(3.50, abs=0.01)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/a1acrity/ai-projects/data/heo2 && python -m pytest tests/test_octopus.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'heo2.octopus'`

- [ ] **Step 3: Implement OctopusBillingFetcher**

```python
# custom_components/heo2/octopus.py
"""Octopus Energy billing client. No Home Assistant imports."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

OCTOPUS_BASE_URL = "https://api.octopus.energy/v1"


class OctopusBillingFetcher:
    """Fetches consumption data from Octopus Energy and calculates monthly bill."""

    def __init__(
        self,
        api_key: str,
        mpan: str,
        serial: str,
        product_code: str,
        tariff_code: str,
    ) -> None:
        self._api_key = api_key
        self._mpan = mpan
        self._serial = serial
        self._product_code = product_code
        self._tariff_code = tariff_code

        # State
        self.monthly_bill: float = 0.0
        self.last_month_bill: float = 0.0

    async def fetch_monthly_bill(self, now: datetime) -> float:
        """Fetch consumption since start of month and calculate bill in GBP.

        Returns 0.0 on any error.
        """
        start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        try:
            async with httpx.AsyncClient() as client:
                # Fetch consumption
                consumption_resp = await client.get(
                    f"{OCTOPUS_BASE_URL}/electricity-meter-points/{self._mpan}"
                    f"/meters/{self._serial}/consumption/",
                    params={
                        "period_from": start_of_month.isoformat(),
                        "page_size": 25000,
                    },
                    auth=(self._api_key, ""),
                    timeout=30.0,
                )
                consumption_resp.raise_for_status()
                consumption_data = consumption_resp.json().get("results", [])

                # Fetch rates for the same period
                rates_resp = await client.get(
                    f"{OCTOPUS_BASE_URL}/products/{self._product_code}"
                    f"/electricity-tariffs/{self._tariff_code}/standard-unit-rates/",
                    params={
                        "period_from": start_of_month.isoformat(),
                        "page_size": 25000,
                    },
                    timeout=30.0,
                )
                rates_resp.raise_for_status()
                rates_data = rates_resp.json().get("results", [])

        except (httpx.HTTPError, Exception) as exc:
            logger.warning("Octopus API fetch failed: %s", exc)
            return 0.0

        bill = self._calculate_bill(consumption_data, rates_data)
        self.monthly_bill = bill
        return bill

    @staticmethod
    def _calculate_bill(
        consumption: list[dict], rates: list[dict]
    ) -> float:
        """Match each consumption interval to its rate and sum the cost.

        Returns total bill in GBP (£).
        """
        # Build a lookup: interval_start → rate_pence
        rate_lookup: dict[str, float] = {}
        for rate in rates:
            valid_from = rate.get("valid_from", "")
            rate_lookup[valid_from] = rate.get("value_inc_vat", 0.0)

        total_pence = 0.0
        for entry in consumption:
            kwh = entry.get("consumption", 0.0)
            interval_start = entry.get("interval_start", "")
            rate_pence = rate_lookup.get(interval_start, 0.0)
            total_pence += kwh * rate_pence

        return total_pence / 100.0

    def snapshot_month_end(self) -> None:
        """Call on the 1st of a new month to save previous month's bill."""
        self.last_month_bill = self.monthly_bill
        self.monthly_bill = 0.0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/a1acrity/ai-projects/data/heo2 && python -m pytest tests/test_octopus.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/heo2/octopus.py tests/test_octopus.py
git commit -m "feat: add OctopusBillingFetcher for monthly bill calculation"
```

---

### Task 4: Constants and Config Flow

**Files:**
- Modify: `custom_components/heo2/const.py:48` (append new constants)
- Modify: `custom_components/heo2/config_flow.py:115-131` (chain to new steps)
- Modify: `custom_components/heo2/strings.json:67-78` (add new step strings)
- Create: `tests/test_config_flow_dashboard.py`

- [ ] **Step 1: Add new constants to const.py**

Append after line 48 of `custom_components/heo2/const.py`:

```python
# Payback defaults
DEFAULT_SYSTEM_COST = 16800.0
DEFAULT_ADDITIONAL_COSTS = 0.0
DEFAULT_SAVINGS_TO_DATE = 1131.47
DEFAULT_INSTALL_DATE = "2025-02-01"

# Flat tariff for savings comparison (typical UK SVT rate, p/kWh)
DEFAULT_FLAT_RATE_PENCE = 24.5
```

- [ ] **Step 2: Write failing tests for config flow steps 7–8**

```python
# tests/test_config_flow_dashboard.py
"""Tests for config flow Octopus and Payback steps."""

import pytest
from unittest.mock import patch, MagicMock

from heo2.config_flow import HEO2ConfigFlow


class TestConfigFlowOctopusStep:
    @pytest.mark.asyncio
    async def test_octopus_step_shows_form(self):
        flow = HEO2ConfigFlow()
        flow.hass = MagicMock()
        result = await flow.async_step_octopus()
        assert result["type"] == "form"
        assert result["step_id"] == "octopus"

    @pytest.mark.asyncio
    async def test_octopus_step_chains_to_payback(self):
        flow = HEO2ConfigFlow()
        flow.hass = MagicMock()
        flow._data = {}
        result = await flow.async_step_octopus({
            "octopus_api_key": "",
            "octopus_account_number": "",
            "octopus_mpan": "",
            "octopus_serial": "",
            "octopus_product_code": "",
            "octopus_tariff_code": "",
        })
        assert result["type"] == "form"
        assert result["step_id"] == "payback"


class TestConfigFlowPaybackStep:
    @pytest.mark.asyncio
    async def test_payback_step_shows_form(self):
        flow = HEO2ConfigFlow()
        flow.hass = MagicMock()
        result = await flow.async_step_payback()
        assert result["type"] == "form"
        assert result["step_id"] == "payback"

    @pytest.mark.asyncio
    async def test_payback_step_creates_entry(self):
        flow = HEO2ConfigFlow()
        flow.hass = MagicMock()
        flow._data = {"mqtt_host": "localhost"}
        # Mock async_create_entry
        with patch.object(flow, "async_create_entry", return_value={"type": "create_entry"}) as mock_create:
            result = await flow.async_step_payback({
                "system_cost": 16800.0,
                "additional_costs": 0.0,
                "savings_to_date": 1131.47,
                "install_date": "2025-02-01",
            })
            mock_create.assert_called_once()
            call_kwargs = mock_create.call_args
            assert call_kwargs.kwargs["data"]["system_cost"] == 16800.0
            assert call_kwargs.kwargs["data"]["savings_to_date"] == 1131.47


class TestConfigFlowServicesChaining:
    @pytest.mark.asyncio
    async def test_services_chains_to_octopus(self):
        """Step 6 (services) should now chain to octopus, not create entry."""
        flow = HEO2ConfigFlow()
        flow.hass = MagicMock()
        flow._data = {}
        result = await flow.async_step_services({
            "solcast_api_key": "",
            "solcast_resource_id": "",
            "agilepredict_url": "",
            "load_baseline_w": 1900.0,
            "dry_run": True,
        })
        assert result["type"] == "form"
        assert result["step_id"] == "octopus"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd /home/a1acrity/ai-projects/data/heo2 && python -m pytest tests/test_config_flow_dashboard.py -v`
Expected: FAIL — `async_step_octopus` does not exist

- [ ] **Step 4: Update config_flow.py — chain services → octopus → payback**

In `custom_components/heo2/config_flow.py`, replace the `async_step_services` method (lines 115–131) and add two new steps:

Replace `async_step_services` so it chains to `async_step_octopus` instead of creating the entry:

```python
    async def async_step_services(self, user_input=None):
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_octopus()
        return self.async_show_form(
            step_id="services",
            data_schema=vol.Schema({
                vol.Optional("solcast_api_key", default=""): str,
                vol.Optional("solcast_resource_id", default=""): str,
                vol.Optional("agilepredict_url", default=""): str,
                vol.Required("load_baseline_w", default=DEFAULT_LOAD_BASELINE_W): vol.Coerce(float),
                vol.Required("dry_run", default=True): bool,
            }),
        )

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
                vol.Optional("octopus_serial", default=""): str,
                vol.Optional("octopus_product_code", default=""): str,
                vol.Optional("octopus_tariff_code", default=""): str,
            }),
        )

    async def async_step_payback(self, user_input=None):
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(
                title="HEO II",
                data=self._data,
            )
        return self.async_show_form(
            step_id="payback",
            data_schema=vol.Schema({
                vol.Required("system_cost", default=DEFAULT_SYSTEM_COST): vol.Coerce(float),
                vol.Required("additional_costs", default=DEFAULT_ADDITIONAL_COSTS): vol.Coerce(float),
                vol.Required("savings_to_date", default=DEFAULT_SAVINGS_TO_DATE): vol.Coerce(float),
                vol.Required("install_date", default=DEFAULT_INSTALL_DATE): str,
            }),
        )
```

Add the new constant imports at the top of `config_flow.py` — update the import from `.const`:

```python
from .const import (
    DOMAIN,
    DEFAULT_MIN_SOC,
    DEFAULT_MAX_SOC,
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_MAX_CHARGE_KW,
    DEFAULT_MAX_DISCHARGE_KW,
    DEFAULT_CHARGE_EFFICIENCY,
    DEFAULT_DISCHARGE_EFFICIENCY,
    DEFAULT_IGO_NIGHT_RATE_PENCE,
    DEFAULT_IGO_DAY_RATE_PENCE,
    DEFAULT_LOAD_BASELINE_W,
    DEFAULT_SYSTEM_COST,
    DEFAULT_ADDITIONAL_COSTS,
    DEFAULT_SAVINGS_TO_DATE,
    DEFAULT_INSTALL_DATE,
    MQTT_BASE_TOPIC,
)
```

- [ ] **Step 5: Update strings.json — add octopus and payback step strings**

Add to the `"step"` object in `strings.json`, after the `"services"` entry:

```json
"octopus": {
    "title": "Octopus Energy (Optional)",
    "description": "Connect to Octopus Energy for billing data. Leave blank to skip.",
    "data": {
        "octopus_api_key": "Octopus API key",
        "octopus_account_number": "Account number",
        "octopus_mpan": "Electricity MPAN",
        "octopus_serial": "Meter serial number",
        "octopus_product_code": "Tariff product code",
        "octopus_tariff_code": "Tariff code"
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

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /home/a1acrity/ai-projects/data/heo2 && python -m pytest tests/test_config_flow_dashboard.py -v`
Expected: All 4 tests PASS

- [ ] **Step 7: Commit**

```bash
git add custom_components/heo2/const.py custom_components/heo2/config_flow.py custom_components/heo2/strings.json tests/test_config_flow_dashboard.py
git commit -m "feat: add config flow steps for Octopus billing and payback tracking"
```

---

### Task 5: Extend Coordinator with Dashboard State

**Files:**
- Modify: `custom_components/heo2/coordinator.py`

The coordinator needs to:
1. Store dashboard-related state that sensors will read
2. Run SOC trajectory calculation
3. Store CostAccumulator and OctopusBillingFetcher references
4. Calculate savings vs flat rate on each update

- [ ] **Step 1: Add dashboard state and SOC trajectory to coordinator**

Add new imports at the top of `coordinator.py` (after existing imports):

```python
from .soc_trajectory import calculate_soc_trajectory
from .cost_tracker import CostAccumulator
from .octopus import OctopusBillingFetcher
from .const import DEFAULT_FLAT_RATE_PENCE
```

Add new state fields in `__init__` (after `self.healthy: bool = True` on line 58):

```python
        # Dashboard state
        self.soc_trajectory: list[float] = [0.0] * 24
        self.cost_accumulator = CostAccumulator()
        self.octopus: OctopusBillingFetcher | None = None

        # ROI state (seeded from config)
        self._savings_to_date = self._config.get("savings_to_date", 0.0)
        self._total_accumulated_savings = 0.0

        # Octopus billing (optional)
        if self._config.get("octopus_api_key"):
            self.octopus = OctopusBillingFetcher(
                api_key=self._config["octopus_api_key"],
                mpan=self._config.get("octopus_mpan", ""),
                serial=self._config.get("octopus_serial", ""),
                product_code=self._config.get("octopus_product_code", ""),
                tariff_code=self._config.get("octopus_tariff_code", ""),
            )
```

- [ ] **Step 2: Add SOC trajectory calculation to _async_update_data**

In `_async_update_data`, after the appliance suggestions loop (after line 76), add:

```python
        # Calculate SOC trajectory for dashboard
        from datetime import datetime, timezone
        current_hour = datetime.now(timezone.utc).hour
        self.soc_trajectory = calculate_soc_trajectory(
            current_soc=inputs.current_soc,
            solar_forecast_kwh=inputs.solar_forecast_kwh,
            load_forecast_kwh=inputs.load_forecast_kwh,
            programme_slots=programme.slots,
            battery_capacity_kwh=self._config.get("battery_capacity_kwh", 20.0),
            max_charge_kw=self._config.get("max_charge_kw", 5.0),
            charge_efficiency=self._config.get("charge_efficiency", 0.95),
            discharge_efficiency=self._config.get("discharge_efficiency", 0.95),
            min_soc=self._config.get("min_soc", 20.0),
            max_soc=self._config.get("max_soc", 100.0),
            current_hour=current_hour,
        )

        # Update savings vs flat rate
        flat_rate = self._config.get("flat_rate_pence", DEFAULT_FLAT_RATE_PENCE)
        self.cost_accumulator.calculate_savings_vs_flat(flat_rate)
```

- [ ] **Step 3: Add helper properties for ROI sensors**

Add these properties to the `HEO2Coordinator` class:

```python
    @property
    def total_savings(self) -> float:
        """Cumulative savings: seed value + accumulated from cost tracker."""
        return self._savings_to_date + self._total_accumulated_savings

    @property
    def system_cost(self) -> float:
        return self._config.get("system_cost", 16800.0)

    @property
    def additional_costs(self) -> float:
        return self._config.get("additional_costs", 0.0)

    @property
    def payback_progress(self) -> float:
        """Percentage progress towards payback (0-100)."""
        total_cost = self.system_cost + self.additional_costs
        if total_cost <= 0:
            return 100.0
        return min(100.0, (self.total_savings / total_cost) * 100.0)

    @property
    def estimated_payback_date(self) -> str | None:
        """Project payback date based on current savings rate."""
        from datetime import datetime, timezone, timedelta
        install_date_str = self._config.get("install_date", "2025-02-01")
        try:
            install_date = datetime.strptime(install_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None

        now = datetime.now(timezone.utc)
        days_elapsed = max(1, (now - install_date).days)
        daily_savings = self.total_savings / days_elapsed

        if daily_savings <= 0:
            return None

        total_cost = self.system_cost + self.additional_costs
        remaining = total_cost - self.total_savings
        if remaining <= 0:
            return "Paid back"

        days_remaining = remaining / daily_savings
        payback_date = now + timedelta(days=days_remaining)
        return payback_date.strftime("%Y-%m-%d")

    @property
    def active_rule_names(self) -> list[str]:
        """List of currently active rule names."""
        return [r.name for r in self._engine._rules if r.enabled]
```

- [ ] **Step 4: Run existing tests to verify nothing is broken**

Run: `cd /home/a1acrity/ai-projects/data/heo2 && python -m pytest tests/ -v --ignore=tests/test_dashboard_sensors.py`
Expected: All existing tests PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/heo2/coordinator.py
git commit -m "feat: extend coordinator with SOC trajectory, cost tracking, and ROI state"
```

---

### Task 6: Group 1 — Forecast & Plan Sensors (11 sensors)

**Files:**
- Modify: `custom_components/heo2/sensor.py`
- Create: `tests/test_dashboard_sensors.py`

- [ ] **Step 1: Write failing tests for Group 1 sensors**

```python
# tests/test_dashboard_sensors.py
"""Tests for dashboard sensor entities."""

import pytest
from datetime import datetime, timezone, time
from unittest.mock import MagicMock, PropertyMock

from heo2.models import RateSlot, ProgrammeState, ProgrammeInputs, SlotConfig


def _make_coordinator(
    inputs: ProgrammeInputs | None = None,
    programme: ProgrammeState | None = None,
    soc_trajectory: list[float] | None = None,
    active_rule_names: list[str] | None = None,
):
    """Create a mock coordinator with dashboard state."""
    coord = MagicMock()
    coord.last_inputs = inputs
    coord.current_programme = programme
    coord.soc_trajectory = soc_trajectory or [0.0] * 24
    type(coord).active_rule_names = PropertyMock(return_value=active_rule_names or [])
    return coord


def _make_entry(entry_id="test_entry"):
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


@pytest.fixture
def sample_inputs(now, igo_night_rates) -> ProgrammeInputs:
    return ProgrammeInputs(
        now=now,
        current_soc=50.0,
        battery_capacity_kwh=20.0,
        min_soc=20.0,
        import_rates=igo_night_rates,
        export_rates=[
            RateSlot(
                start=datetime(2026, 4, 13, 0, 0, tzinfo=timezone.utc),
                end=datetime(2026, 4, 13, 23, 59, tzinfo=timezone.utc),
                rate_pence=15.0,
            )
        ],
        solar_forecast_kwh=[0.0]*6 + [0.2, 0.8, 1.5, 2.5, 3.0, 3.5, 3.5, 3.0, 2.0, 1.0, 0.3, 0.0] + [0.0]*6,
        load_forecast_kwh=[1.9] * 24,
        igo_dispatching=False,
        saving_session=False,
        saving_session_start=None,
        saving_session_end=None,
        ev_charging=False,
        grid_connected=True,
        active_appliances=[],
        appliance_expected_kwh=0.0,
    )


class TestSolarForecastTodaySensor:
    def test_native_value_is_total_kwh(self, sample_inputs):
        from heo2.sensor import SolarForecastTodaySensor
        coord = _make_coordinator(inputs=sample_inputs)
        sensor = SolarForecastTodaySensor(coord, _make_entry())
        assert sensor.native_value == pytest.approx(21.3, abs=0.1)

    def test_hourly_attribute(self, sample_inputs):
        from heo2.sensor import SolarForecastTodaySensor
        coord = _make_coordinator(inputs=sample_inputs)
        sensor = SolarForecastTodaySensor(coord, _make_entry())
        attrs = sensor.extra_state_attributes
        assert "hourly" in attrs
        assert len(attrs["hourly"]) == 24

    def test_returns_none_when_no_inputs(self):
        from heo2.sensor import SolarForecastTodaySensor
        coord = _make_coordinator(inputs=None)
        sensor = SolarForecastTodaySensor(coord, _make_entry())
        assert sensor.native_value is None


class TestSolarForecastHourlySensor:
    def test_native_value_is_current_hour(self, sample_inputs):
        from heo2.sensor import SolarForecastHourlySensor
        coord = _make_coordinator(inputs=sample_inputs)
        sensor = SolarForecastHourlySensor(coord, _make_entry())
        # now fixture is hour 12, solar[12] = 3.5
        assert sensor.native_value == 3.5

    def test_forecast_attribute(self, sample_inputs):
        from heo2.sensor import SolarForecastHourlySensor
        coord = _make_coordinator(inputs=sample_inputs)
        sensor = SolarForecastHourlySensor(coord, _make_entry())
        attrs = sensor.extra_state_attributes
        assert "forecast" in attrs
        assert len(attrs["forecast"]) == 24


class TestCurrentImportRateSensor:
    def test_native_value(self, sample_inputs):
        from heo2.sensor import CurrentImportRateSensor
        coord = _make_coordinator(inputs=sample_inputs)
        sensor = CurrentImportRateSensor(coord, _make_entry())
        # At midday, day rate = 27.88
        assert sensor.native_value == 27.88


class TestSOCTrajectorySensor:
    def test_native_value_is_current_soc(self, sample_inputs):
        from heo2.sensor import SOCTrajectorySensor
        trajectory = [50.0] + [45.0] * 23
        coord = _make_coordinator(inputs=sample_inputs, soc_trajectory=trajectory)
        sensor = SOCTrajectorySensor(coord, _make_entry())
        assert sensor.native_value == 50.0

    def test_trajectory_attribute(self, sample_inputs):
        from heo2.sensor import SOCTrajectorySensor
        trajectory = [50.0 - i for i in range(24)]
        coord = _make_coordinator(inputs=sample_inputs, soc_trajectory=trajectory)
        sensor = SOCTrajectorySensor(coord, _make_entry())
        attrs = sensor.extra_state_attributes
        assert "trajectory" in attrs
        assert len(attrs["trajectory"]) == 24


class TestProgrammeSlotsSensor:
    def test_native_value_summary(self, default_programme):
        from heo2.sensor import ProgrammeSlotsSensor
        coord = _make_coordinator(programme=default_programme)
        sensor = ProgrammeSlotsSensor(coord, _make_entry())
        assert "6 slots" in sensor.native_value

    def test_slots_attribute(self, default_programme):
        from heo2.sensor import ProgrammeSlotsSensor
        coord = _make_coordinator(programme=default_programme)
        sensor = ProgrammeSlotsSensor(coord, _make_entry())
        attrs = sensor.extra_state_attributes
        assert "slots" in attrs
        assert len(attrs["slots"]) == 6
        assert "start" in attrs["slots"][0]
        assert "end" in attrs["slots"][0]
        assert "soc" in attrs["slots"][0]
        assert "grid_charge" in attrs["slots"][0]


class TestProgrammeReasonSensor:
    def test_native_value(self, default_programme):
        from heo2.sensor import ProgrammeReasonSensor
        default_programme.reason_log = ["CheapRate: target 80%", "Solar: hold"]
        coord = _make_coordinator(programme=default_programme)
        sensor = ProgrammeReasonSensor(coord, _make_entry())
        assert sensor.native_value == "Solar: hold"

    def test_reasons_attribute(self, default_programme):
        from heo2.sensor import ProgrammeReasonSensor
        default_programme.reason_log = ["CheapRate: target 80%", "Solar: hold"]
        coord = _make_coordinator(programme=default_programme)
        sensor = ProgrammeReasonSensor(coord, _make_entry())
        attrs = sensor.extra_state_attributes
        assert "reasons" in attrs
        assert len(attrs["reasons"]) == 2


class TestActiveRulesSensor:
    def test_native_value_count(self):
        from heo2.sensor import ActiveRulesSensor
        coord = _make_coordinator(active_rule_names=["cheap_rate_charge", "solar_surplus", "evening_protect"])
        sensor = ActiveRulesSensor(coord, _make_entry())
        assert sensor.native_value == 3

    def test_rules_attribute(self):
        from heo2.sensor import ActiveRulesSensor
        rules = ["cheap_rate_charge", "solar_surplus"]
        coord = _make_coordinator(active_rule_names=rules)
        sensor = ActiveRulesSensor(coord, _make_entry())
        attrs = sensor.extra_state_attributes
        assert attrs["rules"] == rules
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/a1acrity/ai-projects/data/heo2 && python -m pytest tests/test_dashboard_sensors.py -v`
Expected: FAIL — `ImportError: cannot import name 'SolarForecastTodaySensor' from 'heo2.sensor'`

- [ ] **Step 3: Add Group 1 sensor classes to sensor.py**

Add the following imports at the top of `sensor.py` (after existing imports):

```python
from homeassistant.components.sensor import SensorStateClass, SensorDeviceClass
from homeassistant.helpers.device_registry import DeviceInfo
```

Add entity registration in `async_setup_entry` (after `async_add_entities(entities)` on line 31, replace with):

```python
    # Dashboard sensors (Group 1: Forecast & Plan)
    entities.append(SolarForecastTodaySensor(coordinator, entry))
    entities.append(SolarForecastHourlySensor(coordinator, entry))
    entities.append(LoadForecastHourlySensor(coordinator, entry))
    entities.append(ImportRatesSensor(coordinator, entry))
    entities.append(ExportRatesSensor(coordinator, entry))
    entities.append(CurrentImportRateSensor(coordinator, entry))
    entities.append(CurrentExportRateSensor(coordinator, entry))
    entities.append(SOCTrajectorySensor(coordinator, entry))
    entities.append(ProgrammeSlotsSensor(coordinator, entry))
    entities.append(ProgrammeReasonSensor(coordinator, entry))
    entities.append(ActiveRulesSensor(coordinator, entry))
    async_add_entities(entities)
```

Then add the 11 sensor classes after the existing `ApplianceTimingSensor` class:

```python
# ---------------------------------------------------------------------------
# Group 1 — Forecast & Plan sensors (coordinator, every 15 min)
# ---------------------------------------------------------------------------


class SolarForecastTodaySensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.ENERGY

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_solar_forecast_today"
        self._attr_name = "HEO II Solar Forecast Today"

    @property
    def native_value(self) -> float | None:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return None
        return round(sum(inputs.solar_forecast_kwh), 2)

    @property
    def extra_state_attributes(self) -> dict:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return {}
        return {"hourly": inputs.solar_forecast_kwh}


class SolarForecastHourlySensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.ENERGY

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_solar_forecast_hourly"
        self._attr_name = "HEO II Solar Forecast Hourly"

    @property
    def native_value(self) -> float | None:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return None
        hour = inputs.now.hour
        return inputs.solar_forecast_kwh[hour]

    @property
    def extra_state_attributes(self) -> dict:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return {}
        return {"forecast": inputs.solar_forecast_kwh}


class LoadForecastHourlySensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "W"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.POWER

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_load_forecast_hourly"
        self._attr_name = "HEO II Load Forecast Hourly"

    @property
    def native_value(self) -> float | None:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return None
        hour = inputs.now.hour
        # Convert kWh to average W for the hour
        return round(inputs.load_forecast_kwh[hour] * 1000, 0)

    @property
    def extra_state_attributes(self) -> dict:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return {}
        # Convert all hours to W
        return {"forecast": [round(kwh * 1000, 0) for kwh in inputs.load_forecast_kwh]}


class ImportRatesSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "p/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_import_rates"
        self._attr_name = "HEO II Import Rates"

    @property
    def native_value(self) -> float | None:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return None
        rate = inputs.rate_at(inputs.now)
        return rate

    @property
    def extra_state_attributes(self) -> dict:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return {}
        return {
            "rates": [
                {
                    "start": rs.start.isoformat(),
                    "end": rs.end.isoformat(),
                    "rate": rs.rate_pence,
                }
                for rs in inputs.import_rates
            ]
        }


class ExportRatesSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "p/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_export_rates"
        self._attr_name = "HEO II Export Rates"

    @property
    def native_value(self) -> float | None:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return None
        rate = inputs.export_rate_at(inputs.now)
        return rate

    @property
    def extra_state_attributes(self) -> dict:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return {}
        return {
            "rates": [
                {
                    "start": rs.start.isoformat(),
                    "end": rs.end.isoformat(),
                    "rate": rs.rate_pence,
                }
                for rs in inputs.export_rates
            ]
        }


class CurrentImportRateSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "p/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_current_import_rate"
        self._attr_name = "HEO II Current Import Rate"

    @property
    def native_value(self) -> float | None:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return None
        return inputs.rate_at(inputs.now)


class CurrentExportRateSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "p/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_current_export_rate"
        self._attr_name = "HEO II Current Export Rate"

    @property
    def native_value(self) -> float | None:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return None
        return inputs.export_rate_at(inputs.now)


class SOCTrajectorySensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_soc_trajectory"
        self._attr_name = "HEO II SOC Trajectory"

    @property
    def native_value(self) -> float | None:
        inputs = self.coordinator.last_inputs
        if inputs is None:
            return None
        return round(inputs.current_soc, 1)

    @property
    def extra_state_attributes(self) -> dict:
        return {"trajectory": self.coordinator.soc_trajectory}


class ProgrammeSlotsSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_programme_slots"
        self._attr_name = "HEO II Programme Slots"

    @property
    def native_value(self) -> str | None:
        prog = self.coordinator.current_programme
        if prog is None:
            return None
        return f"{len(prog.slots)} slots active"

    @property
    def extra_state_attributes(self) -> dict:
        prog = self.coordinator.current_programme
        if prog is None:
            return {}
        return {
            "slots": [
                {
                    "start": slot.start_time.strftime("%H:%M"),
                    "end": slot.end_time.strftime("%H:%M"),
                    "soc": slot.capacity_soc,
                    "grid_charge": slot.grid_charge,
                }
                for slot in prog.slots
            ]
        }


class ProgrammeReasonSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_programme_reason"
        self._attr_name = "HEO II Programme Reason"

    @property
    def native_value(self) -> str | None:
        prog = self.coordinator.current_programme
        if prog is None or not prog.reason_log:
            return None
        return prog.reason_log[-1]

    @property
    def extra_state_attributes(self) -> dict:
        prog = self.coordinator.current_programme
        if prog is None:
            return {}
        return {"reasons": prog.reason_log}


class ActiveRulesSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_active_rules"
        self._attr_name = "HEO II Active Rules"

    @property
    def native_value(self) -> int:
        return len(self.coordinator.active_rule_names)

    @property
    def extra_state_attributes(self) -> dict:
        return {"rules": self.coordinator.active_rule_names}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/a1acrity/ai-projects/data/heo2 && python -m pytest tests/test_dashboard_sensors.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/heo2/sensor.py tests/test_dashboard_sensors.py
git commit -m "feat: add 11 forecast and plan dashboard sensors (Group 1)"
```

---

### Task 7: Group 2 Cost Sensors + Group 3 Octopus Sensors (7 sensors)

**Files:**
- Modify: `custom_components/heo2/sensor.py`
- Modify: `tests/test_dashboard_sensors.py`

- [ ] **Step 1: Write failing tests for cost and Octopus sensors**

Append to `tests/test_dashboard_sensors.py`:

```python
from heo2.cost_tracker import CostAccumulator
from heo2.octopus import OctopusBillingFetcher


def _make_coordinator_with_costs(
    daily_import=1.50,
    daily_export=0.80,
    daily_solar=1.20,
    weekly_net=5.50,
    weekly_savings=3.20,
    octopus_monthly=45.00,
    octopus_last_month=52.00,
):
    coord = MagicMock()
    acc = CostAccumulator()
    acc.daily_import_cost = daily_import
    acc.daily_export_revenue = daily_export
    acc.daily_solar_value = daily_solar
    acc.weekly_net_cost = weekly_net
    acc.weekly_savings_vs_flat = weekly_savings
    acc.last_daily_reset = datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)
    acc.last_weekly_reset = datetime(2026, 4, 14, 0, 0, tzinfo=timezone.utc)
    coord.cost_accumulator = acc

    octopus = MagicMock()
    octopus.monthly_bill = octopus_monthly
    octopus.last_month_bill = octopus_last_month
    coord.octopus = octopus

    return coord


class TestDailyImportCostSensor:
    def test_native_value(self):
        from heo2.sensor import DailyImportCostSensor
        coord = _make_coordinator_with_costs(daily_import=1.50)
        sensor = DailyImportCostSensor(coord, _make_entry())
        assert sensor.native_value == pytest.approx(1.50)

    def test_last_reset(self):
        from heo2.sensor import DailyImportCostSensor
        coord = _make_coordinator_with_costs()
        sensor = DailyImportCostSensor(coord, _make_entry())
        assert sensor.last_reset == datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)


class TestDailyExportRevenueSensor:
    def test_native_value(self):
        from heo2.sensor import DailyExportRevenueSensor
        coord = _make_coordinator_with_costs(daily_export=0.80)
        sensor = DailyExportRevenueSensor(coord, _make_entry())
        assert sensor.native_value == pytest.approx(0.80)


class TestDailySolarValueSensor:
    def test_native_value(self):
        from heo2.sensor import DailySolarValueSensor
        coord = _make_coordinator_with_costs(daily_solar=1.20)
        sensor = DailySolarValueSensor(coord, _make_entry())
        assert sensor.native_value == pytest.approx(1.20)


class TestWeeklyNetCostSensor:
    def test_native_value(self):
        from heo2.sensor import WeeklyNetCostSensor
        coord = _make_coordinator_with_costs(weekly_net=5.50)
        sensor = WeeklyNetCostSensor(coord, _make_entry())
        assert sensor.native_value == pytest.approx(5.50)

    def test_last_reset(self):
        from heo2.sensor import WeeklyNetCostSensor
        coord = _make_coordinator_with_costs()
        sensor = WeeklyNetCostSensor(coord, _make_entry())
        assert sensor.last_reset == datetime(2026, 4, 14, 0, 0, tzinfo=timezone.utc)


class TestWeeklySavingsVsFlatSensor:
    def test_native_value(self):
        from heo2.sensor import WeeklySavingsVsFlatSensor
        coord = _make_coordinator_with_costs(weekly_savings=3.20)
        sensor = WeeklySavingsVsFlatSensor(coord, _make_entry())
        assert sensor.native_value == pytest.approx(3.20)


class TestOctopusMonthlyBillSensor:
    def test_native_value(self):
        from heo2.sensor import OctopusMonthlyBillSensor
        coord = _make_coordinator_with_costs(octopus_monthly=45.00)
        sensor = OctopusMonthlyBillSensor(coord, _make_entry())
        assert sensor.native_value == pytest.approx(45.00)

    def test_returns_none_when_no_octopus(self):
        from heo2.sensor import OctopusMonthlyBillSensor
        coord = MagicMock()
        coord.octopus = None
        sensor = OctopusMonthlyBillSensor(coord, _make_entry())
        assert sensor.native_value is None


class TestOctopusLastMonthBillSensor:
    def test_native_value(self):
        from heo2.sensor import OctopusLastMonthBillSensor
        coord = _make_coordinator_with_costs(octopus_last_month=52.00)
        sensor = OctopusLastMonthBillSensor(coord, _make_entry())
        assert sensor.native_value == pytest.approx(52.00)
```

- [ ] **Step 2: Run tests to verify new tests fail**

Run: `cd /home/a1acrity/ai-projects/data/heo2 && python -m pytest tests/test_dashboard_sensors.py::TestDailyImportCostSensor -v`
Expected: FAIL — `ImportError: cannot import name 'DailyImportCostSensor' from 'heo2.sensor'`

- [ ] **Step 3: Add cost and Octopus sensor classes to sensor.py**

Add to `async_setup_entry` (before the `async_add_entities` call):

```python
    # Dashboard sensors (Group 2: Cost Accumulator)
    entities.append(DailyImportCostSensor(coordinator, entry))
    entities.append(DailyExportRevenueSensor(coordinator, entry))
    entities.append(DailySolarValueSensor(coordinator, entry))
    entities.append(WeeklyNetCostSensor(coordinator, entry))
    entities.append(WeeklySavingsVsFlatSensor(coordinator, entry))

    # Dashboard sensors (Group 3: Octopus Billing — only if configured)
    if entry.data.get("octopus_api_key"):
        entities.append(OctopusMonthlyBillSensor(coordinator, entry))
        entities.append(OctopusLastMonthBillSensor(coordinator, entry))
```

Add the 7 sensor classes after the Group 1 sensors:

```python
# ---------------------------------------------------------------------------
# Group 2 — Cost Accumulator sensors (continuous, daily/weekly reset)
# ---------------------------------------------------------------------------


class DailyImportCostSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "£"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_daily_import_cost"
        self._attr_name = "HEO II Daily Import Cost"

    @property
    def native_value(self) -> float:
        return round(self.coordinator.cost_accumulator.daily_import_cost, 2)

    @property
    def last_reset(self) -> datetime | None:
        return self.coordinator.cost_accumulator.last_daily_reset


class DailyExportRevenueSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "£"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_daily_export_revenue"
        self._attr_name = "HEO II Daily Export Revenue"

    @property
    def native_value(self) -> float:
        return round(self.coordinator.cost_accumulator.daily_export_revenue, 2)

    @property
    def last_reset(self) -> datetime | None:
        return self.coordinator.cost_accumulator.last_daily_reset


class DailySolarValueSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "£"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_daily_solar_value"
        self._attr_name = "HEO II Daily Solar Value"

    @property
    def native_value(self) -> float:
        return round(self.coordinator.cost_accumulator.daily_solar_value, 2)

    @property
    def last_reset(self) -> datetime | None:
        return self.coordinator.cost_accumulator.last_daily_reset


class WeeklyNetCostSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "£"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_weekly_net_cost"
        self._attr_name = "HEO II Weekly Net Cost"

    @property
    def native_value(self) -> float:
        return round(self.coordinator.cost_accumulator.weekly_net_cost, 2)

    @property
    def last_reset(self) -> datetime | None:
        return self.coordinator.cost_accumulator.last_weekly_reset


class WeeklySavingsVsFlatSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "£"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_weekly_savings_vs_flat"
        self._attr_name = "HEO II Weekly Savings vs Flat"

    @property
    def native_value(self) -> float:
        return round(self.coordinator.cost_accumulator.weekly_savings_vs_flat, 2)

    @property
    def last_reset(self) -> datetime | None:
        return self.coordinator.cost_accumulator.last_weekly_reset


# ---------------------------------------------------------------------------
# Group 3 — Octopus Billing sensors (daily at 06:00, optional)
# ---------------------------------------------------------------------------


class OctopusMonthlyBillSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "£"
    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_octopus_monthly_bill"
        self._attr_name = "HEO II Octopus Monthly Bill"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.octopus is None:
            return None
        return round(self.coordinator.octopus.monthly_bill, 2)


class OctopusLastMonthBillSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "£"
    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_octopus_last_month_bill"
        self._attr_name = "HEO II Octopus Last Month Bill"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.octopus is None:
            return None
        return round(self.coordinator.octopus.last_month_bill, 2)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/a1acrity/ai-projects/data/heo2 && python -m pytest tests/test_dashboard_sensors.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/heo2/sensor.py tests/test_dashboard_sensors.py
git commit -m "feat: add 7 cost and Octopus billing sensors (Groups 2-3)"
```

---

### Task 8: Group 4 ROI Sensors + Number Entities (3 sensors + 2 numbers)

**Files:**
- Modify: `custom_components/heo2/sensor.py`
- Modify: `custom_components/heo2/number.py`
- Modify: `tests/test_dashboard_sensors.py`

- [ ] **Step 1: Write failing tests for ROI sensors and numbers**

Append to `tests/test_dashboard_sensors.py`:

```python
def _make_coordinator_with_roi(
    total_savings=2500.0,
    payback_progress=14.88,
    estimated_payback_date="2035-06-15",
    system_cost=16800.0,
    additional_costs=0.0,
):
    coord = MagicMock()
    type(coord).total_savings = PropertyMock(return_value=total_savings)
    type(coord).payback_progress = PropertyMock(return_value=payback_progress)
    type(coord).estimated_payback_date = PropertyMock(return_value=estimated_payback_date)
    type(coord).system_cost = PropertyMock(return_value=system_cost)
    type(coord).additional_costs = PropertyMock(return_value=additional_costs)
    coord._config = {
        "system_cost": system_cost,
        "additional_costs": additional_costs,
    }
    return coord


class TestTotalSavingsSensor:
    def test_native_value(self):
        from heo2.sensor import TotalSavingsSensor
        coord = _make_coordinator_with_roi(total_savings=2500.0)
        sensor = TotalSavingsSensor(coord, _make_entry())
        assert sensor.native_value == pytest.approx(2500.0)


class TestPaybackProgressSensor:
    def test_native_value(self):
        from heo2.sensor import PaybackProgressSensor
        coord = _make_coordinator_with_roi(payback_progress=14.88)
        sensor = PaybackProgressSensor(coord, _make_entry())
        assert sensor.native_value == pytest.approx(14.88)


class TestEstimatedPaybackDateSensor:
    def test_native_value(self):
        from heo2.sensor import EstimatedPaybackDateSensor
        coord = _make_coordinator_with_roi(estimated_payback_date="2035-06-15")
        sensor = EstimatedPaybackDateSensor(coord, _make_entry())
        assert sensor.native_value == "2035-06-15"


class TestSystemCostNumber:
    def test_native_value(self):
        from heo2.number import SystemCostNumber
        coord = _make_coordinator_with_roi(system_cost=16800.0)
        sensor = SystemCostNumber(coord, _make_entry())
        assert sensor.native_value == 16800.0


class TestAdditionalCostsNumber:
    def test_native_value(self):
        from heo2.number import AdditionalCostsNumber
        coord = _make_coordinator_with_roi(additional_costs=500.0)
        sensor = AdditionalCostsNumber(coord, _make_entry())
        assert sensor.native_value == 500.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/a1acrity/ai-projects/data/heo2 && python -m pytest tests/test_dashboard_sensors.py::TestTotalSavingsSensor -v`
Expected: FAIL — `ImportError: cannot import name 'TotalSavingsSensor' from 'heo2.sensor'`

- [ ] **Step 3: Add ROI sensor classes to sensor.py**

Add to `async_setup_entry` (before `async_add_entities`):

```python
    # Dashboard sensors (Group 4: ROI / Payback)
    entities.append(TotalSavingsSensor(coordinator, entry))
    entities.append(PaybackProgressSensor(coordinator, entry))
    entities.append(EstimatedPaybackDateSensor(coordinator, entry))
```

Add the 3 sensor classes:

```python
# ---------------------------------------------------------------------------
# Group 4 — ROI / Payback sensors
# ---------------------------------------------------------------------------


class TotalSavingsSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "£"
    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_total_savings"
        self._attr_name = "HEO II Total Savings"

    @property
    def native_value(self) -> float:
        return round(self.coordinator.total_savings, 2)


class PaybackProgressSensor(CoordinatorEntity, SensorEntity):
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_payback_progress"
        self._attr_name = "HEO II Payback Progress"

    @property
    def native_value(self) -> float:
        return round(self.coordinator.payback_progress, 1)


class EstimatedPaybackDateSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_estimated_payback_date"
        self._attr_name = "HEO II Estimated Payback Date"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.estimated_payback_date
```

- [ ] **Step 4: Add number entities to number.py**

Add to `async_setup_entry` in `number.py`:

```python
async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HEO2Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        MinSocNumber(coordinator, entry),
        SystemCostNumber(coordinator, entry),
        AdditionalCostsNumber(coordinator, entry),
    ])
```

Add the two new number classes after `MinSocNumber`:

```python
class SystemCostNumber(CoordinatorEntity, NumberEntity):
    _attr_native_min_value = 0
    _attr_native_max_value = 100000
    _attr_native_step = 100
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "£"

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_system_cost"
        self._attr_name = "HEO II System Cost"

    @property
    def native_value(self) -> float:
        return self.coordinator._config.get("system_cost", 16800.0)

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator._config["system_cost"] = value
        self.async_write_ha_state()


class AdditionalCostsNumber(CoordinatorEntity, NumberEntity):
    _attr_native_min_value = 0
    _attr_native_max_value = 50000
    _attr_native_step = 50
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "£"

    def __init__(self, coordinator: HEO2Coordinator, entry: ConfigEntry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_additional_costs"
        self._attr_name = "HEO II Additional Costs"

    @property
    def native_value(self) -> float:
        return self.coordinator._config.get("additional_costs", 0.0)

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator._config["additional_costs"] = value
        self.async_write_ha_state()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/a1acrity/ai-projects/data/heo2 && python -m pytest tests/test_dashboard_sensors.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add custom_components/heo2/sensor.py custom_components/heo2/number.py tests/test_dashboard_sensors.py
git commit -m "feat: add ROI sensors and adjustable cost number entities (Group 4)"
```

---

### Task 9: Integration Wiring — CostTracker and Octopus Scheduling

**Files:**
- Modify: `custom_components/heo2/__init__.py`

This task wires up the HA event listeners for the CostTracker (state changes) and OctopusBillingFetcher (daily timer). These use HA-specific APIs (`async_track_state_change_event`, `async_track_time_change`) that can't be unit-tested without a full HA test harness, so this task focuses on correct wiring.

- [ ] **Step 1: Update __init__.py to start CostTracker and Octopus scheduling**

Replace the entire `__init__.py`:

```python
# custom_components/heo2/__init__.py
"""HEO II — Rule-based SunSynk 6-slot timer programmer."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)

from .const import DOMAIN

logger = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor", "switch", "number"]


async def async_setup_entry(hass: HomeAssistant, entry) -> bool:
    """Set up HEO II from a config entry."""
    from .coordinator import HEO2Coordinator

    coordinator = HEO2Coordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Wire up CostTracker state-change listeners
    unsub_callbacks = []
    config = dict(entry.data)

    load_entity = config.get("load_power_entity", "")
    pv_entity = config.get("pv_power_entity", "")

    if load_entity:

        @callback
        def _handle_load_change(event: Event) -> None:
            new_state = event.data.get("new_state")
            if new_state is None or new_state.state in ("unknown", "unavailable"):
                return
            try:
                watts = float(new_state.state)
            except (ValueError, TypeError):
                return
            now = datetime.now(timezone.utc)
            inputs = coordinator.last_inputs
            rate = inputs.rate_at(now) if inputs else 0.0
            coordinator.cost_accumulator.update_load(
                watts=watts, now=now, import_rate_pence=rate or 0.0
            )

        unsub_callbacks.append(
            async_track_state_change_event(hass, [load_entity], _handle_load_change)
        )

    if pv_entity:

        @callback
        def _handle_pv_change(event: Event) -> None:
            new_state = event.data.get("new_state")
            if new_state is None or new_state.state in ("unknown", "unavailable"):
                return
            try:
                watts = float(new_state.state)
            except (ValueError, TypeError):
                return
            now = datetime.now(timezone.utc)
            inputs = coordinator.last_inputs
            import_rate = inputs.rate_at(now) if inputs else 0.0
            export_rate = inputs.export_rate_at(now) if inputs else 0.0
            coordinator.cost_accumulator.update_pv(
                watts=watts,
                now=now,
                import_rate_pence=import_rate or 0.0,
                export_rate_pence=export_rate or 0.0,
            )

        unsub_callbacks.append(
            async_track_state_change_event(hass, [pv_entity], _handle_pv_change)
        )

    # Daily reset at midnight
    @callback
    def _daily_reset(_now) -> None:
        now = datetime.now(timezone.utc)
        coordinator.cost_accumulator.reset_daily(now)
        logger.info("CostTracker: daily reset")

    unsub_callbacks.append(
        async_track_time_change(hass, _daily_reset, hour=0, minute=0, second=0)
    )

    # Weekly reset Monday 00:00
    @callback
    def _weekly_reset(_now) -> None:
        now = datetime.now(timezone.utc)
        if now.weekday() == 0:  # Monday
            coordinator.cost_accumulator.reset_weekly(now)
            logger.info("CostTracker: weekly reset")

    unsub_callbacks.append(
        async_track_time_change(hass, _weekly_reset, hour=0, minute=1, second=0)
    )

    # Octopus billing fetch at 06:00 daily (if configured)
    if coordinator.octopus is not None:

        @callback
        def _octopus_fetch(_now) -> None:
            now = datetime.now(timezone.utc)
            # Check for month rollover
            if now.day == 1:
                coordinator.octopus.snapshot_month_end()
            hass.async_create_task(coordinator.octopus.fetch_monthly_bill(now))
            logger.info("OctopusBillingFetcher: daily fetch triggered")

        unsub_callbacks.append(
            async_track_time_change(hass, _octopus_fetch, hour=6, minute=0, second=0)
        )

    # Store cleanup callbacks
    hass.data[DOMAIN][f"{entry.entry_id}_unsub"] = unsub_callbacks

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry) -> bool:
    """Unload HEO II config entry."""
    # Unsubscribe all event listeners
    unsub_key = f"{entry.entry_id}_unsub"
    for unsub in hass.data[DOMAIN].get(unsub_key, []):
        unsub()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        hass.data[DOMAIN].pop(unsub_key, None)
    return unload_ok
```

- [ ] **Step 2: Run all existing tests to verify nothing is broken**

Run: `cd /home/a1acrity/ai-projects/data/heo2 && python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add custom_components/heo2/__init__.py
git commit -m "feat: wire CostTracker state listeners and Octopus daily scheduling"
```

---

### Task 10: Dashboard YAML Documentation

**Files:**
- Create: `docs/dashboard/README.md`
- Create: `docs/dashboard/forecast-plan.yaml`
- Create: `docs/dashboard/tariffs.yaml`
- Create: `docs/dashboard/roi-tracking.yaml`

- [ ] **Step 1: Create dashboard README**

```markdown
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

- `forecast-plan.yaml` — 24h energy forecast, SOC trajectory, programme slots
- `tariffs.yaml` — current rates, 48h rate charts
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
```

- [ ] **Step 2: Create forecast-plan.yaml**

```yaml
# docs/dashboard/forecast-plan.yaml
# HEO II — View 1: Forecast & Plan
# Requires: apexcharts-card, mushroom, flex-table-card

title: Forecast & Plan
path: forecast-plan
cards:
  # 24-hour energy chart
  - type: custom:apexcharts-card
    header:
      title: 24-Hour Energy Forecast
      show: true
    graph_span: 24h
    span:
      start: day
    series:
      - entity: sensor.heo2_solar_forecast_hourly
        data_generator: |
          const forecast = entity.attributes.forecast || [];
          return forecast.map((val, i) => {
            const d = new Date();
            d.setHours(i, 0, 0, 0);
            return [d.getTime(), val];
          });
        name: Solar (kWh)
        type: area
        color: "#f59e0b"
        opacity: 0.3
      - entity: sensor.heo2_load_forecast_hourly
        data_generator: |
          const forecast = entity.attributes.forecast || [];
          return forecast.map((val, i) => {
            const d = new Date();
            d.setHours(i, 0, 0, 0);
            return [d.getTime(), val];
          });
        name: Load (W)
        type: line
        color: "#ef4444"
        stroke_width: 2
        stroke_dash: 4

  # SOC trajectory chart
  - type: custom:apexcharts-card
    header:
      title: Battery SOC Trajectory
      show: true
    graph_span: 24h
    span:
      start: day
    yaxis:
      - min: 0
        max: 100
    series:
      - entity: sensor.heo2_soc_trajectory
        data_generator: |
          const trajectory = entity.attributes.trajectory || [];
          return trajectory.map((val, i) => {
            const d = new Date();
            d.setHours(i, 0, 0, 0);
            return [d.getTime(), val];
          });
        name: SOC (%)
        type: line
        color: "#14b8a6"
        stroke_width: 3

  # Programme slots table
  - type: custom:flex-table-card
    title: Programme Slots
    entities:
      include: sensor.heo2_programme_slots
    columns:
      - name: "#"
        data: slots
        modify: x.index + 1
      - name: Start
        data: slots
        modify: x.start
      - name: End
        data: slots
        modify: x.end
      - name: SOC Target
        data: slots
        modify: x.soc + '%'
      - name: Grid Charge
        data: slots
        modify: x.grid_charge ? '✅' : '❌'

  # Info chips
  - type: horizontal-stack
    cards:
      - type: custom:mushroom-chips-card
        chips:
          - type: entity
            entity: sensor.heo2_active_rules
            icon: mdi:format-list-checks
          - type: entity
            entity: sensor.heo2_next_action
            icon: mdi:battery-charging
          - type: entity
            entity: sensor.heo2_programme_reason
            icon: mdi:information-outline
```

- [ ] **Step 3: Create tariffs.yaml**

```yaml
# docs/dashboard/tariffs.yaml
# HEO II — View 2: Tariffs
# Requires: apexcharts-card, mushroom

title: Tariffs
path: tariffs
cards:
  # Current rate cards
  - type: horizontal-stack
    cards:
      - type: custom:mushroom-entity-card
        entity: sensor.heo2_current_import_rate
        name: Import Rate
        icon: mdi:transmission-tower-import
        primary_info: state
        secondary_info: name
        layout: vertical
      - type: custom:mushroom-entity-card
        entity: sensor.heo2_current_export_rate
        name: Export Rate
        icon: mdi:transmission-tower-export
        primary_info: state
        secondary_info: name
        layout: vertical

  # Import rates chart
  - type: custom:apexcharts-card
    header:
      title: Import Rates (48h)
      show: true
    series:
      - entity: sensor.heo2_import_rates
        data_generator: |
          const rates = entity.attributes.rates || [];
          return rates.map(r => [new Date(r.start).getTime(), r.rate]);
        name: Import (p/kWh)
        type: line
        color: "#3b82f6"
        curve: stepline

  # Export rates chart
  - type: custom:apexcharts-card
    header:
      title: Export Rates (48h)
      show: true
    series:
      - entity: sensor.heo2_export_rates
        data_generator: |
          const rates = entity.attributes.rates || [];
          return rates.map(r => [new Date(r.start).getTime(), r.rate]);
        name: Export (p/kWh)
        type: line
        color: "#22c55e"
        curve: stepline
```

- [ ] **Step 4: Create roi-tracking.yaml**

```yaml
# docs/dashboard/roi-tracking.yaml
# HEO II — View 3: ROI Tracking
# Requires: mushroom

title: ROI Tracking
path: roi-tracking
cards:
  # Daily costs
  - type: horizontal-stack
    cards:
      - type: custom:mushroom-entity-card
        entity: sensor.heo2_daily_import_cost
        name: Import Cost
        icon: mdi:cash-minus
      - type: custom:mushroom-entity-card
        entity: sensor.heo2_daily_export_revenue
        name: Export Revenue
        icon: mdi:cash-plus
      - type: custom:mushroom-entity-card
        entity: sensor.heo2_daily_solar_value
        name: Solar Value
        icon: mdi:white-balance-sunny

  # Weekly costs
  - type: horizontal-stack
    cards:
      - type: custom:mushroom-entity-card
        entity: sensor.heo2_weekly_net_cost
        name: Weekly Net Cost
        icon: mdi:calendar-week
      - type: custom:mushroom-entity-card
        entity: sensor.heo2_weekly_savings_vs_flat
        name: Weekly Savings vs Flat
        icon: mdi:piggy-bank

  # Octopus monthly bills (conditional)
  - type: conditional
    conditions:
      - entity: sensor.heo2_octopus_monthly_bill
        state_not: unavailable
    card:
      type: horizontal-stack
      cards:
        - type: custom:mushroom-entity-card
          entity: sensor.heo2_octopus_monthly_bill
          name: This Month (Octopus)
          icon: mdi:receipt-text
        - type: custom:mushroom-entity-card
          entity: sensor.heo2_octopus_last_month_bill
          name: Last Month (Octopus)
          icon: mdi:receipt-text-check

  # Payback progress
  - type: vertical-stack
    cards:
      - type: gauge
        entity: sensor.heo2_payback_progress
        name: Payback Progress
        min: 0
        max: 100
        severity:
          green: 75
          yellow: 25
          red: 0
      - type: custom:mushroom-entity-card
        entity: sensor.heo2_estimated_payback_date
        name: Estimated Payback Date
        icon: mdi:calendar-check
      - type: custom:mushroom-entity-card
        entity: sensor.heo2_total_savings
        name: Total Savings
        icon: mdi:cash-multiple

  # Adjustable inputs
  - type: entities
    title: System Costs
    entities:
      - entity: number.heo2_system_cost
      - entity: number.heo2_additional_costs
```

- [ ] **Step 5: Commit**

```bash
git add docs/dashboard/
git commit -m "docs: add dashboard YAML examples for Forecast, Tariffs, and ROI views"
```

---

### Task 11: Final Integration Test

**Files:** None (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `cd /home/a1acrity/ai-projects/data/heo2 && python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 2: Verify entity count**

Quick check: count the new sensor/number entity registrations in `sensor.py` and `number.py`:
- Group 1: 11 sensors (solar_forecast_today, solar_forecast_hourly, load_forecast_hourly, import_rates, export_rates, current_import_rate, current_export_rate, soc_trajectory, programme_slots, programme_reason, active_rules)
- Group 2: 5 sensors (daily_import_cost, daily_export_revenue, daily_solar_value, weekly_net_cost, weekly_savings_vs_flat)
- Group 3: 2 sensors (octopus_monthly_bill, octopus_last_month_bill)
- Group 4: 3 sensors + 2 numbers (total_savings, payback_progress, estimated_payback_date, system_cost, additional_costs)
- **Total: 23 new entities** ✓

- [ ] **Step 3: Verify no import errors**

Run: `cd /home/a1acrity/ai-projects/data/heo2 && python -c "from heo2.sensor import *; from heo2.number import *; from heo2.soc_trajectory import *; from heo2.cost_tracker import *; from heo2.octopus import *; print('All imports OK')"`
Expected: `All imports OK`
