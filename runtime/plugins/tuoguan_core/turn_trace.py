"""Privacy-preserving per-turn runtime evidence."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re
import threading
import time
from typing import Any, Callable
import uuid

from .store import TuoguanStore
from .write_guard import authorized_system_write


TURN_TRACE_FILE = "turn_traces.jsonl"
_LOCK = threading.RLock()
_ACTIVE: dict[str, dict[str, Any]] = {}
_SESSION_BY_MESSAGE_ID: dict[str, str] = {}
_PROGRESS_LISTENERS: dict[str, tuple[str, Callable[[dict[str, Any]], None]]] = {}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _digest(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:20]


def clear_turn_traces() -> None:
    with _LOCK:
        _ACTIVE.clear()
        _SESSION_BY_MESSAGE_ID.clear()
        _PROGRESS_LISTENERS.clear()


def subscribe_turn_progress(session_id: str, listener: Callable[[dict[str, Any]], None]) -> str:
    """Subscribe a transient local transport to factual lifecycle events.

    The callback is deliberately not persisted and receives no message text,
    Tool args/results, model output, or business object.  It is an observer of
    Hermes lifecycle facts, never an instruction channel into the Agent Loop.
    """

    if not callable(listener):
        raise TypeError("turn_progress_listener_must_be_callable")
    with _LOCK:
        trace = _ACTIVE.get(str(session_id or ""))
        if not trace:
            raise KeyError("turn_progress_trace_not_active")
        token = f"progress_{uuid.uuid4().hex}"
        _PROGRESS_LISTENERS[token] = (str(trace.get("trace_id") or ""), listener)
        return token


def unsubscribe_turn_progress(token: str) -> None:
    with _LOCK:
        _PROGRESS_LISTENERS.pop(str(token or ""), None)


def record_progress_event(
    session_id: str,
    *,
    kind: str,
    source: str,
    tool_name: str = "",
    outcome: str = "",
) -> None:
    """Record and publish a safe real-time lifecycle fact for one turn."""

    event = {
        "kind": str(kind or "")[:80],
        "source": str(source or "")[:40],
        "at": _now(),
    }
    if tool_name:
        event["tool_name"] = str(tool_name)[:120]
    if outcome:
        event["outcome"] = str(outcome)[:80]
    listeners: list[Callable[[dict[str, Any]], None]] = []
    with _LOCK:
        trace = _ACTIVE.get(str(session_id or ""))
        if not trace:
            return
        started_ns = int(trace.get("started_monotonic_ns") or 0)
        if started_ns:
            event["elapsed_ms"] = round((time.monotonic_ns() - started_ns) / 1_000_000, 3)
        trace.setdefault("progress_events", []).append(deepcopy(event))
        trace_id = str(trace.get("trace_id") or "")
        listeners = [
            callback for registered_trace_id, callback in _PROGRESS_LISTENERS.values()
            if registered_trace_id == trace_id
        ]
    for listener in listeners:
        try:
            listener(deepcopy(event))
        except Exception:
            # A disconnected UI cannot change business execution or tracing.
            continue


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
    # A trusted transport may open the trace before the Agent Loop reaches the
    # ``pre_llm_call`` hook.  This is essential for provider failures that
    # happen during the first model request: keep the original trace rather
    # than replacing it when the normal hook later supplies the same turn.
    # This records no extra message content and makes no business decision.
    with _LOCK:
        existing = _ACTIVE.get(key)
        if existing is not None:
            existing["visible_tool_count"] = max(
                int(existing.get("visible_tool_count") or 0),
                max(0, int(visible_tool_count or 0)),
            )
            if str(message_id or "").strip():
                _SESSION_BY_MESSAGE_ID[str(message_id).strip()] = key
            return str(existing.get("trace_id") or "")
        # Gateway creates a durable Hermes session ID after the Robot adapter
        # has already authenticated the inbound transport.  Both layers carry
        # the same trusted operation/message ID.  Alias that earlier transport
        # trace instead of producing a second, empty audit record for one
        # model turn.  This is correlation only: no message text, model choice
        # or business state is changed.
        mapped_key = _SESSION_BY_MESSAGE_ID.get(str(message_id or "").strip(), "")
        existing = _ACTIVE.get(mapped_key)
        if existing is not None:
            _ACTIVE[key] = existing
            _SESSION_BY_MESSAGE_ID[str(message_id).strip()] = key
            existing["session_id_hash"] = _digest(key)
            existing["visible_tool_count"] = max(
                int(existing.get("visible_tool_count") or 0),
                max(0, int(visible_tool_count or 0)),
            )
            return str(existing.get("trace_id") or "")
    trace = {
        "trace_id": f"turn_{uuid.uuid4().hex}",
        "schema_version": 2,
        # Session correlation is needed for multi-turn audit, but must remain
        # non-reversible in the exported Step B trace.
        "session_id_hash": _digest(key),
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
        if str(message_id or "").strip():
            _SESSION_BY_MESSAGE_ID[str(message_id).strip()] = key
    return str(trace["trace_id"])


def resolve_trace_session(session_id: str = "", message_id: str = "") -> str:
    """Resolve a Hermes lifecycle hook back to its active trace.

    Hermes exposes both session and turn/message IDs across lifecycle hooks;
    some provider-error paths only carry the latter.  This index makes failed
    model calls auditable without retaining message content.
    """

    with _LOCK:
        direct = str(session_id or "").strip()
        if direct and direct in _ACTIVE:
            return direct
        mapped = _SESSION_BY_MESSAGE_ID.get(str(message_id or "").strip(), "")
        return mapped if mapped in _ACTIVE else direct


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


def record_context_selection(session_id: str, *, source: str, identifiers: list[str] | tuple[str, ...]) -> None:
    """Attach privacy-safe selected-context evidence to the active turn.

    The raw ledger IDs stay only in the in-memory active trace so the reply
    observer can make an exact application record.  The durable trace retains
    counts and hashes only; no lesson text, user data, or raw business IDs are
    exported.
    """

    label = str(source or "").strip()[:80]
    if not label:
        return
    selected = [str(item or "").strip() for item in identifiers if str(item or "").strip()][:12]
    with _LOCK:
        trace = _ACTIVE.get(str(session_id or ""))
        if not trace:
            return
        raw = trace.setdefault("_context_selection_ids", {})
        if isinstance(raw, dict):
            raw[label] = selected
        durable = trace.setdefault("context_selections", {})
        if isinstance(durable, dict):
            durable[label] = {
                "selected_count": len(selected),
                "identifier_hashes": [_digest(item) for item in selected],
            }


def context_selection_ids(session_id: str, *, source: str) -> list[str]:
    """Return the active turn's raw selected IDs for an internal observer."""

    with _LOCK:
        trace = _ACTIVE.get(str(session_id or ""))
        if not trace:
            return []
        raw = trace.get("_context_selection_ids")
        values = raw.get(str(source or ""), []) if isinstance(raw, dict) else []
        return [str(item or "").strip() for item in values if str(item or "").strip()]


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


