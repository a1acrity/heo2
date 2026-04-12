"""Shared test fixtures for HEO II."""

import pytest
from datetime import time, datetime, timezone

from heo2.models import (
    RateSlot,
    SlotConfig,
    ProgrammeState,
    ProgrammeInputs,
)


@pytest.fixture
def now() -> datetime:
    """Midday on a weekday."""
    return datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def igo_night_rates() -> list[RateSlot]:
    """Standard IGO night rate 23:30-05:30 at 7p, day rate 27.88p."""
    slots = []
    # Day rate: 05:30-23:30
    slots.append(RateSlot(
        start=datetime(2026, 4, 13, 5, 30, tzinfo=timezone.utc),
        end=datetime(2026, 4, 13, 23, 30, tzinfo=timezone.utc),
        rate_pence=27.88,
    ))
    # Night rate: 23:30-05:30 (next day)
    slots.append(RateSlot(
        start=datetime(2026, 4, 13, 23, 30, tzinfo=timezone.utc),
        end=datetime(2026, 4, 14, 5, 30, tzinfo=timezone.utc),
        rate_pence=7.0,
    ))
    return slots


@pytest.fixture
def default_inputs(now, igo_night_rates) -> ProgrammeInputs:
    """Typical midday inputs: 50% SOC, no events, flat load, no solar."""
    return ProgrammeInputs(
        now=now,
        current_soc=50.0,
        battery_capacity_kwh=20.0,
        min_soc=20.0,
        import_rates=igo_night_rates,
        export_rates=[],
        solar_forecast_kwh=[0.0] * 24,
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


@pytest.fixture
def default_programme() -> ProgrammeState:
    """Default 6-slot programme at min_soc=20."""
    return ProgrammeState.default(min_soc=20)
