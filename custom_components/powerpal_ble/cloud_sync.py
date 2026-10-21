"""Download from and upload to the Powerpal web service, driven off the minute log.

No Home Assistant imports, so it can be tested with a fake cloud.

Download: like the phone app, import up to N days on first use, then top up the last few days
once a day. A cloud minute only fills a gap; the device's value is never overwritten.

Upload: reconcile window by window. For each window of the log, ask the cloud which minutes it
already has and POST only the ones it lacks (so nothing the phone or our own download supplied is
sent again, and real gaps get filled). A cursor records how far reconciliation has got. The app
sends at most 1,000 readings per request and one request per 5 minutes; so do we.
"""

from __future__ import annotations

from typing import Protocol

from .cloud import MAX_UPLOAD_ROWS, WINDOW_SECONDS, windows
from .energy import MinuteLog, WhFn

DOWNLOAD_WINDOWS_PER_RUN = 4  # at most 4 requests per run
TOPUP_SECONDS = 24 * 3600
TOPUP_DAYS = 3
UPLOAD_MIN_INTERVAL = 300  # seconds between uploads, as the app does
UPLOAD_SLACK = 3600  # reconciled to within an hour of the newest minute counts as caught up


class Cloud(Protocol):
    async def fetch_readings(self, start: int, end: int) -> list[tuple[int, int]]: ...

    async def post_readings(self, rows: list[tuple[int, int, float]]) -> int: ...


class CloudSync:
    def __init__(
        self,
        log: MinuteLog,
        wh: WhFn,
        download_cursor: int = 0,
        download_checked: int = 0,
        upload_cursor: int = 0,
    ) -> None:
        self.log = log
        self.wh = wh
        self.download_cursor = download_cursor  # cloud history imported up to here; 0 = not started
        self.download_checked = download_checked  # last daily top-up
        self.upload_cursor = upload_cursor  # the log is reconciled with the cloud up to here
        self._last_post = float("-inf")

    def to_dict(self) -> dict:
        return {
            "cloud_cursor": self.download_cursor,
            "cloud_checked": self.download_checked,
            "upload_cursor": self.upload_cursor,
        }

    def seconds_until_next_post(self, now_monotonic: float) -> float:
        """How long the one-upload-per-5-minutes rule still holds us back (0 if not at all)."""
        return max(0.0, UPLOAD_MIN_INTERVAL - (now_monotonic - self._last_post))

    def download_backlog(self, now: int) -> bool:
        """True while the first import is still walking through its windows."""
        return self.download_checked == 0 and 0 < self.download_cursor < now - 60

    def upload_backlog(self, newest: int) -> bool:
        return bool(newest) and len(self.log) > 0 and self.upload_cursor < newest - UPLOAD_SLACK

    async def download(
        self, cloud: Cloud, now: int, backfill_days: int, not_before: int, pending: set[int]
    ) -> int:
        """Merge cloud history into the log; changed hours are added to `pending` as we go (so
        progress survives a later failure). Returns how many minutes were new."""
        before = len(self.log)
        if not self.download_checked:
            # The first import has not finished: walk forward a few windows at a time.
            if not self.download_cursor:
                self.download_cursor = now - backfill_days * 86400
            for start, end in list(windows(self.download_cursor, now))[:DOWNLOAD_WINDOWS_PER_RUN]:
                rows = await cloud.fetch_readings(start, end)
                pending |= self.log.merge_missing(rows, not_before)
                self.download_cursor = end
            if self.download_cursor >= now:
                self.download_checked = now  # done; from here on only the daily top-up
        elif now - self.download_checked >= TOPUP_SECONDS:
            rows = await cloud.fetch_readings(now - TOPUP_DAYS * 86400, now)
            pending |= self.log.merge_missing(rows, not_before)
            self.download_checked = now
        return len(self.log) - before

    async def upload(self, cloud: Cloud, newest: int, now_monotonic: float) -> int:
        """Reconcile one window and upload what the cloud lacks. Returns how many were posted."""
        if not newest or not len(self.log):
            return 0
        start = self.upload_cursor or next(self.log.readings())[0]
        if start >= newest:
            return 0
        end = min(start + WINDOW_SECONDS, newest)
        on_cloud = {timestamp for timestamp, _ in await cloud.fetch_readings(start, end)}
        missing = [
            (timestamp, pulses)
            for timestamp, pulses in self.log.readings(since=start)
            if timestamp <= end and timestamp not in on_cloud
        ]
        batch = missing[:MAX_UPLOAD_ROWS]
        posted = 0
        if batch:
            if now_monotonic - self._last_post < UPLOAD_MIN_INTERVAL:
                return 0
            await cloud.post_readings([(t, p, self.wh(t, p)) for t, p in batch])
            self._last_post = now_monotonic
            posted = len(batch)
            if len(missing) > MAX_UPLOAD_ROWS:
                return posted  # more in this window: leave the cursor, continue next time
        self.upload_cursor = end
        return posted
