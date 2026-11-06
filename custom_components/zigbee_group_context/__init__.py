"""Zigbee Group Context Propagator custom integration for Home Assistant."""

from __future__ import annotations

import datetime
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SERVICE_DATA,
    EVENT_CALL_SERVICE,
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    Context,
    Event,
    EventStateChangedData,
    HomeAssistant,
    ServiceCall,
    callback,
)
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_MQTT_BASE_TOPIC,
    CONF_TTL,
    DEFAULT_MQTT_BASE_TOPIC,
    DEFAULT_TTL,
    DOMAIN,
    SERVICE_REFRESH_GROUPS,
)
from .discovery import ZigbeeGroupDiscovery

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Zigbee Group Context Propagator from configuration.yaml."""
    hass.data.setdefault(DOMAIN, {})

    if DOMAIN in config:
        conf = config[DOMAIN]
        ttl = conf.get(CONF_TTL, DEFAULT_TTL)
        mqtt_base_topic = conf.get(CONF_MQTT_BASE_TOPIC, DEFAULT_MQTT_BASE_TOPIC)

        manager = ZigbeeGroupContextManager(
            hass=hass,
            ttl=float(ttl),
            mqtt_base_topic=mqtt_base_topic,
        )
        hass.data[DOMAIN]["yaml"] = manager
        await manager.async_start()

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the Zigbee Group Context Propagator from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    ttl = entry.options.get(CONF_TTL, entry.data.get(CONF_TTL, DEFAULT_TTL))
    mqtt_base_topic = entry.options.get(
        CONF_MQTT_BASE_TOPIC,
        entry.data.get(CONF_MQTT_BASE_TOPIC, DEFAULT_MQTT_BASE_TOPIC),
    )

    manager = ZigbeeGroupContextManager(
        hass=hass,
        ttl=float(ttl),
        mqtt_base_topic=mqtt_base_topic,
    )
    hass.data[DOMAIN][entry.entry_id] = manager
    await manager.async_start()

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    manager: ZigbeeGroupContextManager | None = hass.data.get(DOMAIN, {}).pop(
        entry.entry_id, None
    )
    if manager is not None:
        await manager.async_stop()
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await hass.config_entries.async_reload(entry.entry_id)


class ZigbeeGroupContextManager:
    """Manages context caching and propagation for Zigbee groups."""

    def __init__(
        self,
        hass: HomeAssistant,
        ttl: float = DEFAULT_TTL,
        mqtt_base_topic: str = DEFAULT_MQTT_BASE_TOPIC,
    ) -> None:
        """Initialize the context manager."""
        self.hass = hass
        self.ttl = ttl
        self.discovery = ZigbeeGroupDiscovery(hass=hass, mqtt_base_topic=mqtt_base_topic)

        # In-memory TTL cache: member_entity_id -> (Context, expiry_timestamp)
        self._ttl_cache: dict[str, tuple[Context, float]] = {}
        self._unsubs: list[Any] = []

    @property
    def ttl_cache(self) -> dict[str, tuple[Context, float]]:
        """Return the active TTL cache."""
        return self._ttl_cache

    async def async_start(self) -> None:
        """Start dynamic discovery, service call tracking, and state correlation."""
        _LOGGER.debug(
            "Starting Zigbee Group Context Manager (TTL=%.1fs)", self.ttl
        )

        # 1. Start group discovery
        await self.discovery.async_start()

        # 2. Intercept outbound service calls
        # Decorated with @callback for immediate synchronous dispatch in Home Assistant 2026.11
        self._unsubs.append(
            self.hass.bus.async_listen(
                EVENT_CALL_SERVICE,
                self._handle_call_service,
            )
        )

        # 3. Intercept inbound state changes
        # Decorated with @callback for immediate synchronous dispatch in Home Assistant 2026.11
        self._unsubs.append(
            self.hass.bus.async_listen(
                EVENT_STATE_CHANGED,
                self._handle_state_changed,
            )
        )

        # 4. Periodic background eviction of expired cache entries (every 15s)
        self._unsubs.append(
            async_track_time_interval(
                self.hass,
                self._purge_expired,
                datetime.timedelta(seconds=15),
            )
        )

        # 5. Register refresh_groups service
        if not self.hass.services.has_service(DOMAIN, SERVICE_REFRESH_GROUPS):
            async def _async_handle_refresh_service(_call: ServiceCall) -> None:
                await self.discovery.async_refresh_all()

            self.hass.services.async_register(
                DOMAIN,
                SERVICE_REFRESH_GROUPS,
                _async_handle_refresh_service,
            )

    async def async_stop(self) -> None:
        """Stop tracking and clean up listeners."""
        _LOGGER.debug("Stopping Zigbee Group Context Manager")
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception as err:
                _LOGGER.debug("Error during unsubscribe: %s", err)
        self._unsubs.clear()

        # Stop discovery
        await self.discovery.async_stop()

        # Clear in-memory cache
        self._ttl_cache.clear()

        # Remove service if registered
        if self.hass.services.has_service(DOMAIN, SERVICE_REFRESH_GROUPS):
            self.hass.services.async_remove(DOMAIN, SERVICE_REFRESH_GROUPS)

    @callback
    def _extract_target_entity_ids(self, service_data: dict[str, Any] | None) -> set[str]:
        """Extract targeted entity IDs from service call data."""
        if not service_data:
            return set()

        targeted: set[str] = set()

        for source in (service_data, service_data.get("target")):
            if not isinstance(source, dict):
                continue
            raw = source.get(ATTR_ENTITY_ID)
            if isinstance(raw, str):
                targeted.add(raw.lower())
            elif isinstance(raw, (list, tuple, set)):
                for item in raw:
                    if isinstance(item, str):
                        targeted.add(item.lower())

        return targeted

    @callback
    def _handle_call_service(self, event: Event) -> None:
        """Intercept outbound service calls targeting recognized Zigbee groups."""
        service_data = event.data.get(ATTR_SERVICE_DATA)
        targeted_entity_ids = self._extract_target_entity_ids(service_data)

        if not targeted_entity_ids:
            return

        now = time.time()
        expiry = now + self.ttl

        for target_id in targeted_entity_ids:
            if not self.discovery.is_group(target_id):
                continue

            members = self.discovery.get_members(target_id)
            if not members:
                continue

            _LOGGER.debug(
                "Service call targeting Zigbee group %s detected. Caching context %s for %d members",
                target_id,
                event.context.id,
                len(members),
            )

            for member_id in members:
                self._ttl_cache[member_id] = (event.context, expiry)

    @callback
    def _handle_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """Intercept inbound state changes on Zigbee group member entities."""
        entity_id = event.data.get("entity_id")
        if not entity_id:
            return

        new_state = event.data.get("new_state")
        if new_state is None:
            return

        # Skip unavailable or unknown states per architectural constraints
        if new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        cached = self._ttl_cache.get(entity_id.lower())
        if not cached:
            # Report arrived from direct physical binding/RF remote, or outside TTL window.
            # Allow untouched without modification.
            return

        origin_context, expiry = cached
        now = time.time()
        if now > expiry:
            # Lazy eviction of expired cache entry
            self._ttl_cache.pop(entity_id.lower(), None)
            return

        # Inject cached parent_id (fallback to origin_context.id) and user_id
        target_parent_id = origin_context.parent_id or origin_context.id
        target_user_id = origin_context.user_id

        # 1. Inject into event.context
        if event.context is not None:
            event.context.parent_id = target_parent_id
            if target_user_id is not None:
                event.context.user_id = target_user_id
            # Invalidate any cached serialization properties (propcache)
            if hasattr(event.context, "_cache") and isinstance(event.context._cache, dict):
                event.context._cache.clear()
        if hasattr(event, "_cache") and isinstance(event._cache, dict):
            event._cache.clear()

        # 2. Inject into new_state.context
        if new_state.context is not None:
            new_state.context.parent_id = target_parent_id
            if target_user_id is not None:
                new_state.context.user_id = target_user_id
            if hasattr(new_state.context, "_cache") and isinstance(new_state.context._cache, dict):
                new_state.context._cache.clear()
        if hasattr(new_state, "_cache") and isinstance(new_state._cache, dict):
            new_state._cache.clear()

        _LOGGER.debug(
            "Injected parent_id=%s, user_id=%s into state change for %s",
            target_parent_id,
            target_user_id,
            entity_id,
        )

    @callback
    def _purge_expired(self, _now: Any = None) -> None:
        """Purge expired entries from the in-memory cache to prevent memory leaks."""
        current_time = time.time()
        expired_keys = [
            k for k, (_, expiry) in self._ttl_cache.items() if current_time > expiry
        ]
        for k in expired_keys:
            self._ttl_cache.pop(k, None)
        if expired_keys:
            _LOGGER.debug("Purged %d expired entries from context cache", len(expired_keys))
