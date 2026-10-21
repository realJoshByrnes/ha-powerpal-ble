"""Flashes per kWh as a timeline, not a single number.

No Home Assistant imports, so it is unit-testable.

What a pulse is worth in kWh depends on which meter was attached when it was counted. A new meter
or a move changes the factor from that moment on, and the earlier pulses must keep the factor they
were counted under. So the factor is a piecewise-constant function of time: a list of
(start, factor) entries, each in force until the next one starts.

    start (UTC unix s)   factor
    2026-01-01 00:00     1000
    2026-02-02 09:30     3000
    2026-03-03 14:15     2000

A minute's energy uses the entry with the latest start at or before the minute's timestamp.
Pulses are never edited; only this timeline is, and energy is always derived from both.

Editing: `with_value(value, start, until=None)` sets the factor from `start` until `until`. By
default that is *until the next existing change*, so correcting one period does not erase the
real meter changes around it. Pass FOREVER to deliberately replace everything from `start` on.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Iterable
from dataclasses import dataclass

MINUTE = 60
FOREVER = 2**62  # "until the end of time": replaces every later change


@dataclass(frozen=True)
class Change:
    """A span of time whose factor differs between two timelines."""

    start: int
    end: int  # FOREVER for "until now and beyond"
    old: int
    new: int


def _normalise(segments: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sorted, one entry per start, first entry at 0, equal neighbours merged."""
    by_start: dict[int, int] = {}
    for start, factor in segments:
        by_start[max(0, int(start))] = int(factor)
    ordered = sorted(by_start.items())
    if not ordered:
        raise ValueError("a timeline needs at least one entry")
    ordered[0] = (0, ordered[0][1])  # the first entry covers everything before it
    merged: list[tuple[int, int]] = []
    for start, factor in ordered:
        if factor < 1:
            raise ValueError("flashes per kWh must be at least 1")
        if not merged or merged[-1][1] != factor:
            merged.append((start, factor))
    return merged


class FactorTimeline:
    def __init__(self, segments: Iterable[tuple[int, int]] | None = None, initial: int = 1000) -> None:
        self._segments = _normalise(segments if segments is not None else [(0, initial)])
        self._starts = [start for start, _ in self._segments]

    # ------------------------------------------------------------------ reading

    @property
    def segments(self) -> tuple[tuple[int, int], ...]:
        return tuple(self._segments)

    def factor_at(self, timestamp: int) -> int:
        """The factor in force at `timestamp` (a boundary instant belongs to the new entry)."""
        return self._segments[bisect.bisect_right(self._starts, timestamp) - 1][1]

    @property
    def current(self) -> int:
        return self._segments[-1][1]

    def _next_boundary_after(self, timestamp: int) -> int:
        index = bisect.bisect_right(self._starts, timestamp)
        return self._starts[index] if index < len(self._starts) else FOREVER

    # ------------------------------------------------------------------ editing

    def with_value(
        self, value: int, start: int, until: int | None = None, origin: int = 0
    ) -> FactorTimeline:
        """A new timeline with `value` from `start` until `until`.

        `until` None means until the next existing change (or forever, if `start` is in the
        latest period). Times are rounded down to the minute so a minute is never split.

        `origin` is the timestamp of the oldest reading. The first period runs from the
        beginning of time, but nothing exists before `origin`, so a `start` at or before it means
        "the first period, whole" and does not split it.
        """
        if value < 1:
            raise ValueError("flashes per kWh must be at least 1")
        start = 0 if start <= origin else start - start % MINUTE
        if until is None:
            end = self._next_boundary_after(start)
        elif until >= FOREVER:
            end = FOREVER
        else:
            end = until - until % MINUTE
        if end <= start:
            raise ValueError("'until' must be after 'from'")
        tail = self.factor_at(end) if end < FOREVER else None
        kept = [(s, f) for s, f in self._segments if s < start or s >= end]
        kept.append((start, value))
        if tail is not None and not any(s == end for s, _ in kept):
            kept.append((end, tail))  # what was in force at `end` carries on from there
        return FactorTimeline(kept)

    # ------------------------------------------------------------------ comparing

    def changes_to(self, other: FactorTimeline) -> list[Change]:
        """The spans of time where `other` assigns a different factor than this timeline."""
        points = sorted(set(self._starts) | set(other._starts))
        out: list[Change] = []
        for i, point in enumerate(points):
            end = points[i + 1] if i + 1 < len(points) else FOREVER
            old, new = self.factor_at(point), other.factor_at(point)
            if old != new:
                out.append(Change(point, end, old, new))
        return out

    def changed_from(self, other: FactorTimeline) -> int | None:
        """The earliest instant whose factor differs, or None if the timelines are equivalent."""
        changes = self.changes_to(other)
        return changes[0].start if changes else None

    def removed_boundaries(self, other: FactorTimeline) -> list[tuple[int, int]]:
        """Entries here that `other` no longer has (real meter changes it would erase)."""
        kept = set(other._segments)
        return [seg for seg in self._segments[1:] if seg not in kept and seg[0] not in other._starts]

    # ------------------------------------------------------------------ display / storage

    def describe(self, fmt: Callable[[int], str], origin: int = 0) -> list[str]:
        """One line per period, e.g. '1 Jan 2026 00:00 to 2 Feb 2026 09:30: 1000'.

        `origin` (the oldest reading) labels the first period's start; without it, "the beginning".
        """
        lines = []
        for i, (start, factor) in enumerate(self._segments):
            end = self._segments[i + 1][0] if i + 1 < len(self._segments) else None
            first = fmt(start) if start else (fmt(origin) if origin else "the beginning")
            span = f"{first} to {fmt(end)}" if end is not None else f"{first} onwards"
            lines.append(f"{span}: {factor}")
        return lines

    def to_dict(self) -> list[list[int]]:
        return [[start, factor] for start, factor in self._segments]

    @classmethod
    def from_dict(cls, data: Iterable[Iterable[int]]) -> FactorTimeline:
        return cls([(int(start), int(factor)) for start, factor in data])
