"""Durable idempotency receipts for WeCom callback delivery."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Any


_CLAIM_STALE_SECONDS = 15 * 60
_RECEIPT_RETENTION_SECONDS = 7 * 24 * 60 * 60


def receipt_key(app_name: str, message_id: str) -> str:
    raw = f"{str(app_name or 'default').strip()}\n{str(message_id or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class WecomInboundReceiptStore:
    """SQLite-backed callback claim store shared across workers and restarts."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(20):
            try:
                with self._connect() as connection:
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute("PRAGMA synchronous=FULL")
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS inbound_receipts (
                            receipt_key TEXT PRIMARY KEY,
                            app_name TEXT NOT NULL,
                            message_id TEXT NOT NULL,
                            user_id TEXT NOT NULL DEFAULT '',
                            session_id TEXT NOT NULL DEFAULT '',
                            status TEXT NOT NULL,
                            attempt_count INTEGER NOT NULL DEFAULT 1,
                            claimed_at REAL NOT NULL,
                            processed_at REAL,
                            failed_at REAL,
                            last_error TEXT NOT NULL DEFAULT ''
                        )
                        """
                    )
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 19:
                    raise
                last_error = exc
                time.sleep(0.025 * (attempt + 1))
        if last_error is not None:
            raise last_error

    def claim(
        self,
        *,
        app_name: str,
        message_id: str,
        user_id: str = "",
        session_id: str = "",
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = float(now if now is not None else time.time())
        key = receipt_key(app_name, message_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM inbound_receipts WHERE receipt_key = ?",
                (key,),
            ).fetchone()
            reclaimed = False
            if row is None:
                connection.execute(
                    """
                    INSERT INTO inbound_receipts (
                        receipt_key, app_name, message_id, user_id, session_id,
                        status, attempt_count, claimed_at
                    ) VALUES (?, ?, ?, ?, ?, 'claimed', 1, ?)
                    """,
                    (key, str(app_name), str(message_id), str(user_id), str(session_id), timestamp),
                )
                accepted = True
            else:
                stale_claim = (
                    str(row["status"]) == "claimed"
                    and timestamp - float(row["claimed_at"] or 0) >= _CLAIM_STALE_SECONDS
                )
                retryable = str(row["status"]) == "failed" or stale_claim
                if retryable:
                    connection.execute(
                        """
                        UPDATE inbound_receipts
                        SET status = 'claimed', attempt_count = attempt_count + 1,
                            user_id = ?, session_id = ?, claimed_at = ?,
                            processed_at = NULL, failed_at = NULL, last_error = ''
                        WHERE receipt_key = ?
                        """,
                        (str(user_id), str(session_id), timestamp, key),
                    )
                    accepted = True
                    reclaimed = True
                else:
                    accepted = False
            connection.commit()
        return {
            "accepted": accepted,
            "duplicate": not accepted,
            "reclaimed": reclaimed,
            "receipt_key": key,
        }

    def mark_processed(self, key: str, *, session_id: str = "", now: float | None = None) -> bool:
        timestamp = float(now if now is not None else time.time())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE inbound_receipts
                SET status = 'processed', processed_at = ?, session_id = ?, last_error = ''
                WHERE receipt_key = ? AND status = 'claimed'
                """,
                (timestamp, str(session_id), str(key)),
            )
        return cursor.rowcount == 1

    def mark_failed(self, key: str, error: str, *, now: float | None = None) -> bool:
        timestamp = float(now if now is not None else time.time())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE inbound_receipts
                SET status = 'failed', failed_at = ?, last_error = ?
                WHERE receipt_key = ? AND status = 'claimed'
                """,
                (timestamp, str(error or "")[:500], str(key)),
            )
        return cursor.rowcount == 1

    def prune(self, *, now: float | None = None) -> int:
        timestamp = float(now if now is not None else time.time())
        cutoff = timestamp - _RECEIPT_RETENTION_SECONDS
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM inbound_receipts WHERE status IN ('processed', 'failed') AND claimed_at < ?",
                (cutoff,),
            )
        return int(cursor.rowcount or 0)

    def status_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM inbound_receipts GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def health_snapshot(self, *, since: float = 0.0) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN attempt_count > 1 THEN 1 ELSE 0 END) AS retried,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN status = 'claimed' THEN 1 ELSE 0 END) AS claimed
                FROM inbound_receipts
                WHERE claimed_at >= ?
                """,
                (float(since or 0.0),),
            ).fetchone()
        return {
            "total_count": int(row["total"] or 0),
            "duplicate_or_retry_count": int(row["retried"] or 0),
            "failed_count": int(row["failed"] or 0),
            "claimed_count": int(row["claimed"] or 0),
        }
