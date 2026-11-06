"""Tests for context correlation and propagation pipeline."""

import time
from unittest.mock import MagicMock

import pytest
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SERVICE_DATA,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Context, Event, HomeAssistant, State

from custom_components.zigbee_group_context import ZigbeeGroupContextManager


def test_extract_target_entity_ids():
    """Test extracting entity IDs from various service call shapes."""
    hass = MagicMock(spec=HomeAssistant)
    manager = ZigbeeGroupContextManager(hass=hass)

    # Empty data
    assert manager._extract_target_entity_ids(None) == set()
    assert manager._extract_target_entity_ids({}) == set()

    # Single string entity_id
    assert manager._extract_target_entity_ids({"entity_id": "light.living_room"}) == {
        "light.living_room"
    }

    # List of entity_ids
    assert manager._extract_target_entity_ids(
        {"entity_id": ["light.living_room", "light.kitchen"]}
    ) == {"light.living_room", "light.kitchen"}

    # Target dict format
    assert manager._extract_target_entity_ids(
        {"target": {"entity_id": ["light.bedroom"]}}
    ) == {"light.bedroom"}

    # Combined direct and target format
    assert manager._extract_target_entity_ids(
        {"entity_id": "light.room1", "target": {"entity_id": "light.room2"}}
    ) == {"light.room1", "light.room2"}


def test_call_service_caches_context():
    """Test that service call targeting a recognized group caches context for members."""
    hass = MagicMock(spec=HomeAssistant)
    manager = ZigbeeGroupContextManager(hass=hass, ttl=4.0)

    # Register group
    manager.discovery.register_group("light.living_room", ["light.bulb_1", "light.bulb_2"])

    # Simulate service call event
    service_context = Context(user_id="user_123", parent_id="automation_parent_456")
    event = Event(
        "call_service",
        data={
            "domain": "light",
            "service": "turn_on",
            ATTR_SERVICE_DATA: {"entity_id": "light.living_room"},
        },
        context=service_context,
    )

    manager._handle_call_service(event)

    # Both members should be cached
    assert "light.bulb_1" in manager.ttl_cache
    assert "light.bulb_2" in manager.ttl_cache

    cached_ctx_1, expiry_1 = manager.ttl_cache["light.bulb_1"]
    assert cached_ctx_1 is service_context
    assert expiry_1 > time.time()


def test_call_service_ignores_non_group():
    """Test that service calls to non-group entities are ignored."""
    hass = MagicMock(spec=HomeAssistant)
    manager = ZigbeeGroupContextManager(hass=hass)

    event = Event(
        "call_service",
        data={
            "domain": "light",
            "service": "turn_on",
            ATTR_SERVICE_DATA: {"entity_id": "light.untracked_light"},
        },
        context=Context(),
    )

    manager._handle_call_service(event)
    assert manager.ttl_cache == {}


def test_state_changed_injects_context():
    """Test that incoming state changes within TTL receive the cached context."""
    hass = MagicMock(spec=HomeAssistant)
    manager = ZigbeeGroupContextManager(hass=hass, ttl=4.0)
    manager.discovery.register_group("light.living_room", ["light.bulb_1"])

    origin_context = Context(user_id="user_admin", parent_id="auto_trigger_789")
    manager._ttl_cache["light.bulb_1"] = (origin_context, time.time() + 4.0)

    # Inbound state change from coordinator radio report (detached context)
    detached_context = Context()
    new_state = State("light.bulb_1", "on", context=detached_context)
    event = Event(
        "state_changed",
        data={
            "entity_id": "light.bulb_1",
            "old_state": State("light.bulb_1", "off"),
            "new_state": new_state,
        },
        context=detached_context,
    )

    manager._handle_state_changed(event)

    # Injected values
    assert event.context.parent_id == "auto_trigger_789"
    assert event.context.user_id == "user_admin"
    assert new_state.context.parent_id == "auto_trigger_789"
    assert new_state.context.user_id == "user_admin"


def test_state_changed_fallback_to_context_id():
    """Test parent_id falls back to origin_context.id when parent_id is None."""
    hass = MagicMock(spec=HomeAssistant)
    manager = ZigbeeGroupContextManager(hass=hass, ttl=4.0)

    # Context with no parent_id (e.g. direct user click in UI)
    origin_context = Context(user_id="user_gui", parent_id=None)
    manager._ttl_cache["light.bulb_1"] = (origin_context, time.time() + 4.0)

    detached_context = Context()
    new_state = State("light.bulb_1", "on", context=detached_context)
    event = Event(
        "state_changed",
        data={
            "entity_id": "light.bulb_1",
            "old_state": State("light.bulb_1", "off"),
            "new_state": new_state,
        },
        context=detached_context,
    )

    manager._handle_state_changed(event)

    assert event.context.parent_id == origin_context.id
    assert event.context.user_id == "user_gui"


