"""Where an alert goes, and what it says.

Every alert carries what broke, how long it has been broken, the last value
that was good, and what to do about it. An alert that only says "check failed"
costs the reader the same five minutes every time.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

__all__ = ["Alert", "Notifier", "Console", "Ntfy", "Webhook", "Fanout"]


@dataclass(frozen=True)
class Alert:
    check: str
    reasons: Sequence[str]
    down_for: str
    last_good: str | None
    restarts: int
    runbook: str | None
    resolved: bool = False

    def title(self) -> str:
        if self.resolved:
            return f"RECOVERED {self.check}"
        return f"{self.check} down {self.down_for}"

    def body(self) -> str:
        if self.resolved:
            line = f"{self.check} is healthy again after {self.down_for}."
            if self.restarts:
                line += f" {self.restarts} restart(s) were attempted."
            return line
        lines = list(self.reasons)
        if self.last_good:
            lines.append(f"last good: {self.last_good}")
        if self.restarts:
            lines.append(f"auto-restart tried {self.restarts}x, did not help")
        if self.runbook:
            lines.append(f"do: {self.runbook}")
        return "\n".join(lines)


class Notifier(Protocol):
    def send(self, alert: Alert) -> None: ...


@dataclass
class Console:
    """Prints to stderr. The default, and enough for a cron job with mail."""

    stream: object = sys.stderr

    def send(self, alert: Alert) -> None:
        print(f"[{alert.title()}]\n{alert.body()}\n", file=self.stream, flush=True)


@dataclass
class Ntfy:
    """Push to an ntfy topic. Free, no account, works on a phone lock screen."""

    topic_url: str
    timeout: float = 10.0
    priority_down: str = "high"
    priority_up: str = "default"

    def send(self, alert: Alert) -> None:
        request = urllib.request.Request(
            self.topic_url,
            data=alert.body().encode("utf-8"),
            headers={
                "Title": alert.title(),
                "Priority": self.priority_up if alert.resolved else self.priority_down,
                "Tags": "white_check_mark" if alert.resolved else "rotating_light",
            },
        )
        urllib.request.urlopen(request, timeout=self.timeout).close()


@dataclass
class Webhook:
    """POST a JSON body. Slack, Teams, Discord, or your own endpoint."""

    url: str
    timeout: float = 10.0
    shape: Callable[[Alert], dict] | None = None

    def send(self, alert: Alert) -> None:
        payload = self.shape(alert) if self.shape else {
            "check": alert.check,
            "title": alert.title(),
            "text": alert.body(),
            "resolved": alert.resolved,
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(request, timeout=self.timeout).close()


@dataclass
class Fanout:
    """Send to several places. One failing destination never blocks the others."""

    targets: Sequence[Notifier]

    def send(self, alert: Alert) -> None:
        for target in self.targets:
            try:
                target.send(alert)
            except Exception as exc:  # noqa: BLE001 - a dead channel must not hide the alert
                print(f"payloadwatch: notifier {type(target).__name__} failed: {exc}",
                      file=sys.stderr, flush=True)
