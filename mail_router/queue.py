from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable

from .models import QueueItem

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    state                TEXT    NOT NULL DEFAULT 'pending'
                              CHECK(state IN ('pending','in_flight','delivered','dead_letter')),
    received_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sender_email         TEXT    NOT NULL,
    recipient_email      TEXT    NOT NULL,
    ds_id                TEXT    NOT NULL,
    mailbox              TEXT,
    idempotency_key      TEXT    NOT NULL,
    raw_eml              BLOB    NOT NULL,
    attempt_count        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at      TIMESTAMP,
    last_error           TEXT,
    delivered_at         TIMESTAMP,
    delivered_message_id TEXT,
    dead_letter_reason   TEXT
);

CREATE INDEX IF NOT EXISTS idx_state_next_attempt ON queue(state, next_attempt_at);
CREATE UNIQUE INDEX IF NOT EXISTS unq_idempotency ON queue(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_ds_id ON queue(ds_id);
"""


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _parse_ts(value: str | None) -> datetime | None:
    if value is None:
        return None
    # sqlite stores "YYYY-MM-DD HH:MM:SS" (no tz). Interpret as UTC.
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _row_to_item(row: sqlite3.Row) -> QueueItem:
    return QueueItem(
        id=row["id"],
        state=row["state"],
        received_at=_parse_ts(row["received_at"]) or _utcnow(),
        sender_email=row["sender_email"],
        recipient_email=row["recipient_email"],
        ds_id=row["ds_id"],
        mailbox=row["mailbox"],
        idempotency_key=row["idempotency_key"],
        raw_eml=row["raw_eml"],
        attempt_count=row["attempt_count"],
        next_attempt_at=_parse_ts(row["next_attempt_at"]),
        last_error=row["last_error"],
        delivered_at=_parse_ts(row["delivered_at"]),
        delivered_message_id=row["delivered_message_id"],
        dead_letter_reason=row["dead_letter_reason"],
    )


class Queue:
    """SQLite-backed persistent queue.

    Thread-safe within a process via an internal lock; safe across processes
    via SQLite WAL + `BEGIN IMMEDIATE` on mutating transactions. Connection is
    shared (sqlite3 module is thread-safe when `check_same_thread=False`).
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self._path,
            isolation_level=None,
            check_same_thread=False,
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def enqueue(
        self,
        *,
        sender_email: str,
        recipient_email: str,
        ds_id: str,
        mailbox: str | None,
        idempotency_key: str,
        raw_eml: bytes,
    ) -> tuple[int, bool]:
        """Insert a new row; return (id, created). If idempotency_key already
        exists the existing row id is returned with created=False.
        """
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO queue
                    (state, received_at, sender_email, recipient_email,
                     ds_id, mailbox, idempotency_key, raw_eml)
                VALUES ('pending', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    _utcnow().isoformat(sep=" "),
                    sender_email,
                    recipient_email,
                    ds_id,
                    mailbox,
                    idempotency_key,
                    raw_eml,
                ),
            )
            if cur.rowcount == 1:
                return int(cur.lastrowid or 0), True
            row = self._conn.execute(
                "SELECT id FROM queue WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            return int(row["id"]), False

    def dequeue_batch(self, limit: int) -> list[QueueItem]:
        """Atomically pick up to `limit` due pending items and move them to
        in_flight. Returned items are snapshots from *before* the transition
        (state still `pending` in memory)."""
        now = _utcnow().isoformat(sep=" ")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    """
                    SELECT * FROM queue
                    WHERE state='pending'
                      AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                    ORDER BY received_at
                    LIMIT ?
                    """,
                    (now, limit),
                ).fetchall()
                if not rows:
                    self._conn.execute("COMMIT")
                    return []
                ids = [row["id"] for row in rows]
                placeholders = ",".join("?" * len(ids))
                self._conn.execute(
                    f"UPDATE queue SET state='in_flight' WHERE id IN ({placeholders})",
                    ids,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return [_row_to_item(r) for r in rows]

    def mark_delivered(self, item_id: int, delivered_message_id: str | None) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE queue
                SET state='delivered',
                    delivered_at=?,
                    delivered_message_id=?,
                    last_error=NULL
                WHERE id=?
                """,
                (_utcnow().isoformat(sep=" "), delivered_message_id, item_id),
            )

    def mark_retry(
        self,
        item_id: int,
        *,
        attempt_count: int,
        next_attempt_at: datetime,
        last_error: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE queue
                SET state='pending',
                    attempt_count=?,
                    next_attempt_at=?,
                    last_error=?
                WHERE id=?
                """,
                (
                    attempt_count,
                    next_attempt_at.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" "),
                    last_error,
                    item_id,
                ),
            )

    def mark_dead_letter(self, item_id: int, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE queue
                SET state='dead_letter',
                    dead_letter_reason=?,
                    last_error=?
                WHERE id=?
                """,
                (reason, reason, item_id),
            )

    def requeue_in_flight(self) -> int:
        """On worker startup, anything stuck in `in_flight` means the previous
        worker crashed mid-delivery. Put it back to pending so it retries.
        Idempotent; returns the number of rows touched."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE queue SET state='pending', next_attempt_at=NULL WHERE state='in_flight'"
            )
            return cur.rowcount or 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) AS n FROM queue GROUP BY state"
            ).fetchall()
        return {row["state"]: row["n"] for row in rows}

    def list_dead_letters(self, limit: int = 50) -> list[QueueItem]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM queue WHERE state='dead_letter' ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_item(r) for r in rows]

    def prune_delivered(self, days: int) -> int:
        cutoff = _utcnow() - timedelta(days=days)
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM queue WHERE state='delivered' AND delivered_at < ?",
                (cutoff.isoformat(sep=" "),),
            )
            return cur.rowcount or 0

    def get(self, item_id: int) -> QueueItem | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM queue WHERE id=?", (item_id,)
            ).fetchone()
        return _row_to_item(row) if row else None

    def iter_all(self) -> Iterable[QueueItem]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM queue ORDER BY id").fetchall()
        for row in rows:
            yield _row_to_item(row)
