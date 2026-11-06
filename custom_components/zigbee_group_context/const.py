"""Constants for the Zigbee Group Context Propagator integration."""

from typing import Final

DOMAIN: Final = "zigbee_group_context"

# Configuration and options
CONF_TTL: Final = "ttl"
DEFAULT_TTL: Final = 4.0  # seconds

CONF_MQTT_BASE_TOPIC: Final = "mqtt_base_topic"
DEFAULT_MQTT_BASE_TOPIC: Final = "zigbee2mqtt"

# Zigbee2MQTT MQTT topics
TOPIC_BRIDGE_GROUPS: Final = "bridge/groups"
TOPIC_BRIDGE_DEVICES: Final = "bridge/devices"

# Domains supported for member state correlation
SUPPORTED_MEMBER_DOMAINS: Final = frozenset({"light", "switch", "fan"})

# Services
SERVICE_REFRESH_GROUPS: Final = "refresh_groups"
