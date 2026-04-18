"""Light platform for Ulanzi K6500 BLE monitor light."""

from __future__ import annotations

import logging
from typing import Any

from bleak import BleakClient

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_MAC, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEFAULT_NAME, DOMAIN

_LOGGER = logging.getLogger(__name__)

# GATT write handle (reverse-engineered for K6500).
GATT_WRITE_HANDLE = 0x0010

COMMAND_ON = bytes.fromhex("55 aa 03 01 00 05 01 28 19 64 00 10 fe")
COMMAND_OFF = bytes.fromhex("55 aa 03 01 00 05 01 00 19 64 00 19 5e")

# Short timeout so the Bluetooth adapter is not held for long.
BLE_CONNECTION_TIMEOUT = 8.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ulanzi light from a config entry."""
    async_add_entities([UlanziK6500Light(entry)], update_before_add=False)


class UlanziK6500Light(LightEntity):
    """Representation of an Ulanzi K6500 BLE light."""

    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_color_mode = ColorMode.ONOFF
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the light from config entry data."""
        self._entry = entry
        self._mac = entry.data[CONF_MAC]
        name = entry.data.get(CONF_NAME) or DEFAULT_NAME
        self._attr_name = name
        self._attr_unique_id = entry.unique_id
        self._state: bool | None = False

        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_BLUETOOTH, dr.format_mac(self._mac))},
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="Ulanzi",
            model="K6500",
            name=name,
        )

    @property
    def is_on(self) -> bool | None:
        """Return true if light is on."""
        return self._state

    async def _send_command(self, payload: bytes) -> bool:
        """Connect briefly and write the GATT characteristic by handle."""
        client = BleakClient(self._mac, timeout=BLE_CONNECTION_TIMEOUT)
        try:
            async with client:
                await client.write_gatt_char(
                    GATT_WRITE_HANDLE, payload, response=False
                )
        except Exception:
            _LOGGER.exception(
                "BLE error while writing to Ulanzi K6500 at %s", self._mac
            )
            return False
        return True

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on (optimistic update after successful write)."""
        if not await self._send_command(COMMAND_ON):
            return
        self._state = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off (optimistic update after successful write)."""
        if not await self._send_command(COMMAND_OFF):
            return
        self._state = False
        self.async_write_ha_state()
