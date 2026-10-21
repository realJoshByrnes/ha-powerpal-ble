import asyncio
import importlib
import pathlib
import sys
import types

import pytest

_base = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "powerpal_ble"
_pkg = types.ModuleType("pbl")  # a stand-in package, so the modules' relative imports work
_pkg.__path__ = [str(_base)]
sys.modules.setdefault("pbl", _pkg)
energy = importlib.import_module("pbl.energy")
cloud_mod = importlib.import_module("pbl.cloud")
sync_mod = importlib.import_module("pbl.cloud_sync")

DAY = 86400
T0 = 1_700_000_000 - 1_700_000_000 % 3600
wh = lambda ts, pulses: float(pulses)  # 1000 flashes per kWh


def run(coro):
    return asyncio.run(coro)


class FakeCloud:
    def __init__(self, rows=None):
        self.rows = dict(rows or {})  # timestamp -> pulses
        self.posts = []  # each: list of (timestamp, pulses, watt_hours)
        self.fetches = []
        self.fail_fetch_on_call = None
        self.fail_post = False

    async def fetch_readings(self, start, end):
        self.fetches.append((start, end))
        if self.fail_fetch_on_call == len(self.fetches):
            raise cloud_mod.CloudError("boom")
        return [(t, p) for t, p in sorted(self.rows.items()) if start <= t <= end]

    async def post_readings(self, rows):
        if self.fail_post:
            raise cloud_mod.CloudError("HTTP 500")
        self.posts.append(list(rows))
        for t, p, _ in rows:
            self.rows[t] = p
        return len(rows)


def make_log(start, minutes, pulses=5):
    log = energy.MinuteLog()
    for i in range(minutes):
        log.add(start + i * 60, pulses)
    return log


def newest(log):
    return max(t for t, _ in log.readings())


# ---------------------------------------------------------------- upload

def test_upload_sends_only_what_the_cloud_lacks_in_the_apps_format():
    log = make_log(T0, 10)
    cloud = FakeCloud({T0 + i * 60: 5 for i in range(4)})  # the cloud already has the first 4
    sync = sync_mod.CloudSync(log, wh)
    posted = run(sync.upload(cloud, newest(log), now_monotonic=1000.0))
    assert posted == 6
    (batch,) = cloud.posts
    assert [t for t, _, _ in batch] == [T0 + i * 60 for i in range(4, 10)]
    assert all(p == 5 and w == 5.0 for _, p, w in batch)
    assert sync.upload_cursor == newest(log)


def test_nothing_is_sent_twice():
    log = make_log(T0, 10)
    cloud = FakeCloud()
    sync = sync_mod.CloudSync(log, wh)
    assert run(sync.upload(cloud, newest(log), 1000.0)) == 10
    log.add(T0 + 10 * 60, 7)  # one new minute arrives
    assert run(sync.upload(cloud, newest(log), 2000.0)) == 1
    assert run(sync.upload(cloud, newest(log), 3000.0)) == 0
    assert sum(len(b) for b in cloud.posts) == 11


def test_minutes_that_came_from_the_cloud_are_not_uploaded_back():
    log = make_log(T0, 10)
    cloud = FakeCloud({T0 + i * 60: 5 for i in range(10)})  # identical, as the phone uploaded them
    assert run(sync_mod.CloudSync(log, wh).upload(cloud, newest(log), 1000.0)) == 0
    assert cloud.posts == []


def test_uploads_are_throttled_to_one_per_five_minutes():
    log = make_log(T0, 10)
    cloud = FakeCloud()
    sync = sync_mod.CloudSync(log, wh)
    assert run(sync.upload(cloud, newest(log), 1000.0)) == 10
    log.add(T0 + 10 * 60, 7)
    assert run(sync.upload(cloud, newest(log), 1000.0 + 299)) == 0  # too soon
    assert len(cloud.posts) == 1
    assert run(sync.upload(cloud, newest(log), 1000.0 + 301)) == 1  # allowed, and still pending


def test_a_large_backlog_goes_up_in_batches_of_at_most_1000():
    log = make_log(T0, 2500)
    cloud = FakeCloud()
    sync = sync_mod.CloudSync(log, wh)
    now = 0.0
    sizes = []
    for _ in range(10):
        now += 301
        sizes.append(run(sync.upload(cloud, newest(log), now)))
        if sync.upload_cursor == newest(log):
            break
    assert sizes[:3] == [1000, 1000, 500]
    assert max(len(b) for b in cloud.posts) == 1000
    assert sorted(cloud.rows) == [T0 + i * 60 for i in range(2500)]


def test_a_failed_upload_does_not_advance_the_cursor():
    log = make_log(T0, 10)
    cloud = FakeCloud()
    cloud.fail_post = True
    sync = sync_mod.CloudSync(log, wh)
    with pytest.raises(cloud_mod.CloudError):
        run(sync.upload(cloud, newest(log), 1000.0))
    assert sync.upload_cursor == 0
    cloud.fail_post = False
    assert run(sync.upload(cloud, newest(log), 1001.0)) == 10  # the next try sends them


def test_reconciliation_walks_a_long_log_in_seven_day_windows():
    log = make_log(T0, 20 * 1440, pulses=1)  # 20 days
    cloud = FakeCloud({t: 1 for t, _ in log.readings()})  # nothing is missing
    sync = sync_mod.CloudSync(log, wh)
    runs = 0
    while sync.upload_cursor < newest(log):
        run(sync.upload(cloud, newest(log), 1000.0 * runs))
        runs += 1
        assert runs < 10
    assert runs == 3  # 7 + 7 + 6 days
    assert cloud.posts == []


