"""Auto-discovery tests against a fake hass.states surface."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from heo3.discovery import (
    discover_all,
    discover_bd_meter_key,
    discover_deye_prefix,
    discover_igo_dispatching_entity,
    discover_inverter_sensor_overrides,
    discover_saving_session_entity,
    discover_tesla_vehicle,
    discover_zappi_prefix,
)


def _hass(entity_ids, state_value: str = "ok"):
    """Build a fake hass with given entity IDs (each with a sentinel state).

    `state_value` is the .state for every entity; pass an
    iterable to vary, or use _hass_with_states for finer control.
    """
    state_objects = {
        eid: SimpleNamespace(entity_id=eid, state=state_value)
        for eid in entity_ids
    }
    hass = MagicMock()
    hass.states.async_all.return_value = list(state_objects.values())
    hass.states.get.side_effect = lambda eid: state_objects.get(eid)
    return hass


def _hass_with_states(states_dict: dict[str, str]):
    """Build a fake hass with explicit per-entity state values."""
    state_objects = {
        eid: SimpleNamespace(entity_id=eid, state=state)
        for eid, state in states_dict.items()
    }
    hass = MagicMock()
    hass.states.async_all.return_value = list(state_objects.values())
    hass.states.get.side_effect = lambda eid: state_objects.get(eid)
    return hass


# ── BD meter key ───────────────────────────────────────────────────


class TestBDMeterKey:
    def test_finds_import_meter(self):
        hass = _hass([
            "event.octopus_energy_electricity_18p5009498_2372761090617_current_day_rates",
            "event.octopus_energy_electricity_18p5009498_2394300396097_export_current_day_rates",
            "sensor.unrelated",
        ])
        assert (
            discover_bd_meter_key(hass)
            == "18p5009498_2372761090617"
        )

    def test_export_meter_skipped(self):
        # Only export entity present — no import meter to discover.
        hass = _hass([
            "event.octopus_energy_electricity_X_Y_export_current_day_rates",
        ])
        assert discover_bd_meter_key(hass) is None

    def test_no_octopus_returns_none(self):
        hass = _hass(["sensor.foo", "sensor.bar"])
        assert discover_bd_meter_key(hass) is None


# ── IGO ────────────────────────────────────────────────────────────


class TestIGODispatching:
    def test_finds_intelligent_dispatching(self):
        hass = _hass([
            "binary_sensor.octopus_energy_00000000_intelligent_dispatching",
            "sensor.unrelated",
        ])
        assert (
            discover_igo_dispatching_entity(hass)
            == "binary_sensor.octopus_energy_00000000_intelligent_dispatching"
        )

    def test_no_match_returns_none(self):
        hass = _hass(["sensor.foo"])
        assert discover_igo_dispatching_entity(hass) is None


# ── Octoplus saving sessions ──────────────────────────────────────


class TestSavingSession:
    def test_finds_octoplus_saving_sessions(self):
        hass = _hass([
            "binary_sensor.octopus_energy_a_8e04cfcf_octoplus_saving_sessions",
        ])
        assert (
            discover_saving_session_entity(hass)
            == "binary_sensor.octopus_energy_a_8e04cfcf_octoplus_saving_sessions"
        )


# ── Zappi ─────────────────────────────────────────────────────────


class TestZappi:
    def test_finds_charge_mode_select(self):
        hass = _hass([
            "select.myenergi_zappi_22752031_charge_mode",
            "sensor.myenergi_zappi_22752031_status",
        ])
        assert discover_zappi_prefix(hass) == "myenergi_zappi_22752031"

    def test_no_zappi_returns_none(self):
        hass = _hass(["sensor.foo"])
        assert discover_zappi_prefix(hass) is None


# ── Tesla ─────────────────────────────────────────────────────────


class TestTesla:
    def test_finds_natalia_when_paired(self):
        hass = _hass([
            "binary_sensor.natalia_located_at_home",
            "switch.natalia_charge",
        ])
        assert discover_tesla_vehicle(hass) == "natalia"

    def test_located_without_charge_switch_skipped(self):
        # presence-only sensor (e.g. iBeacon) shouldn't match Tesla.
        hass = _hass([
            "binary_sensor.iphone_located_at_home",
        ])
        assert discover_tesla_vehicle(hass) is None

    def test_no_tesla_returns_none(self):
        hass = _hass(["sensor.foo"])
        assert discover_tesla_vehicle(hass) is None


# ── Deye prefix ───────────────────────────────────────────────────


class TestDeyePrefix:
    def test_finds_deye_sunsynk_sol_ark(self):
        hass = _hass([
            "select.deye_sunsynk_sol_ark_work_mode",
            "number.deye_sunsynk_sol_ark_capacity_point_1",
        ])
        assert discover_deye_prefix(hass) == "deye_sunsynk_sol_ark_"

    def test_skips_non_inverter_work_mode(self):
        hass = _hass([
            "select.thermostat_work_mode",
        ])
        assert discover_deye_prefix(hass) is None

    def test_no_match_returns_none(self):
        hass = _hass(["sensor.foo"])
        assert discover_deye_prefix(hass) is None

    def test_prefers_inverter_1_over_inverter_2(self):
        # SPEC §2: writes only go to inverter 1; reads should too.
        hass = _hass([
            "select.deye_sunsynk_sol_ark_x_2_inverter_2_work_mode",  # secondary
            "select.deye_sunsynk_sol_ark_work_mode",                  # primary
        ])
        assert discover_deye_prefix(hass) == "deye_sunsynk_sol_ark_"


# ── Inverter sensor overrides ─────────────────────────────────────


class TestInverterOverrides:
    def test_default_battery_soc_present_no_override(self):
        # If the default entity has a real state, no override needed.
        hass = _hass_with_states({
            "sensor.sa_inverter_1_battery_soc": "82",  # default has live state
            "sensor.sa_inverter_1_pv_power": "1500",  # solar override needed
            "sensor.sa_inverter_1_temperature": "35",  # temp override needed
        })
        out = discover_inverter_sensor_overrides(hass)
        assert "battery_soc" not in out
        assert out["solar_power"] == "sensor.sa_inverter_1_pv_power"
        assert out["inverter_temperature"] == "sensor.sa_inverter_1_temperature"

    def test_default_unavailable_treated_as_missing(self):
        # Default entity exists in registry but state=unavailable —
        # discovery should treat as missing and try alternatives.
        hass = _hass_with_states({
            "sensor.sa_inverter_1_battery_soc": "unavailable",
            "sensor.sa_total_battery_state_of_charge": "100",
        })
        out = discover_inverter_sensor_overrides(hass)
        assert (
            out["battery_soc"]
            == "sensor.sa_total_battery_state_of_charge"
        )

    def test_total_state_of_charge_used_when_no_default(self):
        hass = _hass_with_states({
            "sensor.sa_total_battery_state_of_charge": "100",
            "sensor.sa_inverter_1_pv_power": "1500",
            "sensor.sa_inverter_1_temperature": "35",
        })
        out = discover_inverter_sensor_overrides(hass)
        assert (
            out["battery_soc"]
            == "sensor.sa_total_battery_state_of_charge"
        )

    def test_no_relevant_entities_empty_overrides(self):
        # Default present for everything → no overrides.
        hass = _hass_with_states({
            "sensor.sa_inverter_1_battery_soc": "82",
            "sensor.sa_inverter_1_solar_power": "1500",
            "sensor.sa_inverter_1_inverter_temperature": "35",
        })
        assert discover_inverter_sensor_overrides(hass) == {}


# ── discover_all ──────────────────────────────────────────────────


class TestDiscoverAll:
    def test_paddy_install_shape(self):
        # Realistic subset of paddy's install
        hass = _hass([
            "event.octopus_energy_electricity_18p5009498_2372761090617_current_day_rates",
            "binary_sensor.octopus_energy_00000000_intelligent_dispatching",
            "binary_sensor.octopus_energy_a_8e04cfcf_octoplus_saving_sessions",
            "select.myenergi_zappi_22752031_charge_mode",
            "binary_sensor.natalia_located_at_home",
            "switch.natalia_charge",
            "sensor.sa_total_battery_state_of_charge",
            "sensor.sa_inverter_1_pv_power",
            "sensor.sa_inverter_1_temperature",
        ])
        out = discover_all(hass)
        assert out["bd_meter_key"] == "18p5009498_2372761090617"
        assert out["igo_dispatching_entity"].endswith("_intelligent_dispatching")
        assert out["saving_session_entity"].endswith("_octoplus_saving_sessions")
        assert out["zappi_prefix"] == "myenergi_zappi_22752031"
        assert out["tesla_vehicle"] == "natalia"
        assert "battery_soc" in out["inverter_sensor_overrides"]

    def test_empty_hass_all_none(self):
        hass = _hass([])
        out = discover_all(hass)
        assert out["bd_meter_key"] is None
        assert out["igo_dispatching_entity"] is None
        assert out["zappi_prefix"] is None
        assert out["tesla_vehicle"] is None
        assert out["inverter_sensor_overrides"] == {}
