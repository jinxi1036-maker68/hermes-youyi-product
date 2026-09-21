"""Operational fact discovery and confirmation for Youyi.

This module is not an intent router. It only stores and reads facts after the
Hermes Agent decides that an operational fact is needed.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import json
import uuid
from typing import Any

from .models import UserIdentity
from .responsibility_resolver import REGULAR_PROGRAM_ID, SUMMER_PROGRAM_ID, regular_manager_names
from .store import TuoguanStore
from .tenant_context import current_tenant_id


FACTS_FILE = "operational_facts.json"
CANDIDATES_FILE = "operational_fact_candidates.jsonl"
ONBOARDING_FILE = "institution_onboarding_state.json"

HIGH_RISK_FACT_TYPES = {
    "permission_rule",
    "salary_rule",
    "parent_auto_send_rule",
    "data_delete_rule",
    "safety_closure_rule",
    "long_term_policy",
}
MEDIUM_RISK_FACT_TYPES = {
    "manager_scope",
    "teacher_responsibility",
    "student_service_type",
    "student_responsible_teacher",
    "business_line_owner",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _fact_id() -> str:
    return f"fact_{uuid.uuid4().hex[:12]}"


def _candidate_id() -> str:
    return f"fact_candidate_{uuid.uuid4().hex[:12]}"


def _read_facts(store: TuoguanStore) -> dict[str, Any]:
    data = store.read_json(FACTS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("schema_version", 1)
    data.setdefault("tenant_id", current_tenant_id())
    data.setdefault("facts", [])
    data.setdefault("updated_at", "")
    if not isinstance(data["facts"], list):
        data["facts"] = []
    return data


def _write_facts(store: TuoguanStore, data: dict[str, Any]) -> None:
    data["updated_at"] = now_iso()
    store.write_json(FACTS_FILE, data)


def _append_candidate(store: TuoguanStore, row: dict[str, Any]) -> None:
    from .write_guard import assert_business_write_allowed
    assert_business_write_allowed(store.data_dir, CANDIDATES_FILE)
    path = store.path_for(CANDIDATES_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _read_candidates(store: TuoguanStore) -> list[dict[str, Any]]:
    path = store.path_for(CANDIDATES_FILE)
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


def _role_can_confirm(identity: UserIdentity, fact_type: str, scope: str) -> bool:
    if fact_type in HIGH_RISK_FACT_TYPES:
        return identity.role == "boss"
    if identity.role == "boss":
        return True
    if identity.role == "manager" and fact_type in MEDIUM_RISK_FACT_TYPES:
        return str(scope or "").startswith(("program:", "student:", "teacher:", "business_line:"))
    return False


def _risk_level(fact_type: str) -> str:
    if fact_type in HIGH_RISK_FACT_TYPES:
        return "high"
    if fact_type in MEDIUM_RISK_FACT_TYPES:
        return "medium"
    return "low"


def submit_operational_fact_candidate(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    fact_type: str,
    subject: str,
    value: Any,
    scope: str = "institution",
    source_text: str = "",
    operation_id: str = "",
) -> dict[str, Any]:
    fact_type = str(fact_type or "").strip()
    subject = str(subject or "").strip()
    scope = str(scope or "institution").strip()
    if not fact_type or not subject:
        return {"ok": False, "error": "missing_fact_type_or_subject", "message": "缺少事实类型或事实对象。"}
    risk = _risk_level(fact_type)
    can_confirm = _role_can_confirm(identity, fact_type, scope)
    status = "confirmed" if can_confirm and risk in {"low", "medium"} else "pending_confirmation"
    if risk == "high" and identity.role == "boss":
        status = "pending_confirmation"
    candidate = {
        "candidate_id": _candidate_id(),
        "tenant_id": current_tenant_id(),
        "fact_type": fact_type,
        "subject": subject,
        "value": deepcopy(value),
        "scope": scope,
        "risk_level": risk,
        "status": status,
        "source_text": str(source_text or ""),
        "source": {
            "actor_user_id": identity.canonical_user_id,
            "actor_name": identity.person_name,
            "actor_role": identity.role,
            "operation_id": str(operation_id or ""),
        },
        "created_at": now_iso(),
    }
    _append_candidate(store, candidate)
    fact: dict[str, Any] | None = None
    if status == "confirmed":
        fact = _upsert_fact_from_candidate(store, candidate, confirmed_by=identity.canonical_user_id)
    return {
        "ok": True,
        "candidate": candidate,
        "confirmed_fact": fact,
        "writeback_verified": True,
        "rendered_text": _render_candidate(candidate, fact),
    }


def _upsert_fact_from_candidate(store: TuoguanStore, candidate: dict[str, Any], *, confirmed_by: str) -> dict[str, Any]:
    data = _read_facts(store)
    facts = data["facts"]
    key = (
        str(candidate.get("fact_type") or ""),
        str(candidate.get("subject") or ""),
        str(candidate.get("scope") or ""),
    )
    existing = None
    for item in facts:
        if not isinstance(item, dict):
            continue
        item_key = (str(item.get("fact_type") or ""), str(item.get("subject") or ""), str(item.get("scope") or ""))
        if item_key == key and item.get("status") == "active":
            existing = item
            break
    row = {
        "fact_id": existing.get("fact_id") if existing else _fact_id(),
        "tenant_id": current_tenant_id(),
        "fact_type": candidate.get("fact_type"),
        "subject": candidate.get("subject"),
        "value": deepcopy(candidate.get("value")),
        "scope": candidate.get("scope"),
        "risk_level": candidate.get("risk_level"),
        "status": "active",
        "source_candidate_id": candidate.get("candidate_id"),
        "source_text": candidate.get("source_text"),
        "source": deepcopy(candidate.get("source") or {}),
        "confirmed_by": confirmed_by,
        "confirmed_at": now_iso(),
        "updated_at": now_iso(),
    }
    if existing:
        existing.update(row)
        fact = deepcopy(existing)
    else:
        facts.append(row)
        fact = deepcopy(row)
    _write_facts(store, data)
    verify = _read_facts(store)
    if not any(isinstance(item, dict) and item.get("fact_id") == fact["fact_id"] for item in verify.get("facts") or []):
        raise RuntimeError("operational_fact_writeback_failed")
    return fact


def confirm_operational_fact(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    candidate_id: str,
    decision: str,
    note: str = "",
) -> dict[str, Any]:
    if identity.role != "boss":
        return {"ok": False, "error": "permission_denied", "message": "只有老板可以确认或驳回机构运营事实。"}
    target = None
    for item in reversed(_read_candidates(store)):
        if str(item.get("candidate_id") or "") == str(candidate_id or ""):
            target = deepcopy(item)
            break
    if not target:
        return {"ok": False, "error": "candidate_not_found", "message": "没有找到这条待确认事实。"}
    decision = str(decision or "").strip().lower()
    if decision not in {"approve", "reject"}:
        return {"ok": False, "error": "invalid_decision", "message": "decision 必须是 approve 或 reject。"}
    target["review"] = {
        "decision": decision,
        "reviewer": identity.canonical_user_id,
        "reviewer_name": identity.person_name,
        "reviewed_at": now_iso(),
        "note": str(note or ""),
    }
    target["status"] = "confirmed" if decision == "approve" else "rejected"
    _append_candidate(store, target)
    fact = _upsert_fact_from_candidate(store, target, confirmed_by=identity.canonical_user_id) if decision == "approve" else None
    return {
        "ok": True,
        "candidate": target,
        "confirmed_fact": fact,
        "writeback_verified": decision != "approve" or bool(fact),
        "rendered_text": "已确认这条运营事实。" if decision == "approve" else "已驳回这条运营事实，不会用于后续判断。",
    }


def query_operational_facts(
    store: TuoguanStore,
    *,
    fact_type: str = "",
    subject: str = "",
    scope: str = "",
    include_pending: bool = False,
) -> dict[str, Any]:
    facts = [deepcopy(item) for item in _read_facts(store).get("facts") or [] if isinstance(item, dict)]
    if fact_type:
        facts = [item for item in facts if str(item.get("fact_type") or "") == str(fact_type)]
    if subject:
        facts = [item for item in facts if str(item.get("subject") or "") == str(subject)]
    if scope:
        facts = [item for item in facts if str(item.get("scope") or "") == str(scope)]
    pending: list[dict[str, Any]] = []
    if include_pending:
        pending = [item for item in _read_candidates(store) if item.get("status") == "pending_confirmation"]
    return {
        "ok": True,
        "facts": facts,
        "fact_count": len(facts),
        "pending_candidates": pending,
        "pending_count": len(pending),
        "rendered_text": _render_facts(facts, pending),
        "render_verified": True,
    }


def onboarding_gap_audit(store: TuoguanStore, *, program_id: str = REGULAR_PROGRAM_ID) -> dict[str, Any]:
    staff = store.read_json("staff.json", {})
    students = store.read_json("students.json", {})
    if not isinstance(staff, dict):
        staff = {}
    if not isinstance(students, dict):
        students = {}
    facts = query_operational_facts(store).get("facts") or []
    active_regular: list[tuple[str, dict[str, Any]]] = []
    for name, profile in students.items():
        if not isinstance(profile, dict):
            continue
        if str(profile.get("status") or "active") in {"inactive", "cancelled", "left"}:
            continue
        if str(profile.get("campus_id") or "") == "main" or str(profile.get("program_id") or "") == program_id:
            active_regular.append((str(name), profile))
    missing_service_mode = [name for name, profile in active_regular if not any(profile.get(k) for k in ("service_mode", "care_type", "tuoguan_type", "attendance_mode", "enrollment_type"))]
    missing_teacher = [name for name, profile in active_regular if not any(profile.get(k) for k in ("teacher", "teacher_user_id", "primary_teacher_user_id", "main_teacher_user_id"))]
    manager_names = regular_manager_names(store)
    known_manager = bool(manager_names)
    business_lines = _business_lines(students)
    gaps = []
    if not known_manager:
        gaps.append(_gap("regular_manager", "正式托管店长/负责人", "boss", "确认正式托管谁负责日常管理。"))
    if missing_service_mode:
        gaps.append(_gap("student_service_mode", "学生午托/晚托/全托类型", "manager", f"{len(missing_service_mode)}名正式托管学生缺少服务类型。"))
    if missing_teacher:
        gaps.append(_gap("student_responsible_teacher", "学生主责老师", "manager", f"{len(missing_teacher)}名正式托管学生缺少主责老师。"))
    if not any(str(item.get("fact_type") or "") == "owner_current_goal" for item in facts):
        gaps.append(_gap("owner_current_goal", "老板当前经营目标", "boss", "需要确认当前优先是招生、续费、服务质量、风险还是执行力。"))
    priority = gaps[:5]
    result = {
        "ok": True,
        "program_id": program_id,
        "business_lines": business_lines,
        "staff_count": len(staff),
        "regular_student_count": len(active_regular),
        "known_fact_count": len(facts),
        "gap_count": len(gaps),
        "gaps": gaps,
        "priority_questions": priority,
        "missing_service_mode_students": missing_service_mode[:50],
        "missing_teacher_students": missing_teacher[:50],
        "rendered_text": _render_gap_audit(len(active_regular), len(staff), business_lines, gaps),
        "render_verified": True,
    }
    return result


def _business_lines(students: dict[str, Any]) -> list[str]:
    lines = {"formal_tuoguan"}
    for profile in students.values():
        if isinstance(profile, dict) and str(profile.get("program_id") or profile.get("campus_id") or "") == SUMMER_PROGRAM_ID:
            lines.add("summer_program")
    return sorted(lines)


def _gap(gap_type: str, title: str, ask_role: str, reason: str) -> dict[str, Any]:
    return {
        "gap_type": gap_type,
        "title": title,
        "ask_role": ask_role,
        "reason": reason,
        "business_value": "补齐后，Hermes 才能围绕续费、招生、服务证据、执行力和风险控制做可靠推进。",
    }


def _render_candidate(candidate: dict[str, Any], fact: dict[str, Any] | None) -> str:
    if fact:
        return f"已记录并确认运营事实：{candidate.get('subject')}。后续我会按这个事实辅助判断。"
    return f"已记录一条待确认运营事实：{candidate.get('subject')}。确认前我不会把它当成正式规则。"


def _render_facts(facts: list[dict[str, Any]], pending: list[dict[str, Any]]) -> str:
    lines = [f"当前已确认运营事实 {len(facts)} 条。"]
    for item in facts[:10]:
        lines.append(f"- {item.get('fact_type')}｜{item.get('subject')}｜范围：{item.get('scope')}")
    if pending:
        lines.append(f"待确认事实 {len(pending)} 条：")
        for item in pending[:10]:
            lines.append(f"- {item.get('candidate_id')}｜{item.get('fact_type')}｜{item.get('subject')}")
    return "\n".join(lines)


def _render_gap_audit(student_count: int, staff_count: int, business_lines: list[str], gaps: list[dict[str, Any]]) -> str:
    lines = [
        "我先按“新入职数字员工”的方式盘点本机构：",
        f"- 已看到员工 {staff_count} 人、正式托管学生约 {student_count} 人。",
        f"- 当前业务线：{', '.join(business_lines)}。",
    ]
    if not gaps:
        lines.append("目前没有发现阻塞机构理解的关键缺口。下一步可以让老板设定本月经营目标。")
        return "\n".join(lines)
    lines.append("当前最需要补齐的信息：")
    for idx, gap in enumerate(gaps[:5], 1):
        lines.append(f"{idx}. {gap['title']}：{gap['reason']} 建议问{_role_label(gap['ask_role'])}。")
    lines.append("这些问题不是为了填表，是为了后续能更准确地提升续费、招生、服务质量和风险控制。")
    return "\n".join(lines)


def _role_label(role: str) -> str:
    return {"boss": "老板", "manager": "店长", "teacher": "老师"}.get(role, role)
