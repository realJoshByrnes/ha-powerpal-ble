import importlib.util
import json
import pathlib

import pytest

_path = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "powerpal_ble"
    / "energy.py"
)
_spec = importlib.util.spec_from_file_location("powerpal_energy", _path)
energy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(energy)

H = energy.HOUR
T0 = 1_700_000_000 - 1_700_000_000 % H  # an hour boundary


def wh(ts: int, pulses: int) -> float:
    return pulses * 1.0  # 1000 flashes per kWh: one pulse is 1 Wh


def test_add_is_idempotent_so_redownloads_cannot_double_count():
    log = energy.MinuteLog()
    log.add(T0, 10)
    log.add(T0, 10)
    log.add(T0 + 60, 5)
    assert len(log) == 2
    assert log.total_wh(wh) == 15


def test_readings_land_in_their_own_hour():
    log = energy.MinuteLog()
    assert log.add(T0 + 59 * 60, 10) == T0
    assert log.add(T0 + H, 5) == T0 + H
    assert log.hourly_wh(wh) == {T0: 10, T0 + H: 5}


def test_rows_are_cumulative_and_filtered():
    log = energy.MinuteLog()
    log.add(T0, 10)
    log.add(T0 + H, 5)
    log.add(T0 + 2 * H, 1)
    assert log.rows(T0, wh) == [(T0, 10), (T0 + H, 15), (T0 + 2 * H, 16)]
    assert log.rows(T0 + H, wh) == [(T0 + H, 15), (T0 + 2 * H, 16)]


def test_late_minute_in_an_old_hour_raises_later_cumulative_values():
    log = energy.MinuteLog()
    log.add(T0, 10)
    log.add(T0 + H, 5)
    log.add(T0 + 60, 2)
    assert log.rows(T0, wh) == [(T0, 12), (T0 + H, 17)]


def test_readings_are_in_time_order_and_filterable():
    log = energy.MinuteLog()
    for t in (T0 + 120, T0, T0 + 60):
        log.add(t, t - T0)
    assert list(log.readings()) == [(T0, 0), (T0 + 60, 60), (T0 + 120, 120)]
    assert list(log.readings(since=T0 + 60)) == [(T0 + 60, 60), (T0 + 120, 120)]


def test_prune_folds_old_minutes_without_changing_totals_or_cumulative_sums():
    log = energy.MinuteLog()
    for i in range(100 * 60):  # 100 hours of minutes
        log.add(T0 + i * 60, 1)
    before = log.total_wh(wh)
    last_row = log.rows(0, wh)[-1]
    newest = T0 + 100 * H - 60
    log.prune(newest, 10 * H, wh)
    assert log.total_wh(wh) == pytest.approx(before)
    assert log.rows(0, wh)[-1] == pytest.approx(last_row)
    assert min(m * 60 for m in log._pulses) >= log.hour_of(newest - 10 * H)
    assert len(log) < 100 * 60


def test_storage_roundtrip_is_compact_and_lossless():
    log = energy.MinuteLog(base_wh=3.5)
    for i in range(120):
        log.add(T0 + i * 60, i % 7)
    log.add(T0 + 500 * 60, 9)  # a gap creates a second run
    data = log.to_dict()
    assert len(data["runs"]) == 2
    restored = energy.MinuteLog.from_dict(json.loads(json.dumps(data)))
    assert restored.base_wh == 3.5
    assert list(restored.readings()) == list(log.readings())
    assert restored.total_wh(wh) == pytest.approx(log.total_wh(wh))
    # the full 90-day retention of continuous minutes stays well under a megabyte
    big = energy.MinuteLog()
    for i in range(90 * 1440):
        big.add(T0 + i * 60, 40 + i % 20)
    assert len(json.dumps(big.to_dict())) < 600_000


def test_hourly_power_is_mean_min_max_of_minute_rates():
    log = energy.MinuteLog()
    for i, pulses in enumerate([10, 20, 30]):  # 10, 20, 30 Wh in three minutes of one hour
        log.add(T0 + i * 60, pulses)
    log.add(T0 + H, 5)  # a minute in the next hour
    power = log.hourly_power(wh)
    assert power[T0] == pytest.approx((1200.0, 600.0, 1800.0))  # E Wh in one minute is E * 60 W
    assert power[T0 + H] == pytest.approx((300.0, 300.0, 300.0))


def test_hourly_power_can_be_limited_and_skips_hours_without_data():
    log = energy.MinuteLog()
    log.add(T0, 10)
    log.add(T0 + 2 * H, 20)
    assert set(log.hourly_power(wh)) == {T0, T0 + 2 * H}  # the empty hour is absent, not zero
    assert set(log.hourly_power(wh, only={T0 + 2 * H})) == {T0 + 2 * H}
    assert log.hourly_power(wh, only=set()) == {}


def test_a_full_hours_mean_power_equals_its_energy_and_a_busy_minute_shows_in_the_max():
    log = energy.MinuteLog()
    for i in range(60):
        log.add(T0 + i * 60, 1)  # 1 Wh a minute is 60 W
    log.add(T0 + 30 * 60, 50)  # one busy minute: 3000 W
    mean, low, high = log.hourly_power(wh)[T0]
    assert (low, high) == (60.0, 3000.0)
    assert mean == pytest.approx(log.hourly_wh(wh)[T0])  # mean W over a full hour == Wh

