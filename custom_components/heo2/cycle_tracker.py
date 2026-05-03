# custom_components/heo2/cycle_tracker.py
"""H7 battery cycle budget tracking. No HA imports.

A "cycle" is one full charge + discharge of the battery (so 1 kWh
discharged from a 20 kWh battery = 0.05 cycles). The standard
industry definition counts only discharge throughput -- charging is
the means, discharging is what wears the cells.

Inputs:
  * Cumulative battery-out energy counter (kWh, monotonic since some
    epoch). SA exposes
    `sensor.solar_assistant_solar_assistant_total_battery_energy_out_state`
    on Paddy's install.
  * Battery capacity (kWh). From config.

Daily reset records the value at local midnight; today's cycles is
`(current - midnight_snapshot) / capacity`. Coordinator owns the
midnight snapshot via the existing daily_reset hook used by
`cost_accumulator`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CycleTracker:
    """Track battery cycles since the last daily reset.

    `_midnight_total_out_kwh` is the cumulative battery-out value
    captured at the most recent local midnight reset. None means
    "first ever observation" - the next observation seeds it without
    counting cycles since boot, avoiding a misleading huge spike
    after first install.
    """

    battery_capacity_kwh: float
    _midnight_total_out_kwh: float | None = None
    _last_observed_total_out_kwh: float | None = None

    def observe(self, total_out_kwh: float) -> None:
        """Record the latest cumulative battery-out reading.

        First call seeds the midnight snapshot. Subsequent calls
        update the running observation; `cycles_today` reads the
        delta. Out-of-order or counter-reset values (current < snapshot)
        re-seed the snapshot to keep the result non-negative.
        """
        if self._midnight_total_out_kwh is None:
            self._midnight_total_out_kwh = total_out_kwh
        if total_out_kwh < self._midnight_total_out_kwh:
            # Counter reset (e.g. SA restart wiped totals) - re-seed.
            self._midnight_total_out_kwh = total_out_kwh
        self._last_observed_total_out_kwh = total_out_kwh

    def reset_daily(self) -> None:
        """Snapshot today's running total as the new midnight baseline.
        Called by the coordinator's existing daily_reset hook at 00:00.
        """
        if self._last_observed_total_out_kwh is not None:
            self._midnight_total_out_kwh = self._last_observed_total_out_kwh

    @property
    def cycles_today(self) -> float:
        """Battery cycles since last daily reset.

        Returns 0.0 before the first observation lands. A "cycle" is
        defined as one full battery_capacity_kwh of discharge.
        """
        if (
            self._midnight_total_out_kwh is None
            or self._last_observed_total_out_kwh is None
            or self.battery_capacity_kwh <= 0
        ):
            return 0.0
        delta_kwh = max(
            0.0,
            self._last_observed_total_out_kwh - self._midnight_total_out_kwh,
        )
        return delta_kwh / self.battery_capacity_kwh
