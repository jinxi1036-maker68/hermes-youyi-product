"""Evidence-only active work context for natural short-reply continuation."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .digital_employee_state import query_attention_threads, query_relationship_touch_candidates
from .models import UserIdentity
from .social_market_research import query_social_market_research
from .store import TuoguanStore


_OPEN_TASK_STATUSES = {"pending", "active", "waiting_confirmation", "in_progress", "blocked"}


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


def query_active_work_context(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    limit: int = 5,
) -> dict[str, Any]:
    """Return scoped evidence without assigning intent or a next action."""

    maximum = max(1, min(int(limit or 5), 10))
    items: list[dict[str, Any]] = []
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
    for item in items:
        lines.append(
            f"- 类型={item.get('context_type') or ''}；id={item.get('context_id') or ''}；"
            f"状态={item.get('status') or ''}；时间={item.get('updated_at') or '未知'}；内容={item.get('summary') or ''}"
        )
    lines.append("先由模型判断本轮原话与哪条最相关；只有确实相关时才沿该线程回答或调用可信工具。")
    return "\n".join(lines)
