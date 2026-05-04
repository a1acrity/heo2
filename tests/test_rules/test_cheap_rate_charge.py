"""Tests for CheapRateChargeRule (bridge-to-PV strategy)."""

from datetime import time, datetime, timedelta, timezone

from heo2.models import ProgrammeState, ProgrammeInputs, SlotConfig, RateSlot
from heo2.rules.baseline import BaselineRule
from heo2.rules.cheap_rate_charge import CheapRateChargeRule


def _make_baseline(inputs: ProgrammeInputs) -> ProgrammeState:
    """Helper: run BaselineRule to get starting state."""
    return BaselineRule().apply(ProgrammeState.default(min_soc=20), inputs)


def _sunny_tomorrow() -> list[float]:
    """Plausible UK May tomorrow: PV ramps from 06:00, peaks midday,
    falls off by 19:00. Total ~25 kWh."""
    pv = [0.0] * 24
    curve = [0.1, 0.5, 1.5, 2.5, 3.0, 3.5, 3.5, 3.0, 2.5, 1.5, 0.5, 0.1]
    for i, v in enumerate(curve):
        pv[6 + i] = v
    return pv


class TestCheapRateChargeRule:
    def test_short_morning_bridge_when_pv_takes_over_early(
        self, default_inputs,
    ):
        """Sunny tomorrow + low pre-dawn load: PV overtakes load early
        morning, so the overnight charge only needs a small bridge."""
        default_inputs.solar_forecast_kwh_tomorrow = _sunny_tomorrow()
        default_inputs.load_forecast_kwh = [0.3] * 24  # very low
        default_inputs.battery_capacity_kwh = 20.0
        state = _make_baseline(default_inputs)
        rule = CheapRateChargeRule()
        result = rule.apply(state, default_inputs)
        # Bridge is hour 5 = 0.3 - 0 = 0.3 kWh; PV takeover hour 6
        # (load=0.3, PV=0.1 - PV doesn't yet exceed load) ... actually
        # depends on the curve. Just assert target is well under 100.
        overnight = result.slots[0]
        assert overnight.capacity_soc < 50, (
            f"expected small bridge target, got {overnight.capacity_soc}"
        )

    def test_charges_fully_when_no_tomorrow_pv_forecast(self, default_inputs):
        """Without tomorrow's forecast we can't compute the bridge -
        default to filling the battery so we don't strand at floor."""
        default_inputs.solar_forecast_kwh_tomorrow = []
        default_inputs.load_forecast_kwh = [1.0] * 24
        state = _make_baseline(default_inputs)
        rule = CheapRateChargeRule()
        result = rule.apply(state, default_inputs)
        assert result.slots[0].capacity_soc == 100

    def test_charges_fully_when_pv_never_overtakes_load(self, default_inputs):
        """Deep winter: PV never exceeds load all day - bridge is the
        whole day's deficit, target clamps to max."""
        # Tomorrow: tiny PV, big load - PV never overtakes load
        default_inputs.solar_forecast_kwh_tomorrow = [0.05] * 24
        default_inputs.load_forecast_kwh = [2.0] * 24
        default_inputs.battery_capacity_kwh = 20.0
        state = _make_baseline(default_inputs)
        rule = CheapRateChargeRule()
        result = rule.apply(state, default_inputs)
        assert result.slots[0].capacity_soc == 100

    def test_never_below_min_soc(self, default_inputs):
        """Even with massive PV and tiny load, target stays >= min_soc."""
        # Tomorrow: solar floods from hour 0 (unrealistic but tests floor)
        default_inputs.solar_forecast_kwh_tomorrow = [5.0] * 24
        default_inputs.load_forecast_kwh = [0.1] * 24
        state = _make_baseline(default_inputs)
        rule = CheapRateChargeRule()
        result = rule.apply(state, default_inputs)
        overnight = result.slots[0]
        assert overnight.capacity_soc >= int(default_inputs.min_soc)

    def test_max_target_soc_respected(self, default_inputs):
        """max_target_soc caps the overnight charge."""
        default_inputs.solar_forecast_kwh_tomorrow = [0.0] * 24  # never overtakes
        default_inputs.load_forecast_kwh = [2.0] * 24
        state = _make_baseline(default_inputs)
        rule = CheapRateChargeRule(max_target_soc=80)
        result = rule.apply(state, default_inputs)
        overnight = result.slots[0]
        assert overnight.capacity_soc <= 80

    def test_safety_buffer_keeps_target_above_floor(
        self, default_inputs,
    ):
        """Even when bridge is essentially zero, the safety buffer
        keeps target a comfortable margin above min_soc to absorb
        forecast misses."""
        default_inputs.solar_forecast_kwh_tomorrow = _sunny_tomorrow()
        default_inputs.load_forecast_kwh = [0.0] * 24  # zero load = zero bridge
        state = _make_baseline(default_inputs)
        rule = CheapRateChargeRule(safety_buffer_pct=15)
        result = rule.apply(state, default_inputs)
        # Target should be at least min_soc + 15%
        assert result.slots[0].capacity_soc >= int(default_inputs.min_soc) + 14

    def test_bigger_load_means_bigger_bridge_target(self, default_inputs):
        """Same PV curve, larger load -> bridge larger -> target higher."""
        default_inputs.solar_forecast_kwh_tomorrow = _sunny_tomorrow()
        default_inputs.battery_capacity_kwh = 20.0
        rule = CheapRateChargeRule()

        default_inputs.load_forecast_kwh = [0.2] * 24
        s1 = _make_baseline(default_inputs)
        target_low_load = rule.apply(s1, default_inputs).slots[0].capacity_soc

        default_inputs.load_forecast_kwh = [1.0] * 24
        s2 = _make_baseline(default_inputs)
        target_high_load = rule.apply(s2, default_inputs).slots[0].capacity_soc

        assert target_high_load > target_low_load, (
            f"expected high-load target ({target_high_load}) > "
            f"low-load ({target_low_load})"
        )

    def test_reason_log_entry(self, default_inputs):
        default_inputs.solar_forecast_kwh_tomorrow = _sunny_tomorrow()
        state = _make_baseline(default_inputs)
        rule = CheapRateChargeRule()
        result = rule.apply(state, default_inputs)
        assert any("CheapRateCharge" in r for r in result.reason_log)

    def test_also_sets_late_night_slot(self, default_inputs):
        """The 23:30 slot (slot 4) should get the same target as overnight slot 1."""
        default_inputs.solar_forecast_kwh_tomorrow = _sunny_tomorrow()
        default_inputs.load_forecast_kwh = [1.0] * 24
        state = _make_baseline(default_inputs)
        rule = CheapRateChargeRule()
        result = rule.apply(state, default_inputs)
        assert result.slots[0].capacity_soc == result.slots[3].capacity_soc

    def test_cheap_window_end_uses_import_rates(self, default_inputs):
        """End-of-cheap-window is derived from the bottom-25% block of
        import_rates. A shifted window (e.g. Saving Session 02:00-08:00)
        should anchor the bridge calc at 08:00, not the hardcoded 05:00.
        """
        default_inputs.solar_forecast_kwh_tomorrow = _sunny_tomorrow()
        default_inputs.load_forecast_kwh = [1.0] * 24

        # Build a 24h rate horizon from now (12:00 UTC). Cheap block at
        # tomorrow 02:00-08:00 (12 half-hour slots = 25% of 48 total),
        # so it's the unambiguous bottom-25% cohort.
        cheap_p, peak_p = 5.0, 30.0
        rates = []
        cur = default_inputs.now
        end = cur + timedelta(hours=24)
        while cur < end:
            offset = cur - default_inputs.now
            is_cheap = (
                timedelta(hours=14) <= offset < timedelta(hours=20)
            )
            rates.append(RateSlot(
                start=cur,
                end=cur + timedelta(minutes=30),
                rate_pence=cheap_p if is_cheap else peak_p,
            ))
            cur += timedelta(minutes=30)
        default_inputs.import_rates = rates

        state = _make_baseline(default_inputs)
        result = CheapRateChargeRule().apply(state, default_inputs)
        assert any("from 08:00" in r for r in result.reason_log), (
            f"expected reason log to mention 'from 08:00', got: "
            f"{result.reason_log}"
        )

    def test_cheap_window_end_falls_back_when_no_rates(
        self, default_inputs,
    ):
        """No import_rates -> use cheap_window_end_hour_fallback (5)."""
        default_inputs.solar_forecast_kwh_tomorrow = _sunny_tomorrow()
        default_inputs.load_forecast_kwh = [1.0] * 24
        default_inputs.import_rates = []
        state = _make_baseline(default_inputs)
        result = CheapRateChargeRule().apply(state, default_inputs)
        assert any("from 05:00" in r for r in result.reason_log), (
            f"expected fallback 05:00 in reason, got: {result.reason_log}"
        )
