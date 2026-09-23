from datetime import datetime, timedelta, timezone

import pytest

from payloadwatch import (
    Check,
    QuietWindows,
    Store,
    Watchdog,
    changed,
    dig,
    fresh,
    min_length,
    non_zero,
    within,
)
from payloadwatch.notify import Alert, Notifier


class Collector(Notifier):
    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.alerts.append(alert)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.db") as s:
        yield s


def now_iso(offset_seconds: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


# --------------------------------------------------------------------- dig

def test_dig_walks_dicts_and_lists():
    body = {"data": {"bars": [{"close": 1.5}, {"close": 2.5}]}}
    assert dig(body, "data.bars.1.close") == 2.5


def test_dig_returns_sentinel_for_missing_path():
    assert non_zero("nope.missing")({"a": 1}, None).ok is False


# ------------------------------------------------------------------ fresh

def test_fresh_passes_on_a_recent_timestamp():
    assert fresh("ts", "5m")({"ts": now_iso(-10)}, None).ok


def test_fresh_fails_on_a_stale_timestamp():
    verdict = fresh("ts", "5m")({"ts": now_iso(-3600)}, None)
    assert not verdict.ok
    assert "old" in verdict.detail


def test_fresh_accepts_epoch_seconds_and_millis():
    epoch = datetime.now(timezone.utc).timestamp()
    assert fresh("ts", "5m")({"ts": epoch}, None).ok
    assert fresh("ts", "5m")({"ts": epoch * 1000}, None).ok


# --------------------------------------------------------------- non_zero

def test_non_zero_rejects_an_all_zero_frame():
    verdict = non_zero("pixels")({"pixels": [0, 0, 0, 0]}, None)
    assert not verdict.ok
    assert "all zero" in verdict.detail


def test_non_zero_accepts_one_live_value():
    assert non_zero("pixels")({"pixels": [0, 0, 7]}, None).ok


# ---------------------------------------------------------------- changed

def test_changed_passes_on_the_first_run():
    assert changed("seq")({"seq": 1}, None).ok


def test_changed_fails_when_a_live_looking_feed_repeats_itself():
    verdict = changed("seq")({"seq": 1}, {"seq": 1})
    assert not verdict.ok


# ------------------------------------------------------------------ range

def test_within_catches_a_real_but_absurd_value():
    assert not within("price", 100, 10_000)({"price": 0.0001}, None).ok


# ------------------------------------------------------------- quiet hours

def test_quiet_window_wraps_across_the_weekend():
    weekend = QuietWindows.weekly(("fri 21:00", "sun 22:00"))
    saturday = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)   # Saturday
    wednesday = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)  # Wednesday
    assert weekend.is_quiet(saturday)
    assert not weekend.is_quiet(wednesday)


# --------------------------------------------------------------- watchdog

def test_restart_is_tried_before_anyone_is_alerted(store):
    restarts = []
    check = Check(
        name="feed",
        fetch=lambda: {"ts": now_iso(-3600)},
        assertions=[fresh("ts", "5m")],
        restart=lambda: restarts.append(1),
    )
    bell = Collector()
    wd = Watchdog([check], store=store, notifier=bell)

    outcome = wd.run_once()[0]
    assert outcome.restarted
    assert restarts == [1]
    assert bell.alerts == []          # nobody woken on the first failure


def test_alert_fires_once_the_restarts_are_exhausted(store):
    check = Check(
        name="feed",
        fetch=lambda: {"ts": now_iso(-3600)},
        assertions=[fresh("ts", "5m")],
        restart=lambda: None,
        max_restarts=1,
        runbook="ssh the box and restart the collector",
    )
    bell = Collector()
    wd = Watchdog([check], store=store, notifier=bell)

    wd.run_once()                      # restart attempt
    wd.run_once()                      # still broken -> alert
    assert len(bell.alerts) == 1
    assert "do: ssh the box" in bell.alerts[0].body()


def test_repeat_alerts_are_capped(store):
    check = Check(name="feed", fetch=lambda: {"ts": now_iso(-3600)},
                  assertions=[fresh("ts", "5m")])
    bell = Collector()
    wd = Watchdog([check], store=store, notifier=bell, max_alerts=2)

    for _ in range(10):
        wd.run_once()
    assert len(bell.alerts) == 2       # a known outage stops shouting


def test_nothing_is_announced_inside_a_quiet_window(store):
    check = Check(name="feed", fetch=lambda: {"ts": now_iso(-3600)},
                  assertions=[fresh("ts", "5m")])
    bell = Collector()
    wd = Watchdog([check], store=store, notifier=bell,
                  quiet=QuietWindows.weekly(("mon 00:00", "sun 23:59")))

    outcome = wd.run_once()[0]
    assert outcome.quiet
    assert bell.alerts == []


def test_recovery_is_announced(store):
    state = {"ts": now_iso(-3600)}
    check = Check(name="feed", fetch=lambda: state, assertions=[fresh("ts", "5m")])
    bell = Collector()
    wd = Watchdog([check], store=store, notifier=bell)

    wd.run_once()                      # fails, alerts
    state["ts"] = now_iso()            # feed comes back
    wd.run_once()

    assert bell.alerts[-1].resolved
    assert "healthy again" in bell.alerts[-1].body()


def test_a_fetch_that_raises_is_a_normal_failure(store):
    def explode():
        raise ConnectionError("connection refused")

    check = Check(name="feed", fetch=explode, assertions=[])
    bell = Collector()
    wd = Watchdog([check], store=store, notifier=bell)

    outcome = wd.run_once()[0]
    assert not outcome.ok
    assert "connection refused" in outcome.reasons[0]


def test_a_dead_notifier_never_crashes_the_run(store):
    class Broken(Notifier):
        def send(self, alert): raise RuntimeError("ntfy is down")

    check = Check(name="feed", fetch=lambda: {"ts": now_iso(-3600)},
                  assertions=[fresh("ts", "5m")])
    wd = Watchdog([check], store=store, notifier=Broken())
    outcome = wd.run_once()[0]         # must not raise
    assert not outcome.ok


def test_state_survives_a_restart_of_the_watchdog_itself(tmp_path):
    check = Check(name="feed", fetch=lambda: {"ts": now_iso(-3600)},
                  assertions=[fresh("ts", "5m")])
    bell = Collector()

    with Store(tmp_path / "s.db") as s:
        Watchdog([check], store=s, notifier=bell, max_alerts=1).run_once()
    with Store(tmp_path / "s.db") as s:
        Watchdog([check], store=s, notifier=bell, max_alerts=1).run_once()

    assert len(bell.alerts) == 1       # the counter is not reset by a restart
