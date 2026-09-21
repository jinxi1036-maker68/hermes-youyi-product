"""Self-evolution ledger for Xiaoyou as a digital employee.

This module records learning candidates and next-day context only. It does not
send messages, modify institution policy, update the handbook, or route model
intent. Low-risk entries may inform future context; medium/high-risk entries
remain review material.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from difflib import SequenceMatcher
import hashlib
import json
import re
import uuid
from typing import Any

from .models import UserIdentity
from .store import TuoguanStore
from .tenant_context import current_tenant_id


SELF_EVOLUTION_EVENTS_FILE = "self_evolution_events.jsonl"
WORKSTYLE_EVENTS_FILE = "person_workstyle_events.jsonl"
SELF_EVOLUTION_APPLICATION_RECORD_TYPE = "self_evolution_application"

EVOLUTION_CANDIDATE_TYPES = {
    "person_preference_candidate",
    "institution_fact_gap",
    "self_correction",
    "tool_failure_or_bug",
    "handbook_method_candidate",
    "tomorrow_focus",
    "multi_agent_adoption",
}

LOW_RISK_TYPES = {
    "person_preference_candidate",
    "self_correction",
    "tomorrow_focus",
    "multi_agent_adoption",
}

MEDIUM_RISK_TYPES = {
    "institution_fact_gap",
    "tool_failure_or_bug",
    "handbook_method_candidate",
}

HIGH_RISK_TERMS = (
    "工资",
    "薪资",
    "薪酬",
    "绩效",
    "权限",
    "自动联系家长",
    "主动联系家长",
    "发给家长",
    "家長",
    "主动联系家長",
    "删除",
    "清空",
    "退费",
    "退款",
    "收费",
    "学费",
    "价格",
    "优惠",
    "安全事件",
    "闭环安全",
    "改制度",
    "机构制度",
    "正式制度",
    "责任绑定",
    "通讯录权限",
    "parent",
    "parents",
    "guardian",
    "parent outreach",
    "contact parent",
    "contact parents",
    "send parent",
    "send parents",
    "parent message",
    "parent wechat",
    "guardian outreach",
    "salary",
    "payroll",
    "wage",
    "bonus",
    "commission",
    "permission",
    "role permission",
    "admin permission",
    "delete",
    "clear data",
    "remove data",
    "wipe data",
    "refund",
    "tuition",
    "fee",
    "price",
    "discount",
    "safety incident",
    "close safety",
    "policy",
    "institution policy",
    "rule change",
    "responsibility binding",
)

ALLOWED_STATUSES = {
    "candidate",
    "ready_for_application",
    "applied",
    "pending_review",
    "needs_confirmation",
    "rejected",
    "superseded",
    "verified",
    "failed",
    "fixed",
    "expired",
    "needs_retest",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_id() -> str:
    return f"evolution_{uuid.uuid4().hex[:12]}"


def _limit_text(value: Any, limit: int = 500) -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _list_any(value: Any, limit: int = 8) -> list[Any]:
    if not isinstance(value, list):
        return []
    cleaned: list[Any] = []
    for item in value[:limit]:
        if isinstance(item, dict):
            cleaned.append({str(k): _safe_value(v) for k, v in item.items() if str(k) not in {"next_tool", "workflow_step", "expected_reply", "model_intent"}})
        elif isinstance(item, (str, int, float, bool)) or item is None:
            cleaned.append(_limit_text(item, 240) if isinstance(item, str) else item)
    return cleaned


def evolution_evidence_is_usable(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    evidence_keys = {
        "text", "excerpt", "fact", "summary", "result", "raw_text", "final_reply",
        "message_id", "ledger_id", "preference_id", "authorization_id", "goal_action_id",
        "task_id", "writeback_verified",
    }
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or item.get("source_type") or "").strip()
        serialized = json.dumps(item, ensure_ascii=False)
        has_trace = any(item.get(key) not in (None, "", False, []) for key in evidence_keys)
        if source and has_trace and "historical_requires_revalidation" not in serialized:
            return True
    return False


def _safe_value(value: Any) -> Any:
    if isinstance(value, str):
        return _limit_text(value, 500)
    if isinstance(value, list):
        return _list_any(value, 8)
    if isinstance(value, dict):
        return {str(k): _safe_value(v) for k, v in value.items() if str(k) not in {"next_tool", "workflow_step", "expected_reply", "model_intent"}}
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _limit_text(value, 240)


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


def _append_jsonl(store: TuoguanStore, filename: str, row: dict[str, Any]) -> None:
    store.append_jsonl_verified(filename, row)


def normalize_evolution_candidate(raw: dict[str, Any]) -> dict[str, Any]:
    item = raw if isinstance(raw, dict) else {}
    candidate_type = str(item.get("candidate_type") or item.get("event_type") or item.get("type") or "").strip()
    if candidate_type not in EVOLUTION_CANDIDATE_TYPES:
        candidate_type = "tomorrow_focus"
    summary = _limit_text(item.get("summary") or item.get("title") or item.get("text") or item.get("lesson"), 700)
    evidence = _list_any(item.get("evidence") or item.get("sources") or item.get("source_evidence"), 8)
    target_store = _limit_text(item.get("target_store") or item.get("target") or "", 120)
    proposed_effect = _limit_text(item.get("proposed_effect") or item.get("effect") or item.get("next_effect"), 700)
    risk_level = str(item.get("risk_level") or "").strip().lower()
    if risk_level not in {"low", "medium", "high"}:
        risk_level = classify_evolution_risk(
            candidate_type=candidate_type,
            summary=summary,
            evidence=evidence,
            target_store=target_store,
            proposed_effect=proposed_effect,
        )
    status = _normalize_status(
        item.get("status"),
        candidate_type=candidate_type,
        risk_level=risk_level,
        writeback_verified=bool(item.get("writeback_verified")),
    )
    return {
        "candidate_type": candidate_type,
        "summary": summary,
        "evidence": evidence,
        "risk_level": risk_level,
        "status": status,
        "target_store": target_store,
        "proposed_effect": proposed_effect,
        "next_effect": _limit_text(item.get("next_effect") or proposed_effect, 700),
        "applies_to_user_id": _limit_text(item.get("applies_to_user_id") or item.get("target_user_id"), 120),
        "applies_to_role": _limit_text(item.get("applies_to_role") or item.get("target_role"), 40),
        "applies_to_scope": _normalize_application_scope(item.get("applies_to_scope") or item.get("target_scope")),
        "review_required_by": _limit_text(item.get("review_required_by"), 80),
        "source_text": _limit_text(item.get("source_text") or summary, 1000),
        "writeback_verified": bool(item.get("writeback_verified")),
    }


def classify_evolution_risk(
    *,
    candidate_type: str,
    summary: str,
    evidence: list[Any] | None = None,
    target_store: str = "",
    proposed_effect: str = "",
) -> str:
    text = "".join(
        str(value or "")
        for value in (
            candidate_type,
            summary,
            json.dumps(evidence or [], ensure_ascii=False),
            target_store,
            proposed_effect,
        )
    )
    if any(_has_high_risk_term(text, term) for term in HIGH_RISK_TERMS):
        return "high"
    if candidate_type in MEDIUM_RISK_TYPES:
        return "medium"
    if candidate_type in LOW_RISK_TYPES:
        return "low"
    return "medium"


def _has_high_risk_term(text: str, term: str) -> bool:
    raw = str(text or "").lower()
    raw_term = str(term or "").lower().strip()
    if not raw_term:
        return False
    if raw_term.isascii():
        if " " in raw_term:
            compact_text = re.sub(r"[^a-z0-9]+", "", raw)
            compact_term = re.sub(r"[^a-z0-9]+", "", raw_term)
            return bool(compact_term and compact_term in compact_text)
        return re.search(rf"(?<![a-z0-9]){re.escape(raw_term)}(?![a-z0-9])", raw) is not None
    compact = "".join(raw.split())
    return "".join(raw_term.split()) in compact


def submit_self_evolution_event(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    operation_id: str,
    candidate_type: str,
    summary: str,
    evidence: list[Any] | None = None,
    risk_level: str = "",
    status: str = "",
    target_store: str = "",
    proposed_effect: str = "",
    next_effect: str = "",
    applies_to_user_id: str = "",
    applies_to_role: str = "",
    applies_to_scope: str = "",
    review_required_by: str = "",
    source_text: str = "",
    source_message_id: str = "",
    cadence_mode: str = "",
    writeback_verified: bool = False,
    occurred_at: str = "",
) -> dict[str, Any]:
    candidate = normalize_evolution_candidate({
        "candidate_type": candidate_type,
        "summary": summary,
        "evidence": evidence or [],
        "risk_level": risk_level,
        "status": status,
        "target_store": target_store,
        "proposed_effect": proposed_effect,
        "next_effect": next_effect,
        "applies_to_user_id": applies_to_user_id,
        "applies_to_role": applies_to_role,
        "applies_to_scope": applies_to_scope,
        "review_required_by": review_required_by,
        "source_text": source_text,
        "writeback_verified": bool(writeback_verified),
    })
    if not candidate["summary"]:
        return {"ok": False, "error": "self_evolution_summary_required", "message": "进化候选必须包含摘要。"}
    if _summary_is_incomplete(candidate["summary"]):
        return {
            "ok": False,
            "error": "incomplete_self_evolution_summary",
            "message": "进化候选像半句话，未写入账本；请形成完整、可执行的经验后再保存。",
        }
    event_at = str(occurred_at or now_iso()).strip()
    try:
        parsed_event_at = datetime.fromisoformat(event_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid_self_evolution_occurred_at", "message": "进化候选发生时间不是有效 ISO 时间。"}
    event_at = parsed_event_at.isoformat(timespec="seconds")
    fingerprint = _semantic_fingerprint(candidate)
    for existing in reversed(_read_jsonl(store, SELF_EVOLUTION_EVENTS_FILE)[-200:]):
        if str(existing.get("tenant_id") or "") not in {"", current_tenant_id()}:
            continue
        if (
            str(existing.get("semantic_fingerprint") or "") == fingerprint
            or _same_application_lesson(existing, candidate)
        ):
            merged = deepcopy(existing)
            merged["occurrence_count"] = int(existing.get("occurrence_count") or 1) + 1
            merged["updated_at"] = event_at
            merged["evidence"] = _list_any([*(existing.get("evidence") or []), *candidate["evidence"]], 8)
            if _prefer_candidate_summary(candidate["summary"], str(existing.get("summary") or "")):
                merged["summary"] = candidate["summary"]
                merged["source_text"] = candidate["source_text"]
                merged["proposed_effect"] = candidate["proposed_effect"]
                merged["next_effect"] = candidate["next_effect"]
            new_evidence = _has_new_evidence(existing.get("evidence"), candidate["evidence"])
            existing_status = str(existing.get("status") or "")
            if existing_status in {"verified", "fixed"} and new_evidence:
                merged["status"] = "ready_for_application" if candidate["risk_level"] == "low" else candidate["status"]
                merged["reopened_after_verification"] = True
                merged["regression_count"] = int(existing.get("regression_count") or 0) + 1
            elif existing_status == "applied" and new_evidence:
                merged["status"] = "failed"
                merged["regression_count"] = int(existing.get("regression_count") or 0) + 1
            elif existing_status not in {"verified", "fixed", "applied"}:
                merged["status"] = candidate["status"]
            merged["source"] = {
                "actor_user_id": identity.canonical_user_id,
                "actor_name": identity.person_name,
                "actor_role": identity.role,
                "operation_id": str(operation_id or ""),
                "source_message_id": str(source_message_id or ""),
                "cadence_mode": str(cadence_mode or ""),
            }
            _append_jsonl(store, SELF_EVOLUTION_EVENTS_FILE, merged)
            return {
                "ok": True,
                "self_evolution_event": merged,
                "writeback_verified": True,
                "state_changed": True,
                "deduplicated_update": True,
                "rendered_text": "这条自我进化问题已经存在，本轮已合并证据并更新出现次数。",
            }
    row = {
        "record_type": "self_evolution_event",
        "evolution_event_id": _new_id(),
        "tenant_id": current_tenant_id(),
        "candidate_type": candidate["candidate_type"],
        "summary": candidate["summary"],
        "evidence": candidate["evidence"],
        "risk_level": candidate["risk_level"],
        "status": candidate["status"],
        "target_store": candidate["target_store"],
        "proposed_effect": candidate["proposed_effect"],
        "next_effect": candidate["next_effect"],
        "applies_to_user_id": candidate["applies_to_user_id"],
        "applies_to_role": candidate["applies_to_role"],
        "applies_to_scope": candidate["applies_to_scope"],
        "review_required_by": candidate["review_required_by"] or _default_reviewer(candidate["risk_level"]),
        "source_text": candidate["source_text"],
        "writeback_verified": bool(candidate["writeback_verified"]),
        "source": {
            "actor_user_id": identity.canonical_user_id,
            "actor_name": identity.person_name,
            "actor_role": identity.role,
            "operation_id": str(operation_id or ""),
            "source_message_id": str(source_message_id or ""),
            "cadence_mode": str(cadence_mode or ""),
        },
        "semantic_fingerprint": fingerprint,
        "occurrence_count": 1,
        "created_at": event_at,
        "auto_effects": _safe_auto_effects(candidate["risk_level"]),
    }
    _append_jsonl(store, SELF_EVOLUTION_EVENTS_FILE, row)
    verified = any(
        str(item.get("evolution_event_id") or "") == row["evolution_event_id"]
        for item in _read_jsonl(store, SELF_EVOLUTION_EVENTS_FILE)[-80:]
    )
    return {
        "ok": bool(verified),
        "self_evolution_event": row,
        "writeback_verified": bool(verified),
        "state_changed": bool(verified),
        "rendered_text": _render_saved(row) if verified else "已理解这条进化候选，但写后反查未通过。",
    }


def query_self_evolution_ledger(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    candidate_type: str = "",
    status: str = "",
    limit: int = 30,
) -> dict[str, Any]:
    if identity.role not in {"boss", "manager"} and identity.platform != "system":
        return {"ok": False, "error": "permission_denied", "message": "只有老板、店长或系统巡检可以查看小优自我进化账本。"}
    rows = _filtered_events(store, candidate_type=candidate_type, status=status)
    cap = max(1, min(int(limit or 30), 200))
    rows = rows[-cap:]
    status_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    risk_counts: dict[str, int] = {}
    for row in rows:
        status_counts[str(row.get("status") or "candidate")] = status_counts.get(str(row.get("status") or "candidate"), 0) + 1
        type_counts[str(row.get("candidate_type") or "")] = type_counts.get(str(row.get("candidate_type") or ""), 0) + 1
        risk_counts[str(row.get("risk_level") or "")] = risk_counts.get(str(row.get("risk_level") or ""), 0) + 1
    health = _health_signals(rows)
    return {
        "ok": True,
        "tenant_id": current_tenant_id(),
        "report_type": "xiaoyou_self_evolution_ledger_v1",
        "event_count": len(rows),
        "events": deepcopy(rows),
        "status_counts": status_counts,
        "type_counts": type_counts,
        "risk_counts": risk_counts,
        "health_signals": health,
        "rendered_text": _render_ledger(rows, health),
        "render_verified": True,
        "boundary": _boundary(),
    }


def build_self_evolution_brief(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    limit: int = 12,
    now: datetime | None = None,
    scope: str = "",
) -> dict[str, Any]:
    reference = now or datetime.now().astimezone()
    if identity.role in {"boss", "manager"} or identity.platform == "system":
        ledger = query_self_evolution_ledger(store, identity=identity, limit=max(limit, 20))
        events = ledger.get("events") if ledger.get("ok") else []
        events = events if isinstance(events, list) else []
    else:
        events = [item for item in _filtered_events(store) if _applies_to_identity(item, identity)][-max(limit, 20):]
        ledger = {"health_signals": _health_signals(events)}
    recent_workstyles = _recent_workstyle_preferences(store, identity=identity, limit=6)
    eligible_events = [
        item for item in events
        if _is_next_context_candidate(item, now=reference)
        and _applies_to_identity(item, identity)
        and _applies_to_scope(item, scope)
    ]
    applicable_events, suppressed_duplicate_count = _dedupe_application_events(eligible_events)
    applicable_events = applicable_events[-max(1, min(limit, 12)):]
    applicable = [
        _evolution_context_line(item, now=reference)
        for item in applicable_events
    ]
    applicable = [line for line in applicable if line][-max(1, min(limit, 12)):]
    review_queue = [
        item for item in events
        if str(item.get("status") or "") in {"pending_review", "needs_confirmation"}
    ][-8:]
    stale_application_count = sum(
        1
        for item in events
        if _is_low_risk_application_status(item)
        and not _is_next_context_candidate(item, now=reference)
        and _applies_to_identity(item, identity)
    )
    incomplete_application_count = sum(
        1
        for item in events
        if _is_low_risk_application_status(item)
        and _applies_to_identity(item, identity)
        and _summary_is_incomplete(str(item.get("summary") or ""))
    )
    return {
        "ok": True,
        "tenant_id": current_tenant_id(),
        "report_type": "xiaoyou_self_evolution_brief_v1",
        "event_count": len(events),
        "recent_events": deepcopy(events[-max(1, min(limit, 20)):]),
        "next_day_context": applicable,
        "next_day_application_ids": [str(item.get("evolution_event_id") or "") for item in applicable_events],
        "stale_application_count": stale_application_count,
        "suppressed_duplicate_application_count": suppressed_duplicate_count,
        "incomplete_application_count": incomplete_application_count,
        "review_queue": deepcopy(review_queue),
        "review_queue_count": len(review_queue),
        "recent_workstyle_preferences": recent_workstyles,
        "health_signals": ledger.get("health_signals") or {},
        "source_files": [SELF_EVOLUTION_EVENTS_FILE, WORKSTYLE_EVENTS_FILE],
        "boundary": _boundary(),
        "rendered_text": _render_brief(applicable, review_queue, recent_workstyles),
        "render_verified": True,
    }


def conversation_evolution_context_for_user(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    limit: int = 5,
    now: datetime | None = None,
) -> str:
    """Render the model context without turning it into an action plan.

    Callers that need to prove a later application must use
    :func:`conversation_evolution_context_bundle_for_user` and retain the
    selected IDs.  A source label alone is not evidence that a particular
    lesson reached the model.
    """

    return str(conversation_evolution_context_bundle_for_user(
        store,
        identity=identity,
        limit=limit,
        now=now,
    ).get("text") or "")


def conversation_evolution_context_bundle_for_user(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    limit: int = 5,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the rendered experience context and its selected ledger IDs.

    This remains experience material only.  The IDs are correlation metadata
    for the runtime trace and application ledger; they never select a tool or
    dictate a business action.
    """

    brief = build_self_evolution_brief(
        store,
        identity=identity,
        limit=limit,
        now=now,
        scope="direct_reply",
    )
    lines = ["【小优自我进化上下文】"]
    lines.append("来源：夜间复盘账本和已验证工作方式偏好；这是经验材料，不是 Router，也不替模型决定动作。")
    next_context = brief.get("next_day_context") or []
    if next_context:
        lines.append("最近应带入本轮服务的经验：" + "；".join(str(item) for item in next_context[:limit]))
    else:
        lines.append("最近没有新的已生效经验，仍按员工手册、事实和权限边界工作。")
    health = brief.get("health_signals") or {}
    if identity.role == "boss" and int(health.get("open_review_count") or 0) > 0:
        lines.append(f"仍有 {health.get('open_review_count')} 条中高风险进化候选等待人工确认，不能擅自生效。")
    lines.append("如果本轮发现低风险工作方式反馈，可调用偏好工具保存；未写后反查成功前，不得说已保存或以后按这个来。")
    selected_ids = [
        str(item or "").strip()
        for item in (brief.get("next_day_application_ids") or [])
        if str(item or "").strip()
    ][:max(1, min(int(limit or 5), 12))]
    return {
        "text": "\n".join(lines),
        "application_ids": selected_ids,
        "selected_count": len(selected_ids),
    }


