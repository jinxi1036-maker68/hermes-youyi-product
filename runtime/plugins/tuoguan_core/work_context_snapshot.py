"""Build one evidence-only work context snapshot for each model turn."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any
import uuid

from .active_work_context import query_active_work_context
from .models import UserIdentity
from .store import TuoguanStore
from .tenant_context import current_tenant_id


BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")
_TRANSIENT_CONTEXT_TYPES = {
    "recent_outbound", "daily_report", "recent_tool_result", "owner_attention", "relationship_touch", "social_market_research",
}
_SOURCE_CONFIDENCE = {
    "tasks.json": 1.0,
    "notification_outbox.json": 1.0,
    "daily_report_runs/notification_outbox": 0.98,
    "reply_ledger.jsonl": 0.98,
    "attention_threads": 0.95,
    "relationship_touch_candidates": 0.98,
    "goal_actions.jsonl": 0.98,
}
_AUTHORITATIVE_CONTEXT_SOURCES = {
    "task": ("task", "tasks.json"),
    "recent_outbound": ("notification", "notification_outbox.json"),
    "daily_report": ("daily_report", "daily_report_runs.jsonl"),
    "recent_tool_result": ("tool_result", "reply_ledger.jsonl"),
    "owner_attention": ("attention_thread", "attention_threads.jsonl"),
    "relationship_touch": ("relationship_touch", "relationship_touch_candidates.jsonl"),
    "goal_action": ("goal_action", "goal_actions.jsonl"),
    "social_market_research": ("market_evidence", "social_market_research_candidates.jsonl"),
}


@dataclass(frozen=True)
class WorkContextSnapshot:
    snapshot_id: str
    tenant_id: str
    platform: str
    app_id_hash: str
    actor_user_id: str
    actor_role: str
    session_id_hash: str
    message_id_hash: str
    current_time: str
    timezone: str
    candidate_threads: tuple[dict[str, Any], ...]
    authoritative_object_refs: tuple[dict[str, str], ...]
    ambiguity_state: str
    generated_at: str


def _hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:20]


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING)
    return parsed.astimezone(BEIJING)


def _participant_ids(item: dict[str, Any], identity: UserIdentity) -> list[str]:
    values = {
        str(identity.canonical_user_id or ""),
        str(identity.platform_user_id or ""),
        str(item.get("target_user_id") or ""),
        str(item.get("assignee_user_id") or item.get("assignee_userid") or ""),
        str(item.get("created_by_user_id") or item.get("created_by_userid") or ""),
    }
    return sorted(value for value in values if value)


def _normalize_candidate(item: dict[str, Any], *, identity: UserIdentity, now: datetime) -> dict[str, Any] | None:
    context_type = str(item.get("context_type") or "")
    updated_at = _parse_time(item.get("updated_at"))
    age = now - updated_at if updated_at else None
    if context_type in _TRANSIENT_CONTEXT_TYPES and age is not None and age > timedelta(hours=36):
        return None
    freshness = "unknown"
    if age is not None:
        freshness = "current" if age <= timedelta(hours=3) else "recent"
    source = str(item.get("evidence_source") or "")
    confidence = _SOURCE_CONFIDENCE.get(source, 0.9 if source else 0.7)
    object_type, authority = _AUTHORITATIVE_CONTEXT_SOURCES.get(
        context_type, (context_type or "unknown", source or "unknown")
    )
    return {
        "context_type": context_type,
        "context_id": str(item.get("context_id") or ""),
        "summary": str(item.get("summary") or "")[:180],
        "status": str(item.get("status") or ""),
        "updated_at": updated_at.isoformat(timespec="seconds") if updated_at else "",
        "freshness": freshness,
        "confidence": confidence,
        "evidence_source": source,
        "authoritative_object_type": object_type,
        "authoritative_source": authority,
        "participant_ids": _participant_ids(item, identity),
    }


def build_work_context_snapshot(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    platform: str,
    app_id: str,
    session_id: str,
    message_id: str,
    now: datetime | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    current = (now or datetime.now(BEIJING)).astimezone(BEIJING)
    maximum = max(1, min(int(limit or 8), 10))
    result = query_active_work_context(store, identity=identity, limit=10, now=current)
    candidates: list[dict[str, Any]] = []
    for item in result.get("contexts") or []:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_candidate(item, identity=identity, now=current)
        if normalized:
            candidates.append(normalized)
    candidates.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    candidates = candidates[:maximum]
    if not candidates:
        ambiguity = "no_candidate"
    elif len(candidates) == 1:
        ambiguity = "single_candidate"
    else:
        ambiguity = "multiple_candidates_model_must_disambiguate"
    authoritative_refs = tuple({
        "object_type": str(item.get("authoritative_object_type") or "unknown"),
        "object_id": str(item.get("context_id") or ""),
        "source": str(item.get("authoritative_source") or item.get("evidence_source") or "unknown"),
    } for item in candidates)
    snapshot = WorkContextSnapshot(
        snapshot_id=f"snapshot_{uuid.uuid4().hex}",
        tenant_id=current_tenant_id(),
        platform=str(platform or ""),
        app_id_hash=_hash(app_id),
        actor_user_id=str(identity.canonical_user_id or identity.platform_user_id or ""),
        actor_role=str(identity.role or "unbound"),
        session_id_hash=_hash(session_id),
        message_id_hash=_hash(message_id),
        current_time=current.isoformat(timespec="seconds"),
        timezone="Asia/Shanghai",
        candidate_threads=tuple(candidates),
        authoritative_object_refs=authoritative_refs,
        ambiguity_state=ambiguity,
        generated_at=current.isoformat(timespec="seconds"),
    )
    return asdict(snapshot)


def render_work_context_snapshot(snapshot: dict[str, Any]) -> str:
    lines = [
        "【当前活动工作线程｜WorkContextSnapshot】",
        f"当前身份={snapshot.get('actor_role') or 'unbound'}；当前北京时间={snapshot.get('current_time') or '未知'}。",
        "以下是当前人的候选工作线程和证据，不是固定意图、流程或下一工具；由小优结合本轮原话判断。",
    ]
    candidates = [item for item in snapshot.get("candidate_threads") or [] if isinstance(item, dict)]
    for item in candidates:
        lines.append(
            f"- 类型={item.get('context_type') or ''}；id={item.get('context_id') or ''}；"
            f"状态={item.get('status') or ''}；时间={item.get('updated_at') or '未知'}；"
            f"新鲜度={item.get('freshness') or 'unknown'}；证据={item.get('evidence_source') or '未知'}；"
            f"权威来源={item.get('authoritative_source') or '未知'}；"
            f"摘要={item.get('summary') or ''}"
        )
    ambiguity = str(snapshot.get("ambiguity_state") or "")
    if ambiguity == "multiple_candidates_model_must_disambiguate":
        lines.append("存在多个合理线程：模型先判断；仍无法唯一确定时只追问一个关键区分问题，不能强行错挂。")
    elif ambiguity == "no_candidate":
        lines.append("当前没有可验证活动线程；不能猜旧对象，需要执行时先查事实或只问一个关键问题。")
    return "\n".join(lines)
