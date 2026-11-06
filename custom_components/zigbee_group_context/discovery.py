"""Discovery module for Zigbee Group memberships (Zigbee2MQTT, ZHA, HA Groups)."""

from __future__ import annotations

import collections.abc
import json
import logging
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import slugify

from .const import (
    DEFAULT_MQTT_BASE_TOPIC,
    DOMAIN,
    SUPPORTED_MEMBER_DOMAINS,
    TOPIC_BRIDGE_GROUPS,
)

_LOGGER = logging.getLogger(__name__)


def _get_entries_for_device(entity_reg: er.EntityRegistry, device_id: str) -> list[Any]:
    """Helper to safely fetch entries for a device from entity registry."""
    if hasattr(entity_reg, "async_entries_for_device") and callable(
        getattr(entity_reg, "async_entries_for_device")
    ):
        try:
            return entity_reg.async_entries_for_device(device_id)
        except TypeError:
            pass
    try:
        return er.async_entries_for_device(entity_reg, device_id)
    except Exception:
        return []


class ZigbeeGroupDiscovery:
    """Manages dynamic group discovery across Zigbee2MQTT, ZHA, and native HA groups."""

    def __init__(
        self,
        hass: HomeAssistant,
        mqtt_base_topic: str = DEFAULT_MQTT_BASE_TOPIC,
    ) -> None:
        """Initialize the discovery manager."""
        self.hass = hass
        self.mqtt_base_topic = mqtt_base_topic
        # Map group_entity_id -> set of member_entity_ids (both lowercase)
        self._groups: dict[str, set[str]] = {}
        # Map member_entity_id -> set of group_entity_ids (both lowercase)
        self._member_to_groups: dict[str, set[str]] = {}
        self._unsubs: list[CALLBACK_TYPE] = []
        self._z2m_raw_groups: list[dict[str, Any]] = []

    @property
    def all_groups(self) -> dict[str, set[str]]:
        """Return a copy of all discovered groups and their members."""
        return {group: set(members) for group, members in self._groups.items()}

    @callback
    def is_group(self, entity_id: str) -> bool:
        """Check if an entity_id is a recognized group."""
        return entity_id.lower() in self._groups

    @callback
    def get_members(self, group_entity_id: str) -> set[str]:
        """Return member entity IDs for a given group entity ID."""
        return self._groups.get(group_entity_id.lower(), set())

    @callback
    def get_groups_for_member(self, member_entity_id: str) -> set[str]:
        """Return group entity IDs that contain the given member entity ID."""
        return self._member_to_groups.get(member_entity_id.lower(), set())

    @callback
    def register_group(self, group_entity_id: str, members: collections.abc.Iterable[str]) -> None:
        """Register or update a group definition."""
        group_id = group_entity_id.lower()
        member_set = {m.lower() for m in members if isinstance(m, str) and "." in m}
        if not member_set:
            return

        self._groups[group_id] = member_set
        self._rebuild_member_index()
        _LOGGER.debug(
            "Registered Zigbee group %s with %d members: %s",
            group_id,
            len(member_set),
            member_set,
        )

    @callback
    def unregister_group(self, group_entity_id: str) -> None:
        """Remove a group definition."""
        group_id = group_entity_id.lower()
        if group_id in self._groups:
            self._groups.pop(group_id, None)
            self._rebuild_member_index()
            _LOGGER.debug("Unregistered Zigbee group %s", group_id)

    @callback
    def _rebuild_member_index(self) -> None:
        """Rebuild reverse index from member entity ID to group entity IDs."""
        new_index: dict[str, set[str]] = {}
        for group_id, members in self._groups.items():
            for member_id in members:
                new_index.setdefault(member_id, set()).add(group_id)
        self._member_to_groups = new_index

    async def async_start(self) -> None:
        """Start dynamic group discovery for all Zigbee stacks."""
        _LOGGER.debug("Starting Zigbee group discovery")

        # 1. Listen for entity registry updates to re-resolve entities dynamically
        self._unsubs.append(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED,
                self._handle_entity_registry_updated,
            )
        )

        # 2. Setup Zigbee2MQTT MQTT subscription
        await self._async_setup_mqtt()

        # 3. Setup ZHA Gateway discovery
        self._async_setup_zha()

        # 4. Discover Native HA group helpers and fallback entities
        self._async_discover_native_ha_groups()

    async def async_stop(self) -> None:
        """Stop discovery and unsubscribe all listeners."""
        _LOGGER.debug("Stopping Zigbee group discovery")
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception as err:
                _LOGGER.debug("Error during unsub: %s", err)
        self._unsubs.clear()
        self._groups.clear()
        self._member_to_groups.clear()
        self._z2m_raw_groups.clear()

    async def async_refresh_all(self) -> None:
        """Manually trigger a full re-scan of all group sources."""
        _LOGGER.debug("Refreshing all Zigbee group definitions")
        self._async_discover_z2m_cached()
        self._async_setup_zha()
        self._async_discover_native_ha_groups()

    # -------------------------------------------------------------------------
    # Zigbee2MQTT Discovery
    # -------------------------------------------------------------------------

    async def _async_setup_mqtt(self) -> None:
        """Subscribe to Zigbee2MQTT bridge topics."""
        try:
            from homeassistant.components import mqtt  # noqa: PLC0415
        except ImportError:
            _LOGGER.debug("MQTT component not available")
            return

        topic = f"{self.mqtt_base_topic}/{TOPIC_BRIDGE_GROUPS}"

        @callback
        def _mqtt_message_received(msg: Any) -> None:
            """Handle bridge groups MQTT payload."""
            self._handle_z2m_groups_payload(msg.payload)

        try:
            unsub = await mqtt.async_subscribe(self.hass, topic, _mqtt_message_received)
            self._unsubs.append(unsub)
            _LOGGER.debug("Subscribed to Z2M topic: %s", topic)
        except Exception as err:
            _LOGGER.warning("Could not subscribe to MQTT topic %s: %s", topic, err)

    @callback
    def _handle_z2m_groups_payload(self, raw_payload: str | bytes) -> None:
        """Parse Zigbee2MQTT bridge/groups JSON payload."""
        try:
            if isinstance(raw_payload, bytes):
                raw_payload = raw_payload.decode("utf-8")
            payload = json.loads(raw_payload)
        except Exception as err:
            _LOGGER.warning("Failed to decode Z2M groups JSON: %s", err)
            return

        if not isinstance(payload, list):
            _LOGGER.debug("Unexpected Z2M groups format (expected list): %s", type(payload))
            return

        self._z2m_raw_groups = payload
        self._async_discover_z2m_cached()

    @callback
    def _async_discover_z2m_cached(self) -> None:
        """Resolve cached Z2M group definitions to HA entities."""
        if not self._z2m_raw_groups:
            return

        device_reg = dr.async_get(self.hass)
        entity_reg = er.async_get(self.hass)

        for group_data in self._z2m_raw_groups:
            if not isinstance(group_data, dict):
                continue

            friendly_name = group_data.get("friendly_name")
            group_id = group_data.get("id")
            if not friendly_name and group_id is None:
                continue

            # Resolve member entities
            raw_members = group_data.get("members", [])
            resolved_members = self._resolve_z2m_members(raw_members, device_reg, entity_reg)

            # Resolve group entity ID
            group_entity_id = self._resolve_z2m_group_entity(
                friendly_name=friendly_name,
                group_id=group_id,
                entity_reg=entity_reg,
                device_reg=device_reg,
            )

            if group_entity_id and resolved_members:
                self.register_group(group_entity_id, resolved_members)

    def _resolve_z2m_members(
        self,
        raw_members: list[Any],
        device_reg: dr.DeviceRegistry,
        entity_reg: er.EntityRegistry,
    ) -> set[str]:
        """Resolve Z2M group members (IEEE or friendly names) to HA entity IDs."""
        resolved: set[str] = set()

        for member in raw_members:
            ieee: str | None = None
            member_friendly_name: str | None = None
            endpoint: int | None = None

            if isinstance(member, dict):
                ieee = member.get("ieee_address") or member.get("device")
                member_friendly_name = member.get("friendly_name")
                endpoint = member.get("endpoint")
            elif isinstance(member, str):
                if "/" in member:
                    parts = member.split("/", 1)
                    ieee = parts[0]
                    try:
                        endpoint = int(parts[1])
                    except ValueError:
                        pass
                else:
                    ieee = member

            # 1. Match by Device Registry identifiers (Z2M standard formats)
            matched_devices: list[dr.DeviceEntry] = []
            if ieee:
                clean_ieee = ieee.lower().replace("0x", "")
                full_ieee = f"0x{clean_ieee}"
                identifiers_to_try = [
                    ("mqtt", f"zigbee2mqtt_{full_ieee}"),
                    ("mqtt", f"zigbee2mqtt_{clean_ieee}"),
                    ("mqtt", f"{self.mqtt_base_topic}_{full_ieee}"),
                    ("mqtt", f"{self.mqtt_base_topic}_{clean_ieee}"),
                    ("zigbee2mqtt", full_ieee),
                    ("zigbee2mqtt", clean_ieee),
                    ("mqtt", full_ieee),
                    ("mqtt", clean_ieee),
                ]
                for ident in identifiers_to_try:
                    dev = device_reg.async_get_device(identifiers={ident})
                    if dev and dev not in matched_devices:
                        matched_devices.append(dev)

            # Also search by friendly name in device registry if not yet matched
            if not matched_devices and member_friendly_name:
                for dev in device_reg.devices.values():
                    if dev.name == member_friendly_name:
                        matched_devices.append(dev)
                        break

            # 2. Extract entity IDs for matched devices
            device_found = False
            for dev in matched_devices:
                device_found = True
                entries = _get_entries_for_device(entity_reg, dev.id)
                for entry in entries:
                    if entry.domain in SUPPORTED_MEMBER_DOMAINS and not entry.disabled_by:
                        resolved.add(entry.entity_id.lower())

            # 3. Direct entity registry scan if device lookup did not yield entities
            if not device_found:
                targets = [s for s in (ieee, member_friendly_name) if s]
                for target in targets:
                    target_lower = target.lower()
                    target_slug = slugify(target)
                    for entry in entity_reg.entities.values():
                        if entry.domain in SUPPORTED_MEMBER_DOMAINS and not entry.disabled_by:
                            uid_lower = entry.unique_id.lower()
                            if (
                                target_lower in uid_lower
                                or entry.entity_id.endswith(f".{target_slug}")
                            ):
                                resolved.add(entry.entity_id.lower())

        return resolved

    def _resolve_z2m_group_entity(
        self,
        friendly_name: str | None,
        group_id: int | str | None,
        entity_reg: er.EntityRegistry,
        device_reg: dr.DeviceRegistry,
    ) -> str | None:
        """Resolve the Home Assistant group entity ID for a Z2M group."""
        candidates: list[str] = []

        if friendly_name:
            name_slug = slugify(friendly_name)
            candidates.extend([
                f"light.{name_slug}",
                f"switch.{name_slug}",
                f"light.z2m_group_{name_slug}",
                f"light.zigbee2mqtt_{name_slug}",
            ])

        if group_id is not None:
            candidates.extend([
                f"light.z2m_group_{group_id}",
                f"light.group_{group_id}",
            ])

        # Check existing states in HA state machine
        for candidate in candidates:
            if self.hass.states.get(candidate) is not None:
                return candidate.lower()

        # Check entity registry by candidate entity_id
        for candidate in candidates:
            if entity_reg.async_get(candidate) is not None:
                return candidate.lower()

        # Check entity registry by unique_id patterns
        search_terms = []
        if group_id is not None:
            search_terms.append(f"group_{group_id}")
            search_terms.append(f"zigbee2mqtt_{group_id}")
        if friendly_name:
            search_terms.append(slugify(friendly_name))

        for entry in entity_reg.entities.values():
            if entry.domain in ("light", "switch"):
                uid_lower = entry.unique_id.lower()
                for term in search_terms:
                    if term in uid_lower:
                        return entry.entity_id.lower()

        # Fallback to the first candidate if defined
        return candidates[0].lower() if candidates else None

    # -------------------------------------------------------------------------
    # ZHA Discovery
    # -------------------------------------------------------------------------

    @callback
    def _async_setup_zha(self) -> None:
        """Discover groups from ZHA gateway instance."""
        zha_data = self.hass.data.get("zha")
        if not zha_data:
            _LOGGER.debug("ZHA integration data not found in hass.data")
            return

        gateway_proxy = getattr(zha_data, "gateway_proxy", None)
        gateway = getattr(zha_data, "gateway", None)
        if gateway_proxy is None and isinstance(zha_data, dict):
            gateway_proxy = zha_data.get("gateway_proxy")
            gateway = zha_data.get("zha_gateway") or zha_data.get("gateway")

        # Discover groups from gateway_proxy or gateway
        groups_dict: dict[Any, Any] = {}
        if gateway_proxy is not None and hasattr(gateway_proxy, "group_proxies"):
            groups_dict = gateway_proxy.group_proxies
        elif gateway is not None and hasattr(gateway, "groups"):
            groups_dict = gateway.groups

        if not groups_dict:
            _LOGGER.debug("No ZHA groups found in gateway/proxy")
            return

        device_reg = dr.async_get(self.hass)
        entity_reg = er.async_get(self.hass)

        for group_item in groups_dict.values():
            # item may be ZHAGroupProxy or Group
            group_obj = getattr(group_item, "group", group_item)
            group_id = getattr(group_obj, "group_id", None)
            group_name = getattr(group_obj, "name", "")
            members = getattr(group_obj, "members", [])

            if group_id is None:
                continue

            resolved_members = self._resolve_zha_members(
                members=members,
                gateway_proxy=gateway_proxy,
                device_reg=device_reg,
                entity_reg=entity_reg,
            )

            group_entity_id = self._resolve_zha_group_entity(
                group_id=group_id,
                group_name=group_name,
                entity_reg=entity_reg,
            )

            if group_entity_id and resolved_members:
                self.register_group(group_entity_id, resolved_members)

    def _resolve_zha_members(
        self,
        members: list[Any],
        gateway_proxy: Any | None,
        device_reg: dr.DeviceRegistry,
        entity_reg: er.EntityRegistry,
    ) -> set[str]:
        """Resolve ZHA group members to HA entity IDs."""
        resolved: set[str] = set()

        for member in members:
            # Member could be GroupMember or dict or device
            device_obj = getattr(member, "device", None)
            ieee = getattr(device_obj, "ieee", None) if device_obj else getattr(member, "ieee", None)
            if not ieee:
                continue

            ieee_str = str(ieee).lower()

            # 1. Check gateway_proxy ha_entity_refs if available
            if gateway_proxy and hasattr(gateway_proxy, "ha_entity_refs"):
                entity_refs = gateway_proxy.ha_entity_refs.get(ieee) or gateway_proxy.ha_entity_refs.get(ieee_str)
                if entity_refs:
                    for ref in entity_refs:
                        # Skip if it is the group entity itself
                        entity_data = getattr(ref, "entity_data", None)
                        if entity_data and getattr(entity_data, "is_group_entity", False):
                            continue
                        ha_eid = getattr(ref, "ha_entity_id", None)
                        if ha_eid:
                            resolved.add(ha_eid.lower())

            # 2. Check Device Registry for identifier ("zha", ieee_str)
            identifiers_to_try = [
                ("zha", ieee_str),
                ("zha", ieee_str.upper()),
                ("zha", str(ieee)),
            ]
            for ident in identifiers_to_try:
                dev = device_reg.async_get_device(identifiers={ident})
                if dev:
                    entries = _get_entries_for_device(entity_reg, dev.id)
                    for entry in entries:
                        if entry.domain in SUPPORTED_MEMBER_DOMAINS and not entry.disabled_by:
                            resolved.add(entry.entity_id.lower())

        return resolved

    def _resolve_zha_group_entity(
        self,
        group_id: int,
        group_name: str,
        entity_reg: er.EntityRegistry,
    ) -> str | None:
        """Resolve the Home Assistant group entity ID for a ZHA group."""
        hex_id = f"{group_id:04x}"

        # 1. Check entity registry by unique_id pattern
        # Standard ZHA group unique_id: f"{domain}_zha_group_0x{group_id:04x}"
        for entry in entity_reg.entities.values():
            if entry.domain in ("light", "switch", "fan"):
                uid_lower = entry.unique_id.lower()
                if f"zha_group_0x{hex_id}" in uid_lower or f"zha_group_{group_id}" in uid_lower:
                    return entry.entity_id.lower()

        # 2. Check state machine candidates
        candidates = []
        if group_name:
            candidates.append(f"light.{slugify(group_name)}")
            candidates.append(f"switch.{slugify(group_name)}")
        candidates.append(f"light.zha_group_0x{hex_id}")

        for candidate in candidates:
            if self.hass.states.get(candidate) is not None:
                return candidate.lower()

        # Check entity registry by candidate entity_id
        for candidate in candidates:
            if entity_reg.async_get(candidate) is not None:
                return candidate.lower()

        return candidates[0].lower() if candidates else None

    # -------------------------------------------------------------------------
    # Native HA Groups Fallback
    # -------------------------------------------------------------------------

    @callback
    def _async_discover_native_ha_groups(self) -> None:
        """Scan HA states for entities that define an entity_id attribute (group helpers)."""
        for state in self.hass.states.async_all():
            entity_id = state.entity_id.lower()
            # If already recognized via Z2M or ZHA, don't overwrite with fewer details
            if entity_id in self._groups:
                continue

            member_ids = state.attributes.get("entity_id")
            if isinstance(member_ids, (list, tuple, set)) and member_ids:
                valid_members = {
                    m.lower() for m in member_ids
                    if isinstance(m, str) and "." in m
                }
                if valid_members:
                    self.register_group(entity_id, valid_members)

    # -------------------------------------------------------------------------
    # Registry Event Handlers
    # -------------------------------------------------------------------------

    @callback
    def _handle_entity_registry_updated(self, event: Any) -> None:
        """Re-resolve groups when the entity registry changes."""
        _LOGGER.debug("Entity registry updated, re-evaluating Zigbee groups")
        self._async_discover_z2m_cached()
        self._async_setup_zha()
        self._async_discover_native_ha_groups()
