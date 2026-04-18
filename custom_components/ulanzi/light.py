"""Light platform for Ulanzi K6500 BLE monitor light."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakCharacteristicNotFoundError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakOutOfConnectionSlotsError,
    establish_connection,
)

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

# ── GATT handles ─────────────────────────────────────────────────────────────

# BlueZ exposes the write characteristic at handle 0x000f (UUID c304).
# The official Windows app writes to handle 0x0010 because Windows and BlueZ
# number characteristic value handles differently (Windows shows declaration+1,
# BlueZ shows the value handle directly). Using UUID bypasses this discrepancy.
GATT_WRITE_UUID  = "0000c304-0000-1000-8000-00805f9b34fb"  # write / write-without-response

# Notifications arrive on UUID c305 (BlueZ handle 0x0011, Windows Wireshark 0x0012).
# Protocol: 55 aa 04 <cmd_lo> <cmd_hi> <len> <payload…> <checksum 2b>
#   cmd 0x0006 (probe-1 ack): payload[1] = current brightness; 0 = off
#   cmd 0x0001 (ON/OFF ack):  payload[1] = confirmed brightness; 0 = off
GATT_NOTIFY_UUID = "0000c305-0000-1000-8000-00805f9b34fb"  # notify

# Session-init handles written before commands (Wireshark frames 215–220).
# Type (characteristic value vs CCCD descriptor) is resolved at runtime — see
# _gatt_init_handle() — because BlueZ blocks direct CCCD writes.
GATT_INIT_HANDLE_INDICATE = 0x0009
GATT_INIT_PAYLOAD_INDICATE = bytes.fromhex("0200")
GATT_INIT_HANDLE_NOTIFY = 0x000B
GATT_INIT_PAYLOAD_NOTIFY = bytes.fromhex("01")

# Reads performed by the official app before the first write (Wireshark).
GATT_PREP_READ_HANDLES: tuple[int, ...] = (0x0005, 0x0003, 0x0013)

# ── Commands ──────────────────────────────────────────────────────────────────

# Probe 1: device responds with current state (brightness byte in notification).
COMMAND_LINK_PROBE_1 = bytes.fromhex("55 aa 03 06 00 01 01 a0 d8")
# Probe 2: device responds with identity info (model, firmware, MAC).
COMMAND_LINK_PROBE_2 = bytes.fromhex("55 aa 03 21 00 01 01 aa 6c")

COMMAND_ON  = bytes.fromhex("55 aa 03 01 00 05 01 28 19 64 00 10 fe")
COMMAND_OFF = bytes.fromhex("55 aa 03 01 00 05 01 00 19 64 00 19 5e")

# ── Timing ────────────────────────────────────────────────────────────────────

BLE_CONNECT_TIMEOUT        = 30.0   # service-discovery timeout
BLE_PRE_CONNECT_DELAY      = 0.4    # BlueZ prep pause
BLE_IN_PROGRESS_RETRIES    = 4
BLE_IN_PROGRESS_BACKOFF_BASE = 1.0
NOTIFY_TIMEOUT             = 3.0    # wait for device ack after command


# ── Helpers ───────────────────────────────────────────────────────────────────

def _exception_chain_text(exc: BaseException) -> str:
    parts: list[str] = []
    cur: BaseException | None = exc
    while cur is not None:
        parts.append(str(cur))
        cur = cur.__cause__
    return " ".join(parts).lower()


def _parse_notification(data: bytes) -> tuple[int, bool | None]:
    """Decode a 0x0012 notification.

    Returns (cmd_id, state) where state is True/False/None.
    None means this packet carries no ON/OFF information.
    """
    if len(data) < 8 or data[0] != 0x55 or data[1] != 0xAA or data[2] != 0x04:
        return 0, None
    cmd = data[3] | (data[4] << 8)
    payload_len = data[5]
    if len(data) < 6 + payload_len + 2 or payload_len < 2:
        return cmd, None
    brightness = data[7]  # payload[1]: 0 = off, >0 = on
    if cmd in (0x0001, 0x0006):
        return cmd, brightness > 0
    return cmd, None


# ── Platform setup ────────────────────────────────────────────────────────────

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([UlanziK6500Light(entry)], update_before_add=False)


# ── Entity ────────────────────────────────────────────────────────────────────

class UlanziK6500Light(LightEntity):
    """Ulanzi K6500 BLE monitor light."""

    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_color_mode = ColorMode.ONOFF
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._mac = entry.data[CONF_MAC]
        name = entry.data.get(CONF_NAME) or DEFAULT_NAME
        self._attr_name = name
        self._attr_unique_id = entry.unique_id
        self._state: bool | None = None  # None = unknown until first read

        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_BLUETOOTH, dr.format_mac(self._mac))},
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="Ulanzi",
            model="K6500",
            name=name,
        )

    @property
    def is_on(self) -> bool | None:
        return self._state

    # ── HA lifecycle ──────────────────────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.hass.async_create_task(self._async_fetch_state())

    # ── GATT helpers ──────────────────────────────────────────────────────────

    async def _write_handle_att(
        self, client: BleakClient, handle: int, data: bytes, *, response: bool
    ) -> None:
        try:
            await client.write_gatt_char(handle, data, response=response)
        except BleakCharacteristicNotFoundError:
            await client.write_gatt_descriptor(handle, data)

    async def _read_handle_optional(self, client: BleakClient, handle: int) -> None:
        try:
            await client.read_gatt_char(handle)
        except BleakCharacteristicNotFoundError:
            try:
                await client.read_gatt_descriptor(handle)
            except Exception:
                _LOGGER.debug("Prep read desc 0x%04x failed (continuing)", handle, exc_info=True)
        except Exception:
            _LOGGER.debug("Prep read 0x%04x failed (continuing)", handle, exc_info=True)

    async def _gatt_init_handle(
        self, client: BleakClient, handle: int, data: bytes
    ) -> None:
        """Write a session-init handle; resolves type from the service map.

        1. write_gatt_char  if handle is a characteristic value.
        2. start_notify     if handle is a CCCD descriptor (BlueZ blocks direct writes).
        Silently skips if the handle is absent from the discovered map.
        """
        for svc in client.services:
            for char in svc.characteristics:
                if char.handle == handle:
                    try:
                        await client.write_gatt_char(char, data, response=True)
                        _LOGGER.debug("Init 0x%04x: write_gatt_char ok", handle)
                    except Exception:
                        _LOGGER.debug("Init 0x%04x: write_gatt_char failed", handle, exc_info=True)
                    return

        for svc in client.services:
            for char in svc.characteristics:
                for desc in char.descriptors:
                    if desc.handle == handle:
                        try:
                            await client.start_notify(char, lambda _h, _d: None)
                            _LOGGER.debug(
                                "Init 0x%04x: start_notify on char 0x%04x ok",
                                handle, char.handle,
                            )
                        except Exception:
                            _LOGGER.debug("Init 0x%04x: start_notify failed", handle, exc_info=True)
                        return

        _LOGGER.debug("Init handle 0x%04x not in service map — skipping", handle)

    async def _subscribe_notify_handle(
        self,
        client: BleakClient,
        callback: Callable[[int, bytes], None],
    ) -> None:
        """Subscribe to incoming notifications from GATT_NOTIFY_UUID (c305)."""
        try:
            await client.start_notify(GATT_NOTIFY_UUID, callback)
            _LOGGER.debug("Subscribed to %s for %s", GATT_NOTIFY_UUID, self._mac)
        except Exception:
            _LOGGER.debug(
                "start_notify %s failed for %s", GATT_NOTIFY_UUID, self._mac, exc_info=True
            )

    async def _gatt_session_init(self, client: BleakClient) -> None:
        """Mirror the official app: init writes → prep reads."""
        for svc in client.services:
            for char in svc.characteristics:
                _LOGGER.debug(
                    "GATT char 0x%04x %s props=%s", char.handle, char.uuid, char.properties
                )
                for desc in char.descriptors:
                    _LOGGER.debug("  desc 0x%04x %s", desc.handle, desc.uuid)

        await self._gatt_init_handle(client, GATT_INIT_HANDLE_INDICATE, GATT_INIT_PAYLOAD_INDICATE)
        await self._gatt_init_handle(client, GATT_INIT_HANDLE_NOTIFY, GATT_INIT_PAYLOAD_NOTIFY)
        for handle in GATT_PREP_READ_HANDLES:
            await self._read_handle_optional(client, handle)

    # ── BLE connection ────────────────────────────────────────────────────────

    def _ble_address_lookup_keys(self) -> tuple[str, ...]:
        formatted = dr.format_mac(self._mac)
        raw = self._mac.strip()
        return tuple(dict.fromkeys((
            formatted.lower(), formatted.upper(), formatted,
            raw.lower(), raw.upper(), raw,
        )))

    async def _async_resolve_ble_device(self) -> BLEDevice | None:
        hass = self.hass
        keys = self._ble_address_lookup_keys()

        def _lookup_now() -> BLEDevice | None:
            for addr in keys:
                if dev := bluetooth.async_ble_device_from_address(hass, addr, connectable=True):
                    return dev
            for addr in keys:
                scanned = bluetooth.async_scanner_devices_by_address(hass, addr, connectable=True)
                if scanned:
                    return scanned[0].ble_device
            return None

        if dev := _lookup_now():
            return dev
        bluetooth.async_rediscover_address(hass, keys[0])
        await asyncio.sleep(1.5)
        return _lookup_now()

    async def _async_establish_client(self, ble_device: BLEDevice) -> BleakClient:
        await asyncio.sleep(BLE_PRE_CONNECT_DELAY)
        for attempt in range(BLE_IN_PROGRESS_RETRIES):
            try:
                return await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    name=self.name or self._mac,
                    max_attempts=2,
                    timeout=BLE_CONNECT_TIMEOUT,
                    use_services_cache=False,
                )
            except BleakOutOfConnectionSlotsError:
                raise
            except Exception as exc:
                text = _exception_chain_text(exc)
                if "in progress" in text or "error.inprogress" in text.replace(" ", ""):
                    _LOGGER.debug(
                        "BLE connect InProgress for %s (attempt %s/%s), backing off",
                        self._mac, attempt + 1, BLE_IN_PROGRESS_RETRIES,
                    )
                    if attempt + 1 >= BLE_IN_PROGRESS_RETRIES:
                        raise
                    await asyncio.sleep(BLE_IN_PROGRESS_BACKOFF_BASE + 0.85 * attempt)
                    continue
                raise

    # ── State reading & commanding ────────────────────────────────────────────

    async def _async_fetch_state(self) -> None:
        """On startup: connect, send probe 1, read state from notification, disconnect."""
        ble_device = await self._async_resolve_ble_device()
        if ble_device is None:
            _LOGGER.debug("Startup state fetch skipped: %s not found", self._mac)
            return

        received: list[bool | None] = [None]
        event = asyncio.Event()

        def _on_notify(handle: int, data: bytes) -> None:
            cmd, state = _parse_notification(data)
            if cmd == 0x0006 and state is not None:  # probe-1 ack = current state
                received[0] = state
                event.set()

        client: BleakClient | None = None
        try:
            client = await self._async_establish_client(ble_device)
            await self._subscribe_notify_handle(client, _on_notify)
            await self._gatt_session_init(client)
            await client.write_gatt_char(GATT_WRITE_UUID, COMMAND_LINK_PROBE_1, response=False)
            try:
                await asyncio.wait_for(event.wait(), timeout=NOTIFY_TIMEOUT)
            except asyncio.TimeoutError:
                _LOGGER.debug("No probe-1 response from %s at startup", self._mac)
        except Exception:
            _LOGGER.debug("Startup state fetch failed for %s", self._mac, exc_info=True)
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        if received[0] is not None:
            self._state = received[0]
            self.async_write_ha_state()
            _LOGGER.debug(
                "Initial state of %s: %s", self._mac, "ON" if self._state else "OFF"
            )

    async def _send_command(self, payload: bytes, optimistic_state: bool) -> bool:
        """Connect, send probes + command, update state from notification."""
        ble_device = await self._async_resolve_ble_device()
        if ble_device is None:
            _LOGGER.warning(
                "Device %s not found. Turn the lamp on so it advertises "
                "and check Settings → Devices → Bluetooth.",
                self._mac,
            )
            return False

        received: list[bool | None] = [None]
        event = asyncio.Event()

        def _on_notify(handle: int, data: bytes) -> None:
            cmd, state = _parse_notification(data)
            if cmd == 0x0001 and state is not None:  # ON/OFF command ack
                received[0] = state
                event.set()

        client: BleakClient | None = None
        try:
            client = await self._async_establish_client(ble_device)
            await self._subscribe_notify_handle(client, _on_notify)
            await self._gatt_session_init(client)
            for probe in (COMMAND_LINK_PROBE_1, COMMAND_LINK_PROBE_2):
                await client.write_gatt_char(GATT_WRITE_UUID, probe, response=False)
            await client.write_gatt_char(GATT_WRITE_UUID, payload, response=False)
            try:
                await asyncio.wait_for(event.wait(), timeout=NOTIFY_TIMEOUT)
            except asyncio.TimeoutError:
                _LOGGER.debug(
                    "No ON/OFF ack from %s within %.1f s — using optimistic state",
                    self._mac, NOTIFY_TIMEOUT,
                )
        except BleakOutOfConnectionSlotsError:
            _LOGGER.error(
                "Bluetooth: no free connection slot to reach %s. "
                "Close other BLE connections or add a proxy near the lamp.",
                self._mac,
            )
            return False
        except Exception:
            _LOGGER.exception("BLE error while writing to Ulanzi K6500 at %s", self._mac)
            return False
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    _LOGGER.debug("Disconnect raised for %s (ignored)", self._mac, exc_info=True)

        self._state = received[0] if received[0] is not None else optimistic_state
        self.async_write_ha_state()
        return True

    # ── LightEntity interface ─────────────────────────────────────────────────

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._send_command(COMMAND_ON, optimistic_state=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send_command(COMMAND_OFF, optimistic_state=False)
