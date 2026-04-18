"""Light platform for Ulanzi K6500 BLE monitor light."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from bleak import BleakClient
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

# ── GATT UUIDs ────────────────────────────────────────────────────────────────

# BlueZ handle 0x000f / Windows handle 0x0010 — same char, different numbering.
GATT_WRITE_UUID  = "0000c304-0000-1000-8000-00805f9b34fb"
# BlueZ handle 0x0011 / Windows handle 0x0012.
# Protocol: 55 aa 04 <cmd_lo> <cmd_hi> <len> <payload…> <checksum 2b>
#   cmd 0x0006 (probe ack): payload[1] = brightness, 0 = off
#   cmd 0x0001 (ON/OFF ack): payload[1] = confirmed brightness
GATT_NOTIFY_UUID = "0000c305-0000-1000-8000-00805f9b34fb"

# ── Session-init sequence (mirrors Wireshark frames 215–220) ──────────────────

GATT_INIT_HANDLE_INDICATE  = 0x0009
GATT_INIT_PAYLOAD_INDICATE = bytes.fromhex("0200")
GATT_INIT_HANDLE_NOTIFY    = 0x000B
GATT_INIT_PAYLOAD_NOTIFY   = bytes.fromhex("01")

# Prep reads from official app; 0x0013 is the CCCD for c305.
GATT_PREP_READ_HANDLES: tuple[int, ...] = (0x0013,)

# ── Commands ──────────────────────────────────────────────────────────────────

COMMAND_LINK_PROBE_1 = bytes.fromhex("55 aa 03 06 00 01 01 a0 d8")
COMMAND_ON           = bytes.fromhex("55 aa 03 01 00 05 01 28 19 64 00 10 fe")
COMMAND_OFF          = bytes.fromhex("55 aa 03 01 00 05 01 00 19 64 00 19 5e")

# ── Timing ────────────────────────────────────────────────────────────────────

BLE_CONNECT_TIMEOUT          = 30.0
BLE_PRE_CONNECT_DELAY        = 0.1
BLE_IN_PROGRESS_RETRIES      = 4
BLE_IN_PROGRESS_BACKOFF_BASE = 1.0
BLE_NO_SLOT_RETRIES          = 3
BLE_NO_SLOT_BACKOFF          = 3.0

NOTIFY_TIMEOUT     = 3.0   # wait for device ack after command
KEEPALIVE_INTERVAL = 20.0  # probe_1 sent this often to keep link alive


# ── Helpers ───────────────────────────────────────────────────────────────────

def _exception_chain_text(exc: BaseException) -> str:
    parts: list[str] = []
    cur: BaseException | None = exc
    while cur is not None:
        parts.append(str(cur))
        cur = cur.__cause__
    return " ".join(parts).lower()


def _parse_notification(data: bytes) -> tuple[int, bool | None]:
    """Return (cmd_id, on_state). state is None when packet has no ON/OFF info."""
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
    """Ulanzi K6500 BLE monitor light — persistent connection."""

    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_color_mode = ColorMode.ONOFF
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._mac = entry.data[CONF_MAC]
        name = entry.data.get(CONF_NAME) or DEFAULT_NAME
        self._attr_name = name
        self._attr_unique_id = entry.unique_id
        self._state: bool | None = None

        self._attr_device_info = DeviceInfo(
            connections={(dr.CONNECTION_BLUETOOTH, dr.format_mac(self._mac))},
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="Ulanzi",
            model="K6500",
            name=name,
        )

        # Persistent connection state
        self._client: BleakClient | None = None
        self._connect_lock = asyncio.Lock()
        self._cmd_lock = asyncio.Lock()
        self._keepalive_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._removed = False
        self._cancel_ble_callback: Callable[[], None] | None = None

        # Pending notification waiter: (cmd_filter, event, result_holder)
        self._notify_waiter: (
            tuple[frozenset[int], asyncio.Event, list[bool | None]] | None
        ) = None

    @property
    def is_on(self) -> bool | None:
        return self._state

    # ── HA lifecycle ──────────────────────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Start watching for device advertisements — fires whenever K6500 is seen.
        self._cancel_ble_callback = bluetooth.async_register_callback(
            self.hass,
            self._on_ble_advertisement,
            bluetooth.BluetoothCallbackMatcher(address=self._mac.upper()),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
        # Connect immediately if device already known to scanner.
        self.hass.async_create_background_task(self._async_try_connect(), "ulanzi_connect")

    async def async_will_remove_from_hass(self) -> None:
        self._removed = True
        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        await super().async_will_remove_from_hass()
        if self._cancel_ble_callback:
            self._cancel_ble_callback()
            self._cancel_ble_callback = None
        await self._async_disconnect()

    # ── BLE advertisement callback ────────────────────────────────────────────

    def _on_ble_advertisement(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Fired by HA scanner when K6500 broadcasts. Trigger connect if needed."""
        if not self._is_connected() and not self._connect_lock.locked():
            self.hass.async_create_background_task(self._async_try_connect(), "ulanzi_connect")

    # ── Connection management ─────────────────────────────────────────────────

    def _is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def _async_try_connect(self) -> None:
        if self._is_connected() or self._connect_lock.locked():
            return
        async with self._connect_lock:
            if self._is_connected():
                return
            await self._async_connect()

    async def _async_connect(self) -> None:
        ble_device = await self._async_resolve_ble_device()
        if ble_device is None:
            _LOGGER.debug("Connect skipped: %s not visible to scanner", self._mac)
            return

        client: BleakClient | None = None
        try:
            client = await self._async_establish_client(ble_device)
            client.set_disconnected_callback(self._on_disconnected)
            await client.start_notify(GATT_NOTIFY_UUID, self._on_notify_persistent)
            await self._gatt_session_init(client)
            self._client = client
            _LOGGER.debug("Connected to %s", self._mac)
            # Read initial state.
            await self._do_probe()
            # Keep link alive with periodic probes.
            self._keepalive_task = self.hass.async_create_background_task(self._keepalive_loop(), "ulanzi_keepalive")
        except Exception:
            _LOGGER.debug("Connect to %s failed", self._mac, exc_info=True)
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass

    def _on_disconnected(self, _client: BleakClient) -> None:
        """Called by bleak when the device drops the connection."""
        _LOGGER.debug("Disconnected from %s — scheduling reconnect loop", self._mac)
        self._client = None
        if self._keepalive_task:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        # Keep last known state — better than resetting to unknown.
        if self._removed:
            return
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        self._reconnect_task = self.hass.async_create_background_task(self._reconnect_loop(), "ulanzi_reconnect")

    async def _reconnect_loop(self) -> None:
        """Keep retrying until reconnected, regardless of advertisement callbacks."""
        try:
            delay = 5.0
            while not self._is_connected() and not self._removed:
                await asyncio.sleep(delay)
                if self._is_connected() or self._removed:
                    return
                _LOGGER.debug("Reconnect attempt for %s", self._mac)
                await self._async_try_connect()
                delay = min(delay * 1.5, 60.0)  # back off up to 60 s
        except asyncio.CancelledError:
            raise
        finally:
            self._reconnect_task = None

    async def _async_disconnect(self) -> None:
        if self._keepalive_task:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def _keepalive_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
            except asyncio.CancelledError:
                return
            if not self._is_connected():
                return
            try:
                await self._do_probe()
            except Exception:
                _LOGGER.debug("Keepalive probe failed for %s", self._mac, exc_info=True)

    # ── Notification handler ──────────────────────────────────────────────────

    def _on_notify_persistent(self, handle: int, data: bytes) -> None:
        cmd, state = _parse_notification(data)
        if state is not None:
            self._state = state
            self.async_write_ha_state()
        # Unblock any command that's waiting for this cmd id.
        if self._notify_waiter is not None:
            cmd_filter, event, result = self._notify_waiter
            if cmd in cmd_filter and state is not None:
                result[0] = state
                event.set()

    # ── GATT helpers ──────────────────────────────────────────────────────────

    async def _gatt_init_handle(
        self, client: BleakClient, handle: int, data: bytes
    ) -> None:
        """Write a session-init handle; resolves type at runtime from service map."""
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
                            _LOGGER.debug("Init 0x%04x: start_notify ok", handle)
                        except Exception:
                            _LOGGER.debug("Init 0x%04x: start_notify failed", handle, exc_info=True)
                        return

        _LOGGER.debug("Init handle 0x%04x not in service map — skipping", handle)

    async def _read_handle_optional(self, client: BleakClient, handle: int) -> None:
        try:
            await client.read_gatt_char(handle)
        except Exception:
            try:
                await client.read_gatt_descriptor(handle)
            except Exception:
                _LOGGER.debug("Prep read 0x%04x failed (continuing)", handle)

    async def _gatt_session_init(self, client: BleakClient) -> None:
        """Mirror the official app init sequence."""
        await self._gatt_init_handle(client, GATT_INIT_HANDLE_INDICATE, GATT_INIT_PAYLOAD_INDICATE)
        await self._gatt_init_handle(client, GATT_INIT_HANDLE_NOTIFY, GATT_INIT_PAYLOAD_NOTIFY)
        for handle in GATT_PREP_READ_HANDLES:
            await self._read_handle_optional(client, handle)

    async def _do_probe(self) -> None:
        """Send probe_1; device responds with current state via notification."""
        client = self._client
        if client is not None and client.is_connected:
            await client.write_gatt_char(GATT_WRITE_UUID, COMMAND_LINK_PROBE_1, response=False)

    # ── BLE resolution & low-level connect ───────────────────────────────────

    def _ble_address_lookup_keys(self) -> tuple[str, ...]:
        formatted = dr.format_mac(self._mac)
        raw = self._mac.strip()
        return tuple(dict.fromkeys((
            formatted.lower(), formatted.upper(), formatted,
            raw.lower(), raw.upper(), raw,
        )))

    async def _async_resolve_ble_device(self):
        hass = self.hass
        keys = self._ble_address_lookup_keys()

        def _lookup_now():
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

    async def _async_establish_client(self, ble_device) -> BleakClient:
        await asyncio.sleep(BLE_PRE_CONNECT_DELAY)
        for slot_attempt in range(BLE_NO_SLOT_RETRIES):
            try:
                return await self._async_connect_inner(ble_device)
            except BleakOutOfConnectionSlotsError:
                if slot_attempt + 1 >= BLE_NO_SLOT_RETRIES:
                    raise
                _LOGGER.debug(
                    "No BLE slot for %s (attempt %s/%s), waiting %.1f s",
                    self._mac, slot_attempt + 1, BLE_NO_SLOT_RETRIES, BLE_NO_SLOT_BACKOFF,
                )
                await asyncio.sleep(BLE_NO_SLOT_BACKOFF)

    async def _async_connect_inner(self, ble_device) -> BleakClient:
        for attempt in range(BLE_IN_PROGRESS_RETRIES):
            try:
                return await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    name=self.name or self._mac,
                    max_attempts=3,
                    timeout=BLE_CONNECT_TIMEOUT,
                    use_services_cache=True,
                )
            except BleakOutOfConnectionSlotsError:
                raise
            except Exception as exc:
                text = _exception_chain_text(exc)
                if "in progress" in text or "error.inprogress" in text.replace(" ", ""):
                    if attempt + 1 >= BLE_IN_PROGRESS_RETRIES:
                        raise
                    await asyncio.sleep(BLE_IN_PROGRESS_BACKOFF_BASE + 0.85 * attempt)
                    continue
                raise

    # ── LightEntity interface ─────────────────────────────────────────────────

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._send_command(COMMAND_ON, optimistic_state=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send_command(COMMAND_OFF, optimistic_state=False)

    async def _send_command(self, payload: bytes, optimistic_state: bool) -> bool:
        if not self._is_connected():
            _LOGGER.debug("Not connected to %s — attempting connect before command", self._mac)
            await self._async_try_connect()
            if not self._is_connected():
                _LOGGER.warning(
                    "Device %s not reachable. Make sure the lamp is powered and advertising.",
                    self._mac,
                )
                return False

        async with self._cmd_lock:
            client = self._client
            if client is None or not client.is_connected:
                return False

            event = asyncio.Event()
            result: list[bool | None] = [None]
            self._notify_waiter = (frozenset({0x0001}), event, result)
            try:
                await client.write_gatt_char(GATT_WRITE_UUID, COMMAND_LINK_PROBE_1, response=False)
                await client.write_gatt_char(GATT_WRITE_UUID, payload, response=False)
                try:
                    await asyncio.wait_for(event.wait(), timeout=NOTIFY_TIMEOUT)
                except asyncio.TimeoutError:
                    _LOGGER.debug(
                        "No ON/OFF ack from %s within %.1f s — using optimistic state",
                        self._mac, NOTIFY_TIMEOUT,
                    )
                    self._state = optimistic_state
                    self.async_write_ha_state()
            except Exception:
                _LOGGER.exception("BLE error while writing to Ulanzi K6500 at %s", self._mac)
                return False
            finally:
                self._notify_waiter = None

            return True
