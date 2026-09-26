#!/usr/bin/env python3
"""Durable bootstrap ingress holding bridge for WeCom callbacks.

This service is intentionally transport-only. It does not decrypt business
messages or make business decisions.

Semantics:
- FORWARD: transparently proxy WeCom callback traffic to the Gateway.
- HOLD: authenticate POST callbacks, durably persist them, then ACK WeCom.
- FALLBACK: when a retryable forward attempt fails, authenticate + durably
  persist the raw encrypted callback before ACKing WeCom.
- REPLAY: once FORWARD resumes, deliver pending callbacks to the Gateway.
  A row is completed only after a downstream 2xx response.

Transport dedupe uses a stable fingerprint of the encrypted callback envelope.
Business-level exactly-once execution remains the Gateway's responsibility via
its durable decrypted MsgId receipt store.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import pwd
import sqlite3
import time
from typing import Any, Iterable
from urllib.parse import urlsplit

try:
    import defusedxml.ElementTree as ET
except ImportError:  # pragma: no cover - production preflight catches this
    ET = None  # type: ignore[assignment]

try:
    from aiohttp import web
except ImportError:  # pragma: no cover - production preflight catches this
    web = None  # type: ignore[assignment]

try:
    import httpx
except ImportError:  # pragma: no cover - production preflight catches this
    httpx = None  # type: ignore[assignment]


LOGGER = logging.getLogger("xiaoyou.wecom_holding_bridge")

SCHEMA_VERSION = "xiaoyou_wecom_holding_bridge_v1"
MODE_SCHEMA_VERSION = "xiaoyou_wecom_holding_mode_v1"
MODE_HOLD = "hold"
MODE_FORWARD = "forward"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 19191
DEFAULT_CALLBACK_PATH = "/wecom/callback"
DEFAULT_UPSTREAM_ORIGIN = "http://127.0.0.1:8866"
DEFAULT_STATE_ROOT = Path("/var/lib/hermes-youyi/ingress-holding")
DEFAULT_SERVICE_USER = "hermes-youyi"

DEFAULT_REPLAY_POLL_SECONDS = 0.25
DEFAULT_REPLAY_MAX_BACKOFF_SECONDS = 30.0
DEFAULT_RETENTION_SECONDS = 7 * 24 * 3600
DEFAULT_FORWARD_TIMEOUT_SECONDS = 2.0
DEFAULT_MAX_BODY = 65_536

ACK_BODY = b"success"
ACK_CONTENT_TYPE = "text/plain"

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _epoch_now() -> float:
    return time.time()


def _private_dir(path: Path) -> None:
    if path.exists():
        if not path.is_dir() or path.is_symlink():
            raise RuntimeError(f"state_root_not_direct_directory:{path}")
    else:
        path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)


def _private_file(path: Path) -> None:
    if path.exists():
        os.chmod(path, 0o600)


def _service_uid(user_name: str) -> int:
    return pwd.getpwnam(user_name).pw_uid


def _require_service_identity(user_name: str) -> None:
    required = _service_uid(user_name)
    current = os.geteuid()
    if current != required:
        raise PermissionError(
            f"write operation requires {user_name} uid={required}; current uid={current}"
        )


def _tokens_from_environment() -> list[str]:
    raw_json = str(os.getenv("XIAOYOU_WECOM_HOLDING_TOKENS_JSON") or "").strip()
    if raw_json:
        try:
            values = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError("holding_tokens_json_invalid") from exc
        if not isinstance(values, list):
            raise RuntimeError("holding_tokens_json_not_list")
        tokens = [str(value).strip() for value in values if str(value).strip()]
        if tokens:
            return tokens
    single = str(os.getenv("WECOM_CALLBACK_TOKEN") or "").strip()
    return [single] if single else []


def _extract_encrypt(body: bytes) -> str:
    if ET is None:
        raise RuntimeError("defusedxml_unavailable")
    try:
        root = ET.fromstring(body)
    except Exception as exc:
        raise ValueError("invalid_callback_xml") from exc
    encrypted = str(root.findtext("Encrypt", default="") or "").strip()
    if not encrypted:
        raise ValueError("callback_encrypt_missing")
    return encrypted


def _transport_key(body: bytes) -> str:
    encrypted = _extract_encrypt(body)
    return "enc-sha256:" + hashlib.sha256(encrypted.encode("utf-8")).hexdigest()


def _verify_wecom_signature(
    *,
    body: bytes,
    query: dict[str, str],
    tokens: Iterable[str],
) -> tuple[bool, str]:
    signature = str(query.get("msg_signature") or "").strip().lower()
    timestamp = str(query.get("timestamp") or "").strip()
    nonce = str(query.get("nonce") or "").strip()
    if not signature or not timestamp or not nonce:
        return False, "callback_signature_parameters_missing"
    token_values = [str(token).strip() for token in tokens if str(token).strip()]
    if not token_values:
        return False, "holding_callback_tokens_unavailable"
    try:
        encrypted = _extract_encrypt(body)
    except (ValueError, RuntimeError) as exc:
        return False, str(exc)
    for token in token_values:
        payload = "".join(sorted((token, timestamp, nonce, encrypted)))
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
        if hmac.compare_digest(digest, signature):
            return True, ""
    return False, "callback_signature_invalid"


def _raw_path_qs(request: Any) -> str:
    rel_url = getattr(request, "rel_url", None)
    raw_path_qs = str(getattr(rel_url, "raw_path_qs", "") or "")
    if raw_path_qs:
        return raw_path_qs
    path = str(getattr(request, "path", "") or "/")
    query_string = str(getattr(request, "query_string", "") or "")
    return path + (f"?{query_string}" if query_string else "")


def _query_dict(request: Any) -> dict[str, str]:
    query = getattr(request, "query", None)
    if query is None:
        return {}
    return {str(key): str(value) for key, value in query.items()}


def _selected_request_headers(request: Any) -> dict[str, str]:
    headers = getattr(request, "headers", {}) or {}
    result: dict[str, str] = {}
    for name in ("Content-Type", "Accept", "User-Agent"):
        value = str(headers.get(name) or "").strip()
        if value:
            result[name] = value
    return result


def _selected_response_headers(headers: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in ("Content-Type", "Cache-Control"):
        value = str(headers.get(name) or "").strip() if headers is not None else ""
        if value:
            result[name] = value
    return result


class HoldingModeGate:
    """File-backed operator HOLD gate.

    File absent = FORWARD.
    File present but corrupt = fail-closed HOLD.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def snapshot(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "mode": MODE_FORWARD,
                "active": False,
                "valid": True,
                "path": str(self.path),
            }
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {
                "mode": MODE_HOLD,
                "active": True,
                "valid": False,
                "path": str(self.path),
                "error": "holding_mode_state_unreadable",
            }
        valid = (
            isinstance(payload, dict)
            and payload.get("schema_version") == MODE_SCHEMA_VERSION
            and str(payload.get("mode") or "").strip().lower() == MODE_HOLD
        )
        return {
            "mode": MODE_HOLD,
            "active": True,
            "valid": valid,
            "path": str(self.path),
            "requested_at": payload.get("requested_at") if isinstance(payload, dict) else None,
            "reason": str(payload.get("reason") or "") if isinstance(payload, dict) else "",
            **({} if valid else {"error": "holding_mode_state_invalid"}),
        }

    def hold_active(self) -> bool:
        return bool(self.snapshot().get("active"))


