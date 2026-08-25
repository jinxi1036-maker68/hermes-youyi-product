"""Durable idempotency receipts for WeCom callback delivery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


_CLAIM_STALE_SECONDS = 15 * 60
_RECEIPT_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_DELIVERY_ATTEMPTS = 3


def receipt_key(app_name: str, message_id: str) -> str:
    raw = f"{str(app_name or 'default').strip()}\n{str(message_id or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class WecomInboundReceiptStore:
    """SQLite-backed callback claim store shared across workers and restarts."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.owner_id = uuid.uuid4().hex
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
                            processing_at REAL,
                            processed_at REAL,
                            replied_at REAL,
                            failed_at REAL,
                            reply_status TEXT NOT NULL DEFAULT 'received',
                            last_error TEXT NOT NULL DEFAULT '',
                            payload_json TEXT NOT NULL DEFAULT '{}',
                            owner_id TEXT NOT NULL DEFAULT ''
                        )
                        """
                    )
                    columns = {
                        str(row["name"])
                        for row in connection.execute("PRAGMA table_info(inbound_receipts)").fetchall()
                    }
                    if "payload_json" not in columns:
                        connection.execute(
                            "ALTER TABLE inbound_receipts ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'"
                        )
                    if "owner_id" not in columns:
                        connection.execute(
                            "ALTER TABLE inbound_receipts ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''"
                        )
                    if "processing_at" not in columns:
                        connection.execute("ALTER TABLE inbound_receipts ADD COLUMN processing_at REAL")
                    if "replied_at" not in columns:
                        connection.execute("ALTER TABLE inbound_receipts ADD COLUMN replied_at REAL")
                    if "reply_status" not in columns:
                        connection.execute(
                            "ALTER TABLE inbound_receipts ADD COLUMN reply_status TEXT NOT NULL DEFAULT 'received'"
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
        payload: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = float(now if now is not None else time.time())
        key = receipt_key(app_name, message_id)
        payload_json = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))
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
                        status, attempt_count, claimed_at, payload_json, owner_id
                    ) VALUES (?, ?, ?, ?, ?, 'claimed', 1, ?, ?, ?)
                    """,
                    (
                        key,
                        str(app_name),
                        str(message_id),
                        str(user_id),
                        str(session_id),
                        timestamp,
                        payload_json,
                        self.owner_id,
                    ),
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
                            processing_at = NULL, processed_at = NULL, replied_at = NULL,
                            failed_at = NULL, reply_status = 'received', last_error = '',
                            payload_json = CASE WHEN ? = '{}' THEN payload_json ELSE ? END,
                            owner_id = ?
                        WHERE receipt_key = ?
                        """,
                        (str(user_id), str(session_id), timestamp, payload_json, payload_json, self.owner_id, key),
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

    def recover_pending(
        self,
        *,
        max_attempts: int = _MAX_DELIVERY_ATTEMPTS,
        include_owned: bool = False,
    ) -> list[dict[str, Any]]:
        """Reclaim durable callback payloads left unfinished by a prior process."""

        recovered: list[dict[str, Any]] = []
        timestamp = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner_filter = "" if include_owned else "AND owner_id <> ?"
            params: tuple[Any, ...] = (max(1, int(max_attempts)),)
            if not include_owned:
                params += (self.owner_id,)
            rows = connection.execute(
                f"""
                SELECT * FROM inbound_receipts
                WHERE status IN ('claimed', 'failed')
                  AND attempt_count < ?
                  AND payload_json <> '{{}}'
                  {owner_filter}
                ORDER BY claimed_at ASC
                """,
                params,
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(str(row["payload_json"] or "{}"))
                except (TypeError, ValueError):
                    payload = {}
                if not isinstance(payload, dict) or not payload:
                    continue
                connection.execute(
                    """
                    UPDATE inbound_receipts
                    SET status = 'claimed', attempt_count = attempt_count + 1,
                        claimed_at = ?, failed_at = NULL, last_error = '', owner_id = ?
                    WHERE receipt_key = ?
                    """,
                    (timestamp, self.owner_id, str(row["receipt_key"])),
                )
                recovered.append(
                    {
                        "receipt_key": str(row["receipt_key"]),
                        "app_name": str(row["app_name"]),
                        "message_id": str(row["message_id"]),
                        "user_id": str(row["user_id"]),
                        "session_id": str(row["session_id"]),
                        "attempt_count": int(row["attempt_count"] or 0) + 1,
                        "payload": payload,
                    }
                )
            connection.commit()
        return recovered

    def mark_processing(self, key: str, *, now: float | None = None) -> bool:
        """Record that a callback left the queue and entered the model turn."""

        timestamp = float(now if now is not None else time.time())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE inbound_receipts
                SET processing_at = ?, reply_status = 'processing'
                WHERE receipt_key = ? AND status = 'claimed'
                """,
                (timestamp, str(key)),
            )
        return cursor.rowcount == 1

    def mark_processed(self, key: str, *, session_id: str = "", now: float | None = None) -> bool:
        timestamp = float(now if now is not None else time.time())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE inbound_receipts
                SET status = 'processed', processed_at = ?, replied_at = ?, reply_status = 'replied',
                    session_id = ?, last_error = ''
                WHERE receipt_key = ? AND status = 'claimed'
                """,
                (timestamp, timestamp, str(session_id), str(key)),
            )
        return cursor.rowcount == 1

    def mark_failed(self, key: str, error: str, *, now: float | None = None) -> bool:
        timestamp = float(now if now is not None else time.time())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE inbound_receipts
                SET status = 'failed', failed_at = ?, reply_status = 'reply_failed', last_error = ?
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
                       SUM(CASE WHEN status = 'claimed' THEN 1 ELSE 0 END) AS claimed,
                       SUM(CASE WHEN reply_status = 'processing' THEN 1 ELSE 0 END) AS processing,
                       SUM(CASE WHEN reply_status = 'replied' THEN 1 ELSE 0 END) AS replied,
                       SUM(CASE WHEN reply_status = 'reply_failed' THEN 1 ELSE 0 END) AS reply_failed
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
            "received_count": int(row["total"] or 0),
            "processing_count": int(row["processing"] or 0),
            "replied_count": int(row["replied"] or 0),
            "reply_failed_count": int(row["reply_failed"] or 0),
        }
