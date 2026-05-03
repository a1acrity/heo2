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
from .winter_low_pv import WinterLowPVRule
from .eps_mode import EPSModeRule
from .safety import SafetyRule


def default_rules() -> list[Rule]:
    """Return the 11 default rules in priority order.

    SafetyRule is always last and cannot be disabled. EPSModeRule sits
    JUST before SafetyRule because it must override every other rule's
    decisions (SPEC H3: drop SOC floor to 0% during grid loss).
    WinterLowPVRule sits AFTER ExportWindow + EveningProtect so it can
    raise floors that those rules may have lowered, but BEFORE
    SavingSession / IGODispatch / EV / EPS so those event-triggered
    rules can still override the seasonal default.
    """
    return [
        BaselineRule(),
        CheapRateChargeRule(),
        SolarSurplusRule(),
        ExportWindowRule(),
        EveningProtectRule(),
        WinterLowPVRule(),
        SavingSessionRule(),
        IGODispatchRule(),
        EVChargingRule(),
        EPSModeRule(),
        SafetyRule(),
    ]
