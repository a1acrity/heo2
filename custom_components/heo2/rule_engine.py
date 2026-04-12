"""Rule engine for HEO II programme calculation. No Home Assistant imports."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import ProgrammeState, ProgrammeInputs


class Rule(ABC):
    """Abstract base class for all programme rules."""

    name: str = "unnamed"
    description: str = ""
    enabled: bool = True

    @abstractmethod
    def apply(self, state: ProgrammeState, inputs: ProgrammeInputs) -> ProgrammeState:
        """Modify programme state. Append reasoning to state.reason_log."""


class RuleEngine:
    """Evaluates rules in priority order to produce a 6-slot programme."""

    def __init__(self, rules: list[Rule]):
        self._rules = rules

    def calculate(self, inputs: ProgrammeInputs) -> ProgrammeState:
        """Run all enabled rules and return the final programme."""
        state = ProgrammeState.default(min_soc=int(inputs.min_soc))

        for rule in self._rules:
            if rule.enabled:
                state = rule.apply(state, inputs)

        return state
