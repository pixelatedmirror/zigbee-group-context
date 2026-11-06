"""Comprehensive discovery tests for Zigbee Group Context Propagator."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import State

from custom_components.zigbee_group_context.const import DEFAULT_MQTT_BASE_TOPIC
from custom_components.zigbee_group_context.discovery import ZigbeeGroupDiscovery


def test_initialization():
    """Test initial discovery state."""
    hass = MagicMock()
    discovery = ZigbeeGroupDiscovery(hass=hass, mqtt_base_topic=DEFAULT_MQTT_BASE_TOPIC)
    assert discovery.all_groups == {}
    assert not discovery.is_group("light.living_room")
    assert discovery.get_members("light.living_room") == set()


def test_register_and_unregister_group():
    """Test registering and unregistering groups."""
    hass = MagicMock()
    discovery = ZigbeeGroupDiscovery(hass=hass)

    discovery.register_group("light.living_room", ["light.bulb_1", "light.bulb_2"])
    assert discovery.is_group("light.living_room")
    assert discovery.get_members("light.living_room") == {"light.bulb_1", "light.bulb_2"}
    assert discovery.get_groups_for_member("light.bulb_1") == {"light.living_room"}
    assert discovery.get_groups_for_member("light.bulb_2") == {"light.living_room"}

    discovery.unregister_group("light.living_room")
    assert not discovery.is_group("light.living_room")
    assert discovery.get_members("light.living_room") == set()
    assert discovery.get_groups_for_member("light.bulb_1") == set()


def test_handle_z2m_groups_payload_valid():
    """Test parsing and resolving Zigbee2MQTT groups payload."""
    hass = MagicMock()
    discovery = ZigbeeGroupDiscovery(hass=hass)

    # Mock device registry and entity registry
    mock_dev_reg = MagicMock()
    mock_ent_reg = MagicMock()

    # Mock device for bulb 1
    dev1 = MagicMock()
    dev1.id = "dev_id_1"
    mock_dev_reg.async_get_device.side_effect = lambda identifiers: (
        dev1 if any("0x00158d0001" in str(ident) for ident in identifiers) else None
    )

    # Mock entities for dev1
    ent1 = MagicMock()
    ent1.domain = "light"
    ent1.disabled_by = None
    ent1.entity_id = "light.bulb_kitchen_1"
    mock_ent_reg.async_entries_for_device.return_value = [ent1]

    # Mock group entity existence
    hass.states.get.side_effect = lambda eid: (
        State("light.kitchen_spots", "on") if eid == "light.kitchen_spots" else None
    )

    payload = json.dumps([
        {
            "id": 5,
            "friendly_name": "kitchen_spots",
            "members": [
                {"ieee_address": "0x00158d0001", "endpoint": 1}
            ],
        }
    ])

    with patch("homeassistant.helpers.device_registry.async_get", return_value=mock_dev_reg), \
         patch("homeassistant.helpers.entity_registry.async_get", return_value=mock_ent_reg):
        discovery._handle_z2m_groups_payload(payload)

    assert discovery.is_group("light.kitchen_spots")
    assert discovery.get_members("light.kitchen_spots") == {"light.bulb_kitchen_1"}


def test_handle_z2m_groups_payload_invalid_json():
    """Test handling of invalid JSON in Z2M payload."""
    hass = MagicMock()
    discovery = ZigbeeGroupDiscovery(hass=hass)

    # Should not raise exception
    discovery._handle_z2m_groups_payload("invalid json {")
    assert discovery.all_groups == {}

    # Non-list JSON
    discovery._handle_z2m_groups_payload(json.dumps({"error": "not a list"}))
    assert discovery.all_groups == {}


def test_zha_discovery():
    """Test discovery from ZHA gateway and group proxies."""
    hass = MagicMock()
    discovery = ZigbeeGroupDiscovery(hass=hass)

    # Setup mock ZHA data
    mock_group = MagicMock()
    mock_group.group_id = 12
    mock_group.name = "Dining Room Group"
    
    mock_member = MagicMock()
    mock_member.device.ieee = "00:15:8d:00:02:ab:cd:ef"
    mock_group.members = [mock_member]

    mock_group_proxy = MagicMock()
    mock_group_proxy.group = mock_group

    mock_gateway_proxy = MagicMock()
    mock_gateway_proxy.group_proxies = {12: mock_group_proxy}
    mock_gateway_proxy.ha_entity_refs = {}

    hass.data = {"zha": MagicMock(gateway_proxy=mock_gateway_proxy)}

    # Mock dev and entity registry
    mock_dev_reg = MagicMock()
    mock_ent_reg = MagicMock()

    mock_dev = MagicMock()
    mock_dev.id = "zha_dev_1"
    mock_dev_reg.async_get_device.return_value = mock_dev

    ent = MagicMock()
    ent.domain = "light"
    ent.disabled_by = None
    ent.entity_id = "light.dining_chandelier_bulb_1"
    mock_ent_reg.async_entries_for_device.return_value = [ent]

    # Mock group entity in states
    hass.states.get.side_effect = lambda eid: (
        State("light.dining_room_group", "on") if eid == "light.dining_room_group" else None
    )

    with patch("homeassistant.helpers.device_registry.async_get", return_value=mock_dev_reg), \
         patch("homeassistant.helpers.entity_registry.async_get", return_value=mock_ent_reg):
        discovery._async_setup_zha()

    assert discovery.is_group("light.dining_room_group")
    assert discovery.get_members("light.dining_room_group") == {"light.dining_chandelier_bulb_1"}


def test_native_ha_groups_discovery():
    """Test fallback discovery for native Home Assistant group entities."""
    hass = MagicMock()
    discovery = ZigbeeGroupDiscovery(hass=hass)

    # State with entity_id attribute
    group_state = State(
        "light.hallway_group",
        "off",
        attributes={"entity_id": ["light.hallway_1", "light.hallway_2"]}
    )
    regular_state = State("light.standalone_lamp", "on", attributes={})

    hass.states.async_all.return_value = [group_state, regular_state]

    discovery._async_discover_native_ha_groups()

    assert discovery.is_group("light.hallway_group")
    assert discovery.get_members("light.hallway_group") == {"light.hallway_1", "light.hallway_2"}
    assert not discovery.is_group("light.standalone_lamp")
