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
from .safety import SafetyRule


def default_rules() -> list[Rule]:
    """Return the 8 default rules in priority order.

    SafetyRule is always last and cannot be disabled.
    """
    return [
        BaselineRule(),
        CheapRateChargeRule(),
        SolarSurplusRule(),
        ExportWindowRule(),
        EveningProtectRule(),
        IGODispatchRule(),
        EVChargingRule(),
        SafetyRule(),
    ]
