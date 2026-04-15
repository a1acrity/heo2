# tests/test_config_flow_dashboard.py
"""Tests for config flow Octopus and Payback steps."""

import sys
import types
from unittest.mock import patch, MagicMock

# Stub out homeassistant before importing config_flow
_ha = types.ModuleType("homeassistant")
_ha_ce = types.ModuleType("homeassistant.config_entries")


class _ConfigFlow:
    """Minimal stub for config_entries.ConfigFlow."""

    def __init_subclass__(cls, domain=None, **kwargs):
        super().__init_subclass__(**kwargs)

    def async_show_form(self, *, step_id, data_schema=None, errors=None):
        return {"type": "form", "step_id": step_id}

    def async_create_entry(self, *, title, data):
        return {"type": "create_entry", "title": title, "data": data}


_ha_ce.ConfigFlow = _ConfigFlow
_ha_const = types.ModuleType("homeassistant.const")
_ha_const.CONF_HOST = "host"
_ha_const.CONF_PORT = "port"
_ha_const.CONF_USERNAME = "username"
_ha_const.CONF_PASSWORD = "password"

sys.modules.setdefault("homeassistant", _ha)
sys.modules.setdefault("homeassistant.config_entries", _ha_ce)
sys.modules.setdefault("homeassistant.const", _ha_const)

import pytest

from heo2.config_flow import HEO2ConfigFlow


class TestConfigFlowOctopusStep:
    @pytest.mark.asyncio
    async def test_octopus_step_shows_form(self):
        flow = HEO2ConfigFlow()
        flow.hass = MagicMock()
        result = await flow.async_step_octopus()
        assert result["type"] == "form"
        assert result["step_id"] == "octopus"

    @pytest.mark.asyncio
    async def test_octopus_step_chains_to_payback(self):
        flow = HEO2ConfigFlow()
        flow.hass = MagicMock()
        flow._data = {}
        result = await flow.async_step_octopus({
            "octopus_api_key": "",
            "octopus_account_number": "",
            "octopus_mpan": "",
            "octopus_serial": "",
            "octopus_product_code": "",
            "octopus_tariff_code": "",
        })
        assert result["type"] == "form"
        assert result["step_id"] == "payback"


class TestConfigFlowPaybackStep:
    @pytest.mark.asyncio
    async def test_payback_step_shows_form(self):
        flow = HEO2ConfigFlow()
        flow.hass = MagicMock()
        result = await flow.async_step_payback()
        assert result["type"] == "form"
        assert result["step_id"] == "payback"

    @pytest.mark.asyncio
    async def test_payback_step_creates_entry(self):
        flow = HEO2ConfigFlow()
        flow.hass = MagicMock()
        flow._data = {"mqtt_host": "localhost"}
        with patch.object(flow, "async_create_entry", return_value={"type": "create_entry"}) as mock_create:
            result = await flow.async_step_payback({
                "system_cost": 16800.0,
                "additional_costs": 0.0,
                "savings_to_date": 1131.47,
                "install_date": "2025-02-01",
            })
            mock_create.assert_called_once()
            call_kwargs = mock_create.call_args
            assert call_kwargs.kwargs["data"]["system_cost"] == 16800.0
            assert call_kwargs.kwargs["data"]["savings_to_date"] == 1131.47


class TestConfigFlowServicesChaining:
    @pytest.mark.asyncio
    async def test_services_chains_to_octopus(self):
        """Step 6 (services) should now chain to octopus, not create entry."""
        flow = HEO2ConfigFlow()
        flow.hass = MagicMock()
        flow._data = {}
        result = await flow.async_step_services({
            "solcast_api_key": "",
            "solcast_resource_id": "",
            "agilepredict_url": "",
            "load_baseline_w": 1900.0,
            "dry_run": True,
        })
        assert result["type"] == "form"
        assert result["step_id"] == "octopus"
