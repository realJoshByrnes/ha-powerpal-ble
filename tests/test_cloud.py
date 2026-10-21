import asyncio
import gzip
import importlib.util
import json
import pathlib
import uuid

import pytest

_base = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "powerpal_ble"


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, _base / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cloud = _load("powerpal_cloud", "cloud.py")
energy = _load("powerpal_energy2", "energy.py")


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        return self._payload


class FakeSession:
    """Answers 403 unless the Authorization header is `good`; records requests."""

    def __init__(self, good, rows=None):
        self.good = good
        self.rows = rows or []
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        if headers["Authorization"] != self.good:
            return FakeResponse(403)
        return FakeResponse(200, self.rows)

    posts = None

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts = (self.posts or []) + [(url, data, headers)]
        if headers["Authorization"] != self.good:
            return FakeResponse(403)
        if headers.get("Content-Encoding") == "gzip":
            try:
                gzip.decompress(data)
            except OSError:
                return FakeResponse(400)
        return FakeResponse(self.post_status, {"readings": [{"timestamp": 1}] * self.post_count})

    post_status = 200
    post_count = 1


def run(coro):
    return asyncio.run(coro)


def test_token_candidates_cover_the_byte_orders_without_duplicates():
    raw = bytes(range(16))
    candidates = cloud.token_candidates(raw)
    assert str(uuid.UUID(bytes=raw)) in candidates
    assert str(uuid.UUID(bytes=raw[::-1])) in candidates
    assert str(uuid.UUID(bytes_le=raw)) in candidates
    assert len(candidates) == len(set(candidates)) == 3
    assert len(cloud.token_candidates(bytes(16))) == 1  # all-zero: every order is the same


def test_windows_cover_the_range_without_gaps():
    assert list(cloud.windows(0, 10, size=4)) == [(0, 4), (4, 8), (8, 10)]
    assert list(cloud.windows(5, 5)) == []
    spans = list(cloud.windows(1000, 1000 + 90 * 86400))
    assert spans[0][0] == 1000 and spans[-1][1] == 1000 + 90 * 86400
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))
    assert len(spans) == 13  # 90 days in 7-day windows


def test_fetch_uses_lowercase_serial_and_keeps_the_token_out_of_the_url():
    session = FakeSession("tok", rows=[{"timestamp": 60, "pulses": 5, "watt_hours": 5}])
    rows = run(cloud.PowerpalCloud(session, "000A1B2C", "tok").fetch_readings(0, 120))
    assert rows == [(60, 5)]
    url, params, headers = session.calls[0]
    assert url.endswith("/000a1b2c")
    assert "tok" not in url and "tok" not in str(params)
    assert headers["Authorization"] == "tok"


def test_a_rejected_credential_is_an_auth_error():
    session = FakeSession("tok")
    with pytest.raises(cloud.CloudAuthError):
        run(cloud.PowerpalCloud(session, "000a1b2c", "wrong").fetch_readings(0, 60))


def test_malformed_rows_are_a_cloud_error_not_a_crash():
    session = FakeSession("tok", rows=[{"timestamp": 1}])
    with pytest.raises(cloud.CloudError):
        run(cloud.PowerpalCloud(session, "000a1b2c", "tok").fetch_readings(0, 60))


def test_find_token_returns_the_accepted_candidate():
    raw = bytes(range(16))
    good = cloud.token_candidates(raw)[1]
    session = FakeSession(good)
    assert run(cloud.find_token(session, "000a1b2c", raw, now=1000)) == good
    assert run(cloud.find_token(FakeSession("nope"), "000a1b2c", raw, now=1000)) is None


def test_merge_missing_fills_gaps_but_never_overwrites_the_device():
    log = energy.MinuteLog()
    log.add(60, 7)  # from the device
    touched = log.merge_missing([(0, 1), (60, 99), (120, 3)])
    assert dict(log.readings()) == {0: 1, 60: 7, 120: 3}  # the device's 7 is kept
    assert touched == {0}  # all three minutes are in hour 0, reported once
    assert log.merge_missing([(0, 1), (60, 99)]) == set()  # nothing new the second time


def test_merge_missing_ignores_rows_older_than_the_cutoff():
    log = energy.MinuteLog()
    touched = log.merge_missing([(100, 1), (7200, 2)], not_before=3600)
    assert dict(log.readings()) == {7200: 2}
    assert touched == {7200}


def test_a_response_that_fills_the_service_row_limit_is_an_error_not_silent_data_loss():
    full = [{"timestamp": i * 60, "pulses": 1} for i in range(cloud.MAX_ROWS)]
    with pytest.raises(cloud.CloudError, match="row limit"):
        run(cloud.PowerpalCloud(FakeSession("tok", rows=full), "000a1b2c", "tok").fetch_readings(0, 10))
    just_under = full[:-1]
    rows = run(cloud.PowerpalCloud(FakeSession("tok", rows=just_under), "000a1b2c", "tok").fetch_readings(0, 10))
    assert len(rows) == cloud.MAX_ROWS - 1


def test_default_window_is_far_below_the_row_limit():
    assert cloud.WINDOW_SECONDS // 60 * 1 < cloud.MAX_ROWS / 4  # at most one row per minute


def test_post_readings_sends_a_gzip_body_with_only_the_three_fields_the_app_sends():
    session = FakeSession("tok")
    session.post_count = 2
    api = cloud.PowerpalCloud(session, "000A1B2C", "tok")
    echoed = run(api.post_readings([(60, 5, 5.0), (120, 7, 7.0)]))
    assert echoed == 2
    url, body, headers = session.posts[0]
    assert url.endswith("/000a1b2c") and "tok" not in url
    assert json.loads(gzip.decompress(body)) == [
        {"timestamp": 60, "pulses": 5, "watt_hours": 5.0},
        {"timestamp": 120, "pulses": 7, "watt_hours": 7.0},
    ]
    assert headers["Content-Encoding"] == "gzip"
    assert headers["Content-Type"] == "application/json"
    assert headers["Authorization"] == "tok"
    assert b"cost" not in gzip.decompress(body) and b"is_peak" not in gzip.decompress(body)


def test_post_readings_enforces_the_per_request_limit_and_skips_empty_uploads():
    api = cloud.PowerpalCloud(FakeSession("tok"), "000a1b2c", "tok")
    assert run(api.post_readings([])) == 0
    with pytest.raises(ValueError):
        run(api.post_readings([(i * 60, 1, 1.0) for i in range(cloud.MAX_UPLOAD_ROWS + 1)]))
    ok = [(i * 60, 1, 1.0) for i in range(cloud.MAX_UPLOAD_ROWS)]
    assert run(api.post_readings(ok)) == 1  # the fake echoes one row


def test_post_readings_maps_errors():
    with pytest.raises(cloud.CloudAuthError):
        run(cloud.PowerpalCloud(FakeSession("tok"), "000a1b2c", "bad").post_readings([(60, 1, 1.0)]))
    session = FakeSession("tok")
    session.post_status = 500
    with pytest.raises(cloud.CloudError, match="HTTP 500"):
        run(cloud.PowerpalCloud(session, "000a1b2c", "tok").post_readings([(60, 1, 1.0)]))
