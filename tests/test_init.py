"""Tests for initialization, config flow, and teardown."""

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry

from custom_components.zigbee_group_context import (
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.zigbee_group_context.config_flow import (
    ZigbeeGroupContextConfigFlow,
    ZigbeeGroupContextOptionsFlow,
)
from custom_components.zigbee_group_context.const import (
    CONF_MQTT_BASE_TOPIC,
    CONF_TTL,
    DEFAULT_MQTT_BASE_TOPIC,
    DEFAULT_TTL,
    DOMAIN,
)


@pytest.mark.asyncio
async def test_async_setup_yaml():
    """Test setup via YAML."""
    hass = MagicMock()
    hass.data = {}
    hass.bus.async_listen.return_value = MagicMock()
    hass.services.has_service.return_value = False

    config = {
        DOMAIN: {
            CONF_TTL: 5.5,
            CONF_MQTT_BASE_TOPIC: "z2m_custom",
        }
    }

    with patch("custom_components.zigbee_group_context.discovery.ZigbeeGroupDiscovery.async_start", new_callable=AsyncMock):
        assert await async_setup(hass, config) is True

    assert DOMAIN in hass.data
    assert "yaml" in hass.data[DOMAIN]
    manager = hass.data[DOMAIN]["yaml"]
    assert manager.ttl == 5.5
    assert manager.discovery.mqtt_base_topic == "z2m_custom"


@pytest.mark.asyncio
async def test_async_setup_entry_and_unload():
    """Test setup and unload of config entry."""
    hass = MagicMock()
    hass.data = {}
    hass.bus.async_listen.return_value = MagicMock()
    hass.services.has_service.return_value = False

    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "test_entry_123"
    entry.data = {CONF_TTL: 3.0, CONF_MQTT_BASE_TOPIC: "zigbee2mqtt"}
    entry.options = {}

    with patch("custom_components.zigbee_group_context.discovery.ZigbeeGroupDiscovery.async_start", new_callable=AsyncMock), \
         patch("custom_components.zigbee_group_context.discovery.ZigbeeGroupDiscovery.async_stop", new_callable=AsyncMock):
        assert await async_setup_entry(hass, entry) is True
        assert entry.entry_id in hass.data[DOMAIN]

        assert await async_unload_entry(hass, entry) is True
        assert entry.entry_id not in hass.data[DOMAIN]


@pytest.mark.asyncio
async def test_config_flow():
    """Test config flow creation and abort on duplicate."""
    flow = ZigbeeGroupContextConfigFlow()
    flow.hass = MagicMock()

    # Initial step shows form
    with patch.object(flow, "_async_current_entries", return_value=[]):
        result = await flow.async_step_user()
        assert result["type"] == "form"
        assert result["step_id"] == "user"

    # User input creates entry
    with patch.object(flow, "_async_current_entries", return_value=[]):
        result2 = await flow.async_step_user(
            {CONF_TTL: 4.5, CONF_MQTT_BASE_TOPIC: "zigbee2mqtt"}
        )
        assert result2["type"] == "create_entry"
        assert result2["title"] == "Zigbee Group Context Propagator"
        assert result2["data"][CONF_TTL] == 4.5

    # Second entry aborts
    with patch.object(flow, "_async_current_entries", return_value=[MagicMock()]):
        result3 = await flow.async_step_user()
        assert result3["type"] == "abort"
        assert result3["reason"] == "single_instance_allowed"


@pytest.mark.asyncio
async def test_options_flow():
    """Test options flow updates settings."""
    entry = MagicMock(spec=ConfigEntry)
    entry.data = {CONF_TTL: DEFAULT_TTL, CONF_MQTT_BASE_TOPIC: DEFAULT_MQTT_BASE_TOPIC}
    entry.options = {}

    flow = ZigbeeGroupContextOptionsFlow()
    flow.hass = MagicMock()

    with patch.object(
        ZigbeeGroupContextOptionsFlow,
        "config_entry",
        new_callable=PropertyMock,
        return_value=entry,
    ):
        # Shows form
        result = await flow.async_step_init()
        assert result["type"] == "form"
        assert result["step_id"] == "init"

        # Saves options
        result2 = await flow.async_step_init(
            {CONF_TTL: 6.0, CONF_MQTT_BASE_TOPIC: "custom_topic"}
        )
        assert result2["type"] == "create_entry"
        assert result2["data"][CONF_TTL] == 6.0
        assert result2["data"][CONF_MQTT_BASE_TOPIC] == "custom_topic"