def _sanitize_tool_args(args: Any) -> dict[str, Any]:
    """Keep tool-selection evidence without persisting business text or PII.

    The Robot Step B acceptance trace must show that the Hermes model selected
    a particular existing tool and supplied a structurally valid call.  It must
    not become a second copy of students' names or the teacher's free-text
    observation.  Enumerated, non-sensitive control values are retained;
    every other scalar is represented by type/length/digest only.
    """

    if not isinstance(args, dict):
        return {"kind": type(args).__name__, "keys": []}

    safe_literals = {
        "scope", "status", "level", "query_scope", "query_type",
        "report_type", "reason_type", "purpose", "decision",
    }
    normalized: dict[str, Any] = {}
    for raw_key, value in sorted(args.items(), key=lambda item: str(item[0])):
        key = str(raw_key)[:80]
        if key in safe_literals and isinstance(value, (str, int, float, bool)):
            normalized[key] = {"literal": value}
            continue
        if value is None:
            normalized[key] = {"kind": "null"}
            continue
        if isinstance(value, bool):
            normalized[key] = {"kind": "bool", "value": value}
            continue
        if isinstance(value, (int, float)):
            normalized[key] = {"kind": type(value).__name__, "value": value}
            continue
        if isinstance(value, str):
            normalized[key] = {
                "kind": "str",
                "char_count": len(value),
                "sha256_20": _digest(value),
            }
            continue
        try:
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            rendered = repr(value)
        normalized[key] = {
            "kind": type(value).__name__,
            "char_count": len(rendered),
            "sha256_20": _digest(rendered),
        }
    return normalized