def _filtered_events(store: TuoguanStore, *, candidate_type: str = "", status: str = "") -> list[dict[str, Any]]:
    folded: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(store, SELF_EVOLUTION_EVENTS_FILE):
        if str(row.get("record_type") or "") != "self_evolution_event":
            continue
        if str(row.get("tenant_id") or "") not in {"", current_tenant_id()}:
            continue
        key = str(row.get("semantic_fingerprint") or row.get("evolution_event_id") or "")
        if key:
            folded[key] = deepcopy(row)
    rows = [row for row in folded.values() if not candidate_type or str(row.get("candidate_type") or "") == candidate_type]
    rows = [row for row in rows if not status or str(row.get("status") or "") == status]
    rows.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""))
    return rows


def _applies_to_identity(item: dict[str, Any], identity: UserIdentity) -> bool:
    target_user = str(item.get("applies_to_user_id") or "").strip()
    target_role = str(item.get("applies_to_role") or "").strip()
    if target_user and target_user not in {identity.canonical_user_id, identity.platform_user_id}:
        return False
    if target_role and target_role != identity.role:
        return False
    if not target_user and not target_role and _looks_identity_specific(item):
        # Legacy rows that mention a person but omit an explicit scope are
        # quarantined. Guessing identity from prose caused cross-person memory.
        return False
    return True


