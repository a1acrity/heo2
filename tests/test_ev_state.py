# tests/test_ev_state.py
"""Tests for the EV charging-state predicate.

Captured 2026-05-06: pre-fix the coordinator only matched on/true/1 so
the Tesla integration's `sensor.{name}_charging` enum (charging /
starting / stopped / ...) read as "not charging" all day. EVChargingRule
never fired and the battery drained to feed the car at 28.6p import
rate. This file pins the expected vocabulary so the bug can't return.
"""

from __future__ import annotations

import pytest

from heo2.ev_state import is_ev_charging_state


class TestIsEvChargingState:
    @pytest.mark.parametrize("state", [
        "charging",
        "Charging",
        "CHARGING",
        "starting",
        "Starting",
        "boosting",
        "Boosting",
    ])
    def test_enum_charging_states_are_truthy(self, state: str):
        """Tesla / zappi enum sensors emit these values when the charger
        is actively drawing - they MUST signal ev_charging=True."""
        assert is_ev_charging_state(state) is True

    @pytest.mark.parametrize("state", [
        "on",
        "On",
        "ON",
        "true",
        "True",
        "1",
    ])
    def test_binary_truthy_states(self, state: str):
        """Plain binary_sensor.* compatibility - on/true/1 still work."""
        assert is_ev_charging_state(state) is True

    @pytest.mark.parametrize("state", [
        "stopped",
        "complete",
        "disconnected",
        "no_power",
        "Paused",
        "EV Disconnected",
        "off",
        "false",
        "0",
    ])
    def test_non_charging_states_are_falsy(self, state: str):
        assert is_ev_charging_state(state) is False

    @pytest.mark.parametrize("state", [None, "", "unknown", "unavailable"])
    def test_missing_or_unavailable_is_falsy(self, state):
        assert is_ev_charging_state(state) is False
