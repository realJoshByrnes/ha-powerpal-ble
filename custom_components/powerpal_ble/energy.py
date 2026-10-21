"""Minute-level reading store and the hourly energy derived from it.

No Home Assistant imports, so it is unit-testable.

The device records one reading per minute (timestamp, pulses). This log is the integration's
primary storage; everything else is derived from it: hourly energy and the cumulative `sum`
that Home Assistant's long-term statistics want, and later the queue for the cloud upload.

Adding the same minute twice replaces it, so re-downloading history can never double-count.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator

MINUTE = 60
HOUR = 3600

# (timestamp of the minute, pulses in it) -> Wh. The timestamp matters: what a pulse is worth
# depends on which meter was attached then (see factor_timeline.py).
WhFn = Callable[[int, int], float]


class MinuteLog:
    def __init__(self, base_wh: float = 0.0) -> None:
        self.base_wh = base_wh  # Wh of data already pruned away
        self._pulses: dict[int, int] = {}  # minute index (unix s // 60) -> pulses

    @staticmethod
    def hour_of(timestamp: int) -> int:
        return timestamp - timestamp % HOUR

    def __len__(self) -> int:
        return len(self._pulses)

    def add(self, timestamp: int, pulses: int) -> int:
        """Store one minute reading (idempotent). Returns the hour it belongs to."""
        self._pulses[timestamp // MINUTE] = pulses
        return self.hour_of(timestamp)

    def merge_missing(self, rows: Iterable[tuple[int, int]], not_before: int = 0) -> set[int]:
        """Add (timestamp, pulses) rows for minutes we do not have; never overwrite a minute.

        Used for the cloud, whose readings are identical to the device's where both exist.
        Rows older than `not_before` are ignored (that energy is already folded into base_wh).
        Returns the hours that gained data.
        """
        touched: set[int] = set()
        for timestamp, pulses in rows:
            minute = timestamp // MINUTE
            if timestamp >= not_before and minute not in self._pulses:
                self._pulses[minute] = pulses
                touched.add(self.hour_of(timestamp))
        return touched

    def readings(self, since: int = 0) -> Iterator[tuple[int, int]]:
        """(timestamp, pulses) in time order, from `since` (unix s) on."""
        for minute in sorted(self._pulses):
            if minute * MINUTE >= since:
                yield minute * MINUTE, self._pulses[minute]

    def hourly_wh(self, wh: WhFn) -> dict[int, float]:
        hours: dict[int, float] = {}
        for minute, pulses in self._pulses.items():
            hour = self.hour_of(minute * MINUTE)
            hours[hour] = hours.get(hour, 0.0) + wh(minute * MINUTE, pulses)
        return hours

    def hourly_power(
        self, wh: WhFn, only: set[int] | None = None
    ) -> dict[int, tuple[float, float, float]]:
        """hour start -> (mean, min, max) watts over the minutes we have in that hour.

        A one-minute reading of E Wh is a rate of E * 60 W. An hour with no minutes is absent,
        not zero. `only` limits the work to some hours.
        """
        watts: dict[int, list[float]] = {}
        for minute, pulses in self._pulses.items():
            hour = self.hour_of(minute * MINUTE)
            if only is None or hour in only:
                watts.setdefault(hour, []).append(wh(minute * MINUTE, pulses) * 60)
        return {h: (sum(w) / len(w), min(w), max(w)) for h, w in watts.items()}

    def wh_since(self, since: int, wh: WhFn) -> float:
        """Energy in Wh of the minutes at or after `since` (a unix timestamp)."""
        return sum(wh(m * MINUTE, p) for m, p in self._pulses.items() if m * MINUTE >= since)

    def total_wh(self, wh: WhFn) -> float:
        return self.base_wh + sum(self.hourly_wh(wh).values())

    def rows(self, since_hour: int, wh: WhFn) -> list[tuple[int, float]]:
        """(hour start, cumulative Wh at the end of that hour) for hours >= since_hour."""
        hours = self.hourly_wh(wh)
        running = self.base_wh
        out: list[tuple[int, float]] = []
        for hour in sorted(hours):
            running += hours[hour]
            if hour >= since_hour:
                out.append((hour, running))
        return out

    def prune(self, newest_timestamp: int, retention_seconds: int, wh: WhFn) -> None:
        """Drop minutes older than the retention window, folding their energy into base_wh."""
        cutoff = self.hour_of(newest_timestamp - retention_seconds)
        hours = self.hourly_wh(wh)
        old = [hour for hour in hours if hour < cutoff]
        if not old:
            return
        self.base_wh += sum(hours[hour] for hour in old)
        self._pulses = {m: p for m, p in self._pulses.items() if m * MINUTE >= cutoff}

    def to_dict(self) -> dict:
        """Runs of consecutive minutes: [[first_minute, [pulses, ...]], ...] (compact)."""
        runs: list[list] = []
        previous: int | None = None
        for minute in sorted(self._pulses):
            if previous is not None and minute == previous + 1:
                runs[-1][1].append(self._pulses[minute])
            else:
                runs.append([minute, [self._pulses[minute]]])
            previous = minute
        return {"base_wh": self.base_wh, "runs": runs}

    @classmethod
    def from_dict(cls, data: dict) -> MinuteLog:
        log = cls(data.get("base_wh", 0.0))
        for first_minute, values in data.get("runs", []):
            for offset, pulses in enumerate(values):
                log._pulses[first_minute + offset] = pulses
        return log
