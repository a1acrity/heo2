# tests/test_bottlecapdave_client.py
"""Tests for the BottlecapDave Octopus Energy adapter (HEO-14).

The pure helpers (parsing, discovery, merging) are tested in isolation.
The HA-side `read_bottlecapdave_rates` is exercised through a minimal
mock-hass that mimics `hass.states.async_all()` and `hass.states.get()`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from heo2.bottlecapdave_client import (
    GBP_TO_PENCE,
    BottlecapDaveRates,
    discover_meter_keys,
    merge_rate_sources,
    parse_current_rate_pence,
    parse_event_rates,
    pick_freshest_meter_key,
    read_bottlecapdave_rates,
)
from heo2.models import RateSlot

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Fixtures: realistic BottlecapDave shapes
# ---------------------------------------------------------------------------

# Real-world example shape from BottlecapDave's `event.*_day_rates`. Values
# in GBP/kWh, 30-min slots, ISO-formatted local time.
SAMPLE_EXPORT_TODAY_RATES = [
    {
        "start": "2026-04-30T23:00:00+01:00",
        "end": "2026-04-30T23:30:00+01:00",
        "value_inc_vat": 0.04952,
        "is_capped": False,
        "is_intelligent_adjusted": False,
    },
    {
        "start": "2026-04-30T23:30:00+01:00",
        "end": "2026-05-01T00:00:00+01:00",
        "value_inc_vat": 0.13150,
        "is_capped": False,
        "is_intelligent_adjusted": False,
    },
]

SAMPLE_IMPORT_TODAY_RATES = [
    {
        "start": "2026-04-30T23:30:00+01:00",
        "end": "2026-05-01T00:00:00+01:00",
        "value_inc_vat": 0.04952,  # IGO off-peak
        "is_capped": False,
    },
    {
        "start": "2026-05-01T05:30:00+01:00",
        "end": "2026-05-01T06:00:00+01:00",
        "value_inc_vat": 0.24842,  # IGO peak
        "is_capped": False,
    },
]


def _state(entity_id: str, value: object, last_updated: datetime | None = None,
           attributes: dict | None = None):
    """Build a State-like object suitable for hass.states.get() / async_all()."""
    return SimpleNamespace(
        entity_id=entity_id,
        state=value,
        last_updated=last_updated,
        attributes=attributes or {},
    )


def _make_hass(states: list):
    """Mock hass with `states.get()` and `states.async_all()`."""
    by_id = {s.entity_id: s for s in states}

    class _States:
        def get(self, entity_id):
            return by_id.get(entity_id)

        def async_all(self):
            return list(by_id.values())

    return SimpleNamespace(states=_States())


# ---------------------------------------------------------------------------
# parse_event_rates
# ---------------------------------------------------------------------------


class TestParseEventRates:
    def test_converts_gbp_to_pence(self):
        rates = parse_event_rates(SAMPLE_EXPORT_TODAY_RATES)
        # 0.04952 GBP/kWh * 100 = 4.952 p/kWh
        assert rates[0].rate_pence == pytest.approx(4.952)
        # 0.13150 * 100 = 13.150
        assert rates[1].rate_pence == pytest.approx(13.150)

    def test_returns_utc_aware_slots(self):
        rates = parse_event_rates(SAMPLE_EXPORT_TODAY_RATES)
        # 23:00 +01:00 == 22:00 UTC
        assert rates[0].start == datetime(2026, 4, 30, 22, 0, tzinfo=UTC)
        assert rates[0].end == datetime(2026, 4, 30, 22, 30, tzinfo=UTC)
        assert rates[0].start.tzinfo is not None

    def test_sorts_by_start_time(self):
        unsorted = [
            SAMPLE_EXPORT_TODAY_RATES[1],  # 23:30
            SAMPLE_EXPORT_TODAY_RATES[0],  # 23:00
        ]
        rates = parse_event_rates(unsorted)
        assert rates[0].start < rates[1].start

    def test_empty_input_returns_empty(self):
        assert parse_event_rates([]) == []
        assert parse_event_rates(None) == []

    def test_non_list_input_returns_empty(self):
        assert parse_event_rates("not a list") == []
        assert parse_event_rates({"rates": []}) == []

    def test_skips_malformed_entries(self):
        bad = [
            {"start": "2026-04-30T23:00:00+01:00",
             "end": "2026-04-30T23:30:00+01:00",
             "value_inc_vat": 0.05},
            {"start": "broken", "end": "broken", "value_inc_vat": 0.10},
            None,
            {"start": "2026-04-30T23:30:00+01:00",
             "end": "2026-05-01T00:00:00+01:00",
             "value_inc_vat": 0.06},
            {"value_inc_vat": 0.99},  # missing start/end
        ]
        rates = parse_event_rates(bad)
        assert len(rates) == 2
        assert rates[0].rate_pence == pytest.approx(5.0)
        assert rates[1].rate_pence == pytest.approx(6.0)


# ---------------------------------------------------------------------------
# parse_current_rate_pence
# ---------------------------------------------------------------------------


class TestParseCurrentRatePence:
    def test_converts_gbp_string_to_pence(self):
        # 0.132 GBP/kWh -> 13.2 p/kWh
        assert parse_current_rate_pence("0.132") == pytest.approx(13.2)

    def test_handles_float_input(self):
        assert parse_current_rate_pence(0.04952) == pytest.approx(4.952)

    def test_returns_none_for_unavailable(self):
        assert parse_current_rate_pence("unavailable") is None
        assert parse_current_rate_pence("unknown") is None
        assert parse_current_rate_pence("") is None
        assert parse_current_rate_pence("none") is None
        assert parse_current_rate_pence(None) is None

    def test_returns_none_for_garbage(self):
        assert parse_current_rate_pence("not a number") is None
        assert parse_current_rate_pence("x") is None


# ---------------------------------------------------------------------------
# discover_meter_keys
# ---------------------------------------------------------------------------


class TestDiscoverMeterKeys:
    def test_finds_event_pattern(self):
        keys = discover_meter_keys([
            "event.octopus_energy_electricity_1850009498_2394300396097_current_day_rates",
        ])
        assert keys == {"1850009498_2394300396097"}

    def test_finds_sensor_pattern(self):
        keys = discover_meter_keys([
            "sensor.octopus_energy_electricity_1850009498_2394300396097_current_rate",
        ])
        assert keys == {"1850009498_2394300396097"}

    def test_distinguishes_import_export_suffix(self):
        """Critical: `_export_current_day_rates` must not match the
        import pattern with `_export` swallowed into the meter key."""
        keys = discover_meter_keys([
            "event.octopus_energy_electricity_18p5009498_2394300396097_current_day_rates",
            "event.octopus_energy_electricity_18p5009498_2394300396097_export_current_day_rates",
            "event.octopus_energy_electricity_18p5009498_2394300396097_next_day_rates",
            "event.octopus_energy_electricity_18p5009498_2394300396097_export_next_day_rates",
        ])
        # All four must collapse to the same key - if export bled into
        # the key we'd get a second key like "..._export".
        assert keys == {"18p5009498_2394300396097"}

    def test_handles_mpan_with_letter(self):
        """Real-world example from #16 has 'p' in MPAN."""
        keys = discover_meter_keys([
            "sensor.octopus_energy_electricity_18p5009498_2394300396097_current_rate",
        ])
        assert keys == {"18p5009498_2394300396097"}

    def test_ignores_non_octopus_entities(self):
        keys = discover_meter_keys([
            "sensor.solcast_pv_forecast_forecast_today",
            "sensor.sa_inverter_1_capacity_point_1",
            "binary_sensor.heo_ii_writes_blocked",
        ])
        assert keys == set()

    def test_finds_multiple_meters(self):
        keys = discover_meter_keys([
            "sensor.octopus_energy_electricity_1234567890_aaa1_current_rate",
            "sensor.octopus_energy_electricity_9999999999_bbb2_current_rate",
        ])
        assert keys == {"1234567890_aaa1", "9999999999_bbb2"}

    def test_handles_non_string_inputs(self):
        # Defensive: hass.states.async_all() should yield strings, but
        # protect against garbage just in case.
        keys = discover_meter_keys([None, 42, "not_an_octopus_entity"])
        assert keys == set()