def _looks_identity_specific(item: dict[str, Any]) -> bool:
    text = "".join(str(item.get(key) or "") for key in ("summary", "proposed_effect", "next_effect"))
    return any(term in text for term in ("老板", "机构负责人", "店长", "老师"))


def _applies_to_scope(item: dict[str, Any], scope: str) -> bool:
    requested = str(scope or "").strip()
    target = str(item.get("applies_to_scope") or "").strip()
    if not requested or not target or target == "all_communication":
        return True
    return target == requested


def record_self_evolution_application(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    source_message_id: str,
    final_reply: str,
    tool_write_verified: bool = False,
    scope: str = "direct_reply",
    workstyle_adaptation: dict[str, Any] | None = None,
    loaded_event_ids: list[str] | tuple[str, ...] | None = None,
    trace_id: str = "",
    limit: int = 3,
) -> dict[str, Any]:
    """Record only experience that was evidenced as loaded into this turn.

    Older code re-selected every currently-ready lesson after a reply, which
    could make a ledger say "applied" without proving that the same lesson was
    present in the model context.  ``loaded_event_ids`` is deliberately
    required: absence means an honest no-op, never a guessed application.
    """

    selected_ids = {
        str(item or "").strip()
        for item in (loaded_event_ids or [])
        if str(item or "").strip()
    }
    if not selected_ids:
        return {
            "ok": True,
            "applied_count": 0,
            "verified_count": 0,
            "failed_count": 0,
            "verification_pending_count": 0,
            "state_changed": False,
            "writeback_verified": False,
            "skipped": True,
            "reason": "context_selection_evidence_required",
        }
    candidates = [
        item for item in _filtered_events(store)
        if str(item.get("risk_level") or "") == "low"
        and str(item.get("status") or "") in {"ready_for_application", "needs_retest"}
        # Eligibility (including freshness) was established when this exact
        # id was selected into the model/report context.  Re-evaluating its
        # timestamp at reply completion can lose a real application merely
        # because the turn or delivery crossed a time boundary.  The id is
        # not caller-supplied: runtime code retains it in active trace memory
        # or derives it from the just-rendered report.
        and evolution_evidence_is_usable(item.get("evidence"))
        and _applies_to_identity(item, identity)
        and _applies_to_scope(item, scope)
        and str(item.get("evolution_event_id") or "") in selected_ids
    ]
    candidates, _ = _dedupe_application_events(candidates)
    candidates = candidates[-max(1, min(int(limit or 3), 3)):]
    applied: list[dict[str, Any]] = []
    verified: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    application_records: list[dict[str, Any]] = []
    for candidate in candidates:
        applied_at = now_iso()
        application_id = f"evolution_application_{uuid.uuid4().hex[:12]}"
        same_turn_observation = _same_turn_application_observation(
            candidate,
            workstyle_adaptation=workstyle_adaptation or {},
        )
        row = deepcopy(candidate)
        row["status"] = "applied"
        row["updated_at"] = applied_at
        row["application_evidence"] = {
            "application_id": application_id,
            "source_message_id": str(source_message_id or ""),
            "target_user_id": identity.canonical_user_id,
            "target_role": identity.role,
            "reply_excerpt": _limit_text(final_reply, 300),
            "tool_write_verified": bool(tool_write_verified),
            "scope": str(scope or "direct_reply"),
            "context_selection_evidenced": True,
            "applied_at": applied_at,
        }
        _append_jsonl(store, SELF_EVOLUTION_EVENTS_FILE, row)
        applied.append(row)
        application_record = {
            "record_type": SELF_EVOLUTION_APPLICATION_RECORD_TYPE,
            "application_id": application_id,
            "tenant_id": current_tenant_id(),
            "evolution_event_id": str(candidate.get("evolution_event_id") or ""),
            "semantic_fingerprint": str(candidate.get("semantic_fingerprint") or ""),
            "source_message_id": str(source_message_id or ""),
            "trace_id_hash": hashlib.sha256(str(trace_id or "").encode("utf-8")).hexdigest()[:20] if trace_id else "",
            "target_user_id": identity.canonical_user_id,
            "target_role": identity.role,
            "scope": str(scope or "direct_reply"),
            "context_selection_evidenced": True,
            "applied_at": applied_at,
            "outcome": "pending_evidence",
            "writeback_verified": True,
        }
        if same_turn_observation:
            # A compliant reply or a Tool receipt in the application turn is
            # useful evidence, but it is not an effect evaluation.  In
            # particular, it cannot prove that loading the lesson changed the
            # behavior or improved the later business result.
            application_record["same_turn_observation"] = same_turn_observation
        _append_jsonl(store, SELF_EVOLUTION_EVENTS_FILE, application_record)
        application_records.append(application_record)
    return {
        "ok": True,
        "applied_count": len(applied),
        "applied_event_ids": [str(item.get("evolution_event_id") or "") for item in applied],
        "verified_count": len(verified),
        "failed_count": len(failed),
        "verification_pending_count": len(applied) - len(verified) - len(failed),
        "application_record_ids": [str(item.get("application_id") or "") for item in application_records],
        "state_changed": bool(applied),
        "writeback_verified": bool(applied),
    }


