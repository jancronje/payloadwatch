"""Quiet windows.

A closed market looks exactly like a dead feed. So does a machine you turned
off on purpose. Without quiet windows, the first weekend teaches you to ignore
the alerts, and after that the watchdog is decoration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Iterable, Sequence

__all__ = ["QuietWindows", "Window"]

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_SPEC = re.compile(r"\s*(mon|tue|wed|thu|fri|sat|sun)\s+(\d{1,2}):(\d{2})\s*", re.IGNORECASE)


def _parse(spec: str) -> tuple[int, time]:
    match = _SPEC.fullmatch(spec)
    if not match:
        raise ValueError(f"cannot parse {spec!r}; use e.g. 'fri 21:00'")
    day = _DAYS[match.group(1).lower()]
    return day, time(int(match.group(2)), int(match.group(3)))


def _minutes(day: int, clock: time) -> int:
    return day * 1440 + clock.hour * 60 + clock.minute


@dataclass(frozen=True)
class Window:
    """A recurring weekly window, expressed in UTC.

    ``Window("fri 21:00", "sun 22:00")`` covers the weekend market close and
    wraps across the end of the week correctly.
    """

    start: str
    end: str

    def contains(self, moment: datetime) -> bool:
        moment = moment.astimezone(timezone.utc)
        now = _minutes(moment.weekday(), moment.time())
        begin = _minutes(*_parse(self.start))
        finish = _minutes(*_parse(self.end))
        if begin <= finish:
            return begin <= now < finish
        # wraps past Sunday midnight
        return now >= begin or now < finish

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.start} -> {self.end} UTC"


@dataclass
class QuietWindows:
    """A set of windows during which failures are recorded but never announced."""

    windows: list[Window] = field(default_factory=list)

    @classmethod
    def weekly(cls, *pairs: Sequence[str]) -> "QuietWindows":
        """``QuietWindows.weekly(("fri 21:00", "sun 22:00"))``"""
        return cls([Window(start, end) for start, end in pairs])

    def add(self, start: str, end: str) -> "QuietWindows":
        self.windows.append(Window(start, end))
        return self

    def active(self, moment: datetime | None = None) -> Window | None:
        moment = moment or datetime.now(timezone.utc)
        for window in self.windows:
            if window.contains(moment):
                return window
        return None

    def is_quiet(self, moment: datetime | None = None) -> bool:
        return self.active(moment) is not None

    def __bool__(self) -> bool:
        return bool(self.windows)


def market_weekend() -> QuietWindows:
    """The usual FX and CFD close: Friday 21:00 UTC to Sunday 22:00 UTC."""
    return QuietWindows.weekly(("fri 21:00", "sun 22:00"))