def record_tool_event(session_id: str, *, tool_name: str, result: Any, args: Any = None) -> None:
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except (TypeError, ValueError):
        parsed = None
    # Hermes' own tool-discovery handlers return framework objects rather than
    # the JSON envelope used by business tools.  They logged successfully as
    # completed calls in the Agent Loop, so a trace parser must not classify a
    # successful discovery result as a failed business operation merely because
    # it is not JSON.  This preserves the original result and affects audit
    # classification only; no call, tool visibility or response is changed.
    ok = bool(parsed.get("ok")) if isinstance(parsed, dict) else False
    error = str(parsed.get("error") or "") if isinstance(parsed, dict) else "unparseable_tool_result"
    data = parsed.get("data") if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict) else {}
    receipt = data.get("execution_receipt") if isinstance(data.get("execution_receipt"), dict) else {}
    # Retain only public receipt state needed to prove a verified write.  The
    # durable receipt itself stays in the repository; the Step B trace must
    # not copy business data, raw operation IDs, student details or text.
    receipt_metadata = {}
    if receipt:
        receipt_metadata = {
            "status": str(receipt.get("status") or ""),
            "operation_id_hash": _digest(str(receipt.get("operation_id") or "")),
            "idempotency_result": str(receipt.get("idempotency_result") or ""),
            "writeback_verified": bool(receipt.get("writeback_verified")),
        }
    effective_tool_name = str(tool_name or "")
    if effective_tool_name == "tool_call" and error:
        match = re.search(r"tool_call to ['\\\"]([^'\\\"]+)['\\\"]", error)
        if match:
            effective_tool_name = match.group(1)
    discovery_tool = effective_tool_name in {"tool_describe", "tool_search", "skill_view", "skills_list"}
    framework_failure_envelope = isinstance(parsed, dict) and ("ok" in parsed or bool(parsed.get("error")))
    if discovery_tool and not framework_failure_envelope:
        # See the comment above: these completion payloads are framework
        # objects, so parse failure is not a business-tool failure.
        ok = True
        error = ""
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
            "args": _sanitize_tool_args(args),
            "operation": (
                (data.get("facade_operation") if isinstance(data, dict) else "")
                or (parsed.get("facade_operation") if isinstance(parsed, dict) else "")
            ) or None,
            "ok": ok,
            "error": error or None,
            "error_layer": str(receipt.get("error_layer") or "") or None,
            "writeback_verified": bool(data.get("writeback_verified") or receipt.get("writeback_verified")),
            "execution_receipt": receipt_metadata or None,
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
    attempt_metadata: dict[str, Any] | None = None,
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
        # Only aggregate public-hook metadata is retained; never text, URLs,
        # request payloads, response payloads, identities, or credentials.
        metadata = attempt_metadata if isinstance(attempt_metadata, dict) else {}
        for field in (
            "api_call_count", "approx_input_tokens", "request_char_count",
            "max_tokens", "api_duration_ms", "assistant_content_chars",
            "assistant_tool_call_count", "retry_count", "max_retries",
        ):
            value = metadata.get(field)
            if not isinstance(value, bool) and isinstance(value, (int, float)):
                event[field] = value
        for field in ("finish_reason", "status_code", "api_mode"):
            value = str(metadata.get(field) or "").strip()
            if value:
                event[field] = value[:80]
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
    terminal_failure_type: str = "",
) -> dict[str, Any] | None:
    with _LOCK:
        trace = _ACTIVE.get(str(session_id or ""))
        if trace:
            aliases = [key for key, item in _ACTIVE.items() if item is trace]
            for key in aliases:
                _ACTIVE.pop(key, None)
            for message_id, mapped_session in list(_SESSION_BY_MESSAGE_ID.items()):
                if mapped_session in aliases:
                    _SESSION_BY_MESSAGE_ID.pop(message_id, None)
    if not trace:
        return None
    with _LOCK:
        trace_id = str(trace.get("trace_id") or "")
        for token, (registered_trace_id, _listener) in list(_PROGRESS_LISTENERS.items()):
            if registered_trace_id == trace_id:
                _PROGRESS_LISTENERS.pop(token, None)
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
    trace.pop("_context_selection_ids", None)
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
    if str(terminal_failure_type or "").strip():
        failure_type = str(terminal_failure_type).strip()[:120]
    else:
        # Public lifecycle hooks in some Hermes v0.20 paths use a provider
        # correlation key that is not the final gateway/session key.  The
        # trace itself is the durable source of truth: an unrecovered provider
        # failure is terminal unless a later public provider success is
        # recorded for this same turn.  This changes audit classification only
        # — it never retries, replaces a model, or decides a business action.
        provider_events = [item for item in trace.get("provider_events") or [] if isinstance(item, dict)]
        last_failure_index = max(
            (index for index, item in enumerate(provider_events) if str(item.get("outcome") or "") == "request_failed"),
            default=-1,
        )
        recovered_after_failure = any(
            str(item.get("outcome") or "") == "request_succeeded"
            for item in provider_events[last_failure_index + 1:]
        ) if last_failure_index >= 0 else False
        if last_failure_index >= 0 and not recovered_after_failure:
            error_class = str(provider_events[last_failure_index].get("error_class") or "provider_error")
            failure_type = f"provider_{error_class.lower()[:80]}"
    if not failure_type and not str(final_reply or "").strip():
        failure_type = "empty_model_reply"
    elif not failure_type and any(str(item.get("error") or "") == "tool_completion_missing" for item in tool_events):
        failure_type = "tool_completion_missing"
    elif not failure_type and any(str(item.get("guard") or "") == "corrective_tool_retry_exhausted" for item in guard_events):
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
