"""StateReader / parse helpers."""

from __future__ import annotations

import pytest

from heo3.state_reader import (
    MockStateReader,
    parse_bool,
    parse_float,
    parse_int,
    parse_str,
)


class TestMockStateReader:
    def test_known_state_returned(self):
        r = MockStateReader({"sensor.x": "42"})
        assert r.get_state("sensor.x") == "42"

    def test_unknown_state_is_none(self):
        r = MockStateReader()
        assert r.get_state("sensor.nope") is None

    def test_attributes_returned(self):
        r = MockStateReader(
            states={"sensor.x": "42"},
            attributes={"sensor.x": {"unit": "kWh"}},
        )
        assert r.get_attributes("sensor.x") == {"unit": "kWh"}

    def test_unknown_entity_attributes_empty(self):
        assert MockStateReader().get_attributes("sensor.nope") == {}

    def test_set_state_overwrites(self):
        r = MockStateReader()
        r.set_state("sensor.x", "1")
        r.set_state("sensor.x", "2", attributes={"foo": "bar"})
        assert r.get_state("sensor.x") == "2"
        assert r.get_attributes("sensor.x") == {"foo": "bar"}

    def test_remove(self):
        r = MockStateReader({"sensor.x": "1"}, {"sensor.x": {"a": 1}})
        r.remove("sensor.x")
        assert r.get_state("sensor.x") is None
        assert r.get_attributes("sensor.x") == {}

    def test_attributes_returned_as_copy(self):
        attrs = {"unit": "kWh"}
        r = MockStateReader(attributes={"sensor.x": attrs})
        result = r.get_attributes("sensor.x")
        result["mutated"] = True
        # Original storage unaffected.
        assert "mutated" not in r.get_attributes("sensor.x")


class TestParseFloat:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("42.5", 42.5),
            ("0", 0.0),
            ("-3.14", -3.14),
            ("100", 100.0),
        ],
    )
    def test_valid_floats(self, raw, expected):
        assert parse_float(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["unknown", "unavailable", "none", "", "   ", "garbage", None],
    )
    def test_null_states_return_none(self, raw):
        assert parse_float(raw) is None

    def test_case_insensitive_null(self):
        assert parse_float("UNKNOWN") is None
        assert parse_float("Unavailable") is None


class TestParseInt:
    def test_truncates_floats(self):
        assert parse_int("42.7") == 42

    def test_returns_none_on_garbage(self):
        assert parse_int("nope") is None
        assert parse_int(None) is None


class TestParseBool:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("on", True),
            ("ON", True),
            ("true", True),
            ("True", True),
            ("1", True),
            ("yes", True),
            ("off", False),
            ("False", False),
            ("0", False),
            ("no", False),
        ],
    )
    def test_truthy_falsy(self, raw, expected):
        assert parse_bool(raw) == expected

    @pytest.mark.parametrize("raw", [None, "unknown", "maybe", ""])
    def test_unparseable_returns_none(self, raw):
        assert parse_bool(raw) is None


class TestParseStr:
    def test_passes_through(self):
        assert parse_str("Selling first") == "Selling first"

    def test_null_states_to_none(self):
        assert parse_str("unknown") is None
        assert parse_str("") is None
        assert parse_str(None) is None