# ---------------------------------------------------------------------------
# pick_freshest_meter_key
# ---------------------------------------------------------------------------


class TestPickFreshestMeterKey:
    def test_returns_most_recent(self):
        old = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
        new = datetime(2026, 4, 30, 13, 0, tzinfo=UTC)
        result = pick_freshest_meter_key({
            "old_meter": old,
            "new_meter": new,
        })
        assert result == "new_meter"

    def test_empty_returns_none(self):
        assert pick_freshest_meter_key({}) is None

    def test_single_key_returned(self):
        ts = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
        assert pick_freshest_meter_key({"only": ts}) == "only"

    def test_none_freshness_sorts_oldest(self):
        ts = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
        result = pick_freshest_meter_key({
            "missing_ts": None,
            "with_ts": ts,
        })
        assert result == "with_ts"


# ---------------------------------------------------------------------------
# merge_rate_sources
# ---------------------------------------------------------------------------


def _slot(start_h: int, end_h: int, pence: float, day: int = 30) -> RateSlot:
    return RateSlot(
        start=datetime(2026, 4, day, start_h, 0, tzinfo=UTC),
        end=datetime(2026, 4, day, end_h, 0, tzinfo=UTC),
        rate_pence=pence,
    )


class TestMergeRateSources:
    def test_empty_live_returns_fallback(self):
        fb = [_slot(0, 1, 5.0)]
        assert merge_rate_sources([], fb) == fb

    def test_empty_fallback_returns_live(self):
        live = [_slot(0, 1, 5.0)]
        assert merge_rate_sources(live, []) == live

    def test_live_wins_over_fallback_at_same_window(self):
        live = [_slot(10, 11, 5.0)]
        fb = [_slot(10, 11, 99.0)]
        merged = merge_rate_sources(live, fb)
        assert len(merged) == 1
        assert merged[0].rate_pence == 5.0

    def test_fallback_extends_past_live_horizon(self):
        live = [_slot(10, 11, 5.0)]
        fb = [_slot(11, 12, 6.0), _slot(12, 13, 7.0)]
        merged = merge_rate_sources(live, fb)
        assert [s.rate_pence for s in merged] == [5.0, 6.0, 7.0]

    def test_overlap_drops_fallback_slot(self):
        live = [_slot(10, 12, 5.0)]
        fb = [_slot(11, 13, 99.0)]  # partial overlap
        merged = merge_rate_sources(live, fb)
        assert merged == [_slot(10, 12, 5.0)]

    def test_result_sorted_by_start(self):
        live = [_slot(15, 16, 5.0)]
        fb = [_slot(10, 11, 6.0), _slot(20, 21, 7.0)]
        merged = merge_rate_sources(live, fb)
        assert [s.start.hour for s in merged] == [10, 15, 20]


