"""Powerpal BLE constants. Protocol notes: private research docs, section 2-3."""

SERVICE_UUID = "59daabcd-12f4-25a6-7d4f-55961dce4205"
SERVICE_UUID_SHORT = "0000fcd7-0000-1000-8000-00805f9b34fb"


def _char(n: int) -> str:
    return f"59da{n:04x}-12f4-25a6-7d4f-55961dce4205"


CHAR_MEASUREMENT = _char(0x0001)  # notify: uint32 timestamp, uint16 pulses
CHAR_MEASUREMENT_ACCESS = _char(0x0002)  # write: uint32 start, uint32 end
CHAR_PULSE = _char(0x0003)  # read/notify: uint32 ms between last two flashes
CHAR_TIME = _char(0x0004)  # read/write: uint32 unix seconds (last written, not ticking)
CHAR_FIRST_REC = _char(0x0005)  # read: uint32 earliest, uint32 latest
CHAR_LED_SENSITIVITY = _char(0x0008)  # read/write: uint8, 0-14, default 9
CHAR_HARDWARE_UUID = _char(0x0009)  # read: 16 bytes, the web service credential
CHAR_SERIAL = _char(0x0010)  # read: uint32
CHAR_PAIRING_CODE = _char(0x0011)  # write: uint32, required on every connection
CHAR_MILLIS_SINCE_PULSE = _char(0x0012)  # read: uint32
CHAR_BATCH_SIZE = _char(0x0013)  # read/write: uint32 minutes per batch

CHAR_BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"
CHAR_FIRMWARE = "00002a26-0000-1000-8000-00805f9b34fb"

LED_SENSITIVITY_MIN = 0
LED_SENSITIVITY_MAX = 14
LED_SENSITIVITY_DEFAULT = 9

# Name prefix of the discontinued Wi-Fi gateway. Never handled by this project.
PRO_NAME_PREFIX = "Powerpal Pro"

MAX_WH_PER_MINUTE = 416.6667  # 25 kW cap used by the official app
HISTORY_SILENCE_TIMEOUT = 30.0
