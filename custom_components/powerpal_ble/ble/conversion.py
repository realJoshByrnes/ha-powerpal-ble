"""Pulse maths, mirroring the official app (protocol doc 5.2 and 5.3)."""

from __future__ import annotations

from .const import MAX_WH_PER_MINUTE


def live_watts(interval_ms: int, pulses_per_kwh: int) -> float:
    """Power from the milliseconds between the last two flashes, capped at 25 kW."""
    if interval_ms <= 0:
        return 0.0
    seconds = interval_ms / 1000
    wh = min(1000 / pulses_per_kwh, MAX_WH_PER_MINUTE * (seconds / 60))
    return wh / (seconds / 3600)


def reading_wh(pulses: int, pulses_per_kwh: int) -> float:
    """Energy of one stored one-minute reading."""
    return min(pulses * 1000 / pulses_per_kwh, MAX_WH_PER_MINUTE)