def test_state_changed_expired_ttl():
    """Test that expired cache entries are lazily evicted without injecting context."""
    hass = MagicMock(spec=HomeAssistant)
    manager = ZigbeeGroupContextManager(hass=hass)

    origin_context = Context(user_id="user_gui", id="orig_1")
    # Expired 5 seconds ago
    manager._ttl_cache["light.bulb_1"] = (origin_context, time.time() - 5.0)

    detached_context = Context(id="detached_1")
    new_state = State("light.bulb_1", "on", context=detached_context)
    event = Event(
        "state_changed",
        data={
            "entity_id": "light.bulb_1",
            "old_state": State("light.bulb_1", "off"),
            "new_state": new_state,
        },
        context=detached_context,
    )

    manager._handle_state_changed(event)

    # Context should remain untouched
    assert event.context.parent_id is None
    assert event.context.user_id is None
    # Entry should be evicted
    assert "light.bulb_1" not in manager.ttl_cache


def test_state_changed_skips_unavailable_and_unknown():
    """Test that unavailable and unknown states skip context injection."""
    hass = MagicMock(spec=HomeAssistant)
    manager = ZigbeeGroupContextManager(hass=hass)

    origin_context = Context(user_id="user_admin")
    manager._ttl_cache["light.bulb_1"] = (origin_context, time.time() + 4.0)

    for bad_state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        detached_context = Context()
        new_state = State("light.bulb_1", bad_state, context=detached_context)
        event = Event(
            "state_changed",
            data={
                "entity_id": "light.bulb_1",
                "old_state": State("light.bulb_1", "on"),
                "new_state": new_state,
            },
            context=detached_context,
        )

        manager._handle_state_changed(event)
        assert event.context.parent_id is None
        assert event.context.user_id is None


def test_state_changed_untracked_entity():
    """Test that untracked entity state changes pass completely untouched."""
    hass = MagicMock(spec=HomeAssistant)
    manager = ZigbeeGroupContextManager(hass=hass)

    detached_context = Context()
    new_state = State("light.random_entity", "on", context=detached_context)
    event = Event(
        "state_changed",
        data={
            "entity_id": "light.random_entity",
            "old_state": State("light.random_entity", "off"),
            "new_state": new_state,
        },
        context=detached_context,
    )

    manager._handle_state_changed(event)
    assert event.context.parent_id is None
    assert event.context.user_id is None


def test_purge_expired():
    """Test periodic purge of expired cache entries."""
    hass = MagicMock(spec=HomeAssistant)
    manager = ZigbeeGroupContextManager(hass=hass)

    now = time.time()
    manager._ttl_cache["active_1"] = (Context(), now + 10.0)
    manager._ttl_cache["expired_1"] = (Context(), now - 1.0)
    manager._ttl_cache["expired_2"] = (Context(), now - 10.0)

    manager._purge_expired()

    assert "active_1" in manager.ttl_cache
    assert "expired_1" not in manager.ttl_cache
    assert "expired_2" not in manager.ttl_cache


def test_multiple_rapid_service_calls():
    """Test rapid service calls update expiry and context properly."""
    hass = MagicMock(spec=HomeAssistant)
    manager = ZigbeeGroupContextManager(hass=hass, ttl=4.0)
    manager.discovery.register_group("light.group_1", ["light.bulb_1"])

    ctx1 = Context(user_id="user_1", id="ctx_1")
    event1 = Event(
        "call_service",
        data={"domain": "light", "service": "turn_on", ATTR_SERVICE_DATA: {"entity_id": "light.group_1"}},
        context=ctx1,
    )
    manager._handle_call_service(event1)
    assert manager.ttl_cache["light.bulb_1"][0] is ctx1

    # Second rapid service call with new context
    ctx2 = Context(user_id="user_2", id="ctx_2")
    event2 = Event(
        "call_service",
        data={"domain": "light", "service": "turn_on", ATTR_SERVICE_DATA: {"entity_id": "light.group_1"}},
        context=ctx2,
    )
    manager._handle_call_service(event2)
    assert manager.ttl_cache["light.bulb_1"][0] is ctx2


def test_physical_remote_rf_multicast_passes_untouched():
    """Test that physical remote or bound switch reports pass untouched without error."""
    hass = MagicMock(spec=HomeAssistant)
    manager = ZigbeeGroupContextManager(hass=hass, ttl=4.0)
    manager.discovery.register_group("light.living_room", ["light.bulb_1", "light.bulb_2"])

    # Physical remote bound to group sends RF multicast directly over Zigbee.
    # No Home Assistant service call is fired.
    rf_report_context = Context(id="zigbee_rf_report_ulid")
    new_state = State("light.bulb_1", "on", context=rf_report_context)
    event = Event(
        "state_changed",
        data={
            "entity_id": "light.bulb_1",
            "old_state": State("light.bulb_1", "off"),
            "new_state": new_state,
        },
        context=rf_report_context,
    )

    manager._handle_state_changed(event)

    # Event context remains original and untampered
    assert event.context.id == "zigbee_rf_report_ulid"
    assert event.context.parent_id is None
    assert event.context.user_id is None