# ---------------------------------------------------------------------------
# read_bottlecapdave_rates (HA adapter)
# ---------------------------------------------------------------------------


def _make_meter_states(meter_key: str, last_updated: datetime,
                       *, with_export: bool = True,
                       with_tomorrow: bool = False) -> list:
    base = f"octopus_energy_electricity_{meter_key}"
    states = [
        _state(f"sensor.{base}_current_rate", "0.04952", last_updated),
        _state(
            f"event.{base}_current_day_rates",
            "today",
            last_updated,
            attributes={"rates": SAMPLE_IMPORT_TODAY_RATES},
        ),
    ]
    if with_export:
        states.append(_state(
            f"sensor.{base}_export_current_rate", "0.13150", last_updated,
        ))
        states.append(_state(
            f"event.{base}_export_current_day_rates",
            "today",
            last_updated,
            attributes={"rates": SAMPLE_EXPORT_TODAY_RATES},
        ))
    if with_tomorrow:
        states.append(_state(
            f"event.{base}_next_day_rates",
            "tomorrow",
            last_updated,
            attributes={"rates": SAMPLE_IMPORT_TODAY_RATES},
        ))
        if with_export:
            states.append(_state(
                f"event.{base}_export_next_day_rates",
                "tomorrow",
                last_updated,
                attributes={"rates": SAMPLE_EXPORT_TODAY_RATES},
            ))
    return states


