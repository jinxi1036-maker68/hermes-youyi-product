"""Per-turn resource boundaries for the model-led Xiaoyou runtime."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Any


DEFAULT_TOOL_CALL_BUDGET = 12
MAX_CORRECTABLE_FAILURES = 2
_CORRECTABLE_ERRORS = {
    "unknown_facade_operation",
    "invalid_facade_arguments",
    "ambiguous_facade_arguments",
    "missing_required_arguments",
    "unsupported_arguments",
    "wrong_tool_for_cancel_intent",
}
_LOCK = threading.RLock()
_TURN_BUDGETS: dict[str, dict[str, Any]] = {}


def _configured_budget() -> int:
    raw = str(os.getenv("HERMES_TUOGUAN_MAX_TOOL_CALLS_PER_TURN", "") or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_TOOL_CALL_BUDGET
    except (TypeError, ValueError):
        value = DEFAULT_TOOL_CALL_BUDGET
    return min(30, max(3, value))


def _fingerprint(tool_name: str, args: Any) -> str:
    try:
        rendered = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        rendered = repr(args)
    return hashlib.sha256(f"{tool_name}\n{rendered}".encode("utf-8")).hexdigest()


def reset_turn_tool_budget(session_id: str) -> None:
    key = str(session_id or "").strip()
    if not key:
        return
    with _LOCK:
        _TURN_BUDGETS[key] = {
            "count": 0,
            "fingerprints": set(),
            "correctable_failure_count": 0,
            "last_error": "",
        }


def clear_turn_tool_budget(session_id: str) -> None:
    with _LOCK:
        _TURN_BUDGETS.pop(str(session_id or "").strip(), None)


def guard_turn_tool_call(
    session_id: str,
    *,
    tool_name: str,
    args: Any,
) -> dict[str, str] | None:
    """Block exact repeats and runaway tool loops without choosing business intent."""

    key = str(session_id or "").strip()
    name = str(tool_name or "").strip()
    if not key or not name.startswith("tuoguan_"):
        return None
    with _LOCK:
        state = _TURN_BUDGETS.get(key)
        if state is None:
            return None
        if int(state.get("correctable_failure_count") or 0) >= MAX_CORRECTABLE_FAILURES:
            return {
                "action": "block",
                "message": (
                    "本轮业务能力已经连续两次收到无效工具或参数。请停止继续试工具，"
                    "只根据已验证结果回答；若仍缺事实，只说明一个关键缺口。"
                ),
                "reason": "corrective_tool_retry_exhausted",
            }
        fingerprint = _fingerprint(name, args)
        fingerprints = state["fingerprints"]
        if fingerprint in fingerprints:
            return {
                "action": "block",
                "message": (
                    "本轮已经用相同参数调用过这个业务能力。请复用已有结果；"
                    "若结果不足，只说明一个真实缺口，不要重复查询。"
                ),
                "reason": "duplicate_tool_call_in_turn",
            }
        budget = _configured_budget()
        if int(state["count"]) >= budget:
            return {
                "action": "block",
                "message": (
                    f"本轮已达到 {budget} 次业务工具资源上限。请根据已有证据给出当前结论；"
                    "仍缺事实时只说明一个关键缺口，留到下一轮继续。"
                ),
                "reason": "tool_call_budget_exhausted",
            }
        fingerprints.add(fingerprint)
        state["count"] = int(state["count"]) + 1
    return None


def observe_turn_tool_result(session_id: str, *, tool_name: str, result: Any) -> None:
    """Track contract failures so one bad call gets only one correction attempt."""

    key = str(session_id or "").strip()
    if not key:
        return
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except (TypeError, ValueError):
        parsed = {"ok": False, "error": "unparseable_tool_result"}
    error = str(parsed.get("error") or "") if isinstance(parsed, dict) else "unparseable_tool_result"
    if not error and isinstance(parsed, dict) and parsed.get("ok") is not True:
        error = "unknown_tool_failure"
    if "missing required argument" in error:
        error = "missing_required_arguments"
    with _LOCK:
        state = _TURN_BUDGETS.get(key)
        if state is None:
            return
        state["last_error"] = error
        if error in _CORRECTABLE_ERRORS:
            state["correctable_failure_count"] = int(state.get("correctable_failure_count") or 0) + 1


def turn_tool_budget_snapshot(session_id: str) -> dict[str, int]:
    with _LOCK:
        state = _TURN_BUDGETS.get(str(session_id or "").strip()) or {}
        return {
            "count": int(state.get("count") or 0),
            "budget": _configured_budget(),
            "correctable_failure_count": int(state.get("correctable_failure_count") or 0),
            "max_correctable_failures": MAX_CORRECTABLE_FAILURES,
        }
