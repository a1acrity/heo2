# tests/test_inverter_state_reader.py
"""Tests for reading live inverter state from HA-mirrored SA entities."""

from datetime import time

import pytest

from heo2.inverter_state_reader import (
    parse_bool,
    parse_time,
    parse_soc,
    read_programme_state,
)


class TestParsers:
    def test_parse_bool_true_variants(self):
        for v in ["true", "True", "TRUE", "on", "enabled", "Enabled", "yes", "1"]:
            assert parse_bool(v) is True, f"expected True for {v!r}"

    def test_parse_bool_false_variants(self):
        for v in ["false", "False", "off", "disabled", "Disabled", "no", "0", "", "bogus"]:
            assert parse_bool(v) is False, f"expected False for {v!r}"

    def test_parse_time_hhmm(self):
        assert parse_time("05:30") == time(5, 30)
        assert parse_time("23:57") == time(23, 57)
        assert parse_time("00:00") == time(0, 0)

    def test_parse_time_garbage_returns_none(self):
        assert parse_time("") is None
        assert parse_time("not-a-time") is None
        assert parse_time("25:00") is None
        assert parse_time(None) is None

    def test_parse_soc_integer_strings(self):
        assert parse_soc("0") == 0
        assert parse_soc("100") == 100
        assert parse_soc("23") == 23

    def test_parse_soc_float_strings_are_rounded(self):
        """SA may publish '20.0' even for an integer setting."""
        assert parse_soc("20.0") == 20
        assert parse_soc("75.9") == 75  # truncates, consistent with int()

    def test_parse_soc_out_of_range_returns_none(self):
        assert parse_soc("-1") is None
        assert parse_soc("101") is None

    def test_parse_soc_garbage_returns_none(self):
        assert parse_soc("unknown") is None
        assert parse_soc("") is None


class TestReadProgrammeState:
    def _make_lookup(self, values: dict[str, str | None]):
        def lookup(entity_id: str) -> str | None:
            return values.get(entity_id)
        return lookup

    def test_reads_all_six_slots_with_live_values(self):
        """Happy path - matches what HEO II saw on Paddy's install today."""
        values = {
            "sensor.sa_inverter_1_time_point_1": "05:30",
            "sensor.sa_inverter_1_time_point_2": "18:30",
            "sensor.sa_inverter_1_time_point_3": "23:30",
            "sensor.sa_inverter_1_time_point_4": "23:57",
            "sensor.sa_inverter_1_time_point_5": "23:58",
            "sensor.sa_inverter_1_time_point_6": "00:00",
            "sensor.sa_inverter_1_capacity_point_1": "23",
            "sensor.sa_inverter_1_capacity_point_2": "100",
            "sensor.sa_inverter_1_capacity_point_3": "100",
            "sensor.sa_inverter_1_capacity_point_4": "100",
            "sensor.sa_inverter_1_capacity_point_5": "20",
            "sensor.sa_inverter_1_capacity_point_6": "20",
            "sensor.sa_inverter_1_grid_charge_point_1": "false",
            "sensor.sa_inverter_1_grid_charge_point_2": "false",
            "sensor.sa_inverter_1_grid_charge_point_3": "false",
            "sensor.sa_inverter_1_grid_charge_point_4": "false",
            "sensor.sa_inverter_1_grid_charge_point_5": "false",
            "sensor.sa_inverter_1_grid_charge_point_6": "false",
        }
        state = read_programme_state(self._make_lookup(values))

        assert len(state.slots) == 6
        assert state.slots[0].capacity_soc == 23
        assert state.slots[1].capacity_soc == 100
        assert state.slots[4].capacity_soc == 20
        for slot in state.slots:
            assert slot.grid_charge is False
        # Slot 1 starts at time_point_6 (the previous slot end), ends at time_point_1
        assert state.slots[0].start_time == time(0, 0)
        assert state.slots[0].end_time == time(5, 30)
        # Slot 2 starts at time_point_1, ends at time_point_2
        assert state.slots[1].start_time == time(5, 30)
        assert state.slots[1].end_time == time(18, 30)


    def test_grid_charge_variants_parsed_correctly(self):
        values = {
            "sensor.sa_inverter_1_grid_charge_point_1": "true",
            "sensor.sa_inverter_1_grid_charge_point_2": "Enabled",
            "sensor.sa_inverter_1_grid_charge_point_3": "on",
            "sensor.sa_inverter_1_grid_charge_point_4": "false",
            "sensor.sa_inverter_1_grid_charge_point_5": "Disabled",
            "sensor.sa_inverter_1_grid_charge_point_6": "off",
        }
        state = read_programme_state(self._make_lookup(values))
        assert state.slots[0].grid_charge is True
        assert state.slots[1].grid_charge is True
        assert state.slots[2].grid_charge is True
        assert state.slots[3].grid_charge is False
        assert state.slots[4].grid_charge is False
        assert state.slots[5].grid_charge is False

    def test_missing_entities_use_fallbacks(self):
        """If SA entities haven't been discovered yet, return a valid but
        obviously-fallback ProgrammeState. Diff will probably produce
        writes against this, which is harmless and self-correcting."""
        state = read_programme_state(self._make_lookup({}))
        assert len(state.slots) == 6
        for slot in state.slots:
            assert slot.capacity_soc == 50  # fallback
            assert slot.grid_charge is False  # fallback

    def test_unknown_state_treated_as_missing(self):
        values = {
            "sensor.sa_inverter_1_capacity_point_1": "unknown",
            "sensor.sa_inverter_1_capacity_point_2": "100",
        }
        # Lookup should return None for unknown - we simulate that at caller.
        # (HA adapter does it; pure reader just sees None.)
        def lookup(eid):
            v = values.get(eid)
            if v == "unknown":
                return None
            return v
        state = read_programme_state(lookup)
        assert state.slots[0].capacity_soc == 50  # fallback
        assert state.slots[1].capacity_soc == 100

    def test_custom_inverter_name(self):
        """A future inverter 2 over RS232 would use inverter_2."""
        values = {"sensor.sa_inverter_2_capacity_point_1": "42"}
        state = read_programme_state(
            self._make_lookup(values), inverter_name="inverter_2",
        )
        assert state.slots[0].capacity_soc == 42
