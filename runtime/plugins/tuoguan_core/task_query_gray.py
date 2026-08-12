"""Single-capability model-first gray path for querying the current user's tasks."""

from __future__ import annotations

import json
import threading
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from .store import TuoguanStore
from .tenant_context import current_tenant_id


CAPABILITY = "老师本人任务查询"
TOOL_NAME = "tuoguan_query_tasks"
_MY_TASK_PHRASES = {"我的任务", "我的今日任务"}
_ALL_TASK_PHRASES = {"查看全员任务"}
_LOCK = threading.RLock()
_PENDING_BY_USER: dict[str, dict[str, Any]] = {}
_TURN_BY_SESSION: dict[str, dict[str, Any]] = {}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _compact(text: str) -> str:
    return "".join(str(text or "").split()).rstrip("。！？!?；;")


def requested_scope(text: str) -> str:
    compact = _compact(text)
    if compact in _MY_TASK_PHRASES:
        return "mine"
    if compact in _ALL_TASK_PHRASES:
        return "all"
    return ""


def _allowlist_path(store: TuoguanStore) -> Path:
    return store.data_dir / "manual_context" / "hermes_model_context_injection_allowlist_v1.json"


def capability_enabled(store: TuoguanStore) -> bool:
    path = _allowlist_path(store)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    cards = payload.get("allowed_capability_cards") if isinstance(payload, dict) else []
    return any(
        isinstance(card, dict)
        and card.get("capability") == CAPABILITY
        and card.get("status") == "confirmed"
        and card.get("pilot") == 0
        for card in (cards or [])
    )


def _append_jsonl(store: TuoguanStore, filename: str, payload: dict[str, Any]) -> None:
    path = store.data_dir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def begin_inbound(
    *,
    store: TuoguanStore,
    message_id: str,
    conversation_id: str,
    user_id: str,
    role: str,
    raw_text: str,
) -> dict[str, Any] | None:
    scope = requested_scope(raw_text)
    if not scope or not capability_enabled(store):
        return None
    if role not in {"teacher", "boss"}:
        return None
    ledger_id = f"ledger_{uuid.uuid4().hex}"
    item = {
        "ledger_id": ledger_id,
        "message_id": str(message_id or ledger_id),
        "conversation_id": str(conversation_id or user_id),
        "tenant_id": current_tenant_id(),
        "channel": "wecom_callback",
        "user_id": user_id,
        "role": role,
        "raw_text": raw_text,
        "entered_model": False,
        "model_intent": "query_my_tasks" if scope == "mine" else "query_all_tasks",
        "model_confidence": 1.0,
        "used_manual_cards": [CAPABILITY],
        "used_tool_registry_entry": TOOL_NAME,
        "requested_scope": scope,
        "effective_scope": None,
        "tool_calls": [],
        "tool_results": [],
        "writeback_verified": None,
        "legacy_handler_intercepted": False,
        "guard_result": "pending",
        "audit_event_ids": [],
        "final_reply": "",
        "render_verified": False,
        "created_at": _now(),
    }
    with _LOCK:
        _PENDING_BY_USER[user_id] = item
    return deepcopy(item)


def inject_model_context(
    *,
    store: TuoguanStore,
    session_id: str,
    sender_id: str,
    user_message: str,
) -> dict[str, str] | None:
    with _LOCK:
        item = _PENDING_BY_USER.get(str(sender_id or ""))
        if not item or _compact(item.get("raw_text", "")) != _compact(user_message):
            return None
        item["entered_model"] = True
        item["guard_result"] = "allowed"
        item["session_id"] = str(session_id or "")
        _TURN_BY_SESSION[str(session_id or "")] = item
    scope = item["requested_scope"]
    role = item["role"]
    user_id = item["user_id"]
    return {
        "context": (
            "【优益灰度能力卡：老师本人任务查询】\n"
            "本轮必须调用可信工具 tuoguan_query_tasks，不得凭聊天历史回答。\n"
            f"可信角色：{role}；可信 user_id：{user_id}；用户请求范围：{scope}。\n"
            "调用参数必须包含上述 user_id 和 scope。系统会校验权限并返回 effective_scope、result_count、"
            "task_summaries 和 rendered_text。不要自行添加、删除、重算或改写任务；"
            "最终对外内容将由系统按工具结果确定性渲染。"
        )
    }


