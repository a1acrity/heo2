# tests/test_projection.py
"""Tests for the day-ahead programme projection."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest

from heo2.models import ProgrammeInputs, ProgrammeState, RateSlot, SlotConfig
from heo2.projection import Projection, project_day


def _slot(start_h, start_m, end_h, end_m, soc, gc):
    return SlotConfig(
        start_time=time(start_h, start_m),
        end_time=time(end_h, end_m),
        capacity_soc=soc,
        grid_charge=gc,
    )


def _half_hour_rates(
    start: datetime, hours: int, rate_pence: float,
) -> list[RateSlot]:
    """Generate 30-min rate slots from `start` for `hours` at fixed rate."""
    slots = []
    for i in range(hours * 2):
        slots.append(RateSlot(
            start=start + timedelta(minutes=30 * i),
            end=start + timedelta(minutes=30 * (i + 1)),
            rate_pence=rate_pence,
        ))
    return slots


def _inputs_at_midnight(
    *, current_soc=50.0, import_rates=None, export_rates=None,
    solar_24=None, load_24=None, capacity_kwh=20.0,
):
    """Build ProgrammeInputs starting from a known midnight datetime."""
    now = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
    return ProgrammeInputs(
        now=now,
        current_soc=current_soc,
        battery_capacity_kwh=capacity_kwh,
        min_soc=10.0,
        import_rates=import_rates or [],
        export_rates=export_rates or [],
        solar_forecast_kwh=solar_24 or [0.0] * 24,
        load_forecast_kwh=load_24 or [1.0] * 24,
        igo_dispatching=False,
        saving_session=False,
        saving_session_start=None,
        saving_session_end=None,
        ev_charging=False,
        grid_connected=True,
        active_appliances=[],
        appliance_expected_kwh=0.0,
    )


class TestProjectionSummaryFormatting:
    def test_zero_imports_renders_zero_peak(self):
        p = Projection(
            expected_return_pence=123.4,
            sells_kwh=5.0,
            sells_pence=100.0,
            imports_kwh=0.0,
            imports_pence=0.0,
            peak_import_kwh=0.0,
        )
        s = p.summary()
        assert "+£1.23" in s
        assert "ZERO peak-rate import" in s

    def test_negative_return_renders_minus_pounds(self):
        p = Projection(
            expected_return_pence=-50.0,
            sells_kwh=0.0,
            sells_pence=0.0,
            imports_kwh=2.0,
            imports_pence=50.0,
        )
        s = p.summary()
        assert "-£0.50" in s

    def test_peak_kwh_visible_when_nonzero(self):
        p = Projection(peak_import_kwh=1.5, peak_import_pence=37.0)
        s = p.summary()
        assert "1.50 kWh peak-rate import" in s
        assert "ZERO" not in s


class TestProjectDay:
    def test_idle_programme_no_solar_no_export_imports_to_cover_load(self):
        """Programme that holds floor and never grid-charges should
        import enough to cover load once SOC hits min_soc."""
        prog = ProgrammeState(slots=[
            _slot(0, 0, 4, 0, 10, False),
            _slot(4, 0, 8, 0, 10, False),
            _slot(8, 0, 12, 0, 10, False),
            _slot(12, 0, 16, 0, 10, False),
            _slot(16, 0, 20, 0, 10, False),
            _slot(20, 0, 0, 0, 10, False),
        ])
        # Cheap flat 5p import, no export rates, no solar, 1 kWh/h load
        midnight = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
        inputs = _inputs_at_midnight(
            current_soc=10.0,
            import_rates=_half_hour_rates(midnight, 24, 5.0),
            load_24=[1.0] * 24,
        )

        p = project_day(
            prog, inputs,
            battery_capacity_kwh=20.0,
            max_charge_kw=5.0, max_discharge_kw=5.0,
        )
        # Battery starts at min_soc, so all 24 kWh of load is imported.
        assert p.imports_kwh == pytest.approx(24.0, abs=0.5)
        assert p.peak_import_kwh == 0.0
        # No exports happened
        assert p.sells_kwh == 0.0

    def test_grid_charge_in_cheap_window_imports_to_target(self):
        """A grid_charge=True slot pushes SOC up to capacity_soc using
        grid imports, capped by max_charge_kw."""
        prog = ProgrammeState(slots=[
            _slot(0, 0, 5, 30, 80, True),  # Charge to 80% in 5.5h
            _slot(5, 30, 8, 0, 10, False),
            _slot(8, 0, 12, 0, 10, False),
            _slot(12, 0, 16, 0, 10, False),
            _slot(16, 0, 20, 0, 10, False),
            _slot(20, 0, 0, 0, 10, False),
        ])
        midnight = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
        inputs = _inputs_at_midnight(
            current_soc=10.0,
            import_rates=_half_hour_rates(midnight, 24, 5.0),
            load_24=[0.0] * 24,  # No load -> sole demand is the GC
        )

        p = project_day(
            prog, inputs,
            battery_capacity_kwh=20.0,
            max_charge_kw=5.0, max_discharge_kw=5.0,
        )
        # 70% of 20 kWh = 14 kWh stored; with 95% charge eff that's
        # ~14.7 kWh imported. Cap is 5kW * 5.5h = 27.5kWh, so cheap-rate
        # window can finish the job.
        assert p.imports_kwh > 13.0
        assert p.imports_kwh < 17.0
        assert p.peak_import_kwh == 0.0

    def test_peak_import_logged_when_floor_forces_grid(self):
        """Battery at floor + load with peak-rate import -> peak_import_kwh > 0."""
        prog = ProgrammeState(slots=[
            _slot(0, 0, 4, 0, 10, False),
            _slot(4, 0, 8, 0, 10, False),
            _slot(8, 0, 12, 0, 10, False),
            _slot(12, 0, 16, 0, 10, False),
            _slot(16, 0, 20, 0, 10, False),
            _slot(20, 0, 0, 0, 10, False),
        ])
        midnight = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
        # 25p flat all day -> well above 24p peak threshold
        inputs = _inputs_at_midnight(
            current_soc=10.0,
            import_rates=_half_hour_rates(midnight, 24, 25.0),
            load_24=[1.0] * 24,
        )

        p = project_day(
            prog, inputs,
            battery_capacity_kwh=20.0,
            peak_threshold_p=24.0,
        )
        assert p.peak_import_kwh == pytest.approx(p.imports_kwh, abs=0.5)
        assert p.peak_import_kwh > 20.0

    def test_planned_dispatch_overrides_published_rate_to_off_peak(self):
        """Octopus retroactively bills any import inside a smart-charge
        dispatch at the IGO off-peak rate, regardless of what the
        published live-rate sensor said at the moment of import. The
        projection must reflect this so it doesn't false-alarm on
        peak-rate import while the EV is being smart-charged.
        """
        from datetime import timedelta as _td
        from heo2.models import PlannedDispatch
        prog = ProgrammeState(slots=[
            _slot(0, 0, 4, 0, 100, True),
            _slot(4, 0, 8, 0, 10, False),
            _slot(8, 0, 12, 0, 10, False),
            _slot(12, 0, 16, 0, 10, False),
            _slot(16, 0, 20, 0, 10, False),
            _slot(20, 0, 0, 0, 10, False),
        ])
        midnight = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
        # All-day 25p (above peak threshold). Without the dispatch
        # override, slot 1's grid_charge=True window books at 25p.
        # With the override it books at the off-peak 4.95p.
        inputs = _inputs_at_midnight(
            current_soc=10.0,
            import_rates=_half_hour_rates(midnight, 24, 25.0),
            load_24=[0.0] * 24,
        )
        # Dispatch covers the whole 00:00-04:00 charge window
        inputs.planned_dispatches = [PlannedDispatch(
            start=midnight,
            end=midnight + _td(hours=4),
        )]

        p = project_day(
            prog, inputs,
            battery_capacity_kwh=20.0,
            peak_threshold_p=24.0,
            igo_off_peak_p=4.95,
        )
        # All imports during the dispatch window -> none should land
        # in peak_import_kwh.
        assert p.peak_import_kwh == pytest.approx(0.0, abs=0.01)
        # imports_avg should reflect the off-peak rate.
        assert p.imports_avg_pence is not None
        assert p.imports_avg_pence == pytest.approx(4.95, abs=0.5)

    def test_top_export_window_drains_battery_to_grid(self):
        """When SOC is high and export rate is in top-N% with capacity_soc
        below current SOC, projection sells to grid."""
        prog = ProgrammeState(slots=[
            _slot(0, 0, 4, 0, 30, False),
            _slot(4, 0, 8, 0, 30, False),
            _slot(8, 0, 12, 0, 30, False),
            _slot(12, 0, 16, 0, 30, False),
            _slot(16, 0, 20, 0, 30, False),  # SOC target 30 -> headroom
            _slot(20, 0, 0, 0, 30, False),
        ])
        midnight = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
        # Single high export rate at 16:00-17:00 (~25p), rest 5p
        export = _half_hour_rates(midnight, 24, 5.0)
        for s in export:
            if s.start.hour == 16:
                s.rate_pence = 25.0
        inputs = _inputs_at_midnight(
            current_soc=80.0,
            import_rates=_half_hour_rates(midnight, 24, 5.0),
            export_rates=export,
            load_24=[0.0] * 24,
        )

        p = project_day(
            prog, inputs,
            battery_capacity_kwh=20.0,
            export_top_pct=10,  # top 10% catches just 16:00-17:00
        )
        assert p.sells_kwh > 0
        assert p.sells_avg_pence is not None
        assert p.sells_avg_pence == pytest.approx(25.0, abs=0.1)
