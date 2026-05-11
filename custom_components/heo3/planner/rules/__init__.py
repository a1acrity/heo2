"""Concrete rules. Three tiers."""

from .tier1 import EPSLockdownRule, MinSOCFloorRule
from .tier2 import EVDeferralRule, IGODispatchRule, SavingSessionRule
from .tier3 import (
    CheapRateChargeRule,
    EveningDrainRule,
    PeakExportArbitrageRule,
    SolarSurplusRule,
)

ALL_RULES = (
    # Tier 1 — safety
    EPSLockdownRule,
    MinSOCFloorRule,
    # Tier 2 — modes
    SavingSessionRule,
    IGODispatchRule,
    EVDeferralRule,
    # Tier 3 — optimisation
    CheapRateChargeRule,
    PeakExportArbitrageRule,
    SolarSurplusRule,
    EveningDrainRule,
)
