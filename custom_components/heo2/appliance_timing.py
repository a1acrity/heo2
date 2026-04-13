# custom_components/heo2/appliance_timing.py
"""Appliance timing suggestion calculator. No Home Assistant imports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

from .models import ProgrammeInputs


@dataclass
class ApplianceSuggestion:
    """Recommended run window for an appliance."""
    appliance_name: str
    start_hour: int
    duration_hours: int
    reason: str  # "solar_surplus" | "cheap_rate" | "no_good_window"
    solar_coverage_pct: float  # 0–100
    estimated_cost_pence: float
    draw_kw: float


class ApplianceTimingCalculator:
    """Find optimal run windows for household appliances."""

    def best_window(
        self,
        inputs: ProgrammeInputs,
        draw_kw: float,
        duration_hours: int,
        appliance_name: str,
    ) -> ApplianceSuggestion:
        """Find the best window in the next 24 hours.

        Priority: solar surplus first, then cheapest import rate.
        """
        candidates: list[tuple[int, float, float, str]] = []

        for start in range(24):
            end = start + duration_hours
            if end > 24:
                continue

            solar_in_window = sum(
                inputs.solar_forecast_kwh[h] for h in range(start, end)
            )
            load_in_window = sum(
                inputs.load_forecast_kwh[h] for h in range(start, end)
            )
            available_solar = max(0, solar_in_window - load_in_window)
            needed_kwh = draw_kw * duration_hours
            solar_coverage = min(1.0, available_solar / needed_kwh) if needed_kwh > 0 else 0.0

            uncovered_kwh = needed_kwh * (1 - solar_coverage)
            avg_rate = self._avg_import_rate(inputs, start, end)
            cost = uncovered_kwh * avg_rate if avg_rate is not None else float("inf")

            reason = "solar_surplus" if solar_coverage > 0.5 else "cheap_rate"
            candidates.append((start, cost, solar_coverage * 100, reason))

        if not candidates:
            return ApplianceSuggestion(
                appliance_name=appliance_name,
                start_hour=0,
                duration_hours=duration_hours,
                reason="no_good_window",
                solar_coverage_pct=0.0,
                estimated_cost_pence=0.0,
                draw_kw=draw_kw,
            )

        if appliance_name == "ev":
            candidates.sort(key=lambda c: (-c[2], c[1]))
        else:
            candidates.sort(key=lambda c: (0 if c[3] == "solar_surplus" else 1, c[1], -c[2]))

        best = candidates[0]
        reason = best[3] if best[1] < float("inf") else "no_good_window"

        return ApplianceSuggestion(
            appliance_name=appliance_name,
            start_hour=best[0],
            duration_hours=duration_hours,
            reason=reason,
            solar_coverage_pct=best[2],
            estimated_cost_pence=best[1] if best[1] < float("inf") else 0.0,
            draw_kw=draw_kw,
        )

    def _avg_import_rate(
        self, inputs: ProgrammeInputs, start_hour: int, end_hour: int
    ) -> float | None:
        """Average import rate across hours in the window.

        Hours that wrap past midnight (i.e. start_hour >= 23 meaning they
        effectively run into the next day) use tomorrow's date so that
        overnight rate slots are found correctly.
        """
        rates = []
        base = inputs.now.replace(hour=0, minute=0, second=0, microsecond=0)
        for h in range(start_hour, end_hour):
            # Check rate at the midpoint of the hour (h:30) so that overnight
            # slots starting at e.g. 23:30 are correctly attributed to hour 23.
            hour_dt = base + timedelta(hours=h, minutes=30)
            rate = inputs.rate_at(hour_dt)
            if rate is not None:
                rates.append(rate)
        return sum(rates) / len(rates) if rates else None
