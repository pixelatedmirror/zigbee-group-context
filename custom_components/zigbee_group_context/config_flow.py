"""Config flow and options flow for Zigbee Group Context Propagator."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_MQTT_BASE_TOPIC,
    CONF_TTL,
    DEFAULT_MQTT_BASE_TOPIC,
    DEFAULT_TTL,
    DOMAIN,
)


class ZigbeeGroupContextConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Zigbee Group Context Propagator."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            return self.async_create_entry(
                title="Zigbee Group Context Propagator",
                data=user_input,
            )

        data_schema = vol.Schema({
            vol.Optional(CONF_TTL, default=DEFAULT_TTL): vol.Coerce(float),
            vol.Optional(
                CONF_MQTT_BASE_TOPIC, default=DEFAULT_MQTT_BASE_TOPIC
            ): str,
        })

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return ZigbeeGroupContextOptionsFlow()


class ZigbeeGroupContextOptionsFlow(OptionsFlow):
    """Handle options flow for Zigbee Group Context Propagator."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        entry = self.config_entry
        current_ttl = entry.options.get(
            CONF_TTL,
            entry.data.get(CONF_TTL, DEFAULT_TTL),
        )
        current_mqtt_topic = entry.options.get(
            CONF_MQTT_BASE_TOPIC,
            entry.data.get(
                CONF_MQTT_BASE_TOPIC, DEFAULT_MQTT_BASE_TOPIC
            ),
        )

        options_schema = vol.Schema({
            vol.Optional(CONF_TTL, default=current_ttl): vol.Coerce(float),
            vol.Optional(
                CONF_MQTT_BASE_TOPIC, default=current_mqtt_topic
            ): str,
        })

        return self.async_show_form(
            step_id="init",
            data_schema=options_schema,
        )
