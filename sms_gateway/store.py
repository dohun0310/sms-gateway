"""SQLite persistence for received and outgoing messages.

Outgoing states:
    draft    created through the API, waiting for an explicit send
    pending  created by the gateway itself (emergency), never attempted yet
    sending  handed to the modem; if the process dies here the outcome is unknown
    sent     the modem reported the message as sent (not proof of delivery)
    failed   the modem rejected the message
    unknown  was ``sending`` when the process stopped; never retried automatically
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound (
    key         TEXT PRIMARY KEY,
    number      TEXT NOT NULL,
    text        TEXT NOT NULL,
    sent_at     TEXT NOT NULL,
    received_at TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('normal', 'emergency', 'rate_limited'))
);
CREATE TABLE IF NOT EXISTS outbound (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('draft', 'emergency', 'emergency_reply')),
    number      TEXT NOT NULL,
    text        TEXT NOT NULL,
    source_key  TEXT REFERENCES inbound(key),
    state       TEXT NOT NULL,
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (kind, source_key, number)
);
"""


@dataclass(frozen=True)
class Inbound:
    key: str
    number: str
    text: str
    sent_at: str
    received_at: str
    kind: str


@dataclass(frozen=True)
class Outbound:
    id: str
    kind: str
    number: str
    text: str
    source_key: str | None
    state: str
    error: str | None
    created_at: str
    updated_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def inbound_key(number: str, sent_at: str, text: str) -> str:
    """Stable identity for a received SMS; ModemManager object paths change on restart."""
    return hashlib.sha256("\x1f".join((number, sent_at, text)).encode()).hexdigest()


class Store:
    def __init__(self, path: Path | str):
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    # inbound

    def has_inbound(self, key: str) -> bool:
        return self._db.execute("SELECT 1 FROM inbound WHERE key = ?", (key,)).fetchone() is not None

    def emergencies_since(self, since: timedelta) -> int:
        cutoff = (datetime.now(UTC) - since).isoformat(timespec="seconds")
        row = self._db.execute(
            "SELECT count(*) FROM inbound WHERE kind = 'emergency' AND received_at >= ?", (cutoff,)
        ).fetchone()
        return row[0]

    def add_inbound(self, msg: Inbound, forwards: Sequence[tuple[str, str]] = ()) -> bool:
        """Record a message and the emergency forwards it triggers, atomically.

        Returns False when the message was already recorded.
        """
        now = _now()
        try:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute(
                "INSERT INTO inbound VALUES (?, ?, ?, ?, ?, ?)",
                (msg.key, msg.number, msg.text, msg.sent_at, msg.received_at, msg.kind),
            )
            for number, text in forwards:
                self._insert_outbound("emergency", number, text, msg.key, "pending", now)
            self._db.execute("COMMIT")
            return True
        except sqlite3.IntegrityError:
            self._db.execute("ROLLBACK")
            return False
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def recent_inbound(self, limit: int) -> list[Inbound]:
        rows = self._db.execute(
            "SELECT * FROM inbound ORDER BY received_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Inbound(**dict(r)) for r in rows]

    def emergencies_awaiting_reply(self, within: timedelta = timedelta(hours=1)) -> list[Inbound]:
        """Recent emergencies whose forwards have all been attempted and that have no reply yet.

        The time window keeps old requests from being answered if replies are switched on later.
        """
        cutoff = (datetime.now(UTC) - within).isoformat(timespec="seconds")
        rows = self._db.execute(
            """
            SELECT i.* FROM inbound i
            WHERE i.kind = 'emergency' AND i.received_at >= ?
              AND NOT EXISTS (SELECT 1 FROM outbound o
                              WHERE o.source_key = i.key AND o.kind = 'emergency_reply')
              AND NOT EXISTS (SELECT 1 FROM outbound o
                              WHERE o.source_key = i.key AND o.kind = 'emergency'
                                AND o.state IN ('pending', 'sending'))
            """,
            (cutoff,),
        ).fetchall()
        return [Inbound(**dict(r)) for r in rows]

    def forward_results(self, key: str) -> tuple[int, int]:
        row = self._db.execute(
            "SELECT sum(state = 'sent'), count(*) FROM outbound WHERE source_key = ? AND kind = 'emergency'",
            (key,),
        ).fetchone()
        return row[0] or 0, row[1]

    # outbound

    def _insert_outbound(self, kind, number, text, source_key, state, now) -> str:
        oid = uuid.uuid4().hex[:12]
        self._db.execute(
            "INSERT INTO outbound VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (oid, kind, number, text, source_key, state, now, now),
        )
        return oid

    def add_outbound(self, kind: str, number: str, text: str, state: str, source_key: str | None = None) -> Outbound:
        oid = self._insert_outbound(kind, number, text, source_key, state, _now())
        created = self.get_outbound(oid)
        assert created is not None
        return created

    def get_outbound(self, oid: str) -> Outbound | None:
        row = self._db.execute("SELECT * FROM outbound WHERE id = ?", (oid,)).fetchone()
        return Outbound(**dict(row)) if row else None

    def pending_outbound(self) -> list[Outbound]:
        rows = self._db.execute("SELECT * FROM outbound WHERE state = 'pending' ORDER BY created_at, rowid").fetchall()
        return [Outbound(**dict(r)) for r in rows]

    def transition(self, oid: str, from_state: str, to_state: str, error: str | None = None) -> bool:
        """Move a message between states only if it is still in ``from_state``."""
        cur = self._db.execute(
            "UPDATE outbound SET state = ?, error = ?, updated_at = ? WHERE id = ? AND state = ?",
            (to_state, error, _now(), oid, from_state),
        )
        return cur.rowcount == 1

    def mark_interrupted(self) -> int:
        """At startup: anything left in ``sending`` has an unknown outcome."""
        cur = self._db.execute(
            "UPDATE outbound SET state = 'unknown', updated_at = ? WHERE state = 'sending'", (_now(),)
        )
        return cur.rowcount
