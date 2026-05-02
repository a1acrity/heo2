"""CheapRateChargeRule -- rank-based overnight/cheap-window charge target."""

from __future__ import annotations

from ..const import (
    DEFAULT_CHEAP_CHARGE_BOTTOM_PCT,
    DEFAULT_HIGH_SOC_THRESHOLD,
    DEFAULT_IGO_OFF_PEAK_PENCE,
    DEFAULT_LOW_SOC_THRESHOLD,
    DEFAULT_MAX_DISCHARGE_KW,
    DEFAULT_SELL_TOP_PCT,
    DEFAULT_SELL_TOP_PCT_HIGH_SOC,
    DEFAULT_SELL_TOP_PCT_LOW_SOC,
    ROUND_TRIP_EFFICIENCY,
)
from ..models import ProgrammeInputs, ProgrammeState
from ..rank_pricing import (
    estimate_profitable_export_kwh,
    filter_today,
    select_export_top_pct,
    select_worth_selling_windows,
)
from ..rule_engine import Rule


class CheapRateChargeRule(Rule):
    """Set the SOC target on grid_charge slots based on expected demand,
    expected solar, and rank-based profitable-export volume.

    Implements SPEC §5a charging-from-grid logic generalised:
    `worth_charging_kwh = expected_demand - expected_solar + expected_export`,
    where `expected_export` is derived from today's published export rates
    and the rank-based "worth selling" filter rather than a fixed
    pence threshold.

    Replaces the legacy version's `>7.86p effective stored cost` test.
    The legacy version over-charged when winter Agile distributions sat
    above 7.86p (looked profitable everywhere) and under-charged when
    summer distributions stayed below.
    """

    name = "cheap_rate_charge"
    description = "Calculate cheap-window charge target from expected demand and rank-worthy export"

    def __init__(
        self,
        max_target_soc: int = 100,
        *,
        replacement_cost_pence: float = DEFAULT_IGO_OFF_PEAK_PENCE,
        round_trip_efficiency: float = ROUND_TRIP_EFFICIENCY,
        max_discharge_kw: float = DEFAULT_MAX_DISCHARGE_KW,
        low_soc_threshold: float = DEFAULT_LOW_SOC_THRESHOLD,
        high_soc_threshold: float = DEFAULT_HIGH_SOC_THRESHOLD,
        n_low: int = DEFAULT_SELL_TOP_PCT_LOW_SOC,
        n_med: int = DEFAULT_SELL_TOP_PCT,
        n_high: int = DEFAULT_SELL_TOP_PCT_HIGH_SOC,
        cheap_bottom_pct: int = DEFAULT_CHEAP_CHARGE_BOTTOM_PCT,
    ):
        self.max_target_soc = max_target_soc
        self.replacement_cost_pence = replacement_cost_pence
        self.round_trip_efficiency = round_trip_efficiency
        self.max_discharge_kw = max_discharge_kw
        self.low_soc_threshold = low_soc_threshold
        self.high_soc_threshold = high_soc_threshold
        self.n_low = n_low
        self.n_med = n_med
        self.n_high = n_high
        self.cheap_bottom_pct = cheap_bottom_pct

    def apply(self, state: ProgrammeState, inputs: ProgrammeInputs) -> ProgrammeState:
        expected_demand_kwh = sum(inputs.load_forecast_kwh)
        expected_solar_kwh = sum(inputs.solar_forecast_kwh)
        daily_load_kwh = expected_demand_kwh or 1.0

        # Rank-based estimate of how much we can profitably sell today.
        # Driven by today's live export rates; falls back to zero when
        # BD hasn't returned anything (writes are H4-blocked anyway).
        today_export_rates = filter_today(inputs.export_rates, inputs.now)

        tomorrow_solar_kwh = sum(inputs.solar_forecast_kwh_tomorrow) if (
            inputs.solar_forecast_kwh_tomorrow
        ) else expected_solar_kwh

        n_pct, n_reason = select_export_top_pct(
            current_soc=inputs.current_soc,
            tomorrow_solar_kwh=tomorrow_solar_kwh,
            daily_load_kwh=daily_load_kwh,
            low_soc_threshold=self.low_soc_threshold,
            high_soc_threshold=self.high_soc_threshold,
            n_low=self.n_low,
            n_med=self.n_med,
            n_high=self.n_high,
        )

        worth_windows = select_worth_selling_windows(
            today_export_rates,
            n_pct=n_pct,
            replacement_cost_pence=self.replacement_cost_pence,
            round_trip_efficiency=self.round_trip_efficiency,
        )
        expected_profitable_export_kwh = estimate_profitable_export_kwh(
            worth_windows,
            max_discharge_kw=self.max_discharge_kw,
        )

        worth_charging_kwh = (
            expected_demand_kwh
            + expected_profitable_export_kwh
            - expected_solar_kwh
        )

        if worth_charging_kwh <= 0:
            target_soc = int(inputs.min_soc)
        else:
            target_soc = int(
                inputs.min_soc
                + (worth_charging_kwh / inputs.battery_capacity_kwh * 100)
            )
        target_soc = max(int(inputs.min_soc), min(self.max_target_soc, target_soc))

        for slot in state.slots:
            if slot.grid_charge:
                slot.capacity_soc = target_soc

        state.reason_log.append(
            f"CheapRateCharge: target {target_soc}% via {n_reason} "
            f"(demand {expected_demand_kwh:.1f} kWh, "
            f"solar {expected_solar_kwh:.1f} kWh, "
            f"profitable export {expected_profitable_export_kwh:.1f} kWh "
            f"from {len(worth_windows)} slots, "
            f"worth charging {worth_charging_kwh:.1f} kWh)"
        )
        return state