def _same_turn_application_observation(
    candidate: dict[str, Any],
    *,
    workstyle_adaptation: dict[str, Any],
) -> dict[str, Any]:
    """Keep same-turn facts without promoting them to evolution success."""

    summary = "".join(
        str(candidate.get(key) or "")
        for key in ("summary", "proposed_effect", "next_effect", "target_store")
    )
    workstyle_terms = (
        "工作方式", "偏好", "汇报", "日报", "早报", "晚报", "只说重点",
        "先说结论", "格式", "语气", "提醒时间", "回复长度", WORKSTYLE_EVENTS_FILE,
    )
    if not any(term in summary for term in workstyle_terms):
        return {}
    application_result = workstyle_adaptation.get("application_result")
    application = (
        application_result.get("application")
        if isinstance(application_result, dict) and isinstance(application_result.get("application"), dict)
        else {}
    )
    compliance = application.get("compliance") if isinstance(application.get("compliance"), dict) else None
    if compliance is None:
        return {}
    return {
        "source": "workstyle_application_observer",
        "compliance_ok": bool(compliance.get("ok")),
        "failures": [_limit_text(item, 120) for item in (compliance.get("failures") or [])[:8]],
        "unverified_commitment": bool(workstyle_adaptation.get("unverified_commitment")),
        "effect_verification": False,
    }


