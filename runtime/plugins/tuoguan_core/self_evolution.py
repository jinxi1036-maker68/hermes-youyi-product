"""Self-evolution ledger for Xiaoyou as a digital employee.

This module records learning candidates and next-day context only. It does not
send messages, modify institution policy, update the handbook, or route model
intent. Low-risk entries may inform future context; medium/high-risk entries
remain review material.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
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
    review_required_by: str = "",
    source_text: str = "",
    source_message_id: str = "",
    cadence_mode: str = "",
    writeback_verified: bool = False,
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
        "review_required_by": review_required_by,
        "source_text": source_text,
        "writeback_verified": bool(writeback_verified),
    })
    if not candidate["summary"]:
        return {"ok": False, "error": "self_evolution_summary_required", "message": "进化候选必须包含摘要。"}
    fingerprint = _semantic_fingerprint(candidate)
    for existing in reversed(_read_jsonl(store, SELF_EVOLUTION_EVENTS_FILE)[-200:]):
        if str(existing.get("tenant_id") or "") not in {"", current_tenant_id()}:
            continue
        if str(existing.get("semantic_fingerprint") or "") == fingerprint:
            merged = deepcopy(existing)
            merged["occurrence_count"] = int(existing.get("occurrence_count") or 1) + 1
            merged["updated_at"] = now_iso()
            merged["evidence"] = _list_any([*(existing.get("evidence") or []), *candidate["evidence"]], 8)
            if str(existing.get("status") or "") not in {"verified", "applied"}:
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
        "created_at": now_iso(),
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
) -> dict[str, Any]:
    if identity.role in {"boss", "manager"} or identity.platform == "system":
        ledger = query_self_evolution_ledger(store, identity=identity, limit=max(limit, 20))
        events = ledger.get("events") if ledger.get("ok") else []
        events = events if isinstance(events, list) else []
    else:
        events = [item for item in _filtered_events(store) if _applies_to_identity(item, identity)][-max(limit, 20):]
        ledger = {"health_signals": _health_signals(events)}
    recent_workstyles = _recent_workstyle_preferences(store, identity=identity, limit=6)
    applicable_events = [
        item for item in events
        if _is_next_context_candidate(item) and _applies_to_identity(item, identity)
    ][-max(1, min(limit, 12)):]
    applicable = [
        _evolution_context_line(item)
        for item in applicable_events
    ]
    applicable = [line for line in applicable if line][-max(1, min(limit, 12)):]
    review_queue = [
        item for item in events
        if str(item.get("status") or "") in {"pending_review", "needs_confirmation"}
    ][-8:]
    return {
        "ok": True,
        "tenant_id": current_tenant_id(),
        "report_type": "xiaoyou_self_evolution_brief_v1",
        "event_count": len(events),
        "recent_events": deepcopy(events[-max(1, min(limit, 20)):]),
        "next_day_context": applicable,
        "next_day_application_ids": [str(item.get("evolution_event_id") or "") for item in applicable_events],
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
) -> str:
    brief = build_self_evolution_brief(store, identity=identity, limit=limit)
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
    return "\n".join(lines)


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
    if not target_user and not target_role:
        text = "".join(str(item.get(key) or "") for key in ("summary", "proposed_effect", "next_effect"))
        if any(term in text for term in ("李老师", "CeShi")):
            return identity.canonical_user_id == "CeShi" or identity.platform_user_id == "CeShi"
        if any(term in text for term in ("老板", "金总", "老板日报")):
            return identity.role == "boss"
        if "店长" in text:
            return identity.role == "manager"
        if "老师" in text:
            return identity.role == "teacher"
    return True


def record_self_evolution_application(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    source_message_id: str,
    final_reply: str,
    tool_write_verified: bool = False,
    limit: int = 3,
) -> dict[str, Any]:
    """Record that ready experience was actually carried into a real reply."""

    candidates = [
        item for item in _filtered_events(store)
        if str(item.get("risk_level") or "") == "low"
        and str(item.get("status") or "") == "ready_for_application"
        and _applies_to_identity(item, identity)
    ][-max(1, min(int(limit or 3), 3)):]
    applied: list[dict[str, Any]] = []
    for candidate in candidates:
        row = deepcopy(candidate)
        row["status"] = "applied"
        row["updated_at"] = now_iso()
        row["application_evidence"] = {
            "source_message_id": str(source_message_id or ""),
            "target_user_id": identity.canonical_user_id,
            "target_role": identity.role,
            "reply_excerpt": _limit_text(final_reply, 300),
            "tool_write_verified": bool(tool_write_verified),
        }
        _append_jsonl(store, SELF_EVOLUTION_EVENTS_FILE, row)
        applied.append(row)
    return {
        "ok": True,
        "applied_count": len(applied),
        "applied_event_ids": [str(item.get("evolution_event_id") or "") for item in applied],
        "state_changed": bool(applied),
        "writeback_verified": bool(applied),
    }


def verify_self_evolution_application(
    store: TuoguanStore,
    *,
    evolution_event_id: str,
    succeeded: bool,
    evidence: str,
) -> dict[str, Any]:
    """Close one applied experience only when a later check has real evidence."""

    target = next(
        (item for item in reversed(_filtered_events(store)) if str(item.get("evolution_event_id") or "") == str(evolution_event_id or "")),
        None,
    )
    if target is None:
        return {"ok": False, "error": "evolution_event_not_found", "writeback_verified": False}
    row = deepcopy(target)
    row["status"] = "verified" if succeeded else "failed"
    row["updated_at"] = now_iso()
    row["verification_evidence"] = _limit_text(evidence, 700)
    _append_jsonl(store, SELF_EVOLUTION_EVENTS_FILE, row)
    return {"ok": True, "self_evolution_event": row, "writeback_verified": True}


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
        )
    )
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]


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


def _is_next_context_candidate(item: dict[str, Any]) -> bool:
    if str(item.get("risk_level") or "") != "low":
        return False
    ctype = str(item.get("candidate_type") or "")
    status = str(item.get("status") or "")
    if ctype == "person_preference_candidate":
        return status == "applied" and bool(item.get("writeback_verified"))
    return status in {"ready_for_application", "applied"}


def _evolution_context_line(item: dict[str, Any]) -> str:
    ctype = str(item.get("candidate_type") or "")
    summary = _limit_text(item.get("summary"), 100)
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
    return {
        "open_review_count": len(open_review),
        "tool_failure_candidate_count": len(tool_failures),
        "self_correction_count": len(corrections),
        "has_repeated_tool_failure": len(tool_failures) >= 2,
        "has_recent_self_correction": bool(corrections),
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
