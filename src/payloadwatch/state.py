"""Durable state, so a restart does not reset the alert counter.

Everything the watchdog needs to behave sensibly across runs lives here: the
last payload it saw, when a check was last healthy, and how many times it has
already shouted about the current outage.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["Store", "CheckState"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS check_state (
    name           TEXT PRIMARY KEY,
    last_payload   TEXT,
    last_ok_at     REAL,
    failing_since  REAL,
    alerts_sent    INTEGER NOT NULL DEFAULT 0,
    last_alert_at  REAL,
    restarts       INTEGER NOT NULL DEFAULT 0,
    updated_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL,
    at        REAL NOT NULL,
    ok        INTEGER NOT NULL,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS history_name_at ON history (name, at DESC);
"""


@dataclass
class CheckState:
    name: str
    last_payload: Any = None
    last_ok_at: float | None = None
    failing_since: float | None = None
    alerts_sent: int = 0
    last_alert_at: float | None = None
    restarts: int = 0

    @property
    def healthy(self) -> bool:
        return self.failing_since is None

    def down_for(self, now: float | None = None) -> float:
        if self.failing_since is None:
            return 0.0
        return (now or time.time()) - self.failing_since


class Store:
    """A tiny SQLite-backed store. One file, no server, safe to delete."""

    def __init__(self, path: str | Path = "payloadwatch.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def load(self, name: str) -> CheckState:
        row = self._db.execute("SELECT * FROM check_state WHERE name = ?", (name,)).fetchone()
        if row is None:
            return CheckState(name=name)
        payload = None
        if row["last_payload"]:
            try:
                payload = json.loads(row["last_payload"])
            except json.JSONDecodeError:
                payload = None
        return CheckState(
            name=name,
            last_payload=payload,
            last_ok_at=row["last_ok_at"],
            failing_since=row["failing_since"],
            alerts_sent=row["alerts_sent"],
            last_alert_at=row["last_alert_at"],
            restarts=row["restarts"],
        )

    def save(self, state: CheckState) -> None:
        try:
            payload = json.dumps(state.last_payload, default=str)
        except (TypeError, ValueError):
            payload = None
        self._db.execute(
            """
            INSERT INTO check_state
                (name, last_payload, last_ok_at, failing_since, alerts_sent,
                 last_alert_at, restarts, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                last_payload  = excluded.last_payload,
                last_ok_at    = excluded.last_ok_at,
                failing_since = excluded.failing_since,
                alerts_sent   = excluded.alerts_sent,
                last_alert_at = excluded.last_alert_at,
                restarts      = excluded.restarts,
                updated_at    = excluded.updated_at
            """,
            (
                state.name,
                payload,
                state.last_ok_at,
                state.failing_since,
                state.alerts_sent,
                state.last_alert_at,
                state.restarts,
                time.time(),
            ),
        )

    def record(self, name: str, ok: bool, detail: str) -> None:
        self._db.execute(
            "INSERT INTO history (name, at, ok, detail) VALUES (?, ?, ?, ?)",
            (name, time.time(), 1 if ok else 0, detail),
        )

    def recent(self, name: str, limit: int = 20) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT at, ok, detail FROM history WHERE name = ? ORDER BY at DESC LIMIT ?",
            (name, limit),
        ).fetchall()

    def prune(self, keep_days: float = 30.0) -> int:
        cutoff = time.time() - keep_days * 86400
        cur = self._db.execute("DELETE FROM history WHERE at < ?", (cutoff,))
        return cur.rowcount or 0
