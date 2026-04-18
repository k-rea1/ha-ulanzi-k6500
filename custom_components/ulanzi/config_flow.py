"""Config flow for Ulanzi K6500 BLE Light."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_MAC, CONF_NAME
import homeassistant.helpers.config_validation as cv

from .const import DEFAULT_NAME, DOMAIN

MAC_REGEX = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$")


def _normalize_mac(value: str) -> str:
    """Normalize and validate a Bluetooth MAC address."""
    cleaned = value.strip().upper().replace("-", ":")
    if not MAC_REGEX.match(cleaned):
        raise vol.Invalid("invalid_mac")
    return cleaned


class UlanziConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow: add Ulanzi K6500 by MAC address."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Prompt for MAC and optional name."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                mac = _normalize_mac(user_input[CONF_MAC])
            except vol.Invalid:
                errors["base"] = "invalid_mac"
            else:
                raw_name = user_input.get(CONF_NAME)
                if isinstance(raw_name, str):
                    name = raw_name.strip() or DEFAULT_NAME
                else:
                    name = DEFAULT_NAME

                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=name,
                    data={CONF_MAC: mac, CONF_NAME: name},
                )

        defaults = user_input or {}
        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_MAC,
                    default=defaults.get(CONF_MAC, ""),
                ): cv.string,
                vol.Optional(
                    CONF_NAME,
                    default=defaults.get(CONF_NAME, DEFAULT_NAME),
                ): cv.string,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )
