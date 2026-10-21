from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class Reading:
    """One stored one-minute reading."""

    timestamp: int  # unix seconds, UTC
    pulses: int

    @classmethod
    def from_notification(cls, data: bytes) -> Reading:
        # Only the first 6 bytes are meaningful; the rest is constant padding.
        timestamp, pulses = struct.unpack_from("<IH", data)
        return cls(timestamp, pulses)


@dataclass(frozen=True)
class PowerpalState:
    serial: str
    firmware: str
    battery: int | None
    live_interval_ms: int  # max(last interval, millis since last pulse)
    device_time: int  # 0 means the device rebooted
    first_record: int
    last_record: int
    led_sensitivity: int | None = None
