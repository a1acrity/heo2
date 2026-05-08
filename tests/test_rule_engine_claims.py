"""Unit tests for the claims/arbitration machinery (HEO-31 Phase 3 PR 2).

Pin the StateBuilder / Claim / arbitration semantics independent of
any specific rule. Existing rule behaviour tests stay in
`test_rules/`; the precedence regression net stays in
`test_rules/test_precedence.py`. This file covers the engine itself.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from heo2.models import ProgrammeInputs, ProgrammeState
from heo2.rule_engine import (
    Claim,
    PRIO_BASELINE,
    PRIO_EPS,
    PRIO_SAVING_SESSION,
    Rule,
    RuleEngine,
    StateBuilder,
)


def _inputs() -> ProgrammeInputs:
    return ProgrammeInputs(
        now=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
        current_soc=50.0,
        battery_capacity_kwh=20.0,
        min_soc=20.0,
        import_rates=[],
        export_rates=[],
        solar_forecast_kwh=[0.0] * 24,
        load_forecast_kwh=[0.5] * 24,
        igo_dispatching=False,
        saving_session=False,
        saving_session_start=None,
        saving_session_end=None,
        ev_charging=False,
        grid_connected=True,
        active_appliances=[],
        appliance_expected_kwh=0.0,
    )


class _SocAt:
    """Helper rule that writes capacity_soc on a fixed slot at a fixed
    priority. Used to drive arbitration tests without dragging in real
    rule logic."""

    def __init__(self, name, slot_idx, value, prio, reason="set"):
        self.name = name
        self.description = ""
        self.enabled = True
        self.priority_class = prio
        self._slot = slot_idx
        self._val = value
        self._reason = reason

    def propose(self, view, inputs):
        view.claim_slot(self._slot, "capacity_soc", self._val, reason=self._reason)
        view.log(f"{self.name}: slot {self._slot} -> {self._val}")


# Plumbing for the @abstractmethod check
class _StubRule(Rule):
    name = "stub"
    priority_class = 50

    def propose(self, view, inputs):
        view.claim_slot(0, "capacity_soc", 80, reason="stub")


class TestStateBuilderSeed:
    def test_reads_return_seed_when_no_claim_made(self):
        initial = ProgrammeState.default(min_soc=15)
        builder = StateBuilder(initial)
        # Seed values from the default programme
        for i in range(6):
            assert builder.get_slot(i, "capacity_soc") == 15
            assert builder.get_slot(i, "grid_charge") is False
        assert builder.get_global("work_mode") is None

    def test_seed_priority_below_any_rule_priority(self):
        """A rule's first claim must override the seed regardless of
        its priority_class."""
        initial = ProgrammeState.default(min_soc=15)
        builder = StateBuilder(initial)
        rule = _SocAt("rule_a", slot_idx=0, value=80, prio=1)
        view = builder.view_for_rule(rule)
        rule.propose(view, _inputs())
        assert builder.get_slot(0, "capacity_soc") == 80


class TestArbitration:
    def test_higher_priority_wins(self):
        initial = ProgrammeState.default(min_soc=15)
        builder = StateBuilder(initial)
        for rule in [
            _SocAt("low", 0, 30, PRIO_BASELINE),
            _SocAt("high", 0, 80, PRIO_SAVING_SESSION),
        ]:
            view = builder.view_for_rule(rule)
            rule.propose(view, _inputs())
        state = builder.materialise()
        assert state.slots[0].capacity_soc == 80

    def test_priority_order_independent_of_proposal_order(self):
        """Same two claims, different proposal order, same outcome."""
        for order in (("low_first", "high_first"), ("high_first", "low_first")):
            initial = ProgrammeState.default(min_soc=15)
            builder = StateBuilder(initial)
            rules = {
                "low_first": _SocAt("low", 0, 30, PRIO_BASELINE),
                "high_first": _SocAt("high", 0, 80, PRIO_SAVING_SESSION),
            }
            for n in order:
                rule = rules[n]
                view = builder.view_for_rule(rule)
                rule.propose(view, _inputs())
            state = builder.materialise()
            assert state.slots[0].capacity_soc == 80

    def test_tie_broken_by_insertion_order(self):
        """Same priority, later proposer wins (matches legacy
        last-writer-wins)."""
        initial = ProgrammeState.default(min_soc=15)
        builder = StateBuilder(initial)
        for rule in [
            _SocAt("first", 0, 30, prio=50),
            _SocAt("second", 0, 80, prio=50),
        ]:
            view = builder.view_for_rule(rule)
            rule.propose(view, _inputs())
        state = builder.materialise()
        assert state.slots[0].capacity_soc == 80

    def test_claim_on_one_slot_does_not_affect_others(self):
        initial = ProgrammeState.default(min_soc=15)
        builder = StateBuilder(initial)
        rule = _SocAt("only_slot_2", 2, 70, PRIO_EPS)
        view = builder.view_for_rule(rule)
        rule.propose(view, _inputs())
        state = builder.materialise()
        assert state.slots[0].capacity_soc == 15
        assert state.slots[1].capacity_soc == 15
        assert state.slots[2].capacity_soc == 70
        assert state.slots[3].capacity_soc == 15


class TestPropoeReadsCurrentWinner:
    """A rule reading `view.get_slot(...)` should see the highest-
    priority claim made so far, regardless of subsequent rules."""

    def test_late_rule_reads_current_winner(self):
        initial = ProgrammeState.default(min_soc=15)
        builder = StateBuilder(initial)

        class _PostHoc:
            name = "post"
            description = ""
            enabled = True
            priority_class = 100

            def __init__(self):
                self.observed = None

            def propose(self_, view, inputs):
                self_.observed = view.get_slot(0, "capacity_soc")
                view.claim_slot(0, "capacity_soc", 90, reason="post")

        for rule in [
            _SocAt("baseline", 0, 30, PRIO_BASELINE),
            _SocAt("ss", 0, 70, PRIO_SAVING_SESSION),
        ]:
            view = builder.view_for_rule(rule)
            rule.propose(view, _inputs())
        post_rule = _PostHoc()
        view = builder.view_for_rule(post_rule)
        post_rule.propose(view, _inputs())
        # The post-hoc rule saw 70 (SavingSession's claim, the winner
        # at that point), not 30 (Baseline) or 15 (seed).
        assert post_rule.observed == 70


class TestApplyShimBackCompat:
    """`Rule.apply(state, inputs)` is the legacy entry point. Tests
    that call apply() directly must keep working: reads return state
    values seeded into the builder, the rule's claims override them,
    and the materialised state reflects the rule's writes."""

    def test_apply_propose_rule_returns_modified_state(self):
        rule = _StubRule()
        initial = ProgrammeState.default(min_soc=15)
        out = rule.apply(initial, _inputs())
        assert out.slots[0].capacity_soc == 80

    def test_apply_does_not_mutate_input_state(self):
        rule = _StubRule()
        initial = ProgrammeState.default(min_soc=15)
        rule.apply(initial, _inputs())
        # Initial state must be untouched (rules return new state).
        assert initial.slots[0].capacity_soc == 15

    def test_apply_seeds_reads_from_state(self):
        """A rule reading view.get_slot should see whatever the input
        state had; its own claim then overrides."""
        observations = {}

        class _Reader(Rule):
            name = "reader"
            priority_class = 50

            def propose(self_, view, inputs):
                observations["pre"] = view.get_slot(0, "capacity_soc")
                view.claim_slot(0, "capacity_soc", 99, reason="reader")
                observations["post"] = view.get_slot(0, "capacity_soc")

        initial = ProgrammeState.default(min_soc=42)
        rule = _Reader()
        out = rule.apply(initial, _inputs())
        assert observations["pre"] == 42  # seed from state
        assert observations["post"] == 99
        assert out.slots[0].capacity_soc == 99


