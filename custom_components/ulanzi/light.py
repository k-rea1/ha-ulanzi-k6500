"""Light platform for Ulanzi K6500 BLE monitor light."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from bleak import BleakClient
from bleak.backends.device import BLEDevice
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
# 0x000b Value 01 — frame 92 (Wireshark; один байт, не CCCD 01 00).
GATT_INIT_HANDLE_INDICATE = 0x0009
GATT_INIT_PAYLOAD_INDICATE = bytes.fromhex("0200")
GATT_INIT_HANDLE_NOTIFY = 0x000B
GATT_INIT_PAYLOAD_NOTIFY = bytes.fromhex("01")

# Official app reads these handles before Write Command on 0x0010 (Wireshark ~13.07–13.55 s).
GATT_PREP_READ_HANDLES: tuple[int, ...] = (0x0005, 0x0003, 0x0013)

# Write Command on 0x0010 after prep reads (Wireshark) — not ON/OFF (9 bytes each).
COMMAND_LINK_PROBE_1 = bytes.fromhex("55 aa 03 06 00 01 01 a0 d8")  # frame 235
COMMAND_LINK_PROBE_2 = bytes.fromhex("55 aa 03 21 00 01 01 aa 6c")  # frame 239

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
        """Mirror app: init writes, prep reads, then caller writes to 0x0010."""
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
        for handle in GATT_PREP_READ_HANDLES:
            try:
                await client.read_gatt_char(handle)
            except Exception:
                _LOGGER.debug(
                    "Prep read handle 0x%04x failed for %s (continuing)",
                    handle,
                    self._mac,
                    exc_info=True,
                )

    def _ble_address_lookup_keys(self) -> tuple[str, ...]:
        """Keys used in HA Bluetooth history (BlueZ often uses lowercase MAC)."""
        formatted = dr.format_mac(self._mac)
        raw = self._mac.strip()
        return tuple(
            dict.fromkeys(
                (
                    formatted.lower(),
                    formatted.upper(),
                    formatted,
                    raw.lower(),
                    raw.upper(),
                    raw,
                )
            )
        )

    async def _async_resolve_ble_device(self) -> BLEDevice | None:
        """Resolve BLEDevice from HA scanners; tolerate MAC letter case."""
        hass = self.hass
        keys = self._ble_address_lookup_keys()

        def _lookup_now() -> BLEDevice | None:
            for addr in keys:
                if dev := bluetooth.async_ble_device_from_address(
                    hass, addr, connectable=True
                ):
                    return dev
            for addr in keys:
                scanned = bluetooth.async_scanner_devices_by_address(
                    hass, addr, connectable=True
                )
                if scanned:
                    return scanned[0].ble_device
            return None

        if dev := _lookup_now():
            return dev

        bluetooth.async_rediscover_address(hass, keys[0])
        await asyncio.sleep(1.5)
        return _lookup_now()

    async def _send_command(self, payload: bytes) -> bool:
        """Connect briefly and write the GATT characteristic by handle."""
        ble_device = await self._async_resolve_ble_device()
        if ble_device is None:
            _LOGGER.warning(
                "Device %s not found by Home Assistant Bluetooth (tried several MAC spellings). "
                "Turn the lamp on so it advertises, move it near the adapter, "
                "and check Settings → Devices → Bluetooth.",
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
            for probe in (COMMAND_LINK_PROBE_1, COMMAND_LINK_PROBE_2):
                await client.write_gatt_char(
                    GATT_WRITE_HANDLE, probe, response=False
                )
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
