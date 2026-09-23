# payloadwatch

[![tests](https://github.com/jancronje/payloadwatch/actions/workflows/tests.yml/badge.svg)](https://github.com/jancronje/payloadwatch/actions/workflows/tests.yml) [![python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org) [![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE) [![dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)](pyproject.toml)

**Check the payload, not the status flag.**

A service can return `200`, with a perfectly well-formed body, while serving data
that stopped updating three hours ago. Every uptime monitor you own will call
that healthy. `payloadwatch` asserts on the *contents* instead: is this timestamp
current, is this number plausible, did anything actually change since last time.

No agent, no daemon, no server, no dependencies. One SQLite file and a function
you call from cron.

```python
from payloadwatch import Check, Watchdog, fresh, non_zero, changed

Watchdog([
    Check(
        name="price-feed",
        fetch=lambda: requests.get(FEED).json(),
        assertions=[
            fresh("data.timestamp", "5m"),   # is it current
            non_zero("data.bid"),            # is it real
            changed("data.timestamp"),       # is it moving
        ],
        restart=lambda: subprocess.run(["systemctl", "restart", "collector"]),
        runbook="check the collector log, then the upstream broker session",
    ),
]).run_once()
```

---

## Why this exists

I run market-data pipelines. Every rule in this library was written the morning
after something taught it to me.

**A feed that returned nothing but zeros.** The service was up, the response was
well-formed, the status page was green. Every value in it was `0.0`. Nothing alerted,
because nothing was looking at the values themselves. `non_zero` exists because of that.

**A dataset that quietly stopped being fed.** The page rendered perfectly. The
numbers were internally consistent and the arithmetic was correct. The most recent
row in it was months old, on a page that otherwise looked entirely healthy. No
uptime monitor flags that. `fresh` and `changed` exist because of that.

**A weekend of alerts.** A closed market looks exactly like a dead feed. After one
Saturday of being woken by a market that was simply shut, you learn to ignore the
alerts, and from then on the watchdog is decoration. `QuietWindows` exists because
of that.

**A machine that died with its own monitor on it.** An alarm that lives on the box
it watches tells you nothing when the box goes down. That one is a deployment note
rather than code, and it is at the bottom of this file.

## Install

```bash
pip install payloadwatch
```

Python 3.10+. No runtime dependencies, deliberately: this is the thing that has to
still work when everything else is broken.

## The assertions

Each one takes a dotted path into the decoded payload. `"data.bars.0.close"` walks
dicts and list indices alike.

| Assertion | Catches |
|---|---|
| `fresh(path, "5m")` | The endpoint is up and the newest row is from last Tuesday. Accepts ISO-8601, epoch seconds, epoch millis, `datetime`. |
| `non_zero(path)` | A black frame, a price of `0.0`, an empty list, a table of zeroes. |
| `changed(path)` | A process that is alive, serving, and repeating itself. The strongest liveness signal there is. |
| `within(path, lo, hi)` | A value that is real but absurd. A gold price of `0.0001`. A percentage of `4000`. |
| `min_length(path, n)` | A batch that ran, succeeded, and wrote three rows instead of fourteen thousand. |
| `truthy(path)` / `matches(path, regex)` | Flags and status strings, when you really do want to read one. |

Write your own in one line: an assertion is any callable taking
`(payload, previous_payload)` and returning a `Verdict`.

## The escalation order

This is most of the value, and it is four rules learned the hard way.

**1. Try the cheap fix before waking anyone.** Most outages are ended by a restart.
If a `Check` has a `restart`, it is attempted first and nobody is notified. The
next run decides whether it worked.

**2. Stay quiet when quiet is correct.** Inside a `QuietWindows` range, failures
are still recorded and still visible in `status()`, they are simply not announced.
`market_weekend()` gives you Friday 21:00 to Sunday 22:00 UTC.

**3. Cap the shouting.** After `max_alerts` notices about one outage, it stops.
You already know. The counter lives in SQLite, so restarting the watchdog does not
reset it and give you another three.

**4. Say what to do, not just what broke.** Every alert carries how long it has
been down, the last time it was healthy, how many restarts were tried, and the
`runbook` line. An alert that only says `check failed` costs the reader the same
five minutes every single time.

Recovery is announced too. An alert you never see closed is an alert you stop
trusting.

```
[price-feed down 47m]
'data.timestamp' is 47m old, limit is 5m
last good: 2026-09-22T19:48:00+00:00
auto-restart tried 3x, did not help
do: check the collector log, then the upstream broker session
```

## Notifiers

`Console` (default, stderr), `Ntfy` (free, no account, lands on a phone lock
screen), `Webhook` (Slack, Teams, Discord, your own), and `Fanout` to send to
several at once. A notifier that throws is logged and swallowed: a dead alerting
channel must never take down the thing that watches everything else.

Implement `send(alert) -> None` for your own.

## Scheduling

There isn't a scheduler in here, on purpose. Call `run_once()` from cron, a
systemd timer, or Windows Task Scheduler, and let the operating system own the
schedule. A watchdog with its own event loop is one more long-running process that
can die silently, which is the exact problem you installed it to solve.

```cron
*/5 * * * * /usr/bin/python3 /opt/watch/feeds.py
```

## Run it off the box

The one rule no library can enforce for you.

An alarm that lives on the same machine as the thing it watches tells you nothing
when that machine dies, and a dead host is the failure that costs the most. Run
`payloadwatch` somewhere else: a cheap VPS, a different region, anything with a
separate power supply and a separate network. Point it at your production
endpoints from outside.

If you can only run it on the box, at minimum add one external check elsewhere
whose only job is to assert that this one is still reporting.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

The suite covers what the library actually promises: restart before alert, alert
capping, quiet windows, recovery announcements, a fetch that raises, a notifier
that raises, and state surviving a restart of the watchdog itself.

## Licence

MIT. Use it, ship it, sell what you build with it.
