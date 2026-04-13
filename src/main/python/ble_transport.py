# SPDX-License-Identifier: GPL-2.0-or-later
"""
BLE transport for Vial keyboards behind a Keyboard Hub.

Provides BLEVialDevice (drop-in for hid.device()) and scan_ble_vial()
for discovering Vial-capable BLE devices.  Uses bleak for BLE I/O and
runs an asyncio event loop in a background daemon thread so that the
synchronous PyQt5 main thread is not affected.
"""

import asyncio
import logging
import sys
import threading

try:
    from bleak import BleakClient, BleakScanner
    from bleak.backends.device import BLEDevice
    BLEAK_AVAILABLE = True
except ImportError:
    BLEAK_AVAILABLE = False

# These must match the ESP32 firmware (config.h VIAL_BLE_*_UUID).
# The byte arrays there are little-endian; these are the standard
# big-endian UUID strings.
VIAL_BLE_SERVICE_UUID = "00004b48-4248-0001-4b48-484200000000"
VIAL_BLE_RX_CHAR_UUID = "00004b48-4248-0002-4b48-484200000000"
VIAL_BLE_TX_CHAR_UUID = "00004b48-4248-0003-4b48-484200000000"

VIAL_SERIAL_NUMBER_MAGIC = "vial:f64c2b3c"

_loop = None
_thread = None
_lock = threading.Lock()
_scan_lock = threading.Lock()
_scan_cache = []
_scan_cache_time = 0
SCAN_CACHE_TTL = 10.0  # seconds


def _ensure_loop():
    """Start the background asyncio event loop (once)."""
    global _loop, _thread
    with _lock:
        if _loop is not None:
            return
        _loop = asyncio.new_event_loop()
        _thread = threading.Thread(target=_loop.run_forever, daemon=True)
        _thread.start()


def _run(coro, timeout=30):
    """Submit *coro* to the background loop and block until it finishes."""
    _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=timeout)


class BLEVialDevice:
    """Drop-in replacement for hid.device() that talks over BLE GATT."""

    def __init__(self, address):
        self._address = address
        self._client = None
        self._was_connected = False
        self._response = None
        self._response_event = threading.Event()

    def _on_notify(self, _sender, data):
        self._response = bytes(data)
        self._response_event.set()

    async def _connect(self):
        if sys.platform.startswith("linux"):
            dbus_path = "/org/bluez/hci0/dev_" + self._address.replace(":", "_")
            ble_dev = BLEDevice(address=self._address, name="BLE Vial",
                                details={"path": dbus_path})
            self._client = BleakClient(ble_dev)
        else:
            self._client = BleakClient(self._address)
        self._was_connected = self._client.is_connected
        await self._client.connect()
        await self._client.start_notify(VIAL_BLE_TX_CHAR_UUID, self._on_notify)

    def open_path(self, path):
        _run(self._connect())

    def write(self, data):
        # Strip the 0x00 HID report-ID prefix that hidapi convention adds
        payload = data[1:]
        _run(self._client.write_gatt_char(VIAL_BLE_RX_CHAR_UUID, payload,
                                          response=True))
        return len(data)

    def read(self, length, timeout_ms=0):
        timeout_s = timeout_ms / 1000.0 if timeout_ms > 0 else 5.0
        if self._response_event.wait(timeout=timeout_s):
            resp = self._response[:length]
            self._response = None
            self._response_event.clear()
            return resp
        return b""

    def close(self):
        if self._client and self._client.is_connected and not self._was_connected:
            try:
                _run(self._client.disconnect(), timeout=10)
            except Exception:
                pass
        self._client = None


def _make_vial_desc(addr, name="BLE Vial"):
    return {
        "vendor_id": 0x0000,
        "product_id": 0x0000,
        "serial_number": VIAL_SERIAL_NUMBER_MAGIC,
        "path": "ble:{}".format(addr).encode(),
        "usage_page": 0xFF60,
        "usage": 0x61,
        "manufacturer_string": "Keyboard Hub",
        "product_string": name,
        "_ble_address": addr,
    }


async def _find_connected_vial_devices():
    """Find already-connected BlueZ devices that expose the Vial GATT service."""
    if sys.platform != "linux":
        return []

    try:
        from dbus_fast.aio import MessageBus
        from dbus_fast import BusType
    except ImportError:
        return []

    results = []
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        introspection = await bus.introspect("org.bluez", "/")
        proxy = bus.get_proxy_object("org.bluez", "/", introspection)
        manager = proxy.get_interface(
            "org.freedesktop.DBus.ObjectManager"
        )
        objects = await manager.call_get_managed_objects()

        for path, interfaces in objects.items():
            if "org.bluez.Device1" not in interfaces:
                continue
            props = interfaces["org.bluez.Device1"]
            connected = props.get("Connected")
            if not (connected and connected.value):
                continue
            uuids = props.get("UUIDs")
            uuid_list = [str(u) for u in uuids.value] if uuids else []
            if VIAL_BLE_SERVICE_UUID not in uuid_list:
                continue

            addr_v = props.get("Address")
            name_v = props.get("Name")
            addr = str(addr_v.value) if addr_v else ""
            name = str(name_v.value) if name_v else "BLE Vial"
            results.append(_make_vial_desc(addr, name))
    except Exception as e:
        logging.warning("D-Bus connected device scan failed: %s", e)
    finally:
        bus.disconnect()

    return results


def scan_ble_vial(timeout=2.0):
    """Return a list of synthetic HID-descriptor dicts for BLE Vial devices.

    Checks two sources:
    1. BLE advertising scan (for devices not yet connected)
    2. Already-connected BlueZ devices with the Vial GATT service

    Results are cached for SCAN_CACHE_TTL seconds so that rapid polling
    from the Vial GUI doesn't pile up BlueZ scan requests.
    """
    import time
    global _scan_cache, _scan_cache_time

    if not BLEAK_AVAILABLE:
        return []

    now = time.monotonic()
    if now - _scan_cache_time < SCAN_CACHE_TTL:
        return list(_scan_cache)

    if not _scan_lock.acquire(blocking=False):
        return list(_scan_cache)

    results = []
    seen_addrs = set()

    try:
        connected = _run(_find_connected_vial_devices(), timeout=5)
        for dev in connected:
            results.append(dev)
            seen_addrs.add(dev["_ble_address"])
    except Exception as e:
        logging.warning("Connected device scan failed: %s", e)

    try:
        devices_adv = _run(
            BleakScanner.discover(
                timeout=timeout,
                service_uuids=[VIAL_BLE_SERVICE_UUID],
                return_adv=True,
            )
        )
        for addr, (device, adv) in devices_adv.items():
            if addr not in seen_addrs:
                results.append(
                    _make_vial_desc(addr, device.name or "BLE Vial")
                )
    except Exception as e:
        logging.warning("BLE scan failed: %s", e)
    finally:
        _scan_lock.release()

    _scan_cache = results
    _scan_cache_time = now
    return list(results)
