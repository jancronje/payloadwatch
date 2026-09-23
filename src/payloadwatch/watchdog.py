"""The loop: fetch, assert, try the cheap fix, escalate, then shut up.

The order matters. Most outages are fixed by a restart, so try that before
waking anyone. Most of the rest are already known about by the second alert,
so stop after a few. And announce the recovery, because an alert you never see
closed is an alert you stop trusting.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from .checks import Assertion, Verdict, _human
from .notify import Alert, Console, Notifier
from .quiet import QuietWindows
from .state import CheckState, Store

__all__ = ["Check", "Outcome", "Watchdog"]


@dataclass
class Check:
    """One thing to watch.

    ``fetch`` returns the decoded payload and may raise: an exception is itself
    a failure, reported with its message. ``restart`` is the cheap fix tried
    before anyone is woken; return value ignored, exceptions are caught.
    """

    name: str
    fetch: Callable[[], Any]
    assertions: Sequence[Assertion]
    restart: Callable[[], Any] | None = None
    runbook: str | None = None
    max_restarts: int = 3
    remember_payload: bool = True


@dataclass
class Outcome:
    check: str
    ok: bool
    verdicts: list[Verdict] = field(default_factory=list)
    error: str | None = None
    restarted: bool = False
    alerted: bool = False
    quiet: bool = False

    @property
    def reasons(self) -> list[str]:
        if self.error:
            return [self.error]
        return [v.detail for v in self.verdicts if not v.ok]


class Watchdog:
    """Runs checks and decides who hears about it.

    Deliberately has no scheduler of its own. Call :meth:`run_once` from cron,
    a systemd timer or Task Scheduler, and let the operating system own the
    schedule. A watchdog with its own event loop is one more thing that can die
    silently.
    """

    def __init__(
        self,
        checks: Sequence[Check],
        *,
        store: Store | None = None,
        notifier: Notifier | None = None,
        quiet: QuietWindows | None = None,
        max_alerts: int = 3,
        announce_recovery: bool = True,
    ) -> None:
        self.checks = list(checks)
        self.store = store or Store()
        self.notifier = notifier or Console()
        self.quiet = quiet or QuietWindows()
        self.max_alerts = max_alerts
        self.announce_recovery = announce_recovery

    # ------------------------------------------------------------------ run

    def run_once(self, now: float | None = None) -> list[Outcome]:
        moment = now or time.time()
        return [self._run_check(check, moment) for check in self.checks]

    def _run_check(self, check: Check, moment: float) -> Outcome:
        state = self.store.load(check.name)
        outcome = self._evaluate(check, state)

        self.store.record(check.name, outcome.ok, "; ".join(outcome.reasons) or "ok")

        if outcome.ok:
            self._on_pass(check, state, outcome, moment)
        else:
            self._on_fail(check, state, outcome, moment)

        self.store.save(state)
        return outcome

    def _evaluate(self, check: Check, state: CheckState) -> Outcome:
        try:
            payload = check.fetch()
        except Exception as exc:  # noqa: BLE001 - a failed fetch is a normal outcome here
            return Outcome(check.name, ok=False, error=f"fetch failed: {exc}")

        verdicts = [assertion(payload, state.last_payload) for assertion in check.assertions]
        ok = all(v.ok for v in verdicts)
        if check.remember_payload:
            state.last_payload = payload
        return Outcome(check.name, ok=ok, verdicts=verdicts)

    # -------------------------------------------------------------- outcomes

    def _on_pass(self, check: Check, state: CheckState, outcome: Outcome, moment: float) -> None:
        was_down = not state.healthy
        down_for = state.down_for(moment)
        restarts = state.restarts

        state.last_ok_at = moment
        state.failing_since = None
        state.alerts_sent = 0
        state.restarts = 0

        if was_down and self.announce_recovery and restarts + 1:
            self._notify(
                Alert(
                    check=check.name,
                    reasons=[],
                    down_for=_human(down_for),
                    last_good=None,
                    restarts=restarts,
                    runbook=None,
                    resolved=True,
                ),
                outcome,
            )

    def _on_fail(self, check: Check, state: CheckState, outcome: Outcome, moment: float) -> None:
        if state.failing_since is None:
            state.failing_since = moment

        # 1. The cheap fix first. Most outages end here and nobody is woken.
        if check.restart is not None and state.restarts < check.max_restarts:
            state.restarts += 1
            outcome.restarted = True
            try:
                check.restart()
            except Exception as exc:  # noqa: BLE001
                outcome.verdicts.append(Verdict(False, f"restart failed: {exc}"))
            return  # next run decides whether it worked

        # 2. Quiet window: record it, do not announce it.
        if self.quiet.is_quiet(datetime.fromtimestamp(moment, tz=timezone.utc)):
            outcome.quiet = True
            return

        # 3. Cap the shouting. A known outage stops after max_alerts.
        if state.alerts_sent >= self.max_alerts:
            return

        state.alerts_sent += 1
        state.last_alert_at = moment
        outcome.alerted = True
        last_good = (
            datetime.fromtimestamp(state.last_ok_at, tz=timezone.utc).isoformat(timespec="seconds")
            if state.last_ok_at
            else "never seen healthy"
        )
        self._notify(
            Alert(
                check=check.name,
                reasons=outcome.reasons,
                down_for=_human(state.down_for(moment)),
                last_good=last_good,
                restarts=state.restarts,
                runbook=check.runbook,
            ),
            outcome,
        )

    def _notify(self, alert: Alert, outcome: Outcome) -> None:
        try:
            self.notifier.send(alert)
        except Exception as exc:  # noqa: BLE001 - never let a dead channel crash the run
            outcome.verdicts.append(Verdict(False, f"notifier failed: {exc}"))

    # ----------------------------------------------------------------- utils

    def status(self) -> dict[str, CheckState]:
        return {check.name: self.store.load(check.name) for check in self.checks}
