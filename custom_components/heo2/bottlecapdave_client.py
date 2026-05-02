# custom_components/heo2/bottlecapdave_client.py
"""BottlecapDave Octopus Energy integration adapter.

Reads live, published Octopus rates from BottlecapDave's `octopus_energy`
HACS integration entities. This is HEO II's PRIMARY source for rates;
AgilePredict is fallback only for prices beyond what BottlecapDave
currently knows (typically tomorrow-after-tomorrow on Agile Outgoing).

Defined in docs/SPEC.md hard rule H4 (Live-prices-only writes): the
inverter is only ever programmed using prices Octopus has actually
published. Predictions are for INTERNAL planning ONLY.

## Entity patterns

BottlecapDave creates entities of the form:

    sensor.octopus_energy_electricity_{mpan}_{serial}_current_rate
    sensor.octopus_energy_electricity_{mpan}_{serial}_export_current_rate
    event.octopus_energy_electricity_{mpan}_{serial}_current_day_rates
    event.octopus_energy_electricity_{mpan}_{serial}_next_day_rates
    event.octopus_energy_electricity_{mpan}_{serial}_export_current_day_rates
    event.octopus_energy_electricity_{mpan}_{serial}_export_next_day_rates

State of `*_current_rate` sensors is the rate in GBP/kWh (so 0.132 = 13.2p).
The `event.*_current_day_rates` entities expose all 30-min slots in their
`attributes.rates`. Each rate dict has shape:

    {
      "start": "2026-04-30T23:30:00+01:00",
      "end":   "2026-04-31T00:00:00+01:00",
      "value_inc_vat": 0.04952,
      "is_capped": False,
      "is_intelligent_adjusted": False,
    }

We convert GBP/kWh to pence (x100) at the boundary because HEO II
internally works in pence.

## Auto-discovery

The MPAN/serial varies per install, so we don't hard-code. The discovery
helpers below scan entity ids for the {mpan}_{serial} key chunk and pick
the meter whose entities were most recently updated. A multi-MPAN install
will prefer the active meter automatically.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Mapping

from .models import RateSlot

logger = logging.getLogger(__name__)

# Conversion factor: BottlecapDave publishes GBP/kWh, HEO II uses p/kWh.
GBP_TO_PENCE = 100.0

# Single regex for both `_current_day_rates` and `_next_day_rates`,
# import (no `_export_`) and export (with `_export_`).
#
# The non-greedy `(?P<key>.+?)` lets the engine pin the suffix first.
# For an export entity id, the optional `(?P<export>export_)?` group
# captures `export_` and `key` ends just before it. For an import entity
# id the engine extends `key` further until the suffix matches without
# `export_`. This is why a single combined regex works where two greedy
# patterns would both match an export entity.
RATES_EVENT_PATTERN = re.compile(
    r"^event\.octopus_energy_electricity_"
    r"(?P<key>.+?)"
    r"_(?P<export>export_)?(?P<period>current|next)_day_rates$",
    re.IGNORECASE,
)

RATE_SENSOR_PATTERN = re.compile(
    r"^sensor\.octopus_energy_electricity_"
    r"(?P<key>.+?)"
    r"_(?P<export>export_)?current_rate$",
    re.IGNORECASE,
)


@dataclass
class BottlecapDaveRates:
    """Snapshot of all rates HEO II reads from BottlecapDave.

    Every rate is in pence/kWh (already converted from GBP).
    Lists are empty and `*_now_pence` are None when the corresponding
    BottlecapDave entity is missing or unavailable - callers must
    handle absence rather than falling back silently.
    """

    import_today: list[RateSlot] = field(default_factory=list)
    import_tomorrow: list[RateSlot] = field(default_factory=list)
    export_today: list[RateSlot] = field(default_factory=list)
    export_tomorrow: list[RateSlot] = field(default_factory=list)
    import_now_pence: float | None = None
    export_now_pence: float | None = None
    meter_key: str | None = None

    @property
    def has_any_data(self) -> bool:
        """True if at least one BD entity returned usable data."""
        return bool(
            self.import_today
            or self.import_tomorrow
            or self.export_today
            or self.export_tomorrow
            or self.import_now_pence is not None
            or self.export_now_pence is not None
        )


# ---------------------------------------------------------------------------
# Pure helpers (no Home Assistant imports) - unit-testable in isolation
# ---------------------------------------------------------------------------


def discover_meter_keys(entity_ids: Iterable[str]) -> set[str]:
    """Return all unique BottlecapDave meter keys (`{mpan}_{serial}`) found
    in the given entity ids. Empty set if BottlecapDave isn't installed.
    """
    keys: set[str] = set()
    for eid in entity_ids:
        if not isinstance(eid, str):
            continue
        norm = eid.lower()
        for pattern in (RATES_EVENT_PATTERN, RATE_SENSOR_PATTERN):
            m = pattern.match(norm)
            if m:
                keys.add(m.group("key"))
                break
    return keys


def pick_freshest_meter_key(
    keys_to_freshness: Mapping[str, datetime | None],
) -> str | None:
    """Pick the meter key with the most recent freshness timestamp.

    Multi-MPAN installs publish entities for each meter. Picking the one
    whose entities updated most recently selects the active meter without
    any per-install configuration.

    `None` freshness sorts oldest. Returns None if input is empty.
    """
    if not keys_to_freshness:
        return None
    oldest = datetime.min.replace(tzinfo=timezone.utc)

    def freshness(key: str) -> datetime:
        ts = keys_to_freshness.get(key)
        return ts if ts is not None else oldest

    return max(keys_to_freshness, key=freshness)


def parse_event_rates(attr_rates: object) -> list[RateSlot]:
    """Convert BottlecapDave's `event.*_day_rates.attributes.rates` list
    into RateSlots, applying GBP -> pence conversion.

    Tolerates malformed individual entries (skips), missing keys, and
    non-list inputs (returns empty). Returned slots are UTC-aware and
    sorted by start time.
    """
    if not isinstance(attr_rates, list) or not attr_rates:
        return []

    out: list[RateSlot] = []
    for entry in attr_rates:
        if not isinstance(entry, dict):
            continue
        start_raw = entry.get("start")
        end_raw = entry.get("end")
        value_raw = entry.get("value_inc_vat")
        if start_raw is None or end_raw is None or value_raw is None:
            continue
        try:
            start = datetime.fromisoformat(str(start_raw)).astimezone(timezone.utc)
            end = datetime.fromisoformat(str(end_raw)).astimezone(timezone.utc)
            pence = float(value_raw) * GBP_TO_PENCE
        except (ValueError, TypeError):
            continue
        out.append(RateSlot(start=start, end=end, rate_pence=pence))

    out.sort(key=lambda r: r.start)
    return out


def parse_current_rate_pence(state: object) -> float | None:
    """Convert a BottlecapDave `*_current_rate` sensor state to pence.

    BD publishes the value as a string-typed float in GBP/kWh. Returns
    None when the entity is missing or in an unknown/unavailable state.
    """
    if state is None:
        return None
    text = str(state).strip().lower()
    if text in ("", "unknown", "unavailable", "none"):
        return None
    try:
        return float(state) * GBP_TO_PENCE
    except (ValueError, TypeError):
        return None


def merge_rate_sources(
    live: list[RateSlot],
    fallback: list[RateSlot],
) -> list[RateSlot]:
    """Merge a primary `live` rates list with a `fallback` list.

    Used to extend BD's published rates (live) with AgilePredict / IGO
    fixed slots (fallback) for windows BD doesn't yet cover. A fallback
    slot is dropped if it overlaps any live slot - live always wins.

    Result is sorted by start time. Either input may be empty.
    """
    if not fallback:
        return list(live)
    if not live:
        return list(fallback)

    def overlaps(a: RateSlot, b: RateSlot) -> bool:
        return a.start < b.end and b.start < a.end

    out = list(live)
    for f in fallback:
        if not any(overlaps(f, l) for l in live):
            out.append(f)
    out.sort(key=lambda r: r.start)
    return out


# ---------------------------------------------------------------------------
# Home Assistant adapter
# ---------------------------------------------------------------------------


def read_bottlecapdave_rates(hass) -> BottlecapDaveRates:
    """Read all BottlecapDave rate data from HA into a single snapshot.

    Returns an empty `BottlecapDaveRates` when BottlecapDave isn't
    installed or no entities are populated yet (HA startup race).
    Never raises - always returns a usable structure so the coordinator
    can keep ticking. Caller checks `has_any_data` to decide whether to
    proceed with writes (H4) or fall back.
    """
    if hass is None or getattr(hass, "states", None) is None:
        return BottlecapDaveRates()

    try:
        all_states = list(hass.states.async_all())
    except Exception:  # pragma: no cover - defensive for stub envs
        return BottlecapDaveRates()

    # Group states by meter key, tracking max last_updated for freshness.
    keys_to_freshness: dict[str, datetime | None] = {}
    for state in all_states:
        eid = getattr(state, "entity_id", "")
        if not eid:
            continue
        norm = eid.lower()
        match = (
            RATES_EVENT_PATTERN.match(norm)
            or RATE_SENSOR_PATTERN.match(norm)
        )
        if not match:
            continue
        key = match.group("key")
        ts = getattr(state, "last_updated", None)
        prev = keys_to_freshness.get(key)
        if prev is None or (ts is not None and ts > prev):
            keys_to_freshness[key] = ts

    chosen = pick_freshest_meter_key(keys_to_freshness)
    if chosen is None:
        return BottlecapDaveRates()

    base = f"octopus_energy_electricity_{chosen}"

    def _state(entity_id: str):
        st = hass.states.get(entity_id)
        if st is None:
            return None
        return st.state

    def _attr_rates(entity_id: str):
        st = hass.states.get(entity_id)
        if st is None:
            return None
        attrs = getattr(st, "attributes", None) or {}
        return attrs.get("rates")

    return BottlecapDaveRates(
        import_today=parse_event_rates(_attr_rates(f"event.{base}_current_day_rates")),
        import_tomorrow=parse_event_rates(_attr_rates(f"event.{base}_next_day_rates")),
        export_today=parse_event_rates(
            _attr_rates(f"event.{base}_export_current_day_rates")
        ),
        export_tomorrow=parse_event_rates(
            _attr_rates(f"event.{base}_export_next_day_rates")
        ),
        import_now_pence=parse_current_rate_pence(_state(f"sensor.{base}_current_rate")),
        export_now_pence=parse_current_rate_pence(
            _state(f"sensor.{base}_export_current_rate")
        ),
        meter_key=chosen,
    )
