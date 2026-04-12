"""Tests for CheapRateChargeRule."""

from datetime import time, datetime, timezone

from heo2.models import ProgrammeState, ProgrammeInputs, SlotConfig, RateSlot
from heo2.rules.baseline import BaselineRule
from heo2.rules.cheap_rate_charge import CheapRateChargeRule


def _make_baseline(inputs: ProgrammeInputs) -> ProgrammeState:
    """Helper: run BaselineRule to get starting state."""
    return BaselineRule().apply(ProgrammeState.default(min_soc=20), inputs)


class TestCheapRateChargeRule:
    def test_reduces_overnight_target_when_solar_covers_demand(self, default_inputs):
        """Sunny day: lots of solar, low demand -> don't charge much."""
        default_inputs.solar_forecast_kwh = [0.0] * 6 + [2.0] * 12 + [0.0] * 6  # 24 kWh
        default_inputs.load_forecast_kwh = [1.0] * 24  # 24 kWh total
        state = _make_baseline(default_inputs)
        rule = CheapRateChargeRule()
        result = rule.apply(state, default_inputs)
        # Solar covers all demand, so overnight target should be near min_soc
        overnight = result.slots[0]
        assert overnight.capacity_soc < 100

    def test_charges_fully_when_no_solar(self, default_inputs):
        """Dark winter day: no solar, high demand -> charge to max."""
        default_inputs.solar_forecast_kwh = [0.0] * 24
        default_inputs.load_forecast_kwh = [2.0] * 24  # 48 kWh (exceeds battery)
        state = _make_baseline(default_inputs)
        rule = CheapRateChargeRule()
        result = rule.apply(state, default_inputs)
        overnight = result.slots[0]
        assert overnight.capacity_soc == 100

    def test_never_below_min_soc(self, default_inputs):
        """Even with massive solar, target never drops below min_soc."""
        default_inputs.solar_forecast_kwh = [5.0] * 24  # 120 kWh
        default_inputs.load_forecast_kwh = [0.1] * 24
        state = _make_baseline(default_inputs)
        rule = CheapRateChargeRule()
        result = rule.apply(state, default_inputs)
        overnight = result.slots[0]
        assert overnight.capacity_soc >= int(default_inputs.min_soc)

    def test_max_target_soc_respected(self, default_inputs):
        """max_target_soc caps the overnight charge."""
        default_inputs.solar_forecast_kwh = [0.0] * 24
        default_inputs.load_forecast_kwh = [2.0] * 24
        state = _make_baseline(default_inputs)
        rule = CheapRateChargeRule(max_target_soc=80)
        result = rule.apply(state, default_inputs)
        overnight = result.slots[0]
        assert overnight.capacity_soc <= 80

    def test_accounts_for_profitable_export(self, default_inputs):
        """If export rates are good, charge more to export later."""
        default_inputs.solar_forecast_kwh = [0.0] * 24
        default_inputs.load_forecast_kwh = [0.5] * 24  # 12 kWh -- small
        default_inputs.export_rates = [
            RateSlot(
                start=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
                end=datetime(2026, 4, 13, 16, 0, tzinfo=timezone.utc),
                rate_pence=15.0,  # well above 7.86p effective cost
            ),
        ]
        state = _make_baseline(default_inputs)
        rule = CheapRateChargeRule()
        result = rule.apply(state, default_inputs)
        overnight = result.slots[0]
        # Should charge extra for profitable export
        assert overnight.capacity_soc > int(default_inputs.min_soc) + 10

    def test_reason_log_entry(self, default_inputs):
        state = _make_baseline(default_inputs)
        rule = CheapRateChargeRule()
        result = rule.apply(state, default_inputs)
        assert any("CheapRateCharge" in r for r in result.reason_log)

    def test_also_sets_late_night_slot(self, default_inputs):
        """The 23:30 slot (slot 4) should get the same target as overnight slot 1."""
        default_inputs.solar_forecast_kwh = [0.0] * 24
        default_inputs.load_forecast_kwh = [1.0] * 24
        state = _make_baseline(default_inputs)
        rule = CheapRateChargeRule()
        result = rule.apply(state, default_inputs)
        assert result.slots[0].capacity_soc == result.slots[3].capacity_soc
