"""Energy cost accumulator. No Home Assistant imports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CostAccumulator:
    """Accumulates energy x rate for daily/weekly cost tracking."""

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

    def update_load(self, watts: float, now: datetime, import_rate_pence: float) -> None:
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

    def update_pv(self, watts: float, now: datetime, import_rate_pence: float, export_rate_pence: float) -> None:
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
        """Calculate weekly savings compared to a flat tariff."""
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