def observe_tool_result(
    *,
    session_id: str,
    tool_name: str,
    args: Any,
    result: Any,
) -> None:
    if tool_name != TOOL_NAME:
        return
    with _LOCK:
        item = _TURN_BY_SESSION.get(str(session_id or ""))
        if not item:
            return
        try:
            parsed = json.loads(result) if isinstance(result, str) else deepcopy(result)
        except (TypeError, ValueError):
            parsed = {"ok": False, "error": "unparseable_tool_result"}
        item["tool_calls"].append({"tool": TOOL_NAME, "args": deepcopy(args or {})})
        item["tool_results"].append(parsed)
        data = parsed.get("data", {}) if isinstance(parsed, dict) else {}
        item["effective_scope"] = data.get("effective_scope")
        item["result_count"] = data.get("result_count", data.get("count"))
        item["task_ids"] = list(data.get("task_ids") or [])
        item["task_summaries"] = list(data.get("task_summaries") or [])
        item["data_version"] = data.get("data_version")


def _safe_failure(item: dict[str, Any]) -> str:
    if item.get("requested_scope") == "all" and item.get("role") == "teacher":
        return "你是老师，只能查看本人任务。当前未能完成可靠查询，请稍后再试。"
    return "当前未能完成可靠的本人任务查询，请稍后再试。"


def transform_final_response(*, store: TuoguanStore, session_id: str, response_text: str) -> str | None:
    with _LOCK:
        item = _TURN_BY_SESSION.get(str(session_id or ""))
        if not item:
            return None
        result = item["tool_results"][-1] if item.get("tool_results") else {}
        data = result.get("data", {}) if isinstance(result, dict) else {}
        rendered = str(data.get("rendered_text") or "")
        expected = int(data.get("rendered_count") or 0)
        actual = len(data.get("task_summaries") or [])
        verified = bool(result.get("ok") and rendered and expected == actual)
        if not verified:
            rendered = _safe_failure(item)
        item["rendered_count"] = expected if verified else 0
        item["final_reply_task_line_count"] = actual if verified else 0
        item["render_verified"] = verified
        item["writeback_verified"] = True  # Read-only query verification, no business write.
        item["final_reply"] = rendered
        item["guard_result"] = "allowed" if verified else "failed_safe"
        audit_id = f"audit_{uuid.uuid4().hex}"
        item["audit_event_ids"] = [audit_id]
        item["completed_at"] = _now()
        ledger_payload = deepcopy(item)
        audit_payload = {
            "audit_event_id": audit_id,
            "ledger_id": item["ledger_id"],
            "tenant_id": current_tenant_id(),
            "channel": "wecom_callback",
            "actor_user_id": item["user_id"],
            "actor_role": item["role"],
            "action": "query_my_tasks",
            "requested_scope": item["requested_scope"],
            "effective_scope": item.get("effective_scope"),
            "result": "success" if verified else "failed_safe",
            "result_count": item.get("result_count"),
            "render_verified": verified,
            "writeback_verified": True,
            "created_at": _now(),
        }
        _append_jsonl(store, "reply_ledger.jsonl", ledger_payload)
        _append_jsonl(store, "business_action_audit.jsonl", audit_payload)
        _TURN_BY_SESSION.pop(str(session_id or ""), None)
        _PENDING_BY_USER.pop(item["user_id"], None)
        return rendered


def clear_runtime_state() -> None:
    """Test helper; does not touch persisted ledgers."""
    with _LOCK:
        _PENDING_BY_USER.clear()
        _TURN_BY_SESSION.clear()
