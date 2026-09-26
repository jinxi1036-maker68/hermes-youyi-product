"""Durable bootstrap holding bridge for the WeCom callback ingress.

This is a transport-only safety boundary for the first Runtime Topology
cutover. It does not decrypt WeCom payloads, interpret business messages, or
replace Gateway message-id idempotency.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit

from aiohttp import web
import httpx

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "xiaoyou_wecom_holding_bridge_v1"
MODE_SCHEMA_VERSION = "xiaoyou_wecom_holding_mode_v1"
HOLD_STATE = "hold"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 19091
DEFAULT_UPSTREAM = "http://127.0.0.1:19090"
DEFAULT_CALLBACK_PATH = "/wecom/callback"
DEFAULT_FORWARD_TIMEOUT_SECONDS = 5.0
DEFAULT_REPLAY_INTERVAL_SECONDS = 1.0
DEFAULT_REPLAY_BATCH_SIZE = 50
DEFAULT_COMPLETED_RETENTION_SECONDS = 7 * 24 * 60 * 60

MAX_BODY_BYTES = 65_536
ACK_BODY = b"success"
_TRANSIENT_STATUSES = {408, 425, 429}

# Keep the bridge from becoming a generic credential-forwarding proxy.
# WeCom authentication is carried by the callback query/body, not Authorization.
_ALLOWED_REQUEST_HEADERS = {
    "content-type",
    "user-agent",
    "x-forwarded-for",
    "x-forwarded-proto",
    "x-real-ip",
    "x-request-id",
}


@dataclass(frozen=True)
class HeldRequest:
    fingerprint: str
    method: str
    raw_path: str
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class BridgeResult:
    status: int
    body: bytes
    headers: dict[str, str]
    route: str


@dataclass(frozen=True)
class ForwardResult:
    status: int
    body: bytes
    headers: dict[str, str]


def _now() -> float:
    return time.time()


def _filtered_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        str(key).lower(): str(value)
        for key, value in headers.items()
        if str(key).lower() in _ALLOWED_REQUEST_HEADERS
    }


def _canonical_query(raw_path: str) -> str:
    query = urlsplit(str(raw_path or "")).query
    pairs = parse_qsl(query, keep_blank_values=True)
    pairs.sort(key=lambda item: (item[0], item[1]))
    return urlencode(pairs, doseq=True)


def transport_fingerprint(*, method: str, raw_path: str, body: bytes) -> str:
    """Stable identifier for an identical HTTP transport envelope.

    This deliberately does not inspect/decrypt the business payload. A retry
    whose transport envelope changes may produce a different fingerprint;
    Gateway WecomInboundReceiptStore remains the business message-id authority.
    """

    parsed = urlsplit(str(raw_path or ""))
    canonical = "\n".join(
        (
            str(method or "").upper(),
            parsed.path or "/",
            _canonical_query(raw_path),
            hashlib.sha256(bytes(body)).hexdigest(),
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def make_held_request(
    *,
    method: str,
    raw_path: str,
    headers: Mapping[str, str],
    body: bytes,
) -> HeldRequest:
    return HeldRequest(
        fingerprint=transport_fingerprint(
            method=method,
            raw_path=raw_path,
            body=body,
        ),
        method=str(method or "POST").upper(),
        raw_path=str(raw_path or DEFAULT_CALLBACK_PATH),
        headers=_filtered_headers(headers),
        body=bytes(body),
    )


class HoldingModeGate:
    """Filesystem-backed fail-closed HOLD gate.

    No file means FORWARD. A valid marker means HOLD. A corrupt marker also
    means HOLD so a damaged control file cannot silently reopen dispatch.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def snapshot(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "mode": "FORWARD",
                "active": False,
                "valid": True,
                "path": str(self.path),
            }
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {
                "mode": "HOLD",
                "active": True,
                "valid": False,
                "path": str(self.path),
                "error": "holding_mode_unreadable",
            }

        valid = (
            isinstance(payload, dict)
            and payload.get("schema_version") == MODE_SCHEMA_VERSION
            and str(payload.get("state") or "").strip().lower() == HOLD_STATE
        )
        return {
            "mode": "HOLD",
            "active": True,
            "valid": bool(valid),
            "path": str(self.path),
            "requested_at": (
                payload.get("requested_at") if isinstance(payload, dict) else None
            ),
            "reason": (
                str(payload.get("reason") or "")
                if isinstance(payload, dict)
                else ""
            ),
            **({} if valid else {"error": "holding_mode_invalid"}),
        }

    def is_holding(self) -> bool:
        return bool(self.snapshot().get("active"))


