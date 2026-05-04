# custom_components/heo2/rules/__init__.py
"""HEO II rules package — default rule registry."""

from __future__ import annotations

from ..rule_engine import Rule
from .baseline import BaselineRule
from .cheap_rate_charge import CheapRateChargeRule
from .solar_surplus import SolarSurplusRule
from .export_window import ExportWindowRule
from .evening_protect import EveningProtectRule
from .igo_dispatch import IGODispatchRule
from .ev_charging import EVChargingRule
from .saving_session import SavingSessionRule
from .ev_deferral import EVDeferralRule
from .eps_mode import EPSModeRule
from .peak_export_arbitrage import PeakExportArbitrageRule
from .safety import SafetyRule


def default_rules() -> list[Rule]:
    """Return the 12 default rules in priority order.

    SafetyRule is always last and cannot be disabled. EPSModeRule sits
    JUST before SafetyRule because it must override every other rule's
    decisions (SPEC H3: drop SOC floor to 0% during grid loss).
    EVDeferralRule sits BEFORE EVChargingRule so the deferral signal
    can override EVChargingRule's "hold SOC" behaviour (we WANT to
    drain the battery to grid in deferral mode).

    WinterLowPVRule was removed (PR #67 / 2026-05-03): the old rule
    forced GC slots to 100% whenever sum(solar) < sum(load), which
    undid CheapRateChargeRule's smart bridge-to-PV-takeover sizing.
    Winter behaviour is now handled inside CheapRateChargeRule itself:
    when PV never overtakes load on tomorrow's forecast, the bridge
    accumulates the whole day's deficit and the target clamps to
    max_target_soc - same end-state as the old WinterLowPV did, but
    arrived at via the same maths the rest of the year uses.
    """
    return [
        BaselineRule(),
        CheapRateChargeRule(),
        SolarSurplusRule(),
        ExportWindowRule(),
        EveningProtectRule(),
        PeakExportArbitrageRule(),
        SavingSessionRule(),
        IGODispatchRule(),
        EVDeferralRule(),
        EVChargingRule(),
        EPSModeRule(),
        SafetyRule(),
    ]
