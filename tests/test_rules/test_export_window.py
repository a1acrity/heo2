"""Tests for the rank-based ExportWindowRule."""

from datetime import time, datetime, timezone

from heo2.models import ProgrammeState, ProgrammeInputs, RateSlot
from heo2.rules.baseline import BaselineRule
from heo2.rules.export_window import ExportWindowRule


def _make_baseline(inputs: ProgrammeInputs) -> ProgrammeState:
    return BaselineRule().apply(ProgrammeState.default(min_soc=20), inputs)


def _today_export_distribution(values_pence: list[float]) -> list[RateSlot]:
    """Build a series of consecutive 30-min slots starting at 00:00 today.

    Hour `i // 2` for index `i`. Conftest's `now` fixture is 2026-04-13.
    """
    base = datetime(2026, 4, 13, 0, 0, tzinfo=timezone.utc)
    out = []
    for i, p in enumerate(values_pence):
        h, m = divmod(i * 30, 60)
        if h >= 24:
            break
        start = base.replace(hour=h, minute=m)
        end = start + (datetime(2026, 4, 13, 0, 30, tzinfo=timezone.utc)
                       - datetime(2026, 4, 13, 0, 0, tzinfo=timezone.utc))
        out.append(RateSlot(start=start, end=end, rate_pence=p))
    return out


class TestExportWindowRule:
    def test_no_change_when_no_export_rates(self, default_inputs):
        """No export rate data -> no modifications."""
        default_inputs.export_rates = []
        state = _make_baseline(default_inputs)
        rule = ExportWindowRule()
        original_socs = [s.capacity_soc for s in state.slots]
        result = rule.apply(state, default_inputs)
        assert [s.capacity_soc for s in result.slots] == original_socs

    def test_no_change_when_top_pct_below_breakeven(self, default_inputs):
        """All today's rates are flat and below the worth-selling
        breakeven (replacement_cost / efficiency ~5.49p) - nothing
        ranks as worth selling."""
        # 48 half-hour slots all at 4p (below breakeven of ~5.49p)
        default_inputs.export_rates = _today_export_distribution([4.0] * 48)
        state = _make_baseline(default_inputs)
        rule = ExportWindowRule()
        original_socs = [s.capacity_soc for s in state.slots]
        result = rule.apply(state, default_inputs)
        assert [s.capacity_soc for s in result.slots] == original_socs
        assert any("nothing worth selling" in r.lower() for r in result.reason_log)

    def test_drains_to_min_soc_when_no_evening_demand(self, default_inputs):
        """A clearly profitable slot drains to min_soc when there's no
        evening demand to reserve for (priority 1 doesn't apply)."""
        rates = [3.0] * 48
        rates[28] = 25.0  # 14:00 spike
        default_inputs.export_rates = _today_export_distribution(rates)
        # Zero evening demand -> no priority-1 reserve needed
        default_inputs.load_forecast_kwh = [1.0] * 18 + [0.0] * 6
        state = _make_baseline(default_inputs)
        rule = ExportWindowRule()
        result = rule.apply(state, default_inputs)
        for slot in result.slots:
            if slot.contains_time(time(14, 0)) and not slot.grid_charge:
                assert slot.capacity_soc == int(default_inputs.min_soc)
                break
        else:
            raise AssertionError("no slot covers 14:00 - test fixture wrong")

    def test_evening_demand_floors_drain_target(self, default_inputs):
        """Per SPEC §5 priority 1 (avoid peak import) the rule floors
        the drain target at min_soc + (evening_demand / capacity)."""
        rates = [3.0] * 48
        rates[28] = 25.0  # 14:00 spike
        default_inputs.export_rates = _today_export_distribution(rates)
        # 18:30-23:30 evening demand: 5h × 2 kWh = 10 kWh.
        # required_soc = 20 + (10/20*100) = 70%
        default_inputs.load_forecast_kwh = [1.0] * 18 + [2.0] * 5 + [1.0]
        state = _make_baseline(default_inputs)
        rule = ExportWindowRule()
        result = rule.apply(state, default_inputs)
        for slot in result.slots:
            if slot.contains_time(time(14, 0)) and not slot.grid_charge:
                assert slot.capacity_soc >= 70
                break

    def test_low_soc_uses_n_low(self, default_inputs):
        """Low SOC + low tomorrow forecast picks top 15%, which excludes
        marginal slots that the medium 30% would include."""
        # 48 slots ascending 1..48. Top 15% (8 slots) is 41..48p.
        # Top 30% (15 slots) starts at 34p. So a slot at 36p is in top-30
        # but not top-15.
        rates = [float(i + 1) for i in range(48)]
        default_inputs.export_rates = _today_export_distribution(rates)
        # Force low-SOC + low-solar branch
        default_inputs.current_soc = 30.0
        default_inputs.solar_forecast_kwh_tomorrow = [0.0] * 24
        state = _make_baseline(default_inputs)
        rule = ExportWindowRule()
        result = rule.apply(state, default_inputs)
        # The 36p slot (index 35) is at hour 17 (35*30/60 = 17.5).
        # Not in top-15%, so its slot shouldn't be drained.
        # The 48p slot (index 47) is hour 23. It IS in top-15%.
        # SafetyRule isn't run here so min_soc=20 is the drain target.
        # We just verify the n_low branch fired.
        assert any("top 15%" in r for r in result.reason_log)

    def test_high_soc_high_solar_uses_n_high(self, default_inputs):
        rates = [float(i + 1) for i in range(48)]
        default_inputs.export_rates = _today_export_distribution(rates)
        default_inputs.current_soc = 90.0
        # Tomorrow forecast > daily load -> high-solar branch
        default_inputs.solar_forecast_kwh_tomorrow = [3.0] * 24
        default_inputs.load_forecast_kwh = [1.0] * 24  # 24 kWh
        state = _make_baseline(default_inputs)
        rule = ExportWindowRule()
        result = rule.apply(state, default_inputs)
        assert any("top 50%" in r for r in result.reason_log)

    def test_reason_log_entry(self, default_inputs):
        rates = [3.0] * 48
        rates[28] = 25.0
        default_inputs.export_rates = _today_export_distribution(rates)
        state = _make_baseline(default_inputs)
        rule = ExportWindowRule()
        result = rule.apply(state, default_inputs)
        assert any("ExportWindow" in r for r in result.reason_log)