class HoldingStore:
    """SQLite store for raw encrypted callback transport envelopes.

    Every valid POST is durably staged before any downstream attempt. This
    closes the process-crash window that would exist if persistence happened
    only after a forwarding failure was observed.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=10,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS held_callbacks (
                    fingerprint TEXT PRIMARY KEY,
                    method TEXT NOT NULL,
                    raw_path TEXT NOT NULL,
                    headers_json TEXT NOT NULL DEFAULT '{}',
                    body BLOB NOT NULL,
                    state TEXT NOT NULL,
                    received_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    next_attempt_at REAL NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at REAL,
                    completed_at REAL,
                    last_status INTEGER,
                    last_error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_held_callbacks_pending "
                "ON held_callbacks(state, next_attempt_at, received_at)"
            )
            # A fresh process owns no in-flight direct/replay attempt from the
            # previous process. Recover those durable rows immediately.
            connection.execute(
                """
                UPDATE held_callbacks
                SET state = 'pending', next_attempt_at = ?
                WHERE state IN ('direct', 'replaying')
                """,
                (_now(),),
            )

    def persist_pending(
        self,
        request: HeldRequest,
        *,
        direct_lease_seconds: float = 0.0,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Durably stage one POST and atomically identify transport duplicates.

        A positive direct lease marks the row as owned by the request handler.
        Replay cannot claim it until the lease expires or the handler releases
        it after HOLD/failure. This prevents normal direct forwarding and the
        background replay loop from sending the same transport concurrently.
        """

        timestamp = float(now if now is not None else _now())
        lease_seconds = max(0.0, float(direct_lease_seconds))
        initial_state = "direct" if lease_seconds > 0 else "pending"
        next_attempt_at = timestamp + lease_seconds
        headers_json = json.dumps(
            request.headers,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM held_callbacks WHERE fingerprint = ?",
                (request.fingerprint,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO held_callbacks (
                        fingerprint, method, raw_path, headers_json, body, state,
                        received_at, updated_at, next_attempt_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request.fingerprint,
                        request.method,
                        request.raw_path,
                        headers_json,
                        sqlite3.Binary(request.body),
                        initial_state,
                        timestamp,
                        timestamp,
                        next_attempt_at,
                    ),
                )
                created = True
                state = initial_state
            else:
                created = False
                state = str(row["state"])
            connection.commit()
        return {
            "created": created,
            "state": state,
            "fingerprint": request.fingerprint,
        }

    def pending(
        self,
        *,
        limit: int,
        now: float | None = None,
    ) -> list[HeldRequest]:
        timestamp = float(now if now is not None else _now())
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT fingerprint, method, raw_path, headers_json, body
                FROM held_callbacks
                WHERE state IN ('pending', 'direct') AND next_attempt_at <= ?
                ORDER BY received_at ASC
                LIMIT ?
                """,
                (timestamp, max(1, int(limit))),
            ).fetchall()

        result: list[HeldRequest] = []
        for row in rows:
            try:
                headers = json.loads(str(row["headers_json"] or "{}"))
            except (TypeError, ValueError):
                headers = {}
            if not isinstance(headers, dict):
                headers = {}
            result.append(
                HeldRequest(
                    fingerprint=str(row["fingerprint"]),
                    method=str(row["method"]),
                    raw_path=str(row["raw_path"]),
                    headers={str(k): str(v) for k, v in headers.items()},
                    body=bytes(row["body"] or b""),
                )
            )
        return result

    def mark_attempt(
        self,
        fingerprint: str,
        *,
        now: float | None = None,
    ) -> int:
        timestamp = float(now if now is not None else _now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT attempt_count
                FROM held_callbacks
                WHERE fingerprint = ?
                  AND state IN ('pending', 'direct')
                  AND next_attempt_at <= ?
                """,
                (str(fingerprint), timestamp),
            ).fetchone()
            if row is None:
                connection.commit()
                return 0
            attempt = int(row["attempt_count"] or 0) + 1
            connection.execute(
                """
                UPDATE held_callbacks
                SET state = 'replaying', attempt_count = ?,
                    last_attempt_at = ?, updated_at = ?
                WHERE fingerprint = ?
                  AND state IN ('pending', 'direct')
                  AND next_attempt_at <= ?
                """,
                (
                    attempt,
                    timestamp,
                    timestamp,
                    str(fingerprint),
                    timestamp,
                ),
            )
            connection.commit()
        return attempt

    def release_for_replay(
        self,
        fingerprint: str,
        *,
        now: float | None = None,
    ) -> bool:
        """Release a direct-owned durable row for immediate replay."""

        timestamp = float(now if now is not None else _now())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE held_callbacks
                SET state = 'pending', next_attempt_at = ?, updated_at = ?
                WHERE fingerprint = ? AND state = 'direct'
                """,
                (timestamp, timestamp, str(fingerprint)),
            )
        return cursor.rowcount == 1

    def mark_replay_failure(
        self,
        fingerprint: str,
        *,
        attempt: int,
        error: str,
        status: int | None = None,
        now: float | None = None,
    ) -> None:
        timestamp = float(now if now is not None else _now())
        backoff = min(
            30.0,
            max(1.0, float(2 ** max(0, min(int(attempt), 5) - 1))),
        )
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE held_callbacks
                SET state = 'pending', updated_at = ?, next_attempt_at = ?,
                    last_status = ?, last_error = ?
                WHERE fingerprint = ? AND state = 'replaying'
                """,
                (
                    timestamp,
                    timestamp + backoff,
                    int(status) if status is not None else None,
                    str(error or "")[:500],
                    str(fingerprint),
                ),
            )

    def mark_completed(
        self,
        fingerprint: str,
        *,
        status: int,
        now: float | None = None,
    ) -> bool:
        """Complete only after downstream 2xx and minimize retained payload."""

        timestamp = float(now if now is not None else _now())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE held_callbacks
                SET state = 'completed',
                    raw_path = '',
                    headers_json = '{}',
                    body = X'',
                    updated_at = ?,
                    completed_at = ?,
                    last_status = ?,
                    last_error = ''
                WHERE fingerprint = ? AND state IN ('direct', 'replaying')
                """,
                (
                    timestamp,
                    timestamp,
                    int(status),
                    str(fingerprint),
                ),
            )
        return cursor.rowcount == 1

    def discard_pending(self, fingerprint: str) -> bool:
        """Drop a transport row after an explicit non-transient rejection."""

        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM held_callbacks "
                "WHERE fingerprint = ? AND state IN ('direct', 'pending')",
                (str(fingerprint),),
            )
        return cursor.rowcount == 1

    def counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS count "
                "FROM held_callbacks GROUP BY state"
            ).fetchall()
        counts = {"pending": 0, "completed": 0}
        for row in rows:
            state = str(row["state"])
            count = int(row["count"] or 0)
            if state == "completed":
                counts["completed"] += count
            else:
                counts["pending"] += count
        return counts

    def prune_completed(
        self,
        *,
        retention_seconds: float = DEFAULT_COMPLETED_RETENTION_SECONDS,
        now: float | None = None,
    ) -> int:
        timestamp = float(now if now is not None else _now())
        cutoff = timestamp - max(0.0, float(retention_seconds))
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM held_callbacks "
                "WHERE state = 'completed' AND completed_at < ?",
                (cutoff,),
            )
        return int(cursor.rowcount or 0)


class WecomHoldingBridge:
    def __init__(
        self,
        *,
        upstream_base: str,
        store: HoldingStore,
        mode_gate: HoldingModeGate,
        forward_timeout_seconds: float = DEFAULT_FORWARD_TIMEOUT_SECONDS,
        replay_interval_seconds: float = DEFAULT_REPLAY_INTERVAL_SECONDS,
        replay_batch_size: int = DEFAULT_REPLAY_BATCH_SIZE,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.upstream_base = str(upstream_base or DEFAULT_UPSTREAM).rstrip("/")
        self.store = store
        self.mode_gate = mode_gate
        self.forward_timeout_seconds = max(
            0.1,
            float(forward_timeout_seconds),
        )
        self.replay_interval_seconds = max(
            0.05,
            float(replay_interval_seconds),
        )
        self.replay_batch_size = max(1, int(replay_batch_size))
        self._http_client = http_client
        self._owns_client = http_client is None
        self._replay_task: asyncio.Task | None = None
        self._closed = False

    async def start(self) -> None:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=self.forward_timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                limits=httpx.Limits(
                    max_connections=20,
                    max_keepalive_connections=8,
                ),
            )
        self._closed = False
        if self._replay_task is None or self._replay_task.done():
            self._replay_task = asyncio.create_task(self._replay_loop())

    async def close(self) -> None:
        self._closed = True
        if self._replay_task is not None:
            self._replay_task.cancel()
            try:
                await self._replay_task
            except asyncio.CancelledError:
                pass
            self._replay_task = None
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()
        self._http_client = None

    @staticmethod
    def _is_success(status: int) -> bool:
        return 200 <= int(status) < 300

    @staticmethod
    def _is_transient_status(status: int) -> bool:
        code = int(status)
        return code >= 500 or code in _TRANSIENT_STATUSES

    async def _forward(
        self,
        *,
        method: str,
        raw_path: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> ForwardResult:
        if self._http_client is None:
            raise RuntimeError("bridge_not_started")
        response = await self._http_client.request(
            str(method).upper(),
            f"{self.upstream_base}{raw_path}",
            content=bytes(body),
            headers=_filtered_headers(headers),
            timeout=self.forward_timeout_seconds,
        )
        response_headers: dict[str, str] = {}
        content_type = response.headers.get("content-type")
        if content_type:
            response_headers["content-type"] = content_type
        return ForwardResult(
            status=int(response.status_code),
            body=bytes(response.content),
            headers=response_headers,
        )

    @staticmethod
    def _ack(*, route: str) -> BridgeResult:
        return BridgeResult(
            status=200,
            body=ACK_BODY,
            headers={"content-type": "text/plain; charset=utf-8"},
            route=route,
        )

    @staticmethod
    def _persist_failed() -> BridgeResult:
        return BridgeResult(
            status=503,
            body=b"holding persistence unavailable",
            headers={"content-type": "text/plain; charset=utf-8"},
            route="PERSIST_FAILED",
        )

    async def handle_get(
        self,
        *,
        raw_path: str,
        headers: Mapping[str, str],
    ) -> BridgeResult:
        """Verification GET is transparent and never enters durable holding."""

        try:
            response = await self._forward(
                method="GET",
                raw_path=raw_path,
                headers=headers,
                body=b"",
            )
        except httpx.TimeoutException:
            return BridgeResult(
                status=504,
                body=b"upstream timeout",
                headers={"content-type": "text/plain; charset=utf-8"},
                route="FORWARD",
            )
        except httpx.HTTPError:
            return BridgeResult(
                status=502,
                body=b"upstream unavailable",
                headers={"content-type": "text/plain; charset=utf-8"},
                route="FORWARD",
            )
        return BridgeResult(
            response.status,
            response.body,
            response.headers,
            "FORWARD",
        )

    async def handle_post(
        self,
        *,
        raw_path: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> BridgeResult:
        if len(body) > MAX_BODY_BYTES:
            return BridgeResult(
                status=413,
                body=b"payload too large",
                headers={"content-type": "text/plain; charset=utf-8"},
                route="REJECT",
            )

        query_keys = {
            key
            for key, _ in parse_qsl(
                urlsplit(raw_path).query,
                keep_blank_values=True,
            )
        }
        if not {"msg_signature", "timestamp", "nonce"}.issubset(query_keys):
            return BridgeResult(
                status=400,
                body=b"invalid callback transport",
                headers={"content-type": "text/plain; charset=utf-8"},
                route="REJECT",
            )

        held = make_held_request(
            method="POST",
            raw_path=raw_path,
            headers=headers,
            body=body,
        )

        # Stage first, even on the normal path. Without this ordering, a bridge
        # process crash while the downstream request is in flight would create
        # an unprotected receive-to-persist gap.
        try:
            staged = self.store.persist_pending(
                held,
                direct_lease_seconds=max(
                    5.0,
                    (self.forward_timeout_seconds * 2.0)
                    + self.replay_interval_seconds,
                ),
            )
        except sqlite3.Error:
            logger.exception(
                "[WecomHoldingBridge] durable stage failed fingerprint=%s",
                held.fingerprint,
            )
            return self._persist_failed()

        if not staged["created"]:
            return self._ack(route="DUPLICATE")

        if self.mode_gate.is_holding():
            try:
                self.store.release_for_replay(held.fingerprint)
            except sqlite3.Error:
                # The durable direct row still becomes replayable when its
                # lease expires; HOLD remains safe because no ACK preceded the
                # durable stage.
                logger.exception(
                    "[WecomHoldingBridge] HOLD release failed "
                    "fingerprint=%s",
                    held.fingerprint,
                )
            return self._ack(route="HOLD")

        try:
            response = await self._forward(
                method=held.method,
                raw_path=held.raw_path,
                headers=held.headers,
                body=held.body,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            try:
                self.store.release_for_replay(held.fingerprint)
            except sqlite3.Error:
                logger.exception(
                    "[WecomHoldingBridge] FALLBACK release failed "
                    "fingerprint=%s",
                    held.fingerprint,
                )
            logger.warning(
                "[WecomHoldingBridge] FALLBACK after transport failure "
                "fingerprint=%s error=%s",
                held.fingerprint,
                type(exc).__name__,
            )
            return self._ack(route="FALLBACK")

        if self._is_success(response.status):
            try:
                if not self.store.mark_completed(
                    held.fingerprint,
                    status=response.status,
                ):
                    logger.error(
                        "[WecomHoldingBridge] downstream succeeded but "
                        "transport tombstone was not finalized fingerprint=%s",
                        held.fingerprint,
                    )
            except sqlite3.Error:
                # The durable pending copy already exists. Returning the
                # downstream success is safe: later replay may duplicate
                # transport delivery, while Gateway message-id receipts remain
                # the business idempotency authority.
                logger.exception(
                    "[WecomHoldingBridge] downstream succeeded but completion "
                    "write failed fingerprint=%s",
                    held.fingerprint,
                )
            return BridgeResult(
                response.status,
                response.body,
                response.headers,
                "FORWARD",
            )

        if self._is_transient_status(response.status):
            try:
                self.store.release_for_replay(held.fingerprint)
            except sqlite3.Error:
                logger.exception(
                    "[WecomHoldingBridge] transient-status release failed "
                    "fingerprint=%s status=%s",
                    held.fingerprint,
                    response.status,
                )
            return self._ack(route="FALLBACK")

        # A 3xx/4xx is an explicit downstream rejection, not a transport
        # outage. Preserve it transparently and do not turn it into a replay
        # backlog. It is deliberately not marked completed because only 2xx
        # is a confirmed successful delivery.
        try:
            self.store.discard_pending(held.fingerprint)
        except sqlite3.Error:
            logger.exception(
                "[WecomHoldingBridge] rejected transport cleanup failed "
                "fingerprint=%s status=%s",
                held.fingerprint,
                response.status,
            )
        return BridgeResult(
            response.status,
            response.body,
            response.headers,
            "FORWARD",
        )

    async def replay_once(self) -> dict[str, int]:
        if self.mode_gate.is_holding():
            return {"attempted": 0, "completed": 0, "failed": 0}

        attempted = 0
        completed = 0
        failed = 0

        for held in self.store.pending(limit=self.replay_batch_size):
            if self.mode_gate.is_holding():
                break

            attempt = self.store.mark_attempt(held.fingerprint)
            if attempt <= 0:
                continue
            attempted += 1

            try:
                response = await self._forward(
                    method=held.method,
                    raw_path=held.raw_path,
                    headers=held.headers,
                    body=held.body,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                failed += 1
                self.store.mark_replay_failure(
                    held.fingerprint,
                    attempt=attempt,
                    error=type(exc).__name__,
                )
                continue

            if self._is_success(response.status):
                if self.store.mark_completed(
                    held.fingerprint,
                    status=response.status,
                ):
                    completed += 1
                continue

            failed += 1
            self.store.mark_replay_failure(
                held.fingerprint,
                attempt=attempt,
                error=f"upstream_status_{response.status}",
                status=response.status,
            )

        return {
            "attempted": attempted,
            "completed": completed,
            "failed": failed,
        }

    async def _replay_loop(self) -> None:
        while not self._closed:
            try:
                await self.replay_once()
                self.store.prune_completed()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[WecomHoldingBridge] replay loop iteration failed"
                )
            await asyncio.sleep(self.replay_interval_seconds)

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "ok": True,
            "service": "wecom_holding_bridge",
            "schema_version": SCHEMA_VERSION,
            "mode": self.mode_gate.snapshot(),
            "store": self.store.counts(),
            "upstream": self.upstream_base,
        }


def create_app(
    bridge: WecomHoldingBridge,
    *,
    callback_path: str = DEFAULT_CALLBACK_PATH,
) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY_BYTES)

    async def lifecycle(_app: web.Application):
        await bridge.start()
        yield
        await bridge.close()

    async def health(_request: web.Request) -> web.Response:
        return web.json_response(bridge.health_snapshot())

    async def verify(request: web.Request) -> web.Response:
        result = await bridge.handle_get(
            raw_path=request.raw_path,
            headers=request.headers,
        )
        return web.Response(
            status=result.status,
            body=result.body,
            headers=result.headers,
        )

    async def callback(request: web.Request) -> web.Response:
        try:
            body = await request.read()
        except Exception:
            return web.Response(
                status=400,
                text="invalid request body",
            )
        result = await bridge.handle_post(
            raw_path=request.raw_path,
            headers=request.headers,
            body=body,
        )
        return web.Response(
            status=result.status,
            body=result.body,
            headers=result.headers,
        )

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/health", health)
    app.router.add_get(callback_path, verify)
    app.router.add_post(callback_path, callback)
    return app


def _default_state_root() -> Path:
    home = str(os.getenv("HERMES_HOME") or "").strip()
    if home:
        return Path(home).expanduser() / "state"
    return Path.home() / ".hermes" / "state"


def _env_float(name: str, default: float) -> float:
    raw = str(os.getenv(name) or "").strip()
    try:
        return float(raw) if raw else float(default)
    except ValueError:
        return float(default)


def _env_int(name: str, default: int) -> int:
    raw = str(os.getenv(name) or "").strip()
    try:
        return int(raw) if raw else int(default)
    except ValueError:
        return int(default)


def build_bridge_from_env() -> WecomHoldingBridge:
    state_root = _default_state_root()
    db_path = Path(
        str(os.getenv("XIAOYOU_WECOM_HOLDING_DB") or "").strip()
        or state_root / "wecom_callback_holding.sqlite3"
    )
    mode_path = Path(
        str(os.getenv("XIAOYOU_WECOM_HOLDING_MODE_FILE") or "").strip()
        or state_root / "wecom_callback_holding_mode.json"
    )
    return WecomHoldingBridge(
        upstream_base=str(
            os.getenv("XIAOYOU_WECOM_HOLDING_UPSTREAM")
            or DEFAULT_UPSTREAM
        ),
        store=HoldingStore(db_path),
        mode_gate=HoldingModeGate(mode_path),
        forward_timeout_seconds=_env_float(
            "XIAOYOU_WECOM_HOLDING_TIMEOUT_SECONDS",
            DEFAULT_FORWARD_TIMEOUT_SECONDS,
        ),
        replay_interval_seconds=_env_float(
            "XIAOYOU_WECOM_HOLDING_REPLAY_INTERVAL_SECONDS",
            DEFAULT_REPLAY_INTERVAL_SECONDS,
        ),
        replay_batch_size=_env_int(
            "XIAOYOU_WECOM_HOLDING_REPLAY_BATCH_SIZE",
            DEFAULT_REPLAY_BATCH_SIZE,
        ),
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the XiaoYou durable WeCom holding bridge."
    )
    parser.add_argument(
        "--host",
        default=str(
            os.getenv("XIAOYOU_WECOM_HOLDING_HOST")
            or DEFAULT_HOST
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_env_int(
            "XIAOYOU_WECOM_HOLDING_PORT",
            DEFAULT_PORT,
        ),
    )
    parser.add_argument(
        "--callback-path",
        default=str(
            os.getenv("WECOM_CALLBACK_PATH")
            or DEFAULT_CALLBACK_PATH
        ),
    )
    args = parser.parse_args(
        list(argv) if argv is not None else None
    )

    logging.basicConfig(
        level=os.getenv(
            "XIAOYOU_WECOM_HOLDING_LOG_LEVEL",
            "INFO",
        )
    )
    bridge = build_bridge_from_env()
    web.run_app(
        create_app(
            bridge,
            callback_path=args.callback_path,
        ),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