class TestClaimsLog:
    def test_claims_log_records_all_non_seed_claims(self):
        initial = ProgrammeState.default(min_soc=15)
        builder = StateBuilder(initial)
        for rule in [
            _SocAt("a", 0, 30, prio=10),
            _SocAt("b", 0, 80, prio=70),
            _SocAt("c", 1, 40, prio=20),
        ]:
            view = builder.view_for_rule(rule)
            rule.propose(view, _inputs())
        state = builder.materialise()
        assert len(state.claims_log) == 3
        rule_names = sorted(c.rule_name for c in state.claims_log)
        assert rule_names == ["a", "b", "c"]
        # Seeds excluded
        assert all(c.priority != StateBuilder.SEED_PRIORITY for c in state.claims_log)

    def test_claims_log_preserves_losing_claims(self):
        """Losing claims are kept so the dashboard can render the full
        chain ('Baseline 100% -> CheapRateCharge 80% -> EPS 0%'
        winner: EPS)."""
        initial = ProgrammeState.default(min_soc=15)
        builder = StateBuilder(initial)
        for rule in [
            _SocAt("baseline", 0, 100, PRIO_BASELINE),
            _SocAt("eps", 0, 0, PRIO_EPS),
        ]:
            view = builder.view_for_rule(rule)
            rule.propose(view, _inputs())
        state = builder.materialise()
        soc_claims = [
            c for c in state.claims_log
            if c.field == "capacity_soc" and c.slot_index == 0
        ]
        assert len(soc_claims) == 2
        assert state.slots[0].capacity_soc == 0  # EPS won


class TestSafetyRuleStaysOutsideArbitration:
    def test_safety_runs_post_arbitration_via_legacy_apply(self):
        """Any rule named 'safety' is collected and run after
        materialisation, mutating the state directly. This preserves
        the invariant-pass shape (clamping/snapping/contiguity)."""
        applied = []

        class _FakeSafety(Rule):
            name = "safety"
            priority_class = 999

            def apply(self, state, inputs):
                applied.append(True)
                # Mutate to prove we ran post-arbitration.
                state.slots[0].capacity_soc = 0
                return state

            def propose(self, view, inputs):
                # Should NEVER be called
                view.log("propose called incorrectly")
                view.claim_slot(0, "capacity_soc", 999, reason="propose")

        # A regular rule that would otherwise have set slot 0 to 80.
        class _Setter(Rule):
            name = "setter"
            priority_class = 50

            def propose(self, view, inputs):
                view.claim_slot(0, "capacity_soc", 80, reason="setter")

        engine = RuleEngine(rules=[_Setter(), _FakeSafety()])
        out = engine.calculate(_inputs())
        assert applied == [True]
        # Safety's apply mutation wins; propose was bypassed
        assert out.slots[0].capacity_soc == 0
        assert "propose called incorrectly" not in out.reason_log


class TestEngineRunsRules:
    def test_engine_skips_disabled(self):
        rule = _SocAt("a", 0, 80, PRIO_BASELINE)
        rule.enabled = False
        engine = RuleEngine(rules=[rule])
        out = engine.calculate(_inputs())
        assert out.slots[0].capacity_soc == 20  # seed (min_soc)

    def test_engine_applies_min_soc_seed(self):
        engine = RuleEngine(rules=[])
        out = engine.calculate(_inputs())
        for s in out.slots:
            assert s.capacity_soc == 20
