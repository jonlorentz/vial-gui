# SPDX-License-Identifier: GPL-2.0-or-later
import struct
import sys

from protocol.constants import CMD_VIA_VIAL_PREFIX, CMD_VIAL_GET_KEYBOARD_ID

if sys.platform == "emscripten":

    import vialglue
    import json

    class hiddevice:

        def open_path(self, path):
            print("opening {}...".format(path))

        def write(self, data):
            return vialglue.write_device(data)

        def read(self, length, timeout_ms=0):
            data = vialglue.read_device()
            return data


    class hid:

        @staticmethod
        def enumerate():
            from util import hid_send

            desc = json.loads(vialglue.get_device_desc())
            # hack: we don't know if it's vial or VIA device because webhid doesn't expose serial number
            # so let's probe it with a vial command, and if the response looks good, inject fake vial serial number
            # in the device descriptor
            dev = hid.device()
            data = hid_send(dev, struct.pack("BB", CMD_VIA_VIAL_PREFIX, CMD_VIAL_GET_KEYBOARD_ID), retries=20)
            uid = data[4:12]
            # here, a VIA keyboard will echo back all zeroes, while vial will return a valid UID
            # so if this looks like vial, inject the serial numebr
            if uid != b"\x00" * 8:
                desc["serial_number"] = "vial:f64c2b3c"
            return [desc]

        @staticmethod
        def device():
            return hiddevice()

else:
    if sys.platform.startswith("linux"):
        import hidraw as _platform_hid
    else:
        import hid as _platform_hid

    class _BLEAwareDevice:
        """Wraps a platform HID device but intercepts BLE paths."""

        def __init__(self):
            self._inner = None

        def open_path(self, path):
            if isinstance(path, (bytes, bytearray)):
                path_str = path.decode("utf-8", errors="replace")
            else:
                path_str = path
            if path_str.startswith("ble:"):
                from ble_transport import BLEVialDevice
                addr = path_str[4:]
                self._inner = BLEVialDevice(addr)
                self._inner.open_path(path)
            else:
                self._inner = _platform_hid.device()
                self._inner.open_path(path)

        def write(self, data):
            return self._inner.write(data)

        def read(self, length, timeout_ms=0):
            return self._inner.read(length, timeout_ms=timeout_ms)

        def close(self):
            return self._inner.close()

    class hid:

        @staticmethod
        def enumerate():
            return _platform_hid.enumerate()

        @staticmethod
        def device():
            return _BLEAwareDevice()
