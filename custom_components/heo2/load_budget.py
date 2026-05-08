"""Shared load-budget calculations for the rules engine.

ExportWindowRule (drain target floor) and EveningProtectRule (required
SOC) both ask the same question: "what SOC does the battery need to
hold to ride out the evening peak window from battery alone, without
importing at peak rates?"

Pre-Phase 3 PR 3 the formula was duplicated in both rules with subtle
differences (one had a divide-by-zero guard, the other didn't; one
applied an extra `max(..., min_soc)` clamp). This module is the
single source of truth.

Usage:

    from .load_budget import evening_floor_soc

    floor = evening_floor_soc(inputs)              # defaults 18..24
    floor = evening_floor_soc(inputs, start_hour=17, end_hour=23)
"""

from __future__ import annotations

from .models import ProgrammeInputs


def evening_demand_kwh(
    inputs: ProgrammeInputs,
    *,
    start_hour: int = 18,
    end_hour: int = 24,
) -> float:
    """Sum the load forecast across the evening peak window.

    `start_hour` and `end_hour` are 24h clock indices into
    `inputs.load_forecast_kwh` (which is 24-hour, index 0 = 00:00).
    Default 18..24 covers the 18:00-00:00 evening peak that drives
    HEO II's drain-then-refill cycle.
    """
    return inputs.load_kwh_between(start_hour, end_hour)


def evening_floor_soc(
    inputs: ProgrammeInputs,
    *,
    start_hour: int = 18,
    end_hour: int = 24,
) -> int:
    """Minimum slot SOC required to cover the evening peak window
    from battery alone.

    Returns `min_soc + (evening_demand_kwh / battery_capacity_kwh * 100)`,
    clamped to `[min_soc, 100]`. Battery capacity of 0 (degenerate
    test setup) returns `min_soc` rather than dividing.

    Used by:
      * ExportWindowRule - sets the floor on the drain target so
        a worth-selling export window doesn't leave the battery
        empty entering the evening peak.
      * EveningProtectRule - raises a non-GC slot covering the
        evening boundary if its current target is below this floor.
    """
    if inputs.battery_capacity_kwh <= 0:
        return int(inputs.min_soc)
    demand = evening_demand_kwh(inputs, start_hour=start_hour, end_hour=end_hour)
    pct = demand / inputs.battery_capacity_kwh * 100
    target = int(inputs.min_soc + pct)
    return min(100, max(int(inputs.min_soc), target))
