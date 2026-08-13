"""Evidence-only active work context for natural short-reply continuation."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import json
from typing import Any

from .digital_employee_state import query_attention_threads, query_relationship_touch_candidates
from .models import UserIdentity
from .social_market_research import query_social_market_research
from .proactive_work import query_goal_actions
from .store import TuoguanStore


_OPEN_TASK_STATUSES = {"pending", "active", "waiting_confirmation", "in_progress", "blocked"}
_RECENT_OUTBOUND_TYPES = {
    "autonomous_owner_attention",
    "relationship_touch",
    "external_learning_report",
}


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed.astimezone()


def _time_value(value: Any) -> float:
    parsed = _parse_time(value)
    return parsed.timestamp() if parsed is not None else 0.0


def _read_jsonl(store: TuoguanStore, filename: str) -> list[dict[str, Any]]:
    path = store.path_for(filename)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _identity_ids(identity: UserIdentity) -> set[str]:
    return {
        value
        for value in {
            str(identity.canonical_user_id or ""),
            str(identity.platform_user_id or ""),
        }
        if value
    }


def _target_matches(row: dict[str, Any], identity: UserIdentity) -> bool:
    ids = _identity_ids(identity)
    targets = {
        str(row.get("target_user_id") or ""),
        str(row.get("recipient_user_id") or ""),
        str(row.get("to_user_id") or ""),
        str(row.get("touser") or ""),
        str(row.get("user_id") or ""),
    }
    return bool(ids & targets)


def _item_time(row: dict[str, Any]) -> str:
    return str(
        row.get("sent_at")
        or row.get("observed_at")
        or row.get("last_attempt_at")
        or row.get("queued_at")
        or row.get("updated_at")
        or row.get("created_at")
        or row.get("completed_at")
        or ""
    )


def _time_is_at_or_after(value: Any, cutoff: datetime) -> bool:
    parsed = _parse_time(value)
    return bool(parsed is not None and parsed >= cutoff)


def _for_user(mapping: Any, identity: UserIdentity) -> dict[str, Any]:
    if not isinstance(mapping, dict):
        return {}
    keys = (
        str(identity.canonical_user_id or ""),
        str(identity.platform_user_id or ""),
        f"wecom_callback:{identity.canonical_user_id}",
        f"wecom_callback:{identity.platform_user_id}",
    )
    for key in keys:
        item = mapping.get(key)
        if isinstance(item, dict):
            return deepcopy(item)
    return {}


def _task_for_context(store: TuoguanStore, identity: UserIdentity) -> dict[str, Any]:
    tasks = [item for item in store.load_tasks() if isinstance(item, dict)]
    active_context = _for_user(store.read_json("active_task_context.json", {}), identity)
    focus = _for_user(store.read_json("model_focus.json", {}), identity)
    preferred_ids = [str(active_context.get("task_id") or ""), str(focus.get("task_id") or "")]
    for task_id in preferred_ids:
        if not task_id:
            continue
        matched = next(
            (
                item
                for item in tasks
                if str(item.get("id") or "") == task_id
                and str(item.get("status") or "") in _OPEN_TASK_STATUSES
            ),
            None,
        )
        if matched:
            return deepcopy(matched)
    user_ids = {str(identity.canonical_user_id or ""), str(identity.platform_user_id or "")}
    visible = [
        item
        for item in tasks
        if str(item.get("status") or "") in _OPEN_TASK_STATUSES
        and (
            str(item.get("assignee_userid") or "") in user_ids
            or str(item.get("created_by_userid") or item.get("creator_userid") or "") in user_ids
        )
    ]
    visible.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    return deepcopy(visible[0]) if visible else {}


def _recent_outbound_contexts(store: TuoguanStore, identity: UserIdentity, now: datetime, maximum: int) -> list[dict[str, Any]]:
    outbox = store.read_json("notification_outbox.json", [])
    if not isinstance(outbox, list):
        return []
    cutoff = now - timedelta(hours=3)
    rows: list[dict[str, Any]] = []
    for row in outbox:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "") not in {"sent", "sending", "pending", "retry_pending", "result_unknown"}:
            continue
        notification_type = str(row.get("notification_type") or "")
        if notification_type not in _RECENT_OUTBOUND_TYPES:
            continue
        if not _target_matches(row, identity):
            continue
        when = _parse_time(_item_time(row))
        if when is None or when < cutoff:
            continue
        rows.append(row)
    rows.sort(key=lambda item: _time_value(_item_time(item)), reverse=True)
    return [
        {
            "context_type": "recent_outbound",
            "context_id": str(row.get("id") or row.get("notification_id") or ""),
            "summary": str(row.get("summary") or row.get("content") or row.get("action") or "")[:240],
            "status": str(row.get("status") or ""),
            "updated_at": _item_time(row),
            "evidence_source": "notification_outbox.json",
            "notification_type": str(row.get("notification_type") or ""),
        }
        for row in rows[:maximum]
    ]


def _latest_daily_report_context(store: TuoguanStore, identity: UserIdentity, now: datetime) -> dict[str, Any]:
    if identity.role not in {"boss", "manager"}:
        return {}
    cutoff = now - timedelta(hours=24)
    outbox = store.read_json("notification_outbox.json", [])
    outbox_rows = outbox if isinstance(outbox, list) else []
    rows = [
        row for row in outbox_rows
        if isinstance(row, dict)
        and str(row.get("notification_type") or "") == "autonomous_daily_report"
        and _target_matches(row, identity)
        and _time_is_at_or_after(_item_time(row), cutoff)
    ]
    rows.extend(
        row for row in _read_jsonl(store, "daily_report_runs.jsonl")
        if str(row.get("record_type") or "") == "daily_report_delivery_status"
        and _time_is_at_or_after(_item_time(row), cutoff)
    )
    if not rows:
        return {}
    rows.sort(key=lambda item: _time_value(_item_time(item)), reverse=True)
    row = rows[0]
    return {
        "context_type": "daily_report",
        "context_id": str(row.get("id") or row.get("notification_id") or ""),
        "summary": str(row.get("summary") or row.get("content") or row.get("report_kind") or "最近日报")[:240],
        "status": str(row.get("delivery_status") or row.get("status") or ""),
        "updated_at": _item_time(row),
        "evidence_source": "daily_report_runs/notification_outbox",
    }


def _recent_writeback_contexts(store: TuoguanStore, identity: UserIdentity, now: datetime, maximum: int) -> list[dict[str, Any]]:
    cutoff = now - timedelta(hours=3)
    ids = _identity_ids(identity)
    rows: list[dict[str, Any]] = []
    for row in _read_jsonl(store, "reply_ledger.jsonl")[-200:]:
        if str(row.get("user_id") or "") not in ids:
            continue
        when = _parse_time(_item_time(row))
        if when is None or when < cutoff:
            continue
        if not (row.get("tool_calls") or row.get("used_tool_registry_entry") or row.get("writeback_verified")):
            continue
        rows.append(row)
    rows.sort(key=lambda item: _time_value(_item_time(item)), reverse=True)
    contexts: list[dict[str, Any]] = []
    for row in rows[:maximum]:
        tool_name = str(row.get("used_tool_registry_entry") or "")
        if not tool_name and isinstance(row.get("tool_calls"), list) and row["tool_calls"]:
            first = row["tool_calls"][-1]
            if isinstance(first, dict):
                tool_name = str(first.get("tool") or "")
        contexts.append({
            "context_type": "recent_tool_result",
            "context_id": str(row.get("ledger_id") or row.get("message_id") or ""),
            "summary": f"工具={tool_name or '未知'}；用户原话={str(row.get('raw_text') or '')[:120]}；回复={str(row.get('final_reply') or '')[:120]}",
            "status": "writeback_verified" if row.get("writeback_verified") else "tool_result",
            "updated_at": _item_time(row),
            "evidence_source": "reply_ledger.jsonl",
        })
    return contexts


def query_active_work_context(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    limit: int = 5,
) -> dict[str, Any]:
    """Return scoped evidence without assigning intent or a next action."""

    maximum = max(1, min(int(limit or 5), 10))
    now = datetime.now().astimezone()
    items: list[dict[str, Any]] = []
    items.extend(_recent_outbound_contexts(store, identity, now, maximum))
    latest_daily = _latest_daily_report_context(store, identity, now)
    if latest_daily:
        items.append(latest_daily)
    items.extend(_recent_writeback_contexts(store, identity, now, maximum))
    task = _task_for_context(store, identity)
    if task:
        items.append({
            "context_type": "task",
            "context_id": str(task.get("id") or ""),
            "summary": str(task.get("title") or task.get("content") or "")[:240],
            "status": str(task.get("status") or ""),
            "updated_at": str(task.get("updated_at") or task.get("created_at") or ""),
            "evidence_source": "tasks.json",
        })
    if identity.role in {"boss", "manager"}:
        attention = query_attention_threads(store, identity=identity, include_closed=False, limit=maximum)
        for row in reversed(list(attention.get("attention_threads") or [])):
            items.append({
                "context_type": "owner_attention",
                "context_id": str(row.get("attention_id") or ""),
                "summary": str(row.get("question_text") or row.get("source_decision_summary") or "")[:240],
                "status": str(row.get("status") or ""),
                "updated_at": str(row.get("updated_at") or row.get("created_at") or ""),
                "evidence_source": "attention_threads",
            })
    touches = query_relationship_touch_candidates(
        store,
        identity=identity,
        target_user_id=str(identity.platform_user_id or "") if identity.role != "boss" else "",
        include_closed=False,
        limit=maximum,
    )
    for row in reversed(list(touches.get("candidates") or [])):
        items.append({
            "context_type": "relationship_touch",
            "context_id": str(row.get("candidate_id") or ""),
            "summary": str(row.get("message") or row.get("reason") or "")[:240],
            "status": str(row.get("status") or ""),
            "updated_at": str(row.get("updated_at") or row.get("created_at") or ""),
            "evidence_source": "relationship_touch_candidates",
            "goal_id": str(row.get("goal_id") or ""),
            "goal_action_id": str(row.get("goal_action_id") or ""),
            "delivery_receipt": deepcopy(row.get("delivery_receipt") or {}),
        })
    goal_actions = query_goal_actions(
        store,
        identity=identity,
        include_closed=False,
        due_only=False,
        now_at=now.isoformat(timespec="seconds"),
        limit=maximum,
    )
    for row in reversed(list(goal_actions.get("goal_actions") or [])):
        items.append({
            "context_type": "goal_action",
            "context_id": str(row.get("goal_action_id") or ""),
            "summary": str(row.get("summary") or row.get("evidence_requirement") or "")[:240],
            "status": str(row.get("status") or ""),
            "updated_at": str(row.get("updated_at") or row.get("planned_at") or row.get("created_at") or ""),
            "evidence_source": "goal_actions.jsonl",
            "goal_id": str(row.get("goal_id") or ""),
            "target_user_id": str(row.get("target_user_id") or ""),
        })
    if identity.role in {"boss", "manager"}:
        research = query_social_market_research(store, identity=identity, limit=maximum)
        for row in reversed(list(research.get("candidates") or [])):
            items.append({
                "context_type": "social_market_research",
                "context_id": str(row.get("candidate_id") or row.get("source_id") or row.get("url") or ""),
                "summary": str(row.get("title") or row.get("text_excerpt") or row.get("summary") or "")[:240],
                "status": str(row.get("status") or "candidate"),
                "updated_at": str(row.get("collected_at") or row.get("created_at") or ""),
                "evidence_source": str(row.get("platform") or "social_market_research"),
            })
    items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    items = items[:maximum]
    return {
        "ok": True,
        "generated_at": now.isoformat(timespec="seconds"),
        "current_time": now.isoformat(timespec="seconds"),
        "context_count": len(items),
        "contexts": items,
        "rendered_text": f"查到 {len(items)} 条当前活动事项。它们只是衔接证据，是否相关仍由小优结合本轮原话判断。",
        "render_verified": True,
    }


def render_active_work_context(result: dict[str, Any]) -> str:
    items = [item for item in list(result.get("contexts") or []) if isinstance(item, dict)]
    if not items:
        return ""
    lines = [
        "【当前活动工作线程】",
        "下面是当前人的近期活动事项，只用于判断短回复在回答哪一件事；不要机械选择，也不要在没有真实写入时声称已执行。",
    ]
    current_time = str(result.get("current_time") or "")
    if current_time:
        lines.append(f"当前时间={current_time}；涉及今天/昨晚/晚安/任务截止时必须以此为准。")
    for item in items:
        lines.append(
            f"- 类型={item.get('context_type') or ''}；id={item.get('context_id') or ''}；"
            f"状态={item.get('status') or ''}；时间={item.get('updated_at') or '未知'}；内容={item.get('summary') or ''}"
        )
    lines.append("先由模型判断本轮原话与哪条最相关；只有确实相关时才沿该线程回答或调用可信工具。")
    return "\n".join(lines)
