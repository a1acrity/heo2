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
from .safety import SafetyRule


def default_rules() -> list[Rule]:
    """Return the 9 default rules in priority order.

    SafetyRule is always last and cannot be disabled. SavingSessionRule
    sits AFTER ExportWindowRule + EveningProtectRule so an active
    session always wins over a normal export window or evening floor:
    SPEC §9 says drain to min_soc regardless of the standard Agile
    threshold or evening reserve.
    """
    return [
        BaselineRule(),
        CheapRateChargeRule(),
        SolarSurplusRule(),
        ExportWindowRule(),
        EveningProtectRule(),
        SavingSessionRule(),
        IGODispatchRule(),
        EVChargingRule(),
        SafetyRule(),
    ]
