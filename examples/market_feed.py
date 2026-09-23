"""A realistic setup: a price feed, a database table, and a disk of reports.

Run it from cron, a systemd timer or Task Scheduler. There is no daemon here on
purpose, because a watchdog with its own event loop is one more thing that can
die quietly at 3am.

    */5 * * * * /usr/bin/python3 /opt/watch/market_feed.py
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from pathlib import Path

from payloadwatch import (
    Check,
    Fanout,
    Console,
    Ntfy,
    Store,
    Watchdog,
    changed,
    fresh,
    market_weekend,
    min_length,
    non_zero,
    within,
)

FEED_URL = "https://example.internal/api/quote/XAUUSD"
REPORTS = Path("/var/reports/daily")


def get_json(url: str):
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)


def newest_rows():
    """Pretend this queries your warehouse and returns a small summary."""
    return {
        "rows_today": 14_302,
        "latest_ts": "2026-09-22T20:35:00Z",
        "distinct_symbols": 5,
    }


def report_files():
    files = sorted(REPORTS.glob("*.pdf"))
    newest = max((f.stat().st_mtime for f in files), default=0)
    return {"count": len(files), "newest_mtime": newest}


watchdog = Watchdog(
    checks=[
        # 1. The live price feed. Note what is asserted: not "did it answer",
        #    but "is the answer current, plausible, and different from last time".
        Check(
            name="price-feed",
            fetch=lambda: get_json(FEED_URL),
            assertions=[
                fresh("data.timestamp", "5m"),
                non_zero("data.bid"),
                within("data.bid", 100, 100_000),
                changed("data.timestamp"),
            ],
            restart=lambda: subprocess.run(
                ["systemctl", "restart", "price-collector"], check=False
            ),
            runbook="check the collector log, then the upstream broker session",
        ),

        # 2. The warehouse. A pipeline that runs and writes nothing still exits 0.
        Check(
            name="warehouse-load",
            fetch=newest_rows,
            assertions=[
                fresh("latest_ts", "2h"),
                within("rows_today", 1_000, 10_000_000),
                within("distinct_symbols", 5, 5),
            ],
            runbook="rerun the load for today, then check the source file drop",
        ),

        # 3. The output nobody looks at until it is missing.
        Check(
            name="daily-reports",
            fetch=report_files,
            assertions=[
                min_length("count", 1),
                fresh("newest_mtime", "26h"),
            ],
            runbook="rerun the report job; check disk space before anything else",
        ),
    ],
    store=Store("/var/lib/payloadwatch/state.db"),
    notifier=Fanout([Console(), Ntfy("https://ntfy.sh/your-private-topic")]),

    # A closed market looks exactly like a dead feed.
    quiet=market_weekend(),

    # Three notices about the same outage is plenty. After that you already know.
    max_alerts=3,
)


if __name__ == "__main__":
    for outcome in watchdog.run_once():
        flag = "ok " if outcome.ok else "FAIL"
        note = "" if outcome.ok else " | " + "; ".join(outcome.reasons)
        print(f"{flag} {outcome.check}{note}")
