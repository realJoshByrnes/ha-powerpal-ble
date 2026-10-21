"""Client for the Powerpal readings web service (optional, off by default).

See docs/http-service.md. The readings service answers per-minute rows whose pulses are
identical to the device's own readings for the same minutes.

No Home Assistant imports. The credential is the device's hardware UUID, sent as the
`Authorization` header; it must never be logged or put in a URL.
"""

from __future__ import annotations

import gzip
import json
import uuid
from collections.abc import Iterator, Sequence

import aiohttp

READINGS_URL = "https://readings.powerpal.net/api/v1/meter_reading/{serial}"
WINDOW_SECONDS = 7 * 86400  # ~10,000 rows at most
# The service returns at most this many rows per request, oldest first, and still answers 200.
# A window that fills it has silently lost its NEWEST readings, so it is treated as an error.
MAX_ROWS = 50_000
MAX_UPLOAD_ROWS = 1000  # the app's per-request limit
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)


class CloudError(Exception):
    """The web service could not be reached or returned an error."""


class CloudAuthError(CloudError):
    """The service rejected the credential (HTTP 401/403)."""


def token_candidates(raw: bytes) -> list[str]:
    """The ways the 16 bytes read from characteristic 59DA0009 could be written as a UUID."""
    forms = (uuid.UUID(bytes=raw), uuid.UUID(bytes=raw[::-1]), uuid.UUID(bytes_le=raw))
    out: list[str] = []
    for form in forms:
        text = str(form)
        if text not in out:
            out.append(text)
    return out


def windows(start: int, end: int, size: int = WINDOW_SECONDS) -> Iterator[tuple[int, int]]:
    """Consecutive (start, end) request windows covering [start, end]."""
    while start < end:
        stop = min(start + size, end)
        yield start, stop
        start = stop


class PowerpalCloud:
    def __init__(self, session: aiohttp.ClientSession, serial: str, token: str) -> None:
        self._session = session
        self._serial = serial.lower()  # the service wants lowercase hex (uppercase gives 403)
        self._token = token

    async def fetch_readings(self, start: int, end: int) -> list[tuple[int, int]]:
        """(timestamp, pulses) per minute in [start, end]."""
        try:
            async with self._session.get(
                READINGS_URL.format(serial=self._serial),
                params={"start": start, "end": end},
                headers={"Authorization": self._token, "Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status in (401, 403):
                    raise CloudAuthError(f"HTTP {response.status}")
                if response.status >= 400:
                    raise CloudError(f"HTTP {response.status}")
                rows = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CloudError(type(err).__name__) from err
        if len(rows) >= MAX_ROWS:
            raise CloudError("response hit the service row limit; the window is too large")
        try:
            return [(int(row["timestamp"]), int(row["pulses"])) for row in rows]
        except (KeyError, TypeError, ValueError) as err:
            raise CloudError("unexpected response format") from err

    async def post_readings(self, rows: Sequence[tuple[int, int, float]]) -> int:
        """Upload (timestamp, pulses, watt_hours) readings, as the app does. Returns how many the
        service echoed back priced.

        The body must really be gzip-compressed (a plain body is a 400), and a
        reading from Bluetooth carries only these three fields; the service adds all pricing.
        """
        if not rows:
            return 0
        if len(rows) > MAX_UPLOAD_ROWS:
            raise ValueError(f"at most {MAX_UPLOAD_ROWS} readings per upload")
        payload = [{"timestamp": t, "pulses": p, "watt_hours": wh} for t, p, wh in rows]
        body = gzip.compress(json.dumps(payload, separators=(",", ":")).encode())
        try:
            async with self._session.post(
                READINGS_URL.format(serial=self._serial),
                data=body,
                headers={
                    "Authorization": self._token,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                },
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status in (401, 403):
                    raise CloudAuthError(f"HTTP {response.status}")
                if response.status >= 400:
                    raise CloudError(f"HTTP {response.status}")
                answer = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CloudError(type(err).__name__) from err
        try:
            return len(answer["readings"])
        except (KeyError, TypeError) as err:
            raise CloudError("unexpected response format") from err


async def find_token(
    session: aiohttp.ClientSession, serial: str, raw: bytes, now: int
) -> str | None:
    """Return the candidate UUID string the service accepts, or None if none is accepted."""
    for candidate in token_candidates(raw):
        try:
            await PowerpalCloud(session, serial, candidate).fetch_readings(now - 120, now)
        except CloudAuthError:
            continue
        return candidate
    return None
