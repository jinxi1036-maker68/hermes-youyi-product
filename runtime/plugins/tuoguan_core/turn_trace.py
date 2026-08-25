"""Privacy-preserving per-turn runtime evidence."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re
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
        "schema_version": 2,
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
        "model_events": [],
        "provider_events": [],
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
        now_ns = time.monotonic_ns()
        trace["context_ready_monotonic_ns"] = now_ns
        trace.setdefault("model_segment_started_monotonic_ns", now_ns)


def record_context_budget(
    session_id: str,
    *,
    budget_chars: int,
    rendered_chars: int,
    request_complexity: str,
    source_count: int,
) -> None:
    """Store only aggregate context size and source count, never context text."""

    with _LOCK:
        trace = _ACTIVE.get(str(session_id or ""))
        if not trace:
            return
        trace["context_budget"] = {
            "budget_chars": max(0, int(budget_chars or 0)),
            "rendered_chars": max(0, int(rendered_chars or 0)),
            "request_complexity": str(request_complexity or "unknown")[:20],
            "source_count": max(0, int(source_count or 0)),
        }


def _close_model_segment(trace: dict[str, Any], *, finished_ns: int, phase: str) -> None:
    started_ns = int(trace.pop("model_segment_started_monotonic_ns", 0) or 0)
    if not started_ns or finished_ns < started_ns:
        return
    trace.setdefault("model_events", []).append({
        "phase": phase,
        "duration_ms": round((finished_ns - started_ns) / 1_000_000, 3),
        "outcome": "completed",
    })


def _turn_class(tool_events: list[dict[str, Any]]) -> str:
    if not tool_events:
        return "simple_conversation"
    write_terms = ("submit", "create", "update", "cancel", "execute", "record", "register", "change")
    writes = [
        item for item in tool_events
        if item.get("writeback_verified") is True
        or any(term in str(item.get("tool") or "") or term in str(item.get("operation") or "") for term in write_terms)
    ]
    if len(tool_events) == 1:
        return "direct_write" if writes else "direct_read"
    return "complex"


def begin_tool_event(session_id: str, *, tool_name: str) -> None:
    with _LOCK:
        trace = _ACTIVE.get(str(session_id or ""))
        if not trace:
            return
        phase = "initial_model" if not trace.get("model_events") else "followup_model"
        _close_model_segment(trace, finished_ns=time.monotonic_ns(), phase=phase)
        inflight = trace.setdefault("tool_inflight", {})
        starts = inflight.setdefault(str(tool_name or ""), [])
        starts.append(time.monotonic_ns())


def record_tool_event(session_id: str, *, tool_name: str, result: Any) -> None:
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except (TypeError, ValueError):
        parsed = None
    ok = bool(parsed.get("ok")) if isinstance(parsed, dict) else False
    error = str(parsed.get("error") or "") if isinstance(parsed, dict) else "unparseable_tool_result"
    data = parsed.get("data") if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict) else {}
    receipt = data.get("execution_receipt") if isinstance(data.get("execution_receipt"), dict) else {}
    effective_tool_name = str(tool_name or "")
    if effective_tool_name == "tool_call" and error:
        match = re.search(r"tool_call to ['\\\"]([^'\\\"]+)['\\\"]", error)
        if match:
            effective_tool_name = match.group(1)
    with _LOCK:
        trace = _ACTIVE.get(str(session_id or ""))
        if not trace:
            return
        started_ns = 0
        inflight = trace.get("tool_inflight") if isinstance(trace.get("tool_inflight"), dict) else {}
        starts = inflight.get(effective_tool_name) if isinstance(inflight, dict) else None
        if isinstance(starts, list) and starts:
            started_ns = int(starts.pop(0) or 0)
        trace.setdefault("tool_events", []).append({
            "tool": effective_tool_name,
            "operation": str(
                (data.get("facade_operation") if isinstance(data, dict) else "")
                or (parsed.get("facade_operation") if isinstance(parsed, dict) else "")
            ) or None,
            "ok": ok,
            "error": error or None,
            "error_layer": str(receipt.get("error_layer") or "") or None,
            "writeback_verified": bool(data.get("writeback_verified") or receipt.get("writeback_verified")),
            "delivery_status": str(data.get("delivery_status") or receipt.get("delivery_status") or "") or None,
            "duration_ms": round((time.monotonic_ns() - started_ns) / 1_000_000, 3) if started_ns else None,
        })
        trace["model_segment_started_monotonic_ns"] = time.monotonic_ns()


def record_guard_event(session_id: str, *, guard: str, result: str) -> None:
    with _LOCK:
        trace = _ACTIVE.get(str(session_id or ""))
        if trace:
            trace.setdefault("guard_events", []).append({"guard": str(guard), "result": str(result)})


def record_tool_result_projection(
    session_id: str,
    *,
    tool_name: str,
    original_chars: int,
    projected_chars: int,
) -> None:
    """Record aggregate model-context reduction without retaining tool data."""

    with _LOCK:
        trace = _ACTIVE.get(str(session_id or ""))
        if not trace:
            return
        trace.setdefault("tool_result_projections", []).append({
            "tool": str(tool_name or "")[:120],
            "original_chars": max(0, int(original_chars or 0)),
            "projected_chars": max(0, int(projected_chars or 0)),
        })


def record_response_deduplication(session_id: str, *, removed_chars: int) -> None:
    """Keep a privacy-preserving signal when the final reply was de-duplicated."""

    if int(removed_chars or 0) <= 0:
        return
    with _LOCK:
        trace = _ACTIVE.get(str(session_id or ""))
        if trace:
            trace["response_duplicate_removed_chars"] = max(
                int(trace.get("response_duplicate_removed_chars") or 0),
                int(removed_chars),
            )


def record_provider_event(
    session_id: str,
    *,
    provider: str,
    model: str,
    outcome: str,
    error_class: str,
    circuit_state: str,
    network_egress: str = "",
) -> None:
    """Persist only provider timing/state labels, never URLs, keys or content."""

    with _LOCK:
        trace = _ACTIVE.get(str(session_id or ""))
        if not trace:
            return
        now_ns = time.monotonic_ns()
        events = trace.setdefault("provider_events", [])
        event = {
            "provider": str(provider or "")[:80],
            "model": str(model or "")[:120],
            "outcome": str(outcome or "")[:80],
            "error_class": str(error_class or "")[:120] or None,
            "circuit_state": str(circuit_state or "")[:40] or None,
            "network_egress": str(network_egress or "")[:40] or None,
        }
        if str(outcome) == "request_started":
            event["started_monotonic_ns"] = now_ns
        else:
            for prior in reversed(events):
                if (
                    prior.get("outcome") == "request_started"
                    and prior.get("provider") == event["provider"]
                    and prior.get("model") == event["model"]
                    and "duration_ms" not in prior
                ):
                    started_ns = int(prior.get("started_monotonic_ns") or 0)
                    if started_ns:
                        event["duration_ms"] = round((now_ns - started_ns) / 1_000_000, 3)
                    break
        events.append(event)


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
    phase = "final_model" if trace.get("tool_events") else "initial_model"
    _close_model_segment(trace, finished_ns=finished_ns, phase=phase)
    inflight = trace.pop("tool_inflight", {})
    if isinstance(inflight, dict):
        for tool_name, starts in inflight.items():
            for started_ns in starts if isinstance(starts, list) else []:
                trace.setdefault("tool_events", []).append({
                    "tool": str(tool_name or ""),
                    "operation": None,
                    "ok": False,
                    "error": "tool_completion_missing",
                    "writeback_verified": False,
                    "delivery_status": None,
                    "duration_ms": round((finished_ns - int(started_ns or finished_ns)) / 1_000_000, 3),
                })
    context_ns = int(trace.pop("context_ready_monotonic_ns", 0) or 0)
    started_ns = int(trace.pop("started_monotonic_ns", finished_ns) or finished_ns)
    model_events = [item for item in (trace.get("model_events") or []) if isinstance(item, dict)]
    tool_events = [item for item in (trace.get("tool_events") or []) if isinstance(item, dict)]
    guard_events = [item for item in (trace.get("guard_events") or []) if isinstance(item, dict)]
    corrective_errors = {
        "unknown_facade_operation", "invalid_facade_arguments", "ambiguous_facade_arguments",
        "missing_required_arguments", "unsupported_arguments", "wrong_tool_for_cancel_intent",
    }
    corrective_failure_count = sum(
        1 for item in tool_events
        if str(item.get("error") or "") in corrective_errors
        or "missing required argument" in str(item.get("error") or "")
    )
    failure_type = ""
    if not str(final_reply or "").strip():
        failure_type = "empty_model_reply"
    elif "超时或中断" in str(final_reply) or "连接中断" in str(final_reply):
        failure_type = "provider_timeout_or_interruption"
    elif any(str(item.get("error") or "") == "tool_completion_missing" for item in tool_events):
        failure_type = "tool_completion_missing"
    elif any(str(item.get("guard") or "") == "corrective_tool_retry_exhausted" for item in guard_events):
        failure_type = "tool_contract_retry_exhausted"
    trace.update({
        "completed_at": _now(),
        "context_build_ms": round((context_ns - started_ns) / 1_000_000, 3) if context_ns else None,
        "total_turn_ms": round((finished_ns - started_ns) / 1_000_000, 3),
        "reply_char_count": len(str(final_reply or "")),
        "delivery_status": str(delivery_status or "unknown"),
        "model_segment_count": len(model_events),
        "model_total_ms": round(sum(float(item.get("duration_ms") or 0.0) for item in model_events), 3),
        "corrective_tool_failure_count": corrective_failure_count,
        "corrective_tool_retry_count": max(0, corrective_failure_count - 1),
        "turn_class": _turn_class(tool_events),
        "final_outcome": "failed" if failure_type else "completed",
        "failure_type": failure_type,
    })
    for event in trace.get("provider_events") or []:
        if isinstance(event, dict):
            event.pop("started_monotonic_ns", None)
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
