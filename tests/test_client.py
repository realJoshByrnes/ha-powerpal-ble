"""PowerpalClient against a fake bleak client: connect, bond, unlock, read and history."""

import asyncio
import struct
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from powerpal_ble import BondingError, LinkError, PowerpalClient, Reading, WrongCodeError
from powerpal_ble import client as client_mod
from powerpal_ble.const import (
    CHAR_BATTERY,
    CHAR_FIRMWARE,
    CHAR_FIRST_REC,
    CHAR_LED_SENSITIVITY,
    CHAR_MEASUREMENT,
    CHAR_MEASUREMENT_ACCESS,
    CHAR_MILLIS_SINCE_PULSE,
    CHAR_PAIRING_CODE,
    CHAR_PULSE,
    CHAR_SERIAL,
    CHAR_TIME,
)

CODE = 123456
DEVICE = SimpleNamespace(address="AA:BB:CC:DD:EE:FF")


def u32(n):
    return struct.pack("<I", n)


class FakeBleak:
    """Stands in for BleakClient. Serial reads fail until the right code has been written."""

    def __init__(self, *, pair_error=None, drop_on_pair=False, code=CODE, history=()):
        self.pair_error = pair_error
        self.drop_on_pair = drop_on_pair
        self.code = code
        self.history = list(history)
        self.is_connected = True
        self.unlocked = False
        self.writes = []
        self.disconnects = 0
        self._handler = None
        self.values = {
            CHAR_SERIAL: u32(0x000A1B2C),
            CHAR_PULSE: u32(1800),
            CHAR_MILLIS_SINCE_PULSE: u32(2400),
            CHAR_FIRST_REC: struct.pack("<II", 1000, 2000),
            CHAR_FIRMWARE: b"1.2.3",
            CHAR_BATTERY: bytes([87]),
            CHAR_LED_SENSITIVITY: bytes([9]),
            CHAR_TIME: u32(5000),
        }

    async def pair(self):
        if self.pair_error:
            if self.drop_on_pair:
                self.is_connected = False
            raise self.pair_error

    async def disconnect(self):
        self.disconnects += 1
        self.is_connected = False

    async def write_gatt_char(self, uuid, data, response=False):
        self.writes.append((uuid, bytes(data)))
        if uuid == CHAR_PAIRING_CODE:
            self.unlocked = struct.unpack("<I", data)[0] == self.code
        elif uuid == CHAR_LED_SENSITIVITY:
            self.values[uuid] = bytes(data)
        elif uuid == CHAR_MEASUREMENT_ACCESS:
            for reading in self.history:
                self._handler(None, bytearray(struct.pack("<IH", *reading) + bytes(14)))

    async def read_gatt_char(self, uuid):
        if not self.unlocked and uuid == CHAR_SERIAL:
            raise RuntimeError("Read not permitted")
        return self.values[uuid]

    async def start_notify(self, uuid, handler):
        assert uuid == CHAR_MEASUREMENT
        self._handler = handler

    async def stop_notify(self, uuid):
        self._handler = None


@pytest.fixture
def patched(monkeypatch):
    """Route establish_connection to a queue of fakes and record BlueZ clean-ups."""
    state = SimpleNamespace(fakes=[], forgotten=[], connects=0)

    async def establish(*_args, **_kwargs):
        state.connects += 1
        fake = state.fakes.pop(0)
        if isinstance(fake, Exception):
            raise fake
        return fake

    @asynccontextmanager
    async def no_agent():
        yield

    async def forget(address):
        state.forgotten.append(address)
        return True

    monkeypatch.setattr(client_mod, "establish_connection", establish)
    monkeypatch.setattr(client_mod, "pairing_agent", no_agent)
    monkeypatch.setattr(client_mod, "forget_device", forget)
    return state


def run(coro):
    return asyncio.run(coro)


async def _open(code=CODE, attempts=1):
    return await PowerpalClient(DEVICE, code, connect_attempts=attempts).__aenter__()


def test_unlock_writes_the_code_and_reads_state(patched):
    fake = FakeBleak()
    patched.fakes = [fake]

    async def go():
        async with PowerpalClient(DEVICE, CODE) as client:
            return await client.read_state()

    state = run(go())
    assert (CHAR_PAIRING_CODE, u32(CODE)) in fake.writes
    assert state.serial == "000A1B2C"
    assert state.firmware == "1.2.3"
    assert state.battery == 87
    assert state.led_sensitivity == 9
    assert state.live_interval_ms == 2400  # the larger of the two interval reads
    assert (state.first_record, state.last_record, state.device_time) == (1000, 2000, 5000)
    assert fake.disconnects == 1  # left the device free for the next poll


