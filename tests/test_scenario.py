# tests/test_scenario.py
"""End-to-end scenario tests: full rule chain on representative days."""

from datetime import time, datetime, timezone

from heo2.models import ProgrammeInputs, RateSlot
from heo2.rules import default_rules
from heo2.rule_engine import RuleEngine


def _make_engine() -> RuleEngine:
    return RuleEngine(rules=default_rules())


class TestSunnyDayScenario:
    """Bright summer day: lots of solar, moderate load, no events."""

    def test_sunny_day(self):
        inputs = ProgrammeInputs(
            now=datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc),
            current_soc=80.0,
            battery_capacity_kwh=20.0,
            min_soc=20.0,
            import_rates=[
                RateSlot(datetime(2026, 6, 15, 5, 30, tzinfo=timezone.utc),
                         datetime(2026, 6, 15, 23, 30, tzinfo=timezone.utc), 27.88),
                RateSlot(datetime(2026, 6, 15, 23, 30, tzinfo=timezone.utc),
                         datetime(2026, 6, 16, 5, 30, tzinfo=timezone.utc), 7.0),
            ],
            export_rates=[
                RateSlot(datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc),
                         datetime(2026, 6, 15, 16, 0, tzinfo=timezone.utc), 12.0),
            ],
            solar_forecast_kwh=[0, 0, 0, 0, 0, 0.5, 1.5, 3.0, 4.0, 4.5, 4.5, 4.0,
                                3.0, 2.0, 1.0, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            load_forecast_kwh=[0.8] * 6 + [1.2] * 6 + [1.5] * 6 + [2.0] * 6,
            igo_dispatching=False,
            saving_session=False,
            saving_session_start=None,
            saving_session_end=None,
            ev_charging=False,
            grid_connected=True,
            active_appliances=[],
            appliance_expected_kwh=0.0,
        )

        engine = _make_engine()
        result = engine.calculate(inputs)

        # Validate basics
        assert len(result.slots) == 6
        assert result.validate() == []
        assert result.slots[0].start_time == time(0, 0)

        # With good solar the target may be reduced, but export opportunity can
        # push it back up.  The key check is that grid_charge is enabled and
        # the SOC is within the valid 0–100 range.
        overnight = result.slots[0]
        assert overnight.grid_charge is True
        assert overnight.capacity_soc <= 100  # valid range

        # Reason log should have entries from multiple rules
        assert len(result.reason_log) >= 3


class TestDarkWinterScenario:
    """Dark winter day: no solar, high demand."""

    def test_dark_winter(self):
        inputs = ProgrammeInputs(
            now=datetime(2026, 12, 15, 8, 0, tzinfo=timezone.utc),
            current_soc=30.0,
            battery_capacity_kwh=20.0,
            min_soc=20.0,
            import_rates=[
                RateSlot(datetime(2026, 12, 15, 5, 30, tzinfo=timezone.utc),
                         datetime(2026, 12, 15, 23, 30, tzinfo=timezone.utc), 27.88),
                RateSlot(datetime(2026, 12, 15, 23, 30, tzinfo=timezone.utc),
                         datetime(2026, 12, 16, 5, 30, tzinfo=timezone.utc), 7.0),
            ],
            export_rates=[],
            solar_forecast_kwh=[0.0] * 24,
            load_forecast_kwh=[2.0] * 24,  # 48 kWh — heavy
            igo_dispatching=False,
            saving_session=False,
            saving_session_start=None,
            saving_session_end=None,
            ev_charging=False,
            grid_connected=True,
            active_appliances=[],
            appliance_expected_kwh=0.0,
        )

        engine = _make_engine()
        result = engine.calculate(inputs)

        assert result.validate() == []

        # No solar → charge fully overnight
        assert result.slots[0].capacity_soc == 100
        assert result.slots[0].grid_charge is True


class TestIGODispatchScenario:
    """IGO dispatch active — should charge from grid."""

    def test_igo_dispatch(self):
        inputs = ProgrammeInputs(
            now=datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc),
            current_soc=45.0,
            battery_capacity_kwh=20.0,
            min_soc=20.0,
            import_rates=[
                RateSlot(datetime(2026, 4, 13, 5, 30, tzinfo=timezone.utc),
                         datetime(2026, 4, 13, 23, 30, tzinfo=timezone.utc), 27.88),
            ],
            export_rates=[],
            solar_forecast_kwh=[0.0] * 24,
            load_forecast_kwh=[1.5] * 24,
            igo_dispatching=True,
            saving_session=False,
            saving_session_start=None,
            saving_session_end=None,
            ev_charging=False,
            grid_connected=True,
            active_appliances=[],
            appliance_expected_kwh=0.0,
        )

        engine = _make_engine()
        result = engine.calculate(inputs)

        assert result.validate() == []

        # Current slot (14:00 → slot 2: 05:30–18:30) should have grid charge
        idx = result.find_slot_at(time(14, 0))
        assert result.slots[idx].grid_charge is True
        assert result.slots[idx].capacity_soc >= 45  # at least current SOC
