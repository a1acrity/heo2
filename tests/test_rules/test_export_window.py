"""Tests for ExportWindowRule."""

from datetime import time, datetime, timezone

from heo2.models import ProgrammeState, ProgrammeInputs, RateSlot
from heo2.rules.baseline import BaselineRule
from heo2.rules.export_window import ExportWindowRule


def _make_baseline(inputs: ProgrammeInputs) -> ProgrammeState:
    return BaselineRule().apply(ProgrammeState.default(min_soc=20), inputs)


class TestExportWindowRule:
    def test_no_change_when_no_export_rates(self, default_inputs):
        """No export rate data -> no modifications."""
        default_inputs.export_rates = []
        state = _make_baseline(default_inputs)
        rule = ExportWindowRule()
        original_socs = [s.capacity_soc for s in state.slots]
        result = rule.apply(state, default_inputs)
        assert [s.capacity_soc for s in result.slots] == original_socs

    def test_no_change_when_export_unprofitable(self, default_inputs):
        """Export rate below effective stored cost -> no drain."""
        default_inputs.export_rates = [
            RateSlot(
                start=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
                end=datetime(2026, 4, 13, 16, 0, tzinfo=timezone.utc),
                rate_pence=5.0,  # below 7.86p
            ),
        ]
        state = _make_baseline(default_inputs)
        rule = ExportWindowRule()
        original_socs = [s.capacity_soc for s in state.slots]
        result = rule.apply(state, default_inputs)
        assert [s.capacity_soc for s in result.slots] == original_socs

    def test_sets_drain_target_for_profitable_export(self, default_inputs):
        """High export rate -> drain battery during that window."""
        default_inputs.export_rates = [
            RateSlot(
                start=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
                end=datetime(2026, 4, 13, 16, 0, tzinfo=timezone.utc),
                rate_pence=20.0,  # well above 7.86p
            ),
        ]
        default_inputs.load_forecast_kwh = [1.0] * 24  # modest load
        state = _make_baseline(default_inputs)
        rule = ExportWindowRule()
        result = rule.apply(state, default_inputs)
        # A slot overlapping 12:00-16:00 should have a reduced SOC target
        for slot in result.slots:
            if slot.contains_time(time(14, 0)):
                assert slot.capacity_soc < 100
                break

    def test_floor_protects_evening_demand(self, default_inputs):
        """Drain target never goes below evening demand reserve."""
        default_inputs.export_rates = [
            RateSlot(
                start=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
                end=datetime(2026, 4, 13, 16, 0, tzinfo=timezone.utc),
                rate_pence=25.0,
            ),
        ]
        # High evening load: 18:30-23:30 = 5 hours x 2 kWh = 10 kWh
        default_inputs.load_forecast_kwh = [1.0] * 18 + [2.0] * 5 + [1.0]
        state = _make_baseline(default_inputs)
        rule = ExportWindowRule()
        result = rule.apply(state, default_inputs)
        # Export floor SOC = min_soc + (evening_demand / capacity x 100)
        # = 20 + (10 / 20 x 100) = 70%
        for slot in result.slots:
            if slot.contains_time(time(14, 0)):
                assert slot.capacity_soc >= 70
                break

    def test_reason_log_entry(self, default_inputs):
        default_inputs.export_rates = [
            RateSlot(
                start=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
                end=datetime(2026, 4, 13, 16, 0, tzinfo=timezone.utc),
                rate_pence=20.0,
            ),
        ]
        state = _make_baseline(default_inputs)
        rule = ExportWindowRule()
        result = rule.apply(state, default_inputs)
        assert any("ExportWindow" in r for r in result.reason_log)
