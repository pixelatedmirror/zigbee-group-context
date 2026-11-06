# Zigbee Group Context Propagator (`zigbee_group_context`)

A custom integration for Home Assistant that solves the detached context / broken "Cause" trigger trace problem when controlling native Zigbee groups (via ZHA, Zigbee2MQTT, or native HA group helpers).

## Problem

In Home Assistant, toggling a native Zigbee group executes a native RF multicast/broadcast command at the coordinator level with zero bulb popcorn effect. However, because Home Assistant does not issue individual `light.turn_on` calls for member entities, inbound attribute reports arrive as external state changes. This generates a detached `Context` with no user ID or parent automation ID, breaking the "Cause" / trigger trace in the HA Logbook and Entity History.

## Solution

`zigbee_group_context` bridges this gap seamlessly:
1. **Zero-Config Dynamic Group Discovery**:
   - **Zigbee2MQTT**: Dynamically parses `zigbee2mqtt/bridge/groups` MQTT topic and resolves member IEEE addresses and friendly names to registered Home Assistant entities.
   - **ZHA**: Queries internal ZHA group and gateway tables to pair group IDs with member device endpoints and their entities.
   - **Native HA Groups Fallback**: Detects pre-existing group helpers via entity attributes (`state.attributes["entity_id"]`).
2. **Context Interception & Correlation Pipeline**:
   - Intercepts `EVENT_CALL_SERVICE` targeting any recognized group entity and caches the execution `Context` (`id`, `user_id`, `parent_id`) for each member within a TTL window (default 4.0s).
   - Intercepts incoming `EVENT_STATE_CHANGED` events on member entities and injects the cached `parent_id` and `user_id` so the Logbook displays the true originating trigger.
3. **100% Native Multicast Preservation**:
   - Does not alter or delay how commands are broadcast over the Zigbee radio.
   - Direct physical bindings and RF remote triggers pass untouched without error.
   - Unavailable and unknown states are safely skipped.

## Installation

### Via HACS
1. In HACS, add this repository as a custom integration repository.
2. Search for "Zigbee Group Context Propagator" and click Download.
3. Restart Home Assistant.
4. Go to **Settings -> Devices & Services -> Add Integration** and select **Zigbee Group Context Propagator**.

### Manual Installation
Copy `custom_components/zigbee_group_context` into your Home Assistant `<config>/custom_components/` directory and restart Home Assistant.

## Configuration

You can configure options in the UI via **Configure** on the integration card:
- **Correlation TTL**: Time-to-live window in seconds (default: 4.0s).
- **Zigbee2MQTT Base MQTT Topic**: Default `zigbee2mqtt`.

Or configure via `configuration.yaml`:
```yaml
zigbee_group_context:
  ttl: 4.0
  mqtt_base_topic: zigbee2mqtt
```
