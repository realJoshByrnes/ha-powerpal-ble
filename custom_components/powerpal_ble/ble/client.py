"""Connect-and-poll client: connect, bond, unlock, read state, download history."""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections.abc import Callable

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .agent import forget_device, pairing_agent
from .const import (
    LED_SENSITIVITY_MAX,
    LED_SENSITIVITY_MIN,
    CHAR_BATTERY,
    CHAR_FIRMWARE,
    CHAR_FIRST_REC,
    CHAR_HARDWARE_UUID,
    CHAR_LED_SENSITIVITY,
    CHAR_MEASUREMENT,
    CHAR_MEASUREMENT_ACCESS,
    CHAR_MILLIS_SINCE_PULSE,
    CHAR_PAIRING_CODE,
    CHAR_PULSE,
    CHAR_SERIAL,
    CHAR_TIME,
    HISTORY_SILENCE_TIMEOUT,
)
from .models import PowerpalState, Reading


_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10.0  # seconds per connect attempt


def _describe(err: BaseException) -> str:
    """Short human text for a BLE failure."""
    return str(err).strip() or type(err).__name__


class PowerpalError(Exception):
    """Base class: the device could not be connected, unlocked or read."""


class LinkError(PowerpalError):
    """Could not connect or keep the link (weak signal, device dropped us). Worth retrying."""


class BondingError(LinkError):
    """Connected, but bonding failed. Worth retrying."""


class WrongCodeError(PowerpalError):
    """Bonded and connected, but the device stayed locked: the pairing code is wrong."""


