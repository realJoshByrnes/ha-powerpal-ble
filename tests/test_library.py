import struct

import pytest
from powerpal_ble import Reading, live_watts, reading_wh


def test_reading_decodes_first_six_bytes_and_ignores_padding():
    data = struct.pack("<IH", 1_700_000_000, 49) + bytes(14)
    assert Reading.from_notification(data) == Reading(1_700_000_000, 49)


def test_reading_wh_matches_doc_example():
    assert reading_wh(49, 1000) == pytest.approx(49)  # 49 pulses -> 49 Wh at 1000/kWh


def test_reading_wh_is_capped_at_25kw():
    assert reading_wh(10_000, 1000) == pytest.approx(416.6667)


def test_live_watts():
    # 1000 flashes/kWh, one flash every 3.6 s -> 1 kW
    assert live_watts(3600, 1000) == pytest.approx(1000)


def test_live_watts_zero_interval():
    assert live_watts(0, 1000) == 0.0


def test_live_watts_capped_at_25kw():
    assert live_watts(1, 1000) == pytest.approx(25_000, rel=1e-3)


# ---- LED sensitivity (stored on the device, characteristic 59DA0008)

import asyncio  # noqa: E402

from powerpal_ble import PowerpalClient  # noqa: E402
from powerpal_ble.const import CHAR_LED_SENSITIVITY  # noqa: E402


class FakeGatt:
    def __init__(self, led=9):
        self.led = led
        self.writes = []

    async def write_gatt_char(self, uuid, data, response=True):
        self.writes.append((uuid, bytes(data), response))
        self.led = data[0]

    async def read_gatt_char(self, uuid):
        assert uuid == CHAR_LED_SENSITIVITY
        return bytearray([self.led])


def _client(gatt):
    client = PowerpalClient(device=None, pairing_code=1)
    client._client = gatt
    return client


def test_led_sensitivity_is_written_as_one_byte_with_response_and_read_back():
    gatt = FakeGatt(led=9)
    client = _client(gatt)
    assert asyncio.run(client.read_led_sensitivity()) == 9
    assert asyncio.run(client.write_led_sensitivity(12)) == 12
    assert gatt.writes == [(CHAR_LED_SENSITIVITY, bytes([12]), True)]


@pytest.mark.parametrize("value", [-1, 15, 255])
def test_led_sensitivity_out_of_range_is_refused_before_touching_the_device(value):
    gatt = FakeGatt()
    with pytest.raises(ValueError):
        asyncio.run(_client(gatt).write_led_sensitivity(value))
    assert gatt.writes == []


@pytest.mark.parametrize("value", [0, 14])
def test_led_sensitivity_accepts_both_ends_of_the_range(value):
    assert asyncio.run(_client(FakeGatt()).write_led_sensitivity(value)) == value
