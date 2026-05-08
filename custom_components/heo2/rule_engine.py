"""Rule engine for HEO II programme calculation. No Home Assistant imports.

Phase 3 (HEO-31) replaced the original "each rule mutates state, last
writer wins" pipeline with a claims-and-arbitration model. Every
proposed write is a `Claim` carrying `field`, `value`, `priority`, and
`reason`. The arbiter picks the highest-priority claim per (field,
slot). Two reasons:

1. Removes the implicit "execution-order defines precedence" semantics
   that made the matrix in `docs/rule_field_overlap.md` necessary in
   the first place. Precedence is now an explicit number in the rule.
2. Surfaces the reason chain. `state.claims_log` records every claim
   each rule made and which one won, so the dashboard can answer
   "why is slot 3 at 60%?" by walking the per-field claim list.

`state.reason_log` is kept as the existing flat list of summary
strings rules append via `builder.log()`. The structured per-claim
trace lives in `state.claims_log` so existing tests / sensors that
assert on `reason_log` shape keep working.

`Rule.apply(state, inputs)` is preserved as a back-compat shim that
funnels into `propose()` via a single-rule builder. Existing tests
that call `rule.apply(...)` directly keep working without
modification.

`SafetyRule` does NOT participate in the propose phase - it's an
invariant enforcer (clamping SOC ranges, snapping 5-min granularity,
fixing contiguity) that runs as a final post-arbitration pass. Same
shape as before.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .models import ProgrammeState, ProgrammeInputs


# Standard priority bands. Use these or pick a value within the same
# band rather than a free-floating int, so the registry is readable.
# Higher wins. Bands chosen so per-rule fine-tuning has room without
# bumping into adjacent rule classes.
PRIO_BASELINE = 10           # scaffolder; anything overrides
PRIO_CHEAP_RATE_CHARGE = 20
PRIO_SOLAR_SURPLUS = 30
PRIO_EXPORT_WINDOW = 40
PRIO_EVENING_PROTECT = 50
PRIO_PEAK_EXPORT_ARBITRAGE = 60
PRIO_SAVING_SESSION = 70
PRIO_IGO_DISPATCH = 80
PRIO_EV_DEFERRAL = 90
PRIO_EV_CHARGING = 100
PRIO_EPS = 110               # second-to-last; overrides everything


@dataclass
class Claim:
    """A proposed write by a rule.

    `field` is one of the writable fields on `ProgrammeState` /
    `SlotConfig`: `capacity_soc`, `grid_charge`, `work_mode`,
    `energy_pattern`, `max_charge_a`, `max_discharge_a`,
    `ev_deferral_active`. `slot_index` is None for global fields and
    0..5 for per-slot.

    `priority` decides arbitration: higher wins; ties broken by the
    rule's order in `default_rules()` (later wins, matching legacy
    last-writer-wins).
    """
    field: str
    value: Any
    priority: int
    reason: str
    rule_name: str
    slot_index: int | None = None
    insertion_order: int = 0


class _RuleView:
    """A builder view pre-bound to a rule. Returned by
    `StateBuilder.view_for_rule()`. Rules call `claim_*` and `log` on
    the view; the underlying builder records the rule_name for free.
    """

    def __init__(self, builder: StateBuilder, rule: Rule):
        self._b = builder
        self._rule = rule

    def claim_slot(self, slot_index: int, field_name: str, value, *, reason: str, priority: int | None = None) -> None:
        self._b._record_claim(
            field_name=field_name,
            value=value,
            slot_index=slot_index,
            priority=priority if priority is not None else self._rule.priority_class,
            reason=reason,
            rule_name=self._rule.name,
        )

    def claim_global(self, field_name: str, value, *, reason: str, priority: int | None = None) -> None:
        self._b._record_claim(
            field_name=field_name,
            value=value,
            slot_index=None,
            priority=priority if priority is not None else self._rule.priority_class,
            reason=reason,
            rule_name=self._rule.name,
        )

    def get_slot(self, slot_index: int, field_name: str):
        return self._b.get_slot(slot_index, field_name)

    def get_global(self, field_name: str):
        return self._b.get_global(field_name)

    @property
    def slots(self):
        """Read-only view of the current winning per-slot values, as a
        list of read-shaped objects with `.capacity_soc`, `.grid_charge`,
        `.start_time`, `.end_time` attributes. Useful for `for slot in
        view.slots:` patterns ported from the old apply-style code."""
        return self._b.slot_views()

    def find_slot_at(self, t):
        """Mirror of `ProgrammeState.find_slot_at` over the slot views."""
        return self._b.find_slot_at(t)

    def log(self, message: str) -> None:
        self._b._reason_log.append(message)


class StateBuilder:
    """Accumulates claims; rules read the highest-priority claim so far.

    Reads return either:
    1. The highest-priority claim's value for that (field, slot_index)
       so far, or
    2. The seeded value from the initial state if no rule has claimed
       (seed priority is below all rule priorities so any claim wins).

    `materialise()` folds claims into a final `ProgrammeState`.
    """

    SEED_PRIORITY = -1

    def __init__(self, initial: ProgrammeState):
        self._claims: list[Claim] = []
        self._next_insertion = 0
        self._reason_log: list[str] = list(initial.reason_log)
        # Seed claims from the initial state so rules can read it.
        # Seed priority is below any rule priority so the first claim
        # made by any rule overrides the seed.
        for i, slot in enumerate(initial.slots):
            self._seed_slot(i, "capacity_soc", slot.capacity_soc)
            self._seed_slot(i, "grid_charge", slot.grid_charge)
            self._seed_slot(i, "start_time", slot.start_time)
            self._seed_slot(i, "end_time", slot.end_time)
        self._seed_global("work_mode", initial.work_mode)
        self._seed_global("energy_pattern", initial.energy_pattern)
        self._seed_global("max_charge_a", initial.max_charge_a)
        self._seed_global("max_discharge_a", initial.max_discharge_a)
        self._seed_global("ev_deferral_active", initial.ev_deferral_active)
        self._initial = initial

    @classmethod
    def from_state(cls, state: ProgrammeState, *, seed_priority: int = SEED_PRIORITY) -> StateBuilder:
        """Create a builder seeded from `state`. Used by the back-compat
        `Rule.apply()` shim - tests calling rule.apply(state, inputs)
        get a builder where reads return whatever is currently in state.
        """
        b = cls(state)
        # Override seed priority if requested. The shim sets this so
        # the rule's own claims (at priority_class) override the seeds
        # from state but reads return state values until claimed.
        if seed_priority != cls.SEED_PRIORITY:
            for c in b._claims:
                c.priority = seed_priority
        return b

    def _seed_slot(self, slot_index: int, field_name: str, value) -> None:
        self._claims.append(Claim(
            field=field_name,
            value=value,
            priority=self.SEED_PRIORITY,
            reason="seed from initial state",
            rule_name="<seed>",
            slot_index=slot_index,
            insertion_order=self._next_insertion,
        ))
        self._next_insertion += 1

    def _seed_global(self, field_name: str, value) -> None:
        self._claims.append(Claim(
            field=field_name,
            value=value,
            priority=self.SEED_PRIORITY,
            reason="seed from initial state",
            rule_name="<seed>",
            slot_index=None,
            insertion_order=self._next_insertion,
        ))
        self._next_insertion += 1

    def _record_claim(self, *, field_name, value, slot_index, priority, reason, rule_name) -> None:
        self._claims.append(Claim(
            field=field_name,
            value=value,
            priority=priority,
            reason=reason,
            rule_name=rule_name,
            slot_index=slot_index,
            insertion_order=self._next_insertion,
        ))
        self._next_insertion += 1

    def view_for_rule(self, rule: Rule) -> _RuleView:
        """Return a view pre-bound to `rule`. Rules use this view for
        all claim writes / reads / log appends; rule_name is recorded
        automatically."""
        return _RuleView(self, rule)

    # --- reads --------------------------------------------------------

    def _winning(self, field_name: str, slot_index: int | None):
        candidates = [
            c for c in self._claims
            if c.field == field_name and c.slot_index == slot_index
        ]
        if not candidates:
            return None
        # Highest priority wins; ties broken by insertion order
        # (later insertion wins, matching legacy "last writer wins").
        candidates.sort(key=lambda c: (c.priority, c.insertion_order))
        return candidates[-1]

    def get_slot(self, slot_index: int, field_name: str):
        c = self._winning(field_name, slot_index)
        return c.value if c is not None else None

    def get_global(self, field_name: str):
        c = self._winning(field_name, None)
        return c.value if c is not None else None

    def slot_views(self) -> list[_SlotView]:
        return [_SlotView(self, i) for i in range(len(self._initial.slots))]

    def find_slot_at(self, t):
        for i, view in enumerate(self.slot_views()):
            if view.contains_time(t):
                return i
        raise ValueError(f"No slot contains {t}")

    # --- materialisation ---------------------------------------------

    def materialise(self) -> ProgrammeState:
        """Fold claims into a `ProgrammeState`."""
        from copy import deepcopy
        state = deepcopy(self._initial)
        n_slots = len(state.slots)

        for i in range(n_slots):
            for f in ("capacity_soc", "grid_charge", "start_time", "end_time"):
                c = self._winning(f, i)
                if c is not None:
                    setattr(state.slots[i], f, c.value)

        for f in (
            "work_mode",
            "energy_pattern",
            "max_charge_a",
            "max_discharge_a",
            "ev_deferral_active",
        ):
            c = self._winning(f, None)
            if c is not None:
                setattr(state, f, c.value)

        # reason_log already accumulated during proposes via builder.log()
        state.reason_log = list(self._reason_log)

        # Structured per-field claim trace for the dashboard / debugging.
        state.claims_log = [
            c for c in self._claims if c.priority != self.SEED_PRIORITY
        ]
        return state


class _SlotView:
    """Read-only view of one slot's currently-winning values. Mirrors
    the `SlotConfig` attribute surface so rule code ported from the
    old `for slot in state.slots:` pattern reads naturally.
    """

    def __init__(self, builder: StateBuilder, slot_index: int):
        self._b = builder
        self._idx = slot_index

    @property
    def index(self) -> int:
        return self._idx

    @property
    def capacity_soc(self) -> int:
        return self._b.get_slot(self._idx, "capacity_soc")

    @property
    def grid_charge(self) -> bool:
        return self._b.get_slot(self._idx, "grid_charge")

    @property
    def start_time(self):
        return self._b.get_slot(self._idx, "start_time")

    @property
    def end_time(self):
        return self._b.get_slot(self._idx, "end_time")

    def contains_time(self, t) -> bool:
        from datetime import time as _time
        if self.start_time <= self.end_time:
            return self.start_time <= t < self.end_time
        return t >= self.start_time or t < self.end_time

    def duration_minutes(self) -> int:
        start_mins = self.start_time.hour * 60 + self.start_time.minute
        end_mins = self.end_time.hour * 60 + self.end_time.minute
        if end_mins <= start_mins:
            end_mins += 1440
        return end_mins - start_mins


class Rule(ABC):
    """Base class for all programme rules.

    Rules implement `propose(view, inputs)` to add claims via the
    `_RuleView` interface. The legacy `apply(state, inputs)` shim is
    provided for back-compat with tests that call `rule.apply(...)`
    directly - it builds a single-rule StateBuilder seeded from
    `state`, runs `propose()`, materialises, and returns.
    """

    name: str = "unnamed"
    description: str = ""
    enabled: bool = True
    priority_class: int = 50

    def propose(self, view: _RuleView, inputs: ProgrammeInputs) -> None:
        """Default implementation falls through to `apply()` for any
        rule that hasn't been ported yet. Newly-written rules should
        override this directly.
        """
        # Materialise the current view into a synthetic state, run the
        # legacy apply() against it, then re-emit the diff as claims.
        # This bridge lets the engine support a mix of ported / legacy
        # rules during the migration window.
        snapshot = view._b.materialise()
        snapshot.reason_log = []  # rules append; we'll capture only
        snapshot.claims_log = []
        post = self.apply(snapshot, inputs)
        # Re-emit any deltas as claims.
        for i, slot in enumerate(post.slots):
            if slot.capacity_soc != view.get_slot(i, "capacity_soc"):
                view.claim_slot(i, "capacity_soc", slot.capacity_soc, reason="legacy apply")
            if slot.grid_charge != view.get_slot(i, "grid_charge"):
                view.claim_slot(i, "grid_charge", slot.grid_charge, reason="legacy apply")
            if slot.start_time != view.get_slot(i, "start_time"):
                view.claim_slot(i, "start_time", slot.start_time, reason="legacy apply")
            if slot.end_time != view.get_slot(i, "end_time"):
                view.claim_slot(i, "end_time", slot.end_time, reason="legacy apply")
        for f in ("work_mode", "energy_pattern", "max_charge_a", "max_discharge_a", "ev_deferral_active"):
            new = getattr(post, f)
            if new != view.get_global(f):
                view.claim_global(f, new, reason="legacy apply")
        for entry in post.reason_log:
            view.log(entry)

    def apply(self, state: ProgrammeState, inputs: ProgrammeInputs) -> ProgrammeState:
        """Back-compat: run this rule alone, seeded from `state`,
        return materialised state. Rules ported to `propose()` use this
        shim path automatically; legacy rules still implement `apply`
        directly and the default `propose()` bridges them.
        """
        # Run propose against a builder seeded from `state` so rule
        # reads return state values, and the rule's own claims (at
        # priority_class) override the seeds.
        builder = StateBuilder(state)
        view = builder.view_for_rule(self)
        self.propose(view, inputs)
        return builder.materialise()


class RuleEngine:
    """Evaluates rules in priority order to produce a 6-slot programme.

    The propose phase runs all enabled non-Safety rules in
    `default_rules()` order, each adding claims via its view. Safety
    runs as a final post-arbitration pass over the materialised state.
    """

    def __init__(self, rules: list[Rule]):
        self._rules = rules

    def calculate(self, inputs: ProgrammeInputs) -> ProgrammeState:
        initial = ProgrammeState.default(min_soc=int(inputs.min_soc))
        builder = StateBuilder(initial)

        safety_rules: list[Rule] = []
        for rule in self._rules:
            if not rule.enabled:
                continue
            if rule.name == "safety":
                safety_rules.append(rule)
                continue
            view = builder.view_for_rule(rule)
            rule.propose(view, inputs)

        state = builder.materialise()
        for safety in safety_rules:
            state = safety.apply(state, inputs)
        return state
