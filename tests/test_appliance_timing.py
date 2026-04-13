# tests/test_appliance_timing.py
"""Tests for appliance timing suggestion calculator."""

from datetime import datetime, time, timezone

from heo2.appliance_timing import ApplianceTimingCalculator, ApplianceSuggestion
from heo2.models import ProgrammeInputs, ProgrammeState, RateSlot


class TestApplianceTimingCalculator:
    def test_solar_surplus_preferred(self, default_inputs):
        """When solar surplus covers the appliance, recommend solar window."""
        default_inputs.solar_forecast_kwh = [0] * 6 + [0.5, 1.0, 2.0, 3.0, 3.5, 3.5,
                                                         3.0, 2.0, 1.0, 0.5] + [0] * 8
        default_inputs.load_forecast_kwh = [1.0] * 24

        calc = ApplianceTimingCalculator()
        result = calc.best_window(
            inputs=default_inputs,
            draw_kw=2.0,
            duration_hours=1,
            appliance_name="wash",
        )
        assert result.reason == "solar_surplus"
        assert 9 <= result.start_hour <= 12

    def test_cheap_rate_fallback(self, default_inputs):
        """No solar → recommend cheapest import rate window."""
        default_inputs.solar_forecast_kwh = [0.0] * 24
        default_inputs.import_rates = [
            RateSlot(datetime(2026, 4, 13, 5, 30, tzinfo=timezone.utc),
                     datetime(2026, 4, 13, 23, 30, tzinfo=timezone.utc), 27.88),
            RateSlot(datetime(2026, 4, 13, 23, 30, tzinfo=timezone.utc),
                     datetime(2026, 4, 14, 5, 30, tzinfo=timezone.utc), 7.0),
        ]
        calc = ApplianceTimingCalculator()
        result = calc.best_window(
            inputs=default_inputs,
            draw_kw=2.0,
            duration_hours=1,
            appliance_name="wash",
        )
        assert result.reason == "cheap_rate"
        assert result.start_hour >= 23 or result.start_hour < 6

    def test_ev_ranks_by_solar_coverage(self, default_inputs):
        """EV suggestion prioritises solar coverage fraction."""
        default_inputs.solar_forecast_kwh = [0] * 6 + [1, 2, 3, 4, 5, 5,
                                                         4, 3, 2, 1] + [0] * 8
        default_inputs.load_forecast_kwh = [1.0] * 24
        calc = ApplianceTimingCalculator()
        result = calc.best_window(
            inputs=default_inputs,
            draw_kw=7.0,
            duration_hours=3,
            appliance_name="ev",
        )
        assert result.solar_coverage_pct > 0

    def test_returns_no_good_window_when_no_data(self):
        """No rates, no solar → still returns a suggestion."""
        inputs = ProgrammeInputs(
            now=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
            current_soc=50.0,
            battery_capacity_kwh=20.0,
            min_soc=20.0,
            import_rates=[],
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
        calc = ApplianceTimingCalculator()
        result = calc.best_window(
            inputs=inputs,
            draw_kw=2.0,
            duration_hours=1,
            appliance_name="wash",
        )
        assert result.reason == "no_good_window"