def verify_self_evolution_application(
    store: TuoguanStore,
    *,
    evolution_event_id: str,
    application_id: str = "",
    succeeded: bool,
    evidence: str = "",
    behavior_evidence: dict[str, Any] | None = None,
    result_evidence: dict[str, Any] | None = None,
    baseline_evidence: dict[str, Any] | None = None,
    effect_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate an application without treating state fields as proof.

    ``verified`` requires an immutable application record, exact context
    selection, observable behavior, a separately verified result, a real
    baseline problem, and a measured improvement.  A successful-looking
    reply, model self-assessment, or an ``applied`` flag alone becomes
    ``needs_retest`` rather than evolution success.
    """

    target = next(
        (item for item in reversed(_filtered_events(store)) if str(item.get("evolution_event_id") or "") == str(evolution_event_id or "")),
        None,
    )
    if target is None:
        return {"ok": False, "error": "evolution_event_not_found", "writeback_verified": False}
    selected_application = next(
        (
            item for item in reversed(_read_jsonl(store, SELF_EVOLUTION_EVENTS_FILE))
            if str(item.get("record_type") or "") == SELF_EVOLUTION_APPLICATION_RECORD_TYPE
            and str(item.get("evolution_event_id") or "") == str(evolution_event_id or "")
            and str(item.get("application_id") or "") == str(application_id or "")
            and not item.get("verification_event")
        ),
        {},
    )
    behavior = behavior_evidence if isinstance(behavior_evidence, dict) else {}
    result = result_evidence if isinstance(result_evidence, dict) else {}
    baseline = baseline_evidence if isinstance(baseline_evidence, dict) else {}
    effect = effect_evidence if isinstance(effect_evidence, dict) else {}
    gaps: list[str] = []
    if not application_id or not selected_application:
        gaps.append("immutable_application_record_required")
    elif selected_application.get("context_selection_evidenced") is not True:
        gaps.append("exact_context_selection_required")
    application_trace_hash = str(selected_application.get("trace_id_hash") or "")
    if not str(behavior.get("source") or "").strip():
        gaps.append("behavior_source_required")
    if behavior.get("behavior_changed") is not True:
        gaps.append("observable_behavior_change_required")
    if not str(behavior.get("expected_behavior") or "").strip() or not str(behavior.get("observed_behavior") or "").strip():
        gaps.append("expected_and_observed_behavior_required")
    if application_trace_hash and str(behavior.get("trace_id_hash") or "") != application_trace_hash:
        gaps.append("application_trace_mismatch")
    if not str(baseline.get("source") or "").strip() or baseline.get("problem_observed") is not True:
        gaps.append("real_baseline_problem_required")
    if not str(baseline.get("reference_id_hash") or "").strip():
        gaps.append("baseline_reference_required")
    if not str(result.get("source") or "").strip() or result.get("verified") is not True:
        gaps.append("verified_result_required")
    if not str(result.get("result_id_hash") or "").strip() or not str(result.get("outcome") or "").strip():
        gaps.append("traceable_result_required")
    if not str(effect.get("metric") or "").strip():
        gaps.append("effect_metric_required")
    if effect.get("improved") is not True:
        gaps.append("measured_improvement_required")

    if not succeeded and result.get("verified") is True and selected_application:
        outcome = "failed"
    elif succeeded and not gaps:
        outcome = "verified"
    else:
        outcome = "needs_retest"

    checked_at = now_iso()
    row = deepcopy(target)
    row["status"] = outcome
    row["updated_at"] = checked_at
    row["verification_evidence"] = _limit_text(evidence, 700)
    row["verification_evidence_gaps"] = gaps
    row["last_application_id"] = str(application_id or "")
    _append_jsonl(store, SELF_EVOLUTION_EVENTS_FILE, row)
    verification_row = {
        "record_type": SELF_EVOLUTION_APPLICATION_RECORD_TYPE,
        "application_id": str(application_id or ""),
        "tenant_id": current_tenant_id(),
        "evolution_event_id": str(evolution_event_id or ""),
        "verification_event": True,
        "outcome": outcome,
        "verification_mode": "measured_effect_evidence_v2",
        "verification_evidence": _limit_text(evidence, 700),
        "verification_evidence_gaps": gaps,
        "behavior_evidence": _safe_value(behavior),
        "result_evidence": _safe_value(result),
        "baseline_evidence": _safe_value(baseline),
        "effect_evidence": _safe_value(effect),
        "verified_at": checked_at if outcome == "verified" else "",
        "evaluated_at": checked_at,
        "writeback_verified": True,
    }
    _append_jsonl(store, SELF_EVOLUTION_EVENTS_FILE, verification_row)
    return {
        "ok": True,
        "verified": outcome == "verified",
        "outcome": outcome,
        "evidence_gaps": gaps,
        "self_evolution_event": row,
        "verification_event": verification_row,
        "writeback_verified": True,
    }


def schedule_self_evolution_retest(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    evolution_event_id: str,
    reason: str,
    evidence: list[dict[str, Any]],
    operation_id: str,
) -> dict[str, Any]:
    """Schedule a controlled low-risk retest without pretending it passed.

    This is an explicit audit/review action.  It never creates a business
    result and refuses medium/high-risk lessons, so policy, pay, permissions,
    parent outreach, and similar changes cannot be activated through retest.
    """

    if identity.role not in {"boss", "manager"} and identity.platform != "system":
        return {"ok": False, "error": "permission_denied", "writeback_verified": False}
    target = next(
        (item for item in reversed(_filtered_events(store)) if str(item.get("evolution_event_id") or "") == str(evolution_event_id or "")),
        None,
    )
    if target is None:
        return {"ok": False, "error": "evolution_event_not_found", "writeback_verified": False}
    if str(target.get("risk_level") or "") != "low":
        return {"ok": False, "error": "low_risk_retest_only", "writeback_verified": False}
    if str(target.get("status") or "") not in {"failed", "needs_retest"}:
        return {"ok": False, "error": "retest_requires_failed_or_needs_retest", "writeback_verified": False}
    clean_evidence = _list_any(evidence, 8)
    if not _limit_text(reason, 700) or not evolution_evidence_is_usable(clean_evidence):
        return {"ok": False, "error": "retest_evidence_required", "writeback_verified": False}

    scheduled_at = now_iso()
    row = deepcopy(target)
    row["status"] = "needs_retest"
    row["updated_at"] = scheduled_at
    row["retest_schedule"] = {
        "operation_id": str(operation_id or ""),
        "reason": _limit_text(reason, 700),
        "evidence": clean_evidence,
        "scheduled_by_user_id": identity.canonical_user_id,
        "scheduled_by_role": identity.role,
        "scheduled_at": scheduled_at,
    }
    _append_jsonl(store, SELF_EVOLUTION_EVENTS_FILE, row)
    schedule_row = {
        "record_type": "self_evolution_retest_schedule",
        "tenant_id": current_tenant_id(),
        "evolution_event_id": str(evolution_event_id or ""),
        "operation_id": str(operation_id or ""),
        "reason": _limit_text(reason, 700),
        "evidence": clean_evidence,
        "scheduled_by_user_id": identity.canonical_user_id,
        "scheduled_by_role": identity.role,
        "scheduled_at": scheduled_at,
        "writeback_verified": True,
    }
    _append_jsonl(store, SELF_EVOLUTION_EVENTS_FILE, schedule_row)
    return {
        "ok": True,
        "outcome": "needs_retest",
        "self_evolution_event": row,
        "schedule_event": schedule_row,
        "writeback_verified": True,
    }


def revalidate_superseded_self_evolution(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    evolution_event_id: str,
    reason: str,
    evidence: list[dict[str, Any]],
    operation_id: str,
) -> dict[str, Any]:
    """Re-open one quarantined low-risk lesson using new traceable evidence.

    Legacy prose evidence remains quarantined.  Revalidation replaces the
    active evidence with current structured references and can only schedule a
    retest; it can never mark the lesson applied or verified.
    """

    if identity.role not in {"boss", "manager"} and identity.platform != "system":
        return {"ok": False, "error": "permission_denied", "writeback_verified": False}
    target = next(
        (item for item in reversed(_filtered_events(store)) if str(item.get("evolution_event_id") or "") == str(evolution_event_id or "")),
        None,
    )
    if target is None:
        return {"ok": False, "error": "evolution_event_not_found", "writeback_verified": False}
    if str(target.get("risk_level") or "") != "low":
        return {"ok": False, "error": "low_risk_revalidation_only", "writeback_verified": False}
    if str(target.get("status") or "") != "superseded":
        return {"ok": False, "error": "revalidation_requires_superseded", "writeback_verified": False}
    clean_evidence = _list_any(evidence, 8)
    if not _limit_text(reason, 700) or not evolution_evidence_is_usable(clean_evidence):
        return {"ok": False, "error": "revalidation_evidence_required", "writeback_verified": False}

    revalidated_at = now_iso()
    row = deepcopy(target)
    row["status"] = "needs_retest"
    row["updated_at"] = revalidated_at
    row["historical_evidence_quarantined"] = _safe_value(target.get("evidence") or [])
    row["evidence"] = clean_evidence
    row["revalidation"] = {
        "operation_id": str(operation_id or ""),
        "reason": _limit_text(reason, 700),
        "revalidated_by_user_id": identity.canonical_user_id,
        "revalidated_by_role": identity.role,
        "revalidated_at": revalidated_at,
    }
    _append_jsonl(store, SELF_EVOLUTION_EVENTS_FILE, row)
    audit_row = {
        "record_type": "self_evolution_revalidation",
        "tenant_id": current_tenant_id(),
        "evolution_event_id": str(evolution_event_id or ""),
        "operation_id": str(operation_id or ""),
        "reason": _limit_text(reason, 700),
        "evidence": clean_evidence,
        "prior_status": "superseded",
        "outcome": "needs_retest",
        "revalidated_by_user_id": identity.canonical_user_id,
        "revalidated_by_role": identity.role,
        "revalidated_at": revalidated_at,
        "writeback_verified": True,
    }
    _append_jsonl(store, SELF_EVOLUTION_EVENTS_FILE, audit_row)
    return {
        "ok": True,
        "outcome": "needs_retest",
        "self_evolution_event": row,
        "revalidation_event": audit_row,
        "writeback_verified": True,
    }


def _normalize_status(value: Any, *, candidate_type: str, risk_level: str, writeback_verified: bool) -> str:
    requested = str(value or "").strip()
    if requested in ALLOWED_STATUSES:
        if candidate_type == "person_preference_candidate" and requested in {"applied", "ready_for_application"} and not writeback_verified:
            return "candidate"
        if risk_level == "high" and requested in {"applied", "ready_for_application"}:
            return "needs_confirmation"
        if risk_level == "medium" and requested == "applied":
            return "pending_review"
        return requested
    if risk_level == "high":
        return "needs_confirmation"
    if risk_level == "medium":
        return "pending_review"
    if candidate_type == "person_preference_candidate" and writeback_verified:
        return "applied"
    if candidate_type in {"self_correction", "tomorrow_focus"}:
        return "ready_for_application"
    return "candidate"


def _semantic_fingerprint(candidate: dict[str, Any]) -> str:
    base = "|".join(
        (
            current_tenant_id(),
            str(candidate.get("candidate_type") or ""),
            str(candidate.get("summary") or "").lower(),
            str(candidate.get("target_store") or ""),
            str(candidate.get("applies_to_user_id") or ""),
            str(candidate.get("applies_to_role") or ""),
            str(candidate.get("applies_to_scope") or ""),
        )
    )
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]


def _summary_is_incomplete(summary: str) -> bool:
    text = _limit_text(summary, 700).rstrip("，,；;：:。.!！?？… ")
    return any(text.endswith(suffix) for suffix in (
        "避免只说",
        "不要只说",
        "不能只说",
        "避免仅说",
        "不要仅说",
        "需要先",
        "必须先",
        "并且",
        "以及",
        "因为",
        "所以",
    ))


def _normalized_lesson_text(summary: str) -> str:
    text = str(summary or "").lower()
    text = re.sub(r"\d{4}[-年/]\d{1,2}[-月/]\d{1,2}日?", "", text)
    text = re.sub(r"\d{1,2}月\d{1,2}日", "", text)
    text = re.sub(r"\d{1,2}[:：]\d{2}", "", text)
    text = text.replace("仅答", "只说").replace("仅说", "只说").replace("小优", "")
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def _same_application_lesson(existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
    if str(existing.get("candidate_type") or "") != str(candidate.get("candidate_type") or ""):
        return False
    for key in ("target_store", "applies_to_user_id", "applies_to_role", "applies_to_scope"):
        if str(existing.get(key) or "") != str(candidate.get(key) or ""):
            return False
    left = _normalized_lesson_text(str(existing.get("summary") or ""))
    right = _normalized_lesson_text(str(candidate.get("summary") or ""))
    if min(len(left), len(right)) < 12:
        return False
    return SequenceMatcher(None, left, right).ratio() >= 0.78


def _normalize_application_scope(value: Any) -> str:
    scope = str(value or "").strip().lower()
    allowed = {
        "", "all_communication", "direct_reply", "daily_report", "task_followup",
        "teacher_support", "manager_support", "proactive_question", "autonomous_work",
    }
    return scope if scope in allowed else ""


def _has_new_evidence(existing: Any, candidate: Any) -> bool:
    def fingerprints(value: Any) -> set[str]:
        rows = value if isinstance(value, list) else []
        return {
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for item in rows
            if isinstance(item, (dict, list, str, int, float, bool)) or item is None
        }

    incoming = fingerprints(candidate)
    return bool(incoming - fingerprints(existing))


def _prefer_candidate_summary(candidate: str, existing: str) -> bool:
    if _summary_is_incomplete(existing) and not _summary_is_incomplete(candidate):
        return True
    if _summary_is_incomplete(candidate):
        return False
    return len(_normalized_lesson_text(candidate)) > len(_normalized_lesson_text(existing))


def _dedupe_application_events(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    selected: list[dict[str, Any]] = []
    suppressed = 0
    for item in events:
        match_index = next(
            (index for index, existing in enumerate(selected) if _same_application_lesson(existing, item)),
            None,
        )
        if match_index is None:
            selected.append(item)
            continue
        suppressed += 1
        existing = selected[match_index]
        if _prefer_candidate_summary(str(item.get("summary") or ""), str(existing.get("summary") or "")):
            selected[match_index] = item
    return selected, suppressed


def _default_reviewer(risk_level: str) -> str:
    if risk_level == "high":
        return "boss"
    if risk_level == "medium":
        return "boss_or_manager"
    return ""


def _safe_auto_effects(risk_level: str) -> dict[str, bool]:
    return {
        "sends_parent_messages": False,
        "sends_teacher_messages": False,
        "sends_owner_messages": False,
        "creates_teacher_tasks": False,
        "changes_salary": False,
        "changes_permissions": False,
        "changes_institution_policy": False,
        "changes_handbook": False,
        "updates_long_term_memory": False,
        "changes_router": False,
        "forces_next_action": False,
        "may_inform_next_context": risk_level == "low",
    }


def _boundary() -> dict[str, bool]:
    return {
        "model_led": True,
        "read_only_query": True,
        "sends_parent_messages": False,
        "sends_teacher_messages": False,
        "sends_owner_messages": False,
        "changes_policy": False,
        "changes_handbook": False,
        "changes_router": False,
        "forces_next_action": False,
    }


def _recent_workstyle_preferences(store: TuoguanStore, *, identity: UserIdentity, limit: int = 6) -> list[dict[str, Any]]:
    rows = [
        deepcopy(row)
        for row in _read_jsonl(store, WORKSTYLE_EVENTS_FILE)
        if str(row.get("record_type") or "") == "person_workstyle_preference"
        and str(row.get("status") or "active") == "active"
        and str(row.get("tenant_id") or "") in {"", current_tenant_id()}
        and str(row.get("target_user_id") or "") in {identity.canonical_user_id, identity.platform_user_id}
    ]
    rows.sort(key=lambda item: str(item.get("created_at") or ""))
    compact: list[dict[str, Any]] = []
    for row in rows[-max(1, min(limit, 20)):]:
        compact.append({
            "target_user_id": row.get("target_user_id"),
            "target_name": row.get("target_name"),
            "target_role": row.get("target_role"),
            "scope": row.get("scope"),
            "preference_type": row.get("preference_type"),
            "normalized_rule": row.get("normalized_rule") or row.get("preference_text"),
            "created_at": row.get("created_at"),
            "writeback_verified": True,
        })
    return compact


def _is_low_risk_application_status(item: dict[str, Any]) -> bool:
    return (
        str(item.get("risk_level") or "") == "low"
        and str(item.get("status") or "") in {"ready_for_application", "applied", "needs_retest"}
    )


def _is_next_context_candidate(item: dict[str, Any], *, now: datetime | None = None) -> bool:
    if str(item.get("risk_level") or "") != "low":
        return False
    if not evolution_evidence_is_usable(item.get("evidence")):
        return False
    ctype = str(item.get("candidate_type") or "")
    status = str(item.get("status") or "")
    if _summary_is_incomplete(str(item.get("summary") or "")):
        return False
    if ctype == "person_preference_candidate":
        return status == "applied" and bool(item.get("writeback_verified"))
    # ``applied`` means the lesson reached one turn and is awaiting a real
    # outcome evaluation.  Reloading it immediately on every turn would blur
    # distinct applications and could never prove which turn produced the
    # effect.  ``needs_retest`` is the explicit state that makes it eligible
    # again after an insufficient or non-improving evaluation.
    if status not in {"ready_for_application", "needs_retest"}:
        return False
    reference = now or datetime.now().astimezone()
    event_time = _evolution_event_time(item, reference)
    if event_time is None:
        return False
    max_age = timedelta(hours=36 if ctype == "tomorrow_focus" else 72)
    return reference - max_age <= event_time <= reference + timedelta(minutes=5)


def _evolution_event_time(item: dict[str, Any], reference: datetime) -> datetime | None:
    for key in ("updated_at", "created_at"):
        text = str(item.get(key) or "").strip()
        if not text:
            continue
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=reference.tzinfo)
        return parsed.astimezone(reference.tzinfo) if reference.tzinfo else parsed
    return None


def _anchor_relative_time_text(text: str, *, item: dict[str, Any], now: datetime) -> str:
    event_time = _evolution_event_time(item, now)
    if event_time is None or event_time.date() == now.date():
        return text
    event_label = f"{event_time.month}月{event_time.day}日"
    previous = event_time - timedelta(days=1)
    following = event_time + timedelta(days=1)
    anchored = str(text or "")
    anchored = re.sub(r"今日|今天", event_label, anchored)
    anchored = anchored.replace("今晚", f"{event_label}晚")
    anchored = anchored.replace("昨晚", f"{previous.month}月{previous.day}日晚")
    anchored = anchored.replace("明天", f"{following.month}月{following.day}日")
    anchored = anchored.replace("明日", f"{following.month}月{following.day}日")
    return anchored


def _evolution_context_line(item: dict[str, Any], *, now: datetime | None = None) -> str:
    ctype = str(item.get("candidate_type") or "")
    reference = now or datetime.now().astimezone()
    summary = _concise_context_summary(str(item.get("summary") or ""), item=item, now=reference, limit=100)
    if not summary:
        return ""
    if ctype == "self_correction":
        return f"避免重复错误：{summary}"
    if ctype == "tomorrow_focus":
        return f"今日重点：{summary}"
    if ctype == "person_preference_candidate":
        return f"服务偏好：{summary}"
    if ctype == "multi_agent_adoption":
        return f"已采纳顾问建议：{summary}"
    return summary


def _concise_context_summary(text: str, *, item: dict[str, Any], now: datetime, limit: int) -> str:
    anchored = _anchor_relative_time_text(_limit_text(text, 700), item=item, now=now)
    if len(anchored) <= limit:
        return anchored
    for match in re.finditer(r"[。！？!?；;]", anchored):
        end = match.end()
        if 12 <= end <= limit:
            return anchored[:end].strip()
    return _limit_text(anchored, limit)


def _health_signals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    open_review = [
        row for row in rows
        if str(row.get("status") or "") in {"pending_review", "needs_confirmation"}
    ]
    tool_failures = [
        row for row in rows
        if str(row.get("candidate_type") or "") == "tool_failure_or_bug"
        and str(row.get("status") or "") in {"pending_review", "needs_confirmation", "candidate"}
    ]
    corrections = [
        row for row in rows
        if str(row.get("candidate_type") or "") == "self_correction"
    ]
    unverified_evidence = [
        row for row in rows
        if _is_low_risk_application_status(row)
        and not evolution_evidence_is_usable(row.get("evidence"))
    ]
    return {
        "open_review_count": len(open_review),
        "tool_failure_candidate_count": len(tool_failures),
        "self_correction_count": len(corrections),
        "has_repeated_tool_failure": len(tool_failures) >= 2,
        "has_recent_self_correction": bool(corrections),
        "unverified_application_evidence_count": len(unverified_evidence),
    }


def _render_saved(row: dict[str, Any]) -> str:
    risk = str(row.get("risk_level") or "")
    status = str(row.get("status") or "")
    return f"已记录小优自我进化候选：{_limit_text(row.get('summary'), 80)}（风险：{risk}，状态：{status}）。"


def _render_ledger(rows: list[dict[str, Any]], health: dict[str, Any]) -> str:
    lines = [f"查到 {len(rows)} 条小优自我进化记录。"]
    if health.get("open_review_count"):
        lines.append(f"- {health.get('open_review_count')} 条需要人工确认或复核。")
    if health.get("tool_failure_candidate_count"):
        lines.append(f"- {health.get('tool_failure_candidate_count')} 条工具/能力缺口候选。")
    if rows:
        latest = rows[-1]
        lines.append(f"- 最近一条：{_limit_text(latest.get('summary'), 100)}")
    lines.append("这些记录只作为经验和审核材料，不自动改变制度、权限、手册或外发边界。")
    return "\n".join(lines)


def _render_brief(applicable: list[str], review_queue: list[dict[str, Any]], workstyles: list[dict[str, Any]]) -> str:
    lines = ["小优自我进化简报："]
    if applicable:
        lines.append("- 今日应带入：" + "；".join(applicable[:5]))
    else:
        lines.append("- 今日没有新的低风险经验需要带入。")
    if workstyles:
        lines.append(f"- 已验证个人工作方式偏好：{len(workstyles)} 条。")
    if review_queue:
        lines.append(f"- 待人工确认进化候选：{len(review_queue)} 条。")
    lines.append("- 边界：只提供经验材料，不替模型决策，不改权限/制度/手册，不外发。")
    return "\n".join(lines)