def test_wrong_code_is_reported_as_such_when_bonded(patched):
    patched.fakes = [FakeBleak(code=999999)]
    with pytest.raises(WrongCodeError):
        run(_open())
    assert patched.forgotten == []  # the bond is fine, so it is not thrown away


def test_failed_bond_that_drops_the_link_is_retryable_and_forgets_the_device(patched):
    fake = FakeBleak(pair_error=RuntimeError("AuthenticationFailed"), drop_on_pair=True)
    patched.fakes = [fake]
    with pytest.raises(BondingError, match="AuthenticationFailed"):
        run(_open())
    assert patched.forgotten == [DEVICE.address]


def test_pair_failure_on_a_proxy_is_tolerated_if_the_unlock_works(patched):
    # A proxy may not support pair(); the link stays up and the unlock decides.
    patched.fakes = [FakeBleak(pair_error=NotImplementedError("pair"))]

    async def go():
        async with PowerpalClient(DEVICE, CODE) as client:
            return await client.read_serial()

    assert run(go()) == "000A1B2C"


def test_pair_failure_then_locked_is_a_bonding_error_not_a_wrong_code(patched):
    patched.fakes = [FakeBleak(pair_error=NotImplementedError("pair"), code=1)]
    with pytest.raises(BondingError):
        run(_open())
    assert patched.forgotten == [DEVICE.address]


def test_connect_failure_is_a_link_error_after_all_attempts(patched):
    patched.fakes = [TimeoutError(), TimeoutError()]
    with pytest.raises(LinkError, match="2 x"):
        run(_open(attempts=2))
    assert patched.connects == 2


def test_connect_retries_then_succeeds(patched):
    patched.fakes = [TimeoutError(), FakeBleak()]
    client = run(_open(attempts=2))
    assert patched.connects == 2
    assert client._client.unlocked


def test_link_dropped_after_bonding_reconnects(patched):
    first = FakeBleak()
    second = FakeBleak()

    async def drop_after_pair():
        first.is_connected = False

    first.pair = drop_after_pair
    patched.fakes = [first, second]
    client = run(_open())
    assert client._client is second and second.unlocked


def test_battery_and_led_failures_do_not_fail_a_poll(patched):
    fake = FakeBleak()
    del fake.values[CHAR_BATTERY]
    del fake.values[CHAR_LED_SENSITIVITY]
    patched.fakes = [fake]

    async def go():
        async with PowerpalClient(DEVICE, CODE) as client:
            return await client.read_state()

    state = run(go())
    assert state.battery is None and state.led_sensitivity is None


def test_led_sensitivity_write_is_validated_and_read_back(patched):
    patched.fakes = [FakeBleak()]

    async def go():
        async with PowerpalClient(DEVICE, CODE) as client:
            with pytest.raises(ValueError):
                await client.write_led_sensitivity(15)
            return await client.write_led_sensitivity(12)

    assert run(go()) == 12


def test_set_clock_writes_current_time(patched, monkeypatch):
    fake = FakeBleak()
    patched.fakes = [fake]
    monkeypatch.setattr(client_mod.time, "time", lambda: 1_700_000_000.7)

    async def go():
        async with PowerpalClient(DEVICE, CODE) as client:
            await client.set_clock()

    run(go())
    assert (CHAR_TIME, u32(1_700_000_000)) in fake.writes


def test_history_stops_at_end_and_ignores_later_readings(patched):
    fake = FakeBleak(history=[(100, 5), (160, 6), (220, 7), (280, 9)])
    patched.fakes = [fake]
    seen = []

    async def go():
        async with PowerpalClient(DEVICE, CODE) as client:
            return await client.download_history(100, 220, on_reading=seen.append)

    readings = run(go())
    assert readings == [Reading(100, 5), Reading(160, 6), Reading(220, 7)]
    assert seen == readings
    assert fake._handler is None  # unsubscribed
    assert (CHAR_MEASUREMENT_ACCESS, struct.pack("<II", 100, 220)) in fake.writes


def test_history_ends_after_silence(patched, monkeypatch):
    monkeypatch.setattr(client_mod, "HISTORY_SILENCE_TIMEOUT", 0.05)
    patched.fakes = [FakeBleak(history=[(100, 5), (160, 6)])]  # never reaches `end`

    async def go():
        async with PowerpalClient(DEVICE, CODE) as client:
            return await client.download_history(100, 500)

    assert run(go()) == [Reading(100, 5), Reading(160, 6)]
