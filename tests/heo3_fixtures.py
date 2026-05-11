"""Shared fixtures for HEO III tests — synthetic Snapshot builder.

Lets compute / build tests construct realistic Snapshots without
spinning up the operator or any adapters.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from heo3.types import (
    ApplianceState,
    EVState,
    InverterSettings,
    InverterState,
    LiveRates,
    LoadForecast,
    PredictedRates,
    RatePeriod,
    SlotSettings,
    SolarForecast,
    Snapshot,
    SystemConfig,
    SystemFlags,
    TeslaState,
)


TZ = ZoneInfo("Europe/London")


def baseline_settings(
    work_mode: str = "Zero export to CT",
    energy_pattern: str = "Load first",
) -> InverterSettings:
    slots = tuple(
        SlotSettings(
            start_hhmm=f"{h:02d}:00", grid_charge=False, capacity_pct=50
        )
        for h in (0, 5, 11, 16, 19, 22)
    )
    return InverterSettings(
        work_mode=work_mode,
        energy_pattern=energy_pattern,
        max_charge_a=100.0,
        max_discharge_a=100.0,
        slots=slots,
    )


def cheap_then_peak_rates(
    base_date: datetime,
) -> tuple[tuple[RatePeriod, ...], tuple[RatePeriod, ...]]:
    """Build a 24-hour import-rate profile with a clear cheap window
    (00:00-05:30 at ~5p) and a clear peak window (16:00-19:00 at ~30p).
    Off-peak otherwise (~15p). Returns (today, tomorrow). Tomorrow is
    a flat copy.
    """
    base = base_date.replace(hour=0, minute=0, second=0, microsecond=0)
    today: list[RatePeriod] = []
    for slot_idx in range(48):  # 48 × 30-min = 24h
        start = base + timedelta(minutes=30 * slot_idx)
        end = start + timedelta(minutes=30)
        h = start.hour
        if h < 5 or (h == 5 and start.minute == 0):
            rate = 5.0
        elif 16 <= h < 19:
            rate = 30.0
        else:
            rate = 15.0
        today.append(RatePeriod(start=start, end=end, rate_pence=rate))

    tomorrow_base = base + timedelta(days=1)
    tomorrow = [
        RatePeriod(
            start=p.start + timedelta(days=1),
            end=p.end + timedelta(days=1),
            rate_pence=p.rate_pence,
        )
        for p in today
    ]
    return tuple(today), tuple(tomorrow)


def make_snapshot(
    *,
    captured_at: datetime | None = None,
    soc_pct: float | None = 50.0,
    battery_voltage_v: float | None = 51.2,
    grid_voltage_v: float | None = 240.0,
    work_mode: str = "Zero export to CT",
    today_load_kwh: tuple[float, ...] = (0.5,) * 24,
    tomorrow_load_kwh: tuple[float, ...] = (0.5,) * 24,
    today_solar_kwh: tuple[float, ...] = (0.0,) * 24,
    tomorrow_solar_kwh: tuple[float, ...] = (0.0,) * 24,
    rates: tuple[tuple[RatePeriod, ...], tuple[RatePeriod, ...]] | None = None,
    export_today: tuple[RatePeriod, ...] = (),
    export_tomorrow: tuple[RatePeriod, ...] = (),
    eps_active: bool = False,
    config: SystemConfig | None = None,
) -> Snapshot:
    """Construct a synthetic Snapshot with sensible defaults.

    Override only the fields the test cares about; the rest get
    realistic defaults (50% SOC, 240V grid, no solar, light flat
    load, no rates unless you pass them).
    """
    if captured_at is None:
        captured_at = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)

    if rates is None:
        import_today, import_tomorrow = cheap_then_peak_rates(captured_at)
    else:
        import_today, import_tomorrow = rates

    return Snapshot(
        captured_at=captured_at,
        local_tz=TZ,
        inverter=InverterState(
            battery_soc_pct=soc_pct,
            battery_voltage_v=battery_voltage_v,
            grid_voltage_v=grid_voltage_v,
        ),
        inverter_settings=baseline_settings(work_mode=work_mode),
        ev=EVState(),
        tesla=TeslaState(),
        appliances=ApplianceState(),
        rates_live=LiveRates(
            import_today=import_today,
            import_tomorrow=import_tomorrow,
            export_today=export_today,
            export_tomorrow=export_tomorrow,
        ),
        rates_predicted=PredictedRates(),
        rates_freshness={"import_today": captured_at},
        solar_forecast=SolarForecast(
            today_p50_kwh=today_solar_kwh,
            tomorrow_p50_kwh=tomorrow_solar_kwh,
        ),
        load_forecast=LoadForecast(
            today_hourly_kwh=today_load_kwh,
            tomorrow_hourly_kwh=tomorrow_load_kwh,
        ),
        flags=SystemFlags(eps_active=eps_active, grid_connected=not eps_active),
        config=config or SystemConfig(),
    )
