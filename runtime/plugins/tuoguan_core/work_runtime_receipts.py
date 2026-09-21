"""Passive public Tool-lifecycle receipt relay for the Work Runtime.

This module is deliberately an observer, not a Tool wrapper or a business
dispatcher.  Hermes emits ``post_tool_call`` through its documented plugin
hook surface.  The relay extracts only a real ``execution_receipt`` from that
already-completed Tool result and hands it to explicitly registered
certification/runtime listeners.  Listener failures are isolated from the
Tool result: they cannot rewrite a result, select another Tool, or turn a
failed business operation into success.

It is dormant until an adapter registers a listener.  Production activation is
therefore a separate capability-design decision; this file only defines the
portable public evidence boundary used by isolated certification.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable


@dataclass(frozen=True)
class PublicToolReceiptEvent:
    """A raw, post-execution receipt seen at Hermes' public hook boundary."""

    event_id: str
    observed_at: float
    session_id: str
    turn_id: str
    tool_call_id: str
    tool_name: str
    args: dict[str, Any]
    result_ok: bool | None
    result_writeback_verified: bool
    execution_receipt: dict[str, Any]


_LISTENERS: dict[str, Callable[[PublicToolReceiptEvent], None]] = {}
_LOCK = threading.RLock()
PUBLIC_RECEIPT_EVIDENCE_ENV = "XIAOYOU_WORK_RUNTIME_PUBLIC_RECEIPT_DB"


def _connect(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=2.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def _init_evidence_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS public_work_turns (
            bridge_turn_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            origin_work_id TEXT NOT NULL,
            fact_count INTEGER NOT NULL,
            state TEXT NOT NULL,
            opened_at REAL NOT NULL,
            closed_at REAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS public_work_one_open_turn_per_session
            ON public_work_turns(session_id) WHERE state = 'open';
        CREATE TABLE IF NOT EXISTS public_tool_receipt_events (
            event_id TEXT PRIMARY KEY,
            observed_at REAL NOT NULL,
            hook_session_id TEXT NOT NULL,
            hook_turn_id TEXT NOT NULL,
            tool_call_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            bridge_turn_id TEXT,
            origin_work_id TEXT,
            result_ok INTEGER,
            result_writeback_verified INTEGER NOT NULL,
            receipt_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS public_tool_receipts_by_bridge_turn
            ON public_tool_receipt_events(bridge_turn_id, observed_at);
        """
    )


def _persist_evidence(event: PublicToolReceiptEvent) -> None:
    """Persist passive hook evidence only when an adapter configured a store."""

    raw_path = str(os.environ.get(PUBLIC_RECEIPT_EVIDENCE_ENV) or "").strip()
    if not raw_path:
        return
    path = Path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(str(path))) as connection:
        _init_evidence_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            rows = connection.execute(
                "SELECT bridge_turn_id, origin_work_id, fact_count FROM public_work_turns "
                "WHERE session_id=? AND state='open'",
                (event.session_id,),
            ).fetchall()
            # A single active bridge turn is a trusted scheduling invariant.
            # The hook never chooses an origin: it copies the already-attested
            # origin only for a single-fact turn; a multi-fact write is left
            # unassigned for the bridge to fail closed.
            bridge_turn_id = None
            origin_work_id = None
            if len(rows) == 1:
                bridge_turn_id = str(rows[0]["bridge_turn_id"])
                if int(rows[0]["fact_count"]) == 1:
                    origin_work_id = str(rows[0]["origin_work_id"])
            connection.execute(
                "INSERT INTO public_tool_receipt_events("
                "event_id,observed_at,hook_session_id,hook_turn_id,tool_call_id,tool_name,"
                "bridge_turn_id,origin_work_id,result_ok,result_writeback_verified,receipt_json"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event.event_id, event.observed_at, event.session_id, event.turn_id,
                    event.tool_call_id, event.tool_name, bridge_turn_id, origin_work_id,
                    None if event.result_ok is None else int(event.result_ok),
                    int(event.result_writeback_verified),
                    json.dumps(event.execution_receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise


def register_public_tool_receipt_listener(listener: Callable[[PublicToolReceiptEvent], None]) -> str:
    """Register an explicitly owned passive listener and return its token."""

    if not callable(listener):
        raise TypeError("public_tool_receipt_listener_must_be_callable")
    token = "public-tool-receipt:" + uuid.uuid4().hex
    with _LOCK:
        _LISTENERS[token] = listener
    return token


def unregister_public_tool_receipt_listener(token: str) -> None:
    with _LOCK:
        _LISTENERS.pop(str(token), None)


def _parse_result(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return dict(parsed) if isinstance(parsed, dict) else None


def _execution_receipt(result: dict[str, Any]) -> dict[str, Any] | None:
    """Find the Tool-owned receipt without interpreting its business meaning."""

    candidates = (result, result.get("data"))
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        receipt = candidate.get("execution_receipt")
        if isinstance(receipt, dict):
            return dict(receipt)
    return None


def receipt_event_from_public_hook(**kwargs: Any) -> PublicToolReceiptEvent | None:
    """Normalize one public hook observation without changing Tool dispatch."""

    result = _parse_result(kwargs.get("result"))
    if result is None:
        return None
    receipt = _execution_receipt(result)
    if receipt is None:
        return None
    raw_args = kwargs.get("args")
    return PublicToolReceiptEvent(
        event_id="public-tool-receipt:" + uuid.uuid4().hex,
        observed_at=time.time(),
        session_id=str(kwargs.get("session_id") or ""),
        turn_id=str(kwargs.get("turn_id") or ""),
        tool_call_id=str(kwargs.get("tool_call_id") or ""),
        tool_name=str(kwargs.get("tool_name") or ""),
        args=dict(raw_args) if isinstance(raw_args, dict) else {},
        result_ok=result.get("ok") if isinstance(result.get("ok"), bool) else None,
        result_writeback_verified=bool(
            result.get("writeback_verified")
            if "writeback_verified" in result
            else (result.get("data") or {}).get("writeback_verified") if isinstance(result.get("data"), dict) else False
        ),
        execution_receipt=receipt,
    )


def observe_public_post_tool_call(**kwargs: Any) -> None:
    """Relay one public post-tool event to opt-in listeners, if it has a receipt.

    This function intentionally returns ``None``.  Hermes treats post hooks as
    observational and the relay never participates in the Tool execution path.
    """

    event = receipt_event_from_public_hook(**kwargs)
    if event is None:
        return
    try:
        _persist_evidence(event)
    except Exception:
        # Evidence persistence is a passive observer.  A local store failure
        # must not retroactively alter a completed Tool result; the Work
        # Runtime will later leave the fact pending rather than invent proof.
        pass
    with _LOCK:
        listeners = tuple(_LISTENERS.values())
    for listener in listeners:
        try:
            listener(event)
        except Exception:
            # A receipt observer is never allowed to influence Tool truth.
            continue
