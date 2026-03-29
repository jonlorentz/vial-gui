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
import threading

try:
    from bleak import BleakClient, BleakScanner
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
        self._response = None
        self._response_event = threading.Event()

    def _on_notify(self, _sender, data):
        self._response = bytes(data)
        self._response_event.set()

    async def _connect(self):
        self._client = BleakClient(self._address)
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
        if self._client and self._client.is_connected:
            try:
                _run(self._client.disconnect(), timeout=10)
            except Exception:
                pass
        self._client = None


def scan_ble_vial(timeout=4.0):
    """Return a list of synthetic HID-descriptor dicts for BLE Vial devices."""
    if not BLEAK_AVAILABLE:
        return []

    try:
        devices_adv = _run(
            BleakScanner.discover(
                timeout=timeout,
                service_uuids=[VIAL_BLE_SERVICE_UUID],
                return_adv=True,
            )
        )
    except Exception as e:
        logging.warning("BLE scan failed: %s", e)
        return []

    results = []
    for addr, (device, adv) in devices_adv.items():
        results.append({
            "vendor_id": 0x0000,
            "product_id": 0x0000,
            "serial_number": VIAL_SERIAL_NUMBER_MAGIC,
            "path": "ble:{}".format(addr).encode(),
            "usage_page": 0xFF60,
            "usage": 0x61,
            "manufacturer_string": "Keyboard Hub",
            "product_string": device.name or "BLE Vial",
            "_ble_address": addr,
        })
    return results
