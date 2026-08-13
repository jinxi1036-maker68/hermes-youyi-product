"""Privacy-preserving per-turn runtime evidence."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import threading
import time
from typing import Any
import uuid

from .store import TuoguanStore
from .write_guard import authorized_system_write


TURN_TRACE_FILE = "turn_traces.jsonl"
_LOCK = threading.RLock()
_ACTIVE: dict[str, dict[str, Any]] = {}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _digest(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:20]


def clear_turn_traces() -> None:
    with _LOCK:
        _ACTIVE.clear()


def begin_turn_trace(
    *,
    session_id: str,
    message_id: str,
    tenant_id: str,
    app_id: str,
    user_id: str,
    role: str,
    raw_text: str,
    visible_tool_count: int = 0,
) -> str:
    key = str(session_id or message_id or uuid.uuid4().hex)
    trace = {
        "trace_id": f"turn_{uuid.uuid4().hex}",
        "schema_version": 1,
        "tenant_id": str(tenant_id or ""),
        "app_id_hash": _digest(app_id),
        "actor_id_hash": _digest(user_id),
        "role": str(role or "unbound"),
        "message_id_hash": _digest(message_id),
        "message_char_count": len(str(raw_text or "")),
        "message_content_hash": _digest(raw_text),
        "visible_tool_count": max(0, int(visible_tool_count or 0)),
        "context_sources": [],
        "tool_events": [],
        "guard_events": [],
        "started_at": _now(),
        "started_monotonic_ns": time.monotonic_ns(),
    }
    with _LOCK:
        _ACTIVE[key] = trace
    return str(trace["trace_id"])


def record_context_sources(session_id: str, sources: list[str] | tuple[str, ...]) -> None:
    with _LOCK:
        trace = _ACTIVE.get(str(session_id or ""))
        if not trace:
            return
        existing = list(trace.get("context_sources") or [])
        for source in sources:
            normalized = str(source or "").strip()
            if normalized and normalized not in existing:
                existing.append(normalized)
        trace["context_sources"] = existing
        trace["context_ready_monotonic_ns"] = time.monotonic_ns()


def record_tool_event(session_id: str, *, tool_name: str, result: Any) -> None:
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except (TypeError, ValueError):
        parsed = None
    ok = bool(parsed.get("ok")) if isinstance(parsed, dict) else False
    error = str(parsed.get("error") or "") if isinstance(parsed, dict) else "unparseable_tool_result"
    data = parsed.get("data") if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict) else {}
    receipt = data.get("execution_receipt") if isinstance(data.get("execution_receipt"), dict) else {}
    with _LOCK:
        trace = _ACTIVE.get(str(session_id or ""))
        if not trace:
            return
        trace.setdefault("tool_events", []).append({
            "tool": str(tool_name or ""),
            "ok": ok,
            "error": error or None,
            "writeback_verified": bool(data.get("writeback_verified") or receipt.get("writeback_verified")),
            "delivery_status": str(data.get("delivery_status") or receipt.get("delivery_status") or "") or None,
        })


def record_guard_event(session_id: str, *, guard: str, result: str) -> None:
    with _LOCK:
        trace = _ACTIVE.get(str(session_id or ""))
        if trace:
            trace.setdefault("guard_events", []).append({"guard": str(guard), "result": str(result)})


def finalize_turn_trace(
    store: TuoguanStore,
    *,
    session_id: str,
    delivery_status: str = "",
    final_reply: str = "",
) -> dict[str, Any] | None:
    with _LOCK:
        trace = _ACTIVE.pop(str(session_id or ""), None)
    if not trace:
        return None
    finished_ns = time.monotonic_ns()
    context_ns = int(trace.pop("context_ready_monotonic_ns", 0) or 0)
    started_ns = int(trace.pop("started_monotonic_ns", finished_ns) or finished_ns)
    trace.update({
        "completed_at": _now(),
        "context_build_ms": round((context_ns - started_ns) / 1_000_000, 3) if context_ns else None,
        "total_turn_ms": round((finished_ns - started_ns) / 1_000_000, 3),
        "reply_char_count": len(str(final_reply or "")),
        "delivery_status": str(delivery_status or "unknown"),
    })
    # The trace never contains message text, reply text, names, tool arguments,
    # tool result payloads, student data, or contact details.
    with authorized_system_write(
        store.data_dir,
        job_name="turn_trace_observer",
        allowed_files={TURN_TRACE_FILE},
    ):
        verified = store.append_jsonl_verified(TURN_TRACE_FILE, trace)
    result = deepcopy(trace)
    result["writeback_verified"] = bool(verified)
    return result