def test_upload_does_nothing_for_an_empty_log():
    sync = sync_mod.CloudSync(energy.MinuteLog(), wh)
    assert run(sync.upload(FakeCloud(), 0, 1000.0)) == 0


def test_backlog_flags():
    log = make_log(T0, 3 * 3600 // 60)  # 3 hours
    sync = sync_mod.CloudSync(log, wh)
    assert sync.upload_backlog(newest(log))
    run(sync.upload(FakeCloud(), newest(log), 1000.0))
    assert not sync.upload_backlog(newest(log))


# -------------------------------------------------------------- download

def test_download_first_run_starts_backfill_days_back_and_does_at_most_four_windows():
    now = T0 + 100 * DAY
    log = energy.MinuteLog()
    cloud = FakeCloud({now - 60 * DAY + i * 60: 3 for i in range(100)})
    sync = sync_mod.CloudSync(log, wh)
    pending: set[int] = set()
    added = run(sync.download(cloud, now, backfill_days=90, not_before=0, pending=pending))
    assert len(cloud.fetches) == 4
    assert sync.download_cursor == now - 90 * DAY + 4 * cloud_mod.WINDOW_SECONDS
    assert added == 0 and not pending  # nothing in those first windows
    assert sync.download_backlog(now)
    run(sync.download(cloud, now, 90, 0, pending))
    run(sync.download(cloud, now, 90, 0, pending))
    run(sync.download(cloud, now, 90, 0, pending))
    assert sync.download_cursor == now and not sync.download_backlog(now)
    assert len(log) == 100 and pending


def test_download_never_overwrites_a_device_minute():
    now = T0 + 10 * DAY
    log = energy.MinuteLog()
    log.add(now - DAY, 9)
    cloud = FakeCloud({now - DAY: 1, now - DAY + 60: 2})
    sync = sync_mod.CloudSync(log, wh)
    pending: set[int] = set()
    run(sync.download(cloud, now, 3, 0, pending))
    assert dict(log.readings()) == {now - DAY: 9, now - DAY + 60: 2}


def test_download_progress_survives_a_failed_window():
    now = T0 + 100 * DAY
    log = energy.MinuteLog()
    cloud = FakeCloud({now - 90 * DAY + 60: 3, now - 90 * DAY + 8 * DAY: 4})
    cloud.fail_fetch_on_call = 3
    sync = sync_mod.CloudSync(log, wh)
    pending: set[int] = set()
    with pytest.raises(cloud_mod.CloudError):
        run(sync.download(cloud, now, 90, 0, pending))
    assert sync.download_cursor == now - 90 * DAY + 2 * cloud_mod.WINDOW_SECONDS  # two windows done
    assert len(log) == 2 and len(pending) == 2  # and their hours are marked for the statistics


def test_daily_top_up_after_the_import_is_complete():
    now = T0 + 100 * DAY
    log = energy.MinuteLog()
    cloud = FakeCloud()
    sync = sync_mod.CloudSync(log, wh, download_cursor=now, download_checked=now)
    pending: set[int] = set()
    run(sync.download(cloud, now + 3600, 90, 0, pending))
    assert cloud.fetches == []  # too soon for the daily top-up
    cloud.rows[now + 5 * 3600] = 8
    added = run(sync.download(cloud, now + DAY + 60, 90, 0, pending))
    assert cloud.fetches == [(now + DAY + 60 - 3 * DAY, now + DAY + 60)]
    assert added == 1


def test_state_roundtrip():
    sync = sync_mod.CloudSync(energy.MinuteLog(), wh, 1, 2, 3)
    assert sync.to_dict() == {"cloud_cursor": 1, "cloud_checked": 2, "upload_cursor": 3}


def test_seconds_until_next_post():
    log = make_log(T0, 5)
    sync = sync_mod.CloudSync(log, wh)
    assert sync.seconds_until_next_post(0.0) == 0.0  # nothing posted yet
    run(sync.upload(FakeCloud(), newest(log), 1000.0))
    assert sync.seconds_until_next_post(1100.0) == pytest.approx(200.0)
    assert sync.seconds_until_next_post(1400.0) == 0.0


def test_enabling_both_converges_in_any_order_without_resending_or_losing_anything():
    """Download and upload together: the log ends up with the union, the cloud with the union,
    and nothing the cloud already had is ever posted."""
    now = T0 + 60 * DAY
    cloud_only = {now - 40 * DAY + i * 60: 2 for i in range(500)}  # the phone's older history
    shared = {now - 3 * DAY + i * 60: 4 for i in range(300)}  # both have it
    device_only = {now - 2 * DAY + i * 60: 6 for i in range(200)}  # the phone never sent these
    cloud = FakeCloud({**cloud_only, **shared})
    log = energy.MinuteLog()
    for t, p in {**shared, **device_only}.items():
        log.add(t, p)
    sync = sync_mod.CloudSync(log, wh)
    pending: set[int] = set()
    clock = 0.0
    for _ in range(40):  # alternate as the coordinator's follow-ups do, in either order
        run(sync.download(cloud, now, 90, 0, pending))
        clock += 301
        run(sync.upload(cloud, newest(log), clock))
    union = {**cloud_only, **shared, **device_only}
    assert dict(log.readings()) == union
    assert cloud.rows == union
    posted = [t for batch in cloud.posts for t, _, _ in batch]
    assert sorted(posted) == sorted(device_only)  # exactly the minutes the cloud lacked
    assert len(posted) == len(set(posted))  # none twice
    assert not sync.download_backlog(now) and not sync.upload_backlog(newest(log))