class TestReadBottlecapDaveRates:
    def test_happy_path(self):
        ts = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
        hass = _make_hass(_make_meter_states("1850009498_2394300396097", ts,
                                             with_tomorrow=True))
        result = read_bottlecapdave_rates(hass)

        assert result.meter_key == "1850009498_2394300396097"
        assert result.has_any_data is True
        assert result.import_now_pence == pytest.approx(4.952)
        assert result.export_now_pence == pytest.approx(13.150)
        assert len(result.import_today) == 2
        assert len(result.export_today) == 2
        assert len(result.import_tomorrow) == 2
        assert len(result.export_tomorrow) == 2

    def test_returns_empty_when_bd_not_installed(self):
        # Only non-octopus entities present
        hass = _make_hass([
            _state("sensor.solcast_pv_forecast_forecast_today", "10.5"),
            _state("binary_sensor.heo_ii_writes_blocked", "off"),
        ])
        result = read_bottlecapdave_rates(hass)
        assert result.has_any_data is False
        assert result.meter_key is None
        assert result.import_today == []
        assert result.export_today == []
        assert result.import_now_pence is None

    def test_returns_empty_when_hass_none(self):
        result = read_bottlecapdave_rates(None)
        assert result.has_any_data is False
        assert result.meter_key is None

    def test_picks_freshest_meter_when_multiple(self):
        old = datetime(2026, 4, 30, 6, 0, tzinfo=UTC)
        new = datetime(2026, 4, 30, 16, 0, tzinfo=UTC)
        states = (
            _make_meter_states("oldmeter_aaaa", old)
            + _make_meter_states("freshmeter_bbbb", new)
        )
        hass = _make_hass(states)
        result = read_bottlecapdave_rates(hass)
        assert result.meter_key == "freshmeter_bbbb"

    def test_partial_data_ok(self):
        """Today's import published, but export tomorrow not yet -
        the missing entity simply yields an empty list, not a crash."""
        ts = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
        # Only the import current_rate sensor and current_day_rates event
        states = [
            _state(
                "sensor.octopus_energy_electricity_meter1_meter1_current_rate",
                "0.04952", ts,
            ),
            _state(
                "event.octopus_energy_electricity_meter1_meter1_current_day_rates",
                "today",
                ts,
                attributes={"rates": SAMPLE_IMPORT_TODAY_RATES},
            ),
        ]
        hass = _make_hass(states)
        result = read_bottlecapdave_rates(hass)
        assert result.import_now_pence == pytest.approx(4.952)
        assert result.export_now_pence is None
        assert len(result.import_today) == 2
        assert result.export_today == []
        assert result.import_tomorrow == []

    def test_unavailable_state_yields_none(self):
        ts = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
        states = [
            _state(
                "sensor.octopus_energy_electricity_meter1_meter1_current_rate",
                "unavailable", ts,
            ),
            _state(
                "event.octopus_energy_electricity_meter1_meter1_current_day_rates",
                "today",
                ts,
                attributes={"rates": []},
            ),
        ]
        hass = _make_hass(states)
        result = read_bottlecapdave_rates(hass)
        assert result.import_now_pence is None
        assert result.import_today == []

    def test_rate_now_matches_30min_slot_from_event_attribute(self):
        """Defensive: BD's `current_rate` sensor and the active 30-min
        slot in `current_day_rates` should agree. Test that we return
        both faithfully so callers can verify."""
        ts = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
        # current_rate matches the second slot's value
        bespoke_rates = [
            {"start": "2026-04-30T11:00:00+00:00",
             "end": "2026-04-30T11:30:00+00:00",
             "value_inc_vat": 0.06000},
            {"start": "2026-04-30T11:30:00+00:00",
             "end": "2026-04-30T12:00:00+00:00",
             "value_inc_vat": 0.07000},
            {"start": "2026-04-30T12:00:00+00:00",
             "end": "2026-04-30T12:30:00+00:00",
             "value_inc_vat": 0.08000},
        ]
        states = [
            _state(
                "sensor.octopus_energy_electricity_meter1_meter1_current_rate",
                "0.08000", ts,
            ),
            _state(
                "event.octopus_energy_electricity_meter1_meter1_current_day_rates",
                "today",
                ts,
                attributes={"rates": bespoke_rates},
            ),
        ]
        hass = _make_hass(states)
        result = read_bottlecapdave_rates(hass)
        assert result.import_now_pence == pytest.approx(8.0)
        # The slot covering 12:00 UTC is the third one (8.0p)
        now = datetime(2026, 4, 30, 12, 15, tzinfo=UTC)
        match = [s for s in result.import_today if s.start <= now < s.end]
        assert len(match) == 1
        assert match[0].rate_pence == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# BottlecapDaveRates dataclass
# ---------------------------------------------------------------------------


class TestBottlecapDaveRatesDataclass:
    def test_default_is_empty(self):
        r = BottlecapDaveRates()
        assert r.has_any_data is False
        assert r.import_today == []
        assert r.export_today == []
        assert r.import_now_pence is None
        assert r.export_now_pence is None
        assert r.meter_key is None

    def test_has_any_data_true_with_just_one_field(self):
        r = BottlecapDaveRates(import_now_pence=4.95)
        assert r.has_any_data is True