class HoldingStore:
    """Durable encrypted-callback spool.

    SQLite FULL synchronous durability is intentional: ACK is permitted only
    after commit returns successfully.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        _private_dir(self.path.parent)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS callback_holding (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    transport_key TEXT NOT NULL UNIQUE,
                    method TEXT NOT NULL,
                    path_qs TEXT NOT NULL,
                    content_type TEXT NOT NULL DEFAULT '',
                    body BLOB NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','delivering','completed')),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_attempt_at TEXT,
                    completed_at TEXT,
                    last_http_status INTEGER,
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_callback_holding_delivery
                    ON callback_holding(status, next_attempt_at, id);
                """
            )
            # A crashed process may leave a lease marked delivering. At-least-once
            # replay is safe because Gateway business MsgId receipts dedupe.
            now = _utc_now()
            connection.execute(
                """
                UPDATE callback_holding
                   SET status='pending',
                       next_attempt_at=0,
                       updated_at=?,
                       last_error=CASE
                         WHEN last_error='' THEN 'recovered_after_bridge_restart'
                         ELSE last_error
                       END
                 WHERE status='delivering'
                """,
                (now,),
            )
            connection.commit()
        finally:
            connection.close()
        _private_file(self.path)

    def lookup(self, transport_key: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT id, transport_key, status, attempt_count, completed_at FROM callback_holding WHERE transport_key=?",
                (transport_key,),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def persist(
        self,
        *,
        transport_key: str,
        method: str,
        path_qs: str,
        content_type: str,
        body: bytes,
    ) -> dict[str, Any]:
        now = _utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO callback_holding(
                    transport_key, method, path_qs, content_type, body,
                    status, attempt_count, next_attempt_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', 0, 0, ?, ?)
                """,
                (
                    transport_key,
                    str(method).upper(),
                    path_qs,
                    content_type,
                    sqlite3.Binary(body),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT id, transport_key, status, attempt_count, completed_at
                  FROM callback_holding
                 WHERE transport_key=?
                """,
                (transport_key,),
            ).fetchone()
            connection.commit()
            if row is None:
                raise RuntimeError("holding_persist_missing_after_insert")
            result = dict(row)
            result["inserted"] = cursor.rowcount == 1
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_next(self, *, now_epoch: float | None = None) -> dict[str, Any] | None:
        now_epoch = _epoch_now() if now_epoch is None else float(now_epoch)
        now_iso = _utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT *
                  FROM callback_holding
                 WHERE status='pending'
                   AND next_attempt_at <= ?
                 ORDER BY id ASC
                 LIMIT 1
                """,
                (now_epoch,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            updated = connection.execute(
                """
                UPDATE callback_holding
                   SET status='delivering',
                       attempt_count=attempt_count+1,
                       last_attempt_at=?,
                       updated_at=?
                 WHERE id=? AND status='pending'
                """,
                (now_iso, now_iso, int(row["id"])),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return None
            claimed = connection.execute(
                "SELECT * FROM callback_holding WHERE id=?",
                (int(row["id"]),),
            ).fetchone()
            connection.commit()
            return dict(claimed) if claimed is not None else None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_completed(self, row_id: int, *, http_status: int) -> bool:
        now = _utc_now()
        connection = self._connect()
        try:
            cursor = connection.execute(
                """
                UPDATE callback_holding
                   SET status='completed',
                       completed_at=?,
                       updated_at=?,
                       last_http_status=?,
                       last_error='',
                       next_attempt_at=0
                 WHERE id=? AND status='delivering'
                """,
                (now, now, int(http_status), int(row_id)),
            )
            connection.commit()
            return cursor.rowcount == 1
        finally:
            connection.close()

    def mark_retry(
        self,
        row_id: int,
        *,
        error: str,
        http_status: int | None,
        backoff_seconds: float,
    ) -> bool:
        now = _utc_now()
        next_attempt = _epoch_now() + max(0.0, float(backoff_seconds))
        connection = self._connect()
        try:
            cursor = connection.execute(
                """
                UPDATE callback_holding
                   SET status='pending',
                       updated_at=?,
                       next_attempt_at=?,
                       last_http_status=?,
                       last_error=?
                 WHERE id=? AND status='delivering'
                """,
                (
                    now,
                    next_attempt,
                    int(http_status) if http_status is not None else None,
                    str(error)[:500],
                    int(row_id),
                ),
            )
            connection.commit()
            return cursor.rowcount == 1
        finally:
            connection.close()

    def counts(self) -> dict[str, int]:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total_count,
                    SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending_count,
                    SUM(CASE WHEN status='delivering' THEN 1 ELSE 0 END) AS delivering_count,
                    SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed_count
                  FROM callback_holding
                """
            ).fetchone()
            return {
                "total_count": int(row["total_count"] or 0),
                "pending_count": int(row["pending_count"] or 0),
                "delivering_count": int(row["delivering_count"] or 0),
                "completed_count": int(row["completed_count"] or 0),
            }
        finally:
            connection.close()


    @staticmethod
    def read_counts(path: Path) -> dict[str, int]:
        """Read queue counts without creating or mutating the spool."""
        path = Path(path)
        empty = {
            "total_count": 0,
            "pending_count": 0,
            "delivering_count": 0,
            "completed_count": 0,
        }
        if not path.exists():
            return empty
        uri = f"file:{path.resolve()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total_count,
                    SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending_count,
                    SUM(CASE WHEN status='delivering' THEN 1 ELSE 0 END) AS delivering_count,
                    SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed_count
                  FROM callback_holding
                """
            ).fetchone()
            if row is None:
                return empty
            return {
                "total_count": int(row["total_count"] or 0),
                "pending_count": int(row["pending_count"] or 0),
                "delivering_count": int(row["delivering_count"] or 0),
                "completed_count": int(row["completed_count"] or 0),
            }
        finally:
            connection.close()

    def prune_completed(self, *, older_than_epoch: float) -> int:
        cutoff = datetime.fromtimestamp(float(older_than_epoch), tz=timezone.utc).isoformat(
            timespec="milliseconds"
        )
        connection = self._connect()
        try:
            cursor = connection.execute(
                "DELETE FROM callback_holding WHERE status='completed' AND completed_at < ?",
                (cutoff,),
            )
            connection.commit()
            return int(cursor.rowcount or 0)
        finally:
            connection.close()


class WeComHoldingBridge:
    def __init__(
        self,
        *,
        callback_path: str,
        upstream_origin: str,
        store: HoldingStore,
        mode_gate: HoldingModeGate,
        tokens: list[str],
        client: Any,
        max_body: int = DEFAULT_MAX_BODY,
        replay_poll_seconds: float = DEFAULT_REPLAY_POLL_SECONDS,
        replay_max_backoff_seconds: float = DEFAULT_REPLAY_MAX_BACKOFF_SECONDS,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
    ) -> None:
        callback_path = "/" + str(callback_path or DEFAULT_CALLBACK_PATH).lstrip("/")
        parsed = urlsplit(upstream_origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
            raise ValueError("upstream_origin_must_be_origin_only")
        self.callback_path = callback_path
        self.upstream_origin = upstream_origin.rstrip("/")
        self.store = store
        self.mode_gate = mode_gate
        self.tokens = list(tokens)
        self.client = client
        self.max_body = int(max_body)
        self.replay_poll_seconds = max(0.05, float(replay_poll_seconds))
        self.replay_max_backoff_seconds = max(1.0, float(replay_max_backoff_seconds))
        self.retention_seconds = max(3600.0, float(retention_seconds))
        self._inflight_forwards = 0
        self._replay_task: asyncio.Task | None = None
        self._closed = False
        self._last_prune_epoch = 0.0

    def _upstream_url(self, path_qs: str) -> str:
        return self.upstream_origin + path_qs

    async def _forward(
        self,
        *,
        method: str,
        path_qs: str,
        headers: dict[str, str],
        body: bytes,
    ) -> Any:
        self._inflight_forwards += 1
        try:
            return await self.client.request(
                str(method).upper(),
                self._upstream_url(path_qs),
                headers=headers,
                content=body,
            )
        finally:
            self._inflight_forwards -= 1

    def _ack(self) -> Any:
        return web.Response(body=ACK_BODY, content_type=ACK_CONTENT_TYPE)

    async def handle_verify(self, request: Any) -> Any:
        """GET verification is never queued; it must be answered by Gateway crypto."""
        path_qs = _raw_path_qs(request)
        headers = _selected_request_headers(request)
        try:
            response = await self._forward(
                method="GET",
                path_qs=path_qs,
                headers=headers,
                body=b"",
            )
        except Exception:
            LOGGER.exception("GET callback verification forward failed")
            return web.Response(status=503, text="gateway unavailable")
        return web.Response(
            status=int(response.status_code),
            body=bytes(response.content),
            headers=_selected_response_headers(response.headers),
        )

    async def _persist_authenticated(
        self,
        *,
        request: Any,
        body: bytes,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        query = _query_dict(request)
        valid, reason = _verify_wecom_signature(body=body, query=query, tokens=self.tokens)
        if not valid:
            return False, reason, None
        try:
            key = _transport_key(body)
            row = self.store.persist(
                transport_key=key,
                method=str(getattr(request, "method", "POST") or "POST"),
                path_qs=_raw_path_qs(request),
                content_type=str((getattr(request, "headers", {}) or {}).get("Content-Type") or ""),
                body=body,
            )
        except Exception as exc:
            LOGGER.exception("Durable callback persistence failed")
            return False, f"holding_persist_failed:{type(exc).__name__}", None
        return True, "", row

    async def handle_callback(self, request: Any) -> Any:
        body = await request.read()
        if len(body) > self.max_body:
            return web.Response(status=413, text="payload too large")

        mode = self.mode_gate.snapshot()
        if mode.get("active"):
            ok, reason, _row = await self._persist_authenticated(request=request, body=body)
            if ok:
                return self._ack()
            if reason == "holding_callback_tokens_unavailable" or reason.startswith("holding_persist_failed"):
                return web.Response(status=503, text="holding unavailable")
            return web.Response(status=400, text="invalid callback")

        # If this exact encrypted envelope is already durable, do not race a
        # replay worker with a second direct delivery. Authenticate before ACK.
        try:
            existing_key = _transport_key(body)
            existing = self.store.lookup(existing_key)
        except Exception:
            existing = None
        if existing is not None:
            valid, reason = _verify_wecom_signature(
                body=body,
                query=_query_dict(request),
                tokens=self.tokens,
            )
            if valid:
                return self._ack()
            if reason == "holding_callback_tokens_unavailable":
                return web.Response(status=503, text="holding unavailable")
            return web.Response(status=400, text="invalid callback")

        path_qs = _raw_path_qs(request)
        headers = _selected_request_headers(request)
        try:
            response = await self._forward(
                method="POST",
                path_qs=path_qs,
                headers=headers,
                body=body,
            )
        except Exception:
            LOGGER.warning("Gateway callback forward failed; entering durable fallback", exc_info=True)
            ok, reason, _row = await self._persist_authenticated(request=request, body=body)
            if ok:
                return self._ack()
            if reason == "holding_callback_tokens_unavailable" or reason.startswith("holding_persist_failed"):
                return web.Response(status=503, text="holding unavailable")
            return web.Response(status=400, text="invalid callback")

        status_code = int(response.status_code)
        if not (200 <= status_code < 300):
            # A transport-authentic callback must not be lost merely because
            # the downstream Gateway is misconfigured or temporarily rejects
            # it. Invalid/untrusted requests still receive the Gateway error.
            ok, reason, _row = await self._persist_authenticated(request=request, body=body)
            if ok:
                return self._ack()
            if reason == "holding_callback_tokens_unavailable" or reason.startswith("holding_persist_failed"):
                return web.Response(status=503, text="holding unavailable")
            return web.Response(
                status=status_code,
                body=bytes(response.content),
                headers=_selected_response_headers(response.headers),
            )

        return web.Response(
            status=status_code,
            body=bytes(response.content),
            headers=_selected_response_headers(response.headers),
        )

    async def health(self, _request: Any) -> Any:
        try:
            counts = self.store.counts()
            mode = self.mode_gate.snapshot()
            payload = {
                "ok": True,
                "schema_version": SCHEMA_VERSION,
                "mode": mode.get("mode"),
                "mode_valid": mode.get("valid"),
                "inflight_forwards": self._inflight_forwards,
                "queue": counts,
            }
            return web.json_response(payload)
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": type(exc).__name__},
                status=503,
            )

    async def _deliver_held_row(self, row: dict[str, Any]) -> None:
        row_id = int(row["id"])
        attempt = int(row.get("attempt_count") or 1)
        try:
            response = await self._forward(
                method=str(row["method"]),
                path_qs=str(row["path_qs"]),
                headers={"Content-Type": str(row.get("content_type") or "application/xml")},
                body=bytes(row["body"]),
            )
        except Exception as exc:
            backoff = min(self.replay_max_backoff_seconds, float(2 ** min(attempt, 10)))
            self.store.mark_retry(
                row_id,
                error=f"transport:{type(exc).__name__}",
                http_status=None,
                backoff_seconds=backoff,
            )
            return

        status = int(response.status_code)
        if 200 <= status < 300:
            if not self.store.mark_completed(row_id, http_status=status):
                LOGGER.error("Unable to mark held callback completed row_id=%s", row_id)
            return

        backoff = min(self.replay_max_backoff_seconds, float(2 ** min(attempt, 10)))
        self.store.mark_retry(
            row_id,
            error=f"downstream_http_{status}",
            http_status=status,
            backoff_seconds=backoff,
        )

    async def replay_loop(self) -> None:
        while not self._closed:
            try:
                if self.mode_gate.hold_active():
                    await asyncio.sleep(self.replay_poll_seconds)
                    continue
                row = self.store.claim_next()
                if row is None:
                    now = _epoch_now()
                    if now - self._last_prune_epoch >= 3600:
                        self.store.prune_completed(
                            older_than_epoch=now - self.retention_seconds
                        )
                        self._last_prune_epoch = now
                    await asyncio.sleep(self.replay_poll_seconds)
                    continue
                await self._deliver_held_row(row)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Holding replay loop failed")
                await asyncio.sleep(self.replay_poll_seconds)

    async def startup(self, _app: Any) -> None:
        self._replay_task = asyncio.create_task(self.replay_loop())

    async def cleanup(self, _app: Any) -> None:
        self._closed = True
        if self._replay_task is not None:
            self._replay_task.cancel()
            try:
                await self._replay_task
            except asyncio.CancelledError:
                pass
        close = getattr(self.client, "aclose", None)
        if callable(close):
            await close()

    def application(self) -> Any:
        app = web.Application(client_max_size=self.max_body)
        app.router.add_get("/health", self.health)
        app.router.add_get("/_xiaoyou/status", self.health)
        app.router.add_get(self.callback_path, self.handle_verify)
        app.router.add_post(self.callback_path, self.handle_callback)
        app.on_startup.append(self.startup)
        app.on_cleanup.append(self.cleanup)
        return app


def _atomic_hold(mode_file: Path, *, reason: str) -> None:
    _private_dir(mode_file.parent)
    payload = {
        "schema_version": MODE_SCHEMA_VERSION,
        "mode": MODE_HOLD,
        "requested_at": _utc_now(),
        "reason": str(reason or "maintenance"),
    }
    temporary = mode_file.with_name(f".{mode_file.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, mode_file)
    os.chmod(mode_file, 0o600)


def _resume_forward(mode_file: Path) -> None:
    mode_file.unlink(missing_ok=True)


async def _fetch_local_status(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("holding_status_invalid")
        return payload


async def _wait_status(
    *,
    status_url: str,
    condition: str,
    timeout_seconds: float,
    poll_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    last: dict[str, Any] = {}
    while True:
        try:
            last = await _fetch_local_status(status_url)
        except Exception as exc:
            last = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}

        mode = str(last.get("mode") or "")
        inflight = int(last.get("inflight_forwards") or 0)
        queue = last.get("queue") if isinstance(last.get("queue"), dict) else {}
        pending = int(queue.get("pending_count") or 0)
        delivering = int(queue.get("delivering_count") or 0)

        if condition == "held" and mode == MODE_HOLD and inflight == 0:
            return {**last, "condition": condition, "ready": True}
        if condition == "empty" and mode == MODE_FORWARD and pending == 0 and delivering == 0:
            return {**last, "condition": condition, "ready": True}

        if time.monotonic() >= deadline:
            return {
                **last,
                "condition": condition,
                "ready": False,
                "error": "wait_timeout",
            }
        await asyncio.sleep(max(0.05, float(poll_seconds)))


def _state_paths(state_root: Path) -> tuple[Path, Path]:
    root = Path(state_root)
    return root / "queue.sqlite3", root / "hold.json"


def _serve(args: argparse.Namespace) -> int:
    if web is None or httpx is None or ET is None:
        print(json.dumps({"ok": False, "error": "required_http_dependencies_unavailable"}))
        return 1

    state_root = Path(args.state_root)
    db_path, mode_file = _state_paths(state_root)
    _private_dir(state_root)

    tokens = _tokens_from_environment()
    timeout = httpx.Timeout(
        connect=float(args.forward_timeout),
        read=float(args.forward_timeout),
        write=float(args.forward_timeout),
        pool=float(args.forward_timeout),
    )
    client = httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False)
    store = HoldingStore(db_path)
    bridge = WeComHoldingBridge(
        callback_path=args.callback_path,
        upstream_origin=args.upstream_origin,
        store=store,
        mode_gate=HoldingModeGate(mode_file),
        tokens=tokens,
        client=client,
        max_body=int(args.max_body),
        replay_poll_seconds=float(args.replay_poll_seconds),
        replay_max_backoff_seconds=float(args.replay_max_backoff),
        retention_seconds=float(args.retention_seconds),
    )
    web.run_app(
        bridge.application(),
        host=args.host,
        port=int(args.port),
        access_log=None,
        print=None,
    )
    return 0


def _control(args: argparse.Namespace) -> int:
    state_root = Path(args.state_root)
    db_path, mode_file = _state_paths(state_root)
    try:
        if args.action in {"hold", "forward"}:
            _require_service_identity(args.service_user)
        if args.action == "hold":
            _atomic_hold(mode_file, reason=args.reason)
            result = {
                "ok": True,
                "action": "hold",
                "mode": HoldingModeGate(mode_file).snapshot(),
            }
        elif args.action == "forward":
            _resume_forward(mode_file)
            result = {
                "ok": True,
                "action": "forward",
                "mode": HoldingModeGate(mode_file).snapshot(),
            }
        elif args.action == "offline-status":
            result = {
                "ok": True,
                "action": "offline-status",
                "mode": HoldingModeGate(mode_file).snapshot(),
                "queue": HoldingStore.read_counts(db_path),
            }
        elif args.action in {"wait-held", "wait-empty"}:
            condition = "held" if args.action == "wait-held" else "empty"
            result = asyncio.run(
                _wait_status(
                    status_url=args.status_url,
                    condition=condition,
                    timeout_seconds=float(args.timeout_seconds),
                    poll_seconds=float(args.poll_seconds),
                )
            )
            result["ok"] = bool(result.get("ready"))
            result["action"] = args.action
        else:
            raise RuntimeError(f"unsupported_action:{args.action}")
    except (OSError, RuntimeError, PermissionError, sqlite3.Error) as exc:
        result = {
            "ok": False,
            "action": args.action,
            "error": f"{type(exc).__name__}:{exc}",
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="XiaoYou durable WeCom bootstrap holding bridge")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve")
    serve.add_argument("--host", default=os.getenv("XIAOYOU_WECOM_HOLDING_HOST", DEFAULT_HOST))
    serve.add_argument("--port", type=int, default=int(os.getenv("XIAOYOU_WECOM_HOLDING_PORT", DEFAULT_PORT)))
    serve.add_argument("--callback-path", default=os.getenv("XIAOYOU_WECOM_HOLDING_CALLBACK_PATH", DEFAULT_CALLBACK_PATH))
    serve.add_argument("--upstream-origin", default=os.getenv("XIAOYOU_WECOM_HOLDING_UPSTREAM", DEFAULT_UPSTREAM_ORIGIN))
    serve.add_argument("--state-root", type=Path, default=Path(os.getenv("XIAOYOU_WECOM_HOLDING_STATE_ROOT", str(DEFAULT_STATE_ROOT))))
    serve.add_argument("--max-body", type=int, default=DEFAULT_MAX_BODY)
    serve.add_argument("--forward-timeout", type=float, default=float(os.getenv("XIAOYOU_WECOM_HOLDING_FORWARD_TIMEOUT", DEFAULT_FORWARD_TIMEOUT_SECONDS)))
    serve.add_argument("--replay-poll-seconds", type=float, default=DEFAULT_REPLAY_POLL_SECONDS)
    serve.add_argument("--replay-max-backoff", type=float, default=DEFAULT_REPLAY_MAX_BACKOFF_SECONDS)
    serve.add_argument("--retention-seconds", type=float, default=DEFAULT_RETENTION_SECONDS)

    control = sub.add_parser("control")
    control.add_argument("action", choices=("hold", "forward", "offline-status", "wait-held", "wait-empty"))
    control.add_argument("--state-root", type=Path, default=Path(os.getenv("XIAOYOU_WECOM_HOLDING_STATE_ROOT", str(DEFAULT_STATE_ROOT))))
    control.add_argument("--service-user", default=DEFAULT_SERVICE_USER)
    control.add_argument("--reason", default="runtime-topology-maintenance")
    status_host = str(os.getenv("XIAOYOU_WECOM_HOLDING_HOST", DEFAULT_HOST) or DEFAULT_HOST)
    status_port = int(os.getenv("XIAOYOU_WECOM_HOLDING_PORT", DEFAULT_PORT))
    default_status_url = f"http://{status_host}:{status_port}/_xiaoyou/status"
    control.add_argument(
        "--status-url",
        default=os.getenv("XIAOYOU_WECOM_HOLDING_STATUS_URL", default_status_url),
    )
    control.add_argument("--timeout-seconds", type=float, default=90.0)
    control.add_argument("--poll-seconds", type=float, default=0.2)

    return parser


def main() -> int:
    logging.basicConfig(
        level=os.getenv("XIAOYOU_WECOM_HOLDING_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _parser().parse_args()
    if args.command == "serve":
        return _serve(args)
    return _control(args)


if __name__ == "__main__":
    raise SystemExit(main())
