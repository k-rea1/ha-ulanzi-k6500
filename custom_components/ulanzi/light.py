"""Light platform for Ulanzi K6500 BLE monitor light."""

from __future__ import annotations

import logging
from typing import Any

from bleak import BleakClient
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
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

# Session init (official app, Wireshark): Write Request before commands on 0x0010.
# 0x0009 Value 02 00 = CCCD indications (frame 87 — подтверждено).
# 0x000b — frame 92; в экспорте не было Value; типичная пара к indications — 01 00 (notify).
GATT_INIT_HANDLE_INDICATE = 0x0009
GATT_INIT_PAYLOAD_INDICATE = bytes.fromhex("0200")
GATT_INIT_HANDLE_NOTIFY = 0x000B
GATT_INIT_PAYLOAD_NOTIFY = bytes.fromhex("0100")

COMMAND_ON = bytes.fromhex("55 aa 03 01 00 05 01 28 19 64 00 10 fe")
COMMAND_OFF = bytes.fromhex("55 aa 03 01 00 05 01 00 19 64 00 19 5e")

# BlueZ often needs time for service discovery on first connect; HA recommends >= 10 s.
BLE_CONNECT_TIMEOUT = 30.0


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

    async def _gatt_session_init(self, client: BleakClient) -> None:
        """Mirror app: enable CCCD via Write Request (response=True) before 0x0010."""
        await client.write_gatt_char(
            GATT_INIT_HANDLE_INDICATE,
            GATT_INIT_PAYLOAD_INDICATE,
            response=True,
        )
        await client.write_gatt_char(
            GATT_INIT_HANDLE_NOTIFY,
            GATT_INIT_PAYLOAD_NOTIFY,
            response=True,
        )

    async def _send_command(self, payload: bytes) -> bool:
        """Connect briefly and write the GATT characteristic by handle."""
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, dr.format_mac(self._mac), connectable=True
        )
        if ble_device is None:
            _LOGGER.warning(
                "Device %s not in Home Assistant Bluetooth cache. "
                "Power the lamp on, move it closer, and check Settings → Devices → Bluetooth.",
                self._mac,
            )
            return False

        client: BleakClient | None = None
        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                name=self.name or self._mac,
                max_attempts=4,
                timeout=BLE_CONNECT_TIMEOUT,
            )
            await self._gatt_session_init(client)
            await client.write_gatt_char(
                GATT_WRITE_HANDLE, payload, response=False
            )
        except Exception:
            _LOGGER.exception(
                "BLE error while writing to Ulanzi K6500 at %s", self._mac
            )
            return False
        finally:
            if client is not None and client.is_connected:
                try:
                    await client.disconnect()
                except Exception:
                    _LOGGER.debug(
                        "Disconnect after write raised for %s (ignored)",
                        self._mac,
                        exc_info=True,
                    )

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