class PowerpalClient:
    """One short-lived connection: connect, unlock, read, optionally download history."""

    def __init__(self, device: BLEDevice, pairing_code: int, connect_attempts: int = 1) -> None:
        self._device = device
        self._pairing_code = pairing_code
        self._connect_attempts = connect_attempts
        self._client: BleakClient | None = None

    async def _connect(self) -> BleakClient:
        # On a weak link a connect either succeeds within a few seconds or not at all.
        # bleak-retry-connector hardcodes 20 s per attempt (x4 by default = 80 s), so run
        # single attempts under our own shorter deadline.
        last: Exception | None = None
        for attempt in range(1, self._connect_attempts + 1):
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT):
                    return await establish_connection(
                        BleakClientWithServiceCache,
                        self._device,
                        self._device.address,
                        max_attempts=1,
                    )
            except Exception as err:  # noqa: BLE001  (TimeoutError, BleakError, ...)
                last = err
                _LOGGER.debug(
                    "Connect attempt %d/%d failed: %r", attempt, self._connect_attempts, err
                )
        raise LinkError(
            f"could not connect ({self._connect_attempts} x {CONNECT_TIMEOUT:.0f}s): {last!r}"
        ) from last

    async def _drop(self) -> None:
        if self._client:
            try:
                # A stalled BlueZ call can hang again on disconnect; never wait long.
                async with asyncio.timeout(5):
                    await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def __aenter__(self) -> PowerpalClient:
        try:
            self._client = await self._connect()
            # Gate 1: bond (Just Works, like the Android app). Already-bonded is fine.
            bonded = True
            try:
                async with pairing_agent():
                    await self._client.pair()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("pair() failed: %r", err)
                if not self._client.is_connected:
                    # BlueZ drops the link on a failed bond and keeps failing on that device
                    # object; drop it so the caller's retry starts from a fresh one.
                    await self._drop()
                    await forget_device(self._device.address)
                    raise BondingError(_describe(err)) from err
                # Still connected. A Bluetooth proxy may not support pair() (older ESPHome),
                # or report an error for a device it already bonded; the unlock decides.
                bonded = False
                _LOGGER.debug("Still connected after pair() failed; trying the unlock anyway")
            else:
                _LOGGER.debug("pair() ok")
            # The device may drop the link right after bonding.
            if not self._client.is_connected:
                _LOGGER.debug("Link dropped after bonding, reconnecting")
                self._client = await self._connect()
            # Gate 2: per-connection application unlock.
            _LOGGER.debug("Writing pairing code")
            await self._client.write_gatt_char(
                CHAR_PAIRING_CODE, struct.pack("<I", self._pairing_code), response=True
            )
            _LOGGER.debug("Reading serial")
            try:
                await self._client.read_gatt_char(CHAR_SERIAL)
            except Exception as err:  # ATT "read not permitted": wrong code, or not bonded
                if bonded:
                    raise WrongCodeError("Device stayed locked: check the pairing code") from err
                await self._drop()
                await forget_device(self._device.address)
                raise BondingError(f"not bonded and unlock failed: {_describe(err)}") from err
            _LOGGER.debug("Unlocked")
        except PowerpalError:
            await self._drop()
            raise
        except Exception as err:  # noqa: BLE001
            await self._drop()
            raise LinkError(_describe(err)) from err
        except BaseException:
            await self._drop()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:  # also used on failed __aenter__
        if self._client and self._client.is_connected:
            await self._client.disconnect()

    async def _u32(self, uuid: str) -> int:
        return struct.unpack("<I", (await self._client.read_gatt_char(uuid))[:4])[0]

    async def read_hardware_uuid(self) -> bytes:
        """The 16 raw bytes of the hardware UUID (secret: never log them)."""
        assert self._client
        return bytes(await self._client.read_gatt_char(CHAR_HARDWARE_UUID))

    async def read_led_sensitivity(self) -> int:
        assert self._client
        return (await self._client.read_gatt_char(CHAR_LED_SENSITIVITY))[0]

    async def write_led_sensitivity(self, value: int) -> int:
        """Set the LED sensor sensitivity (0-14) and return what the device now reports."""
        if not LED_SENSITIVITY_MIN <= value <= LED_SENSITIVITY_MAX:
            raise ValueError(f"LED sensitivity must be {LED_SENSITIVITY_MIN}-{LED_SENSITIVITY_MAX}")
        assert self._client
        await self._client.write_gatt_char(CHAR_LED_SENSITIVITY, bytes([value]), response=True)
        return await self.read_led_sensitivity()

    async def read_serial(self) -> str:
        assert self._client
        return f"{await self._u32(CHAR_SERIAL):08X}"

    async def read_state(self) -> PowerpalState:
        assert self._client
        serial = f"{await self._u32(CHAR_SERIAL):08X}"
        interval = max(await self._u32(CHAR_PULSE), await self._u32(CHAR_MILLIS_SINCE_PULSE))
        first, last = struct.unpack("<II", (await self._client.read_gatt_char(CHAR_FIRST_REC))[:8])
        firmware = (await self._client.read_gatt_char(CHAR_FIRMWARE)).decode("utf-8", "replace")
        try:
            battery = (await self._client.read_gatt_char(CHAR_BATTERY))[0]
        except Exception:
            battery = None
        try:
            led = await self.read_led_sensitivity()
        except Exception:  # noqa: BLE001  (a display value: never fail a poll over it)
            led = None
        return PowerpalState(
            serial=serial,
            firmware=firmware,
            battery=battery,
            live_interval_ms=interval,
            device_time=await self._u32(CHAR_TIME),
            first_record=first,
            last_record=last,
            led_sensitivity=led,
        )

    async def set_clock(self) -> None:
        """Only call after seeing device_time == 0 (rebooted)."""
        assert self._client
        await self._client.write_gatt_char(
            CHAR_TIME, struct.pack("<I", int(time.time())), response=True
        )

    async def download_history(
        self,
        start: int,
        end: int,
        on_reading: Callable[[Reading], None] | None = None,
    ) -> list[Reading]:
        """Fetch readings in [start, end] inclusive. Stops on `end`, or after 30 s of silence."""
        assert self._client
        readings: list[Reading] = []
        queue: asyncio.Queue[Reading] = asyncio.Queue()

        def _handler(_: object, data: bytearray) -> None:
            if len(data) >= 6:
                queue.put_nowait(Reading.from_notification(bytes(data)))

        await self._client.start_notify(CHAR_MEASUREMENT, _handler)  # subscribe first
        try:
            await self._client.write_gatt_char(
                CHAR_MEASUREMENT_ACCESS, struct.pack("<II", start, end), response=True
            )
            while True:
                try:
                    reading = await asyncio.wait_for(queue.get(), HISTORY_SILENCE_TIMEOUT)
                except TimeoutError:
                    break
                if reading.timestamp > end:
                    continue
                readings.append(reading)
                if on_reading:
                    on_reading(reading)
                if reading.timestamp == end:
                    break
        finally:
            await self._client.stop_notify(CHAR_MEASUREMENT)
        return readings
