"""Assertions that look at the payload rather than the status code.

A service can return 200 and a perfectly well-formed body while serving data
that stopped updating hours ago. Every assertion here takes the decoded payload
and answers one question about its *contents*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

__all__ = [
    "Verdict",
    "Assertion",
    "dig",
    "fresh",
    "non_zero",
    "min_length",
    "within",
    "truthy",
    "matches",
    "changed",
]


@dataclass(frozen=True)
class Verdict:
    """The outcome of one assertion."""

    ok: bool
    detail: str
    observed: Any = None

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.ok


#: An assertion is a callable taking (payload, previous_payload) and returning a Verdict.
#: ``previous`` is None on the first ever run, or when no prior payload was stored.
Assertion = Callable[[Any, Any], Verdict]


_MISSING = object()


def dig(payload: Any, path: str) -> Any:
    """Walk a dotted path into nested dicts and lists.

    ``dig(body, "data.bars.0.close")`` returns ``body["data"]["bars"][0]["close"]``,
    or ``_MISSING`` if any step is absent. List indices are written as integers.
    """
    current = payload
    if not path:
        return current
    for step in path.split("."):
        if isinstance(current, dict):
            if step not in current:
                return _MISSING
            current = current[step]
        elif isinstance(current, (list, tuple)):
            try:
                current = current[int(step)]
            except (ValueError, IndexError):
                return _MISSING
        else:
            return _MISSING
    return current


def _absent(path: str) -> Verdict:
    return Verdict(False, f"{path!r} is missing from the payload", None)


def _as_datetime(value: Any) -> datetime | None:
    """Accept ISO-8601 strings, epoch seconds, epoch millis, or a datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        # Anything past the year 2286 in seconds is almost certainly milliseconds.
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def parse_duration(spec: str | int | float) -> float:
    """``"90s"``, ``"5m"``, ``"2h"``, ``"1d"`` or a plain number of seconds."""
    if isinstance(spec, (int, float)):
        return float(spec)
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd])\s*", spec, re.IGNORECASE)
    if not match:
        raise ValueError(f"cannot parse duration {spec!r}; use e.g. '5m', '2h', '90s'")
    amount, unit = float(match.group(1)), match.group(2).lower()
    return amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def fresh(path: str, max_age: str | int | float, *, now: Callable[[], datetime] | None = None) -> Assertion:
    """The timestamp at ``path`` must be no older than ``max_age``.

    This is the assertion that catches the failure everything else misses: the
    endpoint is up, the shape is right, and the newest row is from last Tuesday.
    """
    limit = parse_duration(max_age)
    clock = now or (lambda: datetime.now(timezone.utc))

    def check(payload: Any, previous: Any) -> Verdict:
        raw = dig(payload, path)
        if raw is _MISSING:
            return _absent(path)
        stamp = _as_datetime(raw)
        if stamp is None:
            return Verdict(False, f"{path!r} is not a recognisable timestamp", raw)
        age = (clock() - stamp).total_seconds()
        if age > limit:
            return Verdict(
                False,
                f"{path!r} is {_human(age)} old, limit is {_human(limit)}",
                stamp.isoformat(),
            )
        return Verdict(True, f"{path!r} is {_human(age)} old", stamp.isoformat())

    return check


def non_zero(path: str) -> Assertion:
    """The value at ``path`` must not be zero, empty, or all-zero.

    A camera returning a black frame, a price feed returning 0.0 and a table of
    zeroes all pass a status check and all mean the same thing.
    """

    def check(payload: Any, previous: Any) -> Verdict:
        value = dig(payload, path)
        if value is _MISSING:
            return _absent(path)
        if isinstance(value, (list, tuple)):
            if not value:
                return Verdict(False, f"{path!r} is empty", 0)
            if all(_is_zero(v) for v in value):
                return Verdict(False, f"{path!r} is {len(value)} values, all zero", 0)
            return Verdict(True, f"{path!r} has {len(value)} values, not all zero", len(value))
        if _is_zero(value):
            return Verdict(False, f"{path!r} is zero or empty", value)
        return Verdict(True, f"{path!r} is non-zero", value)

    return check


def _is_zero(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, (str, bytes, list, tuple, dict, set)):
        return len(value) == 0
    return False


def min_length(path: str, n: int) -> Assertion:
    """The collection at ``path`` must hold at least ``n`` items."""

    def check(payload: Any, previous: Any) -> Verdict:
        value = dig(payload, path)
        if value is _MISSING:
            return _absent(path)
        try:
            size = len(value)
        except TypeError:
            return Verdict(False, f"{path!r} has no length", value)
        if size < n:
            return Verdict(False, f"{path!r} holds {size} items, expected at least {n}", size)
        return Verdict(True, f"{path!r} holds {size} items", size)

    return check


def within(path: str, low: float, high: float) -> Assertion:
    """The number at ``path`` must sit inside a plausible range.

    Useful where a broken feed reports a real but absurd value rather than a
    null: a price of 0.0001, a percentage of 4000.
    """

    def check(payload: Any, previous: Any) -> Verdict:
        value = dig(payload, path)
        if value is _MISSING:
            return _absent(path)
        try:
            number = float(value)
        except (TypeError, ValueError):
            return Verdict(False, f"{path!r} is not a number", value)
        if not low <= number <= high:
            return Verdict(False, f"{path!r} is {number:g}, outside [{low:g}, {high:g}]", number)
        return Verdict(True, f"{path!r} is {number:g}", number)

    return check


def truthy(path: str) -> Assertion:
    """The value at ``path`` must be present and truthy."""

    def check(payload: Any, previous: Any) -> Verdict:
        value = dig(payload, path)
        if value is _MISSING:
            return _absent(path)
        if not value:
            return Verdict(False, f"{path!r} is falsey ({value!r})", value)
        return Verdict(True, f"{path!r} is set", value)

    return check


def matches(path: str, pattern: str) -> Assertion:
    """The string at ``path`` must match ``pattern``."""
    compiled = re.compile(pattern)

    def check(payload: Any, previous: Any) -> Verdict:
        value = dig(payload, path)
        if value is _MISSING:
            return _absent(path)
        if not isinstance(value, str) or not compiled.search(value):
            return Verdict(False, f"{path!r} does not match {pattern!r}", value)
        return Verdict(True, f"{path!r} matches {pattern!r}", value)

    return check


def changed(path: str) -> Assertion:
    """The value at ``path`` must differ from the last run.

    The strongest liveness signal there is, and the one that catches a process
    that is alive, serving, and quietly repeating itself. Passes on the first
    run, when there is nothing to compare against.
    """

    def check(payload: Any, previous: Any) -> Verdict:
        value = dig(payload, path)
        if value is _MISSING:
            return _absent(path)
        if previous is None:
            return Verdict(True, f"{path!r} has no previous run to compare", value)
        before = dig(previous, path)
        if before is not _MISSING and before == value:
            return Verdict(False, f"{path!r} is unchanged since the last run", value)
        return Verdict(True, f"{path!r} changed since the last run", value)

    return check


def _human(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


def describe(verdicts: Sequence[Verdict]) -> str:
    """One line naming every failure, for an alert body."""
    return "; ".join(v.detail for v in verdicts if not v.ok)
