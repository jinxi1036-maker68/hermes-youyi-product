"""Read-only state and low-risk evidence candidates for Hermes as a digital employee.

This module stores facts, candidates, evidence and work state only. It does not
route user intent, send messages, assign salary, delete data, or decide the
model's next business action.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import json
import re
import uuid
from typing import Any

from .models import UserIdentity
from .store import TuoguanStore
from .tenant_context import current_tenant_id, read_institution_operating_model


SERVICE_RELATIONS_FILE = "service_relations.json"
SERVICE_RELATION_CANDIDATES_FILE = "service_relation_candidates.jsonl"
INFORMATION_REQUESTS_FILE = "information_requests.jsonl"
PROFILE_CANDIDATES_FILE = "profile_candidates.jsonl"
PROFILE_CORRECTIONS_FILE = "profile_candidate_corrections.jsonl"
GOAL_EVIDENCE_FILE = "goal_evidence.jsonl"
PERFORMANCE_EVIDENCE_FILE = "performance_evidence_candidates.jsonl"
PERFORMANCE_EVIDENCE_RESPONSES_FILE = "performance_evidence_responses.jsonl"
VALUE_LEDGER_FILE = "value_ledger.jsonl"
GRAY_OBSERVATIONS_FILE = "gray_observations.jsonl"
GRAY_ROLLOUT_DECISIONS_FILE = "gray_rollout_decisions.jsonl"
GRAY_OPTIMIZATION_DECISIONS_FILE = "gray_optimization_decisions.jsonl"
HERMES_WORK_ITEMS_FILE = "hermes_work_items.jsonl"
WAKEUP_REQUESTS_FILE = "wakeup_requests.jsonl"
BUSINESS_EVENTS_FILE = "business_events.jsonl"
ACTION_EXECUTIONS_FILE = "action_executions.jsonl"
INSTITUTION_UNDERSTANDING_FILE = "institution_understanding_state.json"
HERMES_EMPLOYEE_SCORECARD_FILE = "hermes_employee_scorecard.jsonl"
INDUSTRY_LEARNING_CANDIDATES_FILE = "industry_learning_candidates.jsonl"
EXTERNAL_RESEARCH_RUNS_FILE = "external_research_runs.jsonl"
MARKET_RESEARCH_CANDIDATES_FILE = "market_research_candidates.jsonl"
COMPETITOR_PROFILES_FILE = "competitor_profiles.jsonl"
WEEKLY_MARKET_REPORT_RUNS_FILE = "weekly_market_report_runs.jsonl"
INSTITUTION_FACT_GAP_EVENTS_FILE = "institution_fact_gap_events.jsonl"
VALUE_PROGRESS_LEDGER_FILE = "value_progress_ledger.jsonl"
AGENT_DELEGATIONS_FILE = "agent_delegations.jsonl"
AGENT_DELEGATION_RESULTS_FILE = "agent_delegation_results.jsonl"
ATTENTION_THREADS_FILE = "attention_threads.jsonl"
RELATIONSHIP_TOUCH_CANDIDATES_FILE = "relationship_touch_candidates.jsonl"
RELATIONSHIP_TOUCH_POLICY_FILE = "relationship_touch_policy.json"

_CLOSED_STUDENT_STATUSES = {"inactive", "cancelled", "left", "deleted", "graduated"}
_CLOSED_TASK_STATUSES = {"completed", "cancelled", "closed", "done", "closed_by_admin", "completed_by_admin"}
_PARENT_COMM_TERMS = ("家长", "妈妈", "爸爸", "沟通", "反馈", "续费", "转化", "回访")
_ATTENTION_STATUSES = {"candidate", "queued", "sent", "replied", "resolved", "failed", "superseded"}
_OPEN_ATTENTION_STATUSES = {"candidate", "queued", "sent", "replied", "failed"}
_RELATIONSHIP_TOUCH_TYPES = {
    "care", "encouragement", "thanks", "light_chat", "record_relief",
    "material整理", "material_support", "manager_assist", "owner_business",
    "owner_progress", "presence_report",
}
_RELATIONSHIP_TOUCH_STATUSES = {"candidate", "queued", "sent", "suppressed", "resolved", "failed", "superseded"}
_OPEN_RELATIONSHIP_TOUCH_STATUSES = {"candidate", "queued", "sent", "failed"}


DEFAULT_RELATIONSHIP_TOUCH_POLICY: dict[str, Any] = {
    "schema_version": 1,
    "boss": {
        "mode": "direct",
        "allowed_start": "08:00",
        "allowed_end": "19:00",
        "daily_limit": 2,
        "allowed_types": ["owner_business", "owner_progress", "presence_report"],
    },
    "manager": {
        "mode": "candidate",
        "allowed_start": "10:00",
        "allowed_end": "18:30",
        "daily_limit": 1,
        "allowed_types": ["manager_assist", "encouragement", "record_relief"],
    },
    "teacher": {
        "mode": "candidate",
        "allowed_start": "10:30",
        "allowed_end": "18:30",
        "daily_limit": 1,
        "allowed_types": ["care", "encouragement", "thanks", "light_chat", "record_relief", "material_support"],
    },
    "privacy": {
        "private_chat_enters_boss_material": False,
        "emotional_support_affects_performance": False,
        "business_evidence_requires_explicit_child_or_task_fact": True,
    },
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _append_jsonl(store: TuoguanStore, filename: str, row: dict[str, Any]) -> None:
    from .write_guard import assert_business_write_allowed

    assert_business_write_allowed(store.data_dir, filename)
    path = store.path_for(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


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


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19] if "%H" in fmt else text[:10], fmt)
        except ValueError:
            continue
    return None


def _record_time(record: dict[str, Any]) -> datetime | None:
    return _parse_time(record.get("timestamp") or record.get("created_at") or record.get("time") or record.get("date"))


def _students(store: TuoguanStore) -> dict[str, dict[str, Any]]:
    data = store.read_json("students.json", {})
    if not isinstance(data, dict):
        return {}
    return {str(name): deepcopy(profile) for name, profile in data.items() if isinstance(profile, dict)}


def _visible_student_names(store: TuoguanStore, identity: UserIdentity) -> list[str]:
    from .permissions import PermissionService

    permissions = PermissionService(store)
    return sorted(name for name in _students(store) if permissions.can_view_student(identity, name))


def _active_regular_students(store: TuoguanStore, identity: UserIdentity, *, program_id: str = "regular_tuoguan") -> list[str]:
    students = _students(store)
    visible = set(_visible_student_names(store, identity))
    names: list[str] = []
    for name, profile in students.items():
        if name not in visible:
            continue
        status = str(profile.get("status") or "active").lower()
        if status in _CLOSED_STUDENT_STATUSES:
            continue
        if str(profile.get("program_id") or program_id) == program_id or str(profile.get("campus_id") or "") == "main":
            names.append(name)
    return sorted(names)


def _records(store: TuoguanStore) -> list[dict[str, Any]]:
    rows = store.read_json("records.json", [])
    if not isinstance(rows, list):
        return []
    return [deepcopy(row) for row in rows if isinstance(row, dict)]


def _latest_record(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not records:
        return None
    return max(records, key=lambda item: _record_time(item) or datetime.min)


def query_student_service_relations(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    student_name: str = "",
    program_id: str = "regular_tuoguan",
) -> dict[str, Any]:
    visible = set(_visible_student_names(store, identity))
    students = _students(store)
    relation_doc = store.read_json(SERVICE_RELATIONS_FILE, {"schema_version": 1, "relations": []})
    explicit = relation_doc.get("relations") if isinstance(relation_doc, dict) else []
    if not isinstance(explicit, list):
        explicit = []
    requested = str(student_name or "").strip()
    names = [requested] if requested else sorted(visible)
    relations: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for name in names:
        if name not in students or name not in visible:
            continue
        profile = students[name]
        rows = [
            deepcopy(row)
            for row in explicit
            if isinstance(row, dict)
            and str(row.get("student_name") or "") == name
            and (not program_id or str(row.get("program_id") or program_id) == program_id)
        ]
        if not rows:
            rows = [{
                "relation_id": "",
                "student_name": name,
                "program_id": str(profile.get("program_id") or program_id),
                "campus_id": str(profile.get("campus_id") or ""),
                "service_type": str(profile.get("service_mode") or profile.get("care_type") or profile.get("tuoguan_type") or profile.get("attendance_mode") or ""),
                "responsible_teacher_user_id": str(profile.get("teacher") or profile.get("teacher_user_id") or profile.get("primary_teacher_user_id") or ""),
                "unified_owner_user_id": str(profile.get("unified_owner_user_id") or profile.get("main_teacher_user_id") or profile.get("teacher") or ""),
                "status": str(profile.get("status") or "active"),
                "source": "students_profile_fallback",
            }]
        for row in rows:
            relations.append(row)
            missing_fields = [
                field for field in ("service_type", "responsible_teacher_user_id")
                if not str(row.get(field) or "").strip()
            ]
            if missing_fields:
                missing.append({"student_name": name, "missing_fields": missing_fields})
    return {
        "ok": True,
        "relations": relations,
        "relation_count": len(relations),
        "missing": missing,
        "missing_count": len(missing),
        "rendered_text": f"查到 {len(relations)} 条学生服务关系，{len(missing)} 个学生存在服务类型或责任老师缺口。",
        "render_verified": True,
    }


def query_weekly_record_coverage(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    days: int = 7,
    program_id: str = "regular_tuoguan",
) -> dict[str, Any]:
    days = max(1, min(int(days or 7), 60))
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    names = _active_regular_students(store, identity, program_id=program_id)
    by_student = {name: [] for name in names}
    for record in _records(store):
        name = str(record.get("student_name") or record.get("student") or "").strip()
        if name not in by_student:
            continue
        when = _record_time(record)
        if when and when.replace(tzinfo=None) >= cutoff.replace(tzinfo=None):
            by_student[name].append(record)
    covered = []
    missing = []
    for name, rows in by_student.items():
        latest = _latest_record(rows)
        item = {
            "student_name": name,
            "record_count": len(rows),
            "last_record_at": str((latest or {}).get("timestamp") or (latest or {}).get("created_at") or ""),
        }
        (covered if rows else missing).append(item)
    return {
        "ok": True,
        "days": days,
        "total_students": len(names),
        "covered_count": len(covered),
        "missing_count": len(missing),
        "covered_students": covered,
        "missing_students": missing,
        "rendered_text": f"近 {days} 天表现记录覆盖 {len(covered)}/{len(names)}，缺口 {len(missing)} 名学生。",
        "render_verified": True,
    }


def query_parent_communication_coverage(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    days: int = 31,
    program_id: str = "regular_tuoguan",
) -> dict[str, Any]:
    days = max(1, min(int(days or 31), 120))
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    names = _active_regular_students(store, identity, program_id=program_id)
    by_student = {name: [] for name in names}
    for record in _records(store):
        name = str(record.get("student_name") or record.get("student") or "").strip()
        if name not in by_student:
            continue
        text = str(record.get("content") or record.get("source_text") or record.get("summary") or "")
        if not any(term in text for term in _PARENT_COMM_TERMS):
            continue
        when = _record_time(record)
        if when and when.replace(tzinfo=None) >= cutoff.replace(tzinfo=None):
            by_student[name].append(record)
    covered = []
    missing = []
    for name, rows in by_student.items():
        latest = _latest_record(rows)
        item = {
            "student_name": name,
            "communication_evidence_count": len(rows),
            "last_evidence_at": str((latest or {}).get("timestamp") or (latest or {}).get("created_at") or ""),
        }
        (covered if rows else missing).append(item)
    result = {
        "ok": True,
        "days": days,
        "total_students": len(names),
        "covered_count": len(covered),
        "missing_count": len(missing),
        "covered_students": covered,
        "missing_students": missing,
        "evidence_rule": "record content contains parent-communication terms; this is coverage evidence, not proof of final parent satisfaction.",
        "rendered_text": f"近 {days} 天家校沟通证据覆盖 {len(covered)}/{len(names)}，缺口 {len(missing)} 名学生。",
        "render_verified": True,
    }
    term_state = store.read_json("academic_term_state.json", {})
    if (
        isinstance(term_state, dict)
        and term_state.get("service_relation_policy") == "defer_until_new_term"
    ):
        result.update({
            "data_term": term_state.get("data_term") or "previous_term",
            "roster_confidence": term_state.get("roster_confidence") or "historical_snapshot_only",
            "service_relation_policy": "defer_until_new_term",
            "confirmation_window_start": term_state.get("confirmation_window_start") or "2026-08-25",
            "confirmation_window_end": term_state.get("confirmation_window_end") or "2026-09-10",
            "covered_students": [],
            "missing_students": [],
            "rendered_text": (
                f"历史档案口径：近 {days} 天家校沟通证据覆盖 {len(covered)}/{len(names)}。"
                "这些学生档案不是已确认的新学期在读名单，只能用于历史分析；"
                "新学期名单与责任关系应在确认窗口重新核实。"
            ),
        })
    return result


def query_active_goal_work_state(store: TuoguanStore, *, identity: UserIdentity, goal_id: str = "") -> dict[str, Any]:
    goals = store.read_json("goal_operator_goals.json", {"goals": []})
    if isinstance(goals, dict):
        rows = goals.get("goals") or goals.get("items") or []
    elif isinstance(goals, list):
        rows = goals
    else:
        rows = []
    rows = [deepcopy(row) for row in rows if isinstance(row, dict)]
    if goal_id:
        rows = [row for row in rows if str(row.get("goal_id") or row.get("id") or "") == str(goal_id)]
    else:
        rows = [
            row for row in rows
            if str(row.get("status") or "active").lower() not in {"completed", "cancelled", "closed", "done"}
        ]
    evidence = _read_jsonl(store, GOAL_EVIDENCE_FILE)
    term_state = store.read_json("academic_term_state.json", {})
    deferred = (
        isinstance(term_state, dict)
        and term_state.get("service_relation_policy") == "defer_until_new_term"
    )
    compact_goals = []
    for goal in rows:
        current_goal_id = str(goal.get("goal_id") or goal.get("id") or "")
        work_result = query_hermes_work_items(
            store,
            identity=identity,
            focus_key=f"goal:{current_goal_id}" if current_goal_id else "",
            include_closed=False,
            limit=5,
        )
        work_items = work_result.get("items") if isinstance(work_result, dict) else []
        current_work = work_items[-1] if isinstance(work_items, list) and work_items else {}
        review = goal.get("review_snapshot") if isinstance(goal.get("review_snapshot"), dict) else {}
        compact_goal = {
            "goal_id": current_goal_id,
            "goal_type": goal.get("goal_type"),
            "status": goal.get("status"),
            "goal_text": goal.get("goal_text"),
            "owner_user_id": goal.get("owner_user_id"),
            "created_at": goal.get("created_at"),
            "updated_at": goal.get("updated_at"),
            "current_work_state": {
                "work_item_id": current_work.get("work_item_id"),
                "status": current_work.get("status"),
                "current_phase": current_work.get("current_phase"),
                "current_waiting": current_work.get("current_waiting"),
                "blocked_by": current_work.get("blocked_by"),
                "next_attention_at": current_work.get("next_attention_at"),
                "latest_update_text": current_work.get("latest_update_text"),
                "progress_evidence": (current_work.get("progress_evidence") or [])[-10:],
            },
        }
        if deferred:
            compact_goal["historical_snapshot"] = {
                "data_term": term_state.get("data_term") or "previous_term",
                "roster_confidence": term_state.get("roster_confidence") or "historical_snapshot_only",
                "student_count": review.get("student_count"),
                "communicated_count": review.get("communicated_count"),
                "missing_count": review.get("missing_count"),
                "teacher_count": review.get("teacher_count"),
            }
            compact_goal["new_term_scope"] = {
                "service_relation_policy": "defer_until_new_term",
                "confirmation_window_start": term_state.get("confirmation_window_start") or "2026-08-25",
                "confirmation_window_end": term_state.get("confirmation_window_end") or "2026-09-10",
                "old_roster_is_current_fact": False,
            }
        compact_goals.append(compact_goal)
    compact_evidence = [
        {
            "evidence_id": row.get("evidence_id"),
            "goal_id": row.get("goal_id"),
            "evidence_type": row.get("evidence_type"),
            "summary": row.get("summary") or row.get("evidence_text"),
            "created_at": row.get("created_at"),
        }
        for row in evidence[-20:]
        if isinstance(row, dict)
    ]
    return {
        "ok": True,
        "goal_count": len(compact_goals),
        "goals": compact_goals,
        "evidence_count": len(evidence),
        "recent_evidence": compact_evidence,
        "term_state": term_state if deferred else {},
        "rendered_text": (
            f"当前查到 {len(compact_goals)} 个活跃目标状态。最新工作项代表当前阶段；"
            "旧名单与旧覆盖数字只作为历史快照，不能当作新学期在读事实。"
            if deferred
            else f"当前查到 {len(compact_goals)} 个活跃目标状态，近期目标证据 {min(len(evidence), 20)} 条。"
        ),
        "render_verified": True,
    }


def query_profile_candidates(store: TuoguanStore, *, subject: str = "", include_expired: bool = False, limit: int = 30) -> dict[str, Any]:
    subject = str(subject or "").strip()
    now = datetime.now().astimezone()
    corrections = _profile_corrections_by_candidate(store)
    rows = []
    for row in _read_jsonl(store, PROFILE_CANDIDATES_FILE):
        if subject and str(row.get("subject") or "") != subject:
            continue
        candidate_id = str(row.get("candidate_id") or "")
        candidate_corrections = corrections.get(candidate_id, [])
        if candidate_corrections:
            row = deepcopy(row)
            row["corrections"] = candidate_corrections
            latest = candidate_corrections[-1]
            row["effective_status"] = str(latest.get("decision") or row.get("status") or "candidate")
        else:
            row = deepcopy(row)
            row.setdefault("effective_status", str(row.get("status") or "candidate"))
        if row.get("effective_status") == "retract":
            continue
        expires_at = _parse_time(row.get("expires_at"))
        expired = bool(expires_at and expires_at.replace(tzinfo=None) < now.replace(tzinfo=None))
        if row.get("effective_status") == "expire":
            expired = True
        if not include_expired and expired:
            continue
        row["expired"] = expired
        rows.append(row)
    rows = rows[-max(1, min(int(limit or 30), 100)):]
    return {
        "ok": True,
        "candidate_count": len(rows),
        "candidates": rows,
        "rendered_text": f"查到 {len(rows)} 条画像候选。画像只是候选判断，重要决策前必须看来源、置信度和有效期。",
        "render_verified": True,
    }


def query_value_ledger(store: TuoguanStore, *, subject: str = "", limit: int = 30) -> dict[str, Any]:
    subject = str(subject or "").strip()
    rows = [row for row in _read_jsonl(store, VALUE_LEDGER_FILE) if not subject or str(row.get("subject") or "") == subject]
    rows = rows[-max(1, min(int(limit or 30), 100)):]
    return {
        "ok": True,
        "entry_count": len(rows),
        "entries": rows,
        "rendered_text": f"查到 {len(rows)} 条价值账本记录。账本记录 Hermes 参与推动的证据，不把收入或续费全部归功于 Hermes。",
        "render_verified": True,
    }


def submit_service_relation_fact_candidate(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    student_name: str,
    service_type: str = "",
    responsible_teacher_user_id: str = "",
    unified_owner_user_id: str = "",
    program_id: str = "regular_tuoguan",
    effective_from: str = "",
    source_text: str = "",
    operation_id: str = "",
) -> dict[str, Any]:
    row = {
        "candidate_id": _new_id("service_relation_candidate"),
        "tenant_id": current_tenant_id(),
        "student_name": str(student_name or "").strip(),
        "program_id": str(program_id or "regular_tuoguan"),
        "service_type": str(service_type or ""),
        "responsible_teacher_user_id": str(responsible_teacher_user_id or ""),
        "unified_owner_user_id": str(unified_owner_user_id or ""),
        "effective_from": str(effective_from or ""),
        "status": "pending_confirmation",
        "source_text": str(source_text or ""),
        "source": _source(identity, operation_id),
        "created_at": now_iso(),
    }
    if not row["student_name"]:
        return {"ok": False, "error": "student_name_required", "message": "缺少学生姓名。"}
    _append_jsonl(store, SERVICE_RELATION_CANDIDATES_FILE, row)
    return {"ok": True, "candidate": row, "writeback_verified": True, "rendered_text": "已保存学生服务关系事实候选，等待后续确认或整理。"}


def submit_information_request_record(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    target_person: str,
    reason: str,
    question: str,
    value_level: str = "normal",
    request_type: str = "formal",
    status: str = "asked",
    source_text: str = "",
    operation_id: str = "",
) -> dict[str, Any]:
    row = {
        "request_id": _new_id("information_request"),
        "tenant_id": current_tenant_id(),
        "target_person": str(target_person or "").strip(),
        "reason": str(reason or ""),
        "question": str(question or ""),
        "value_level": str(value_level or "normal"),
        "request_type": str(request_type or "formal"),
        "status": str(status or "asked"),
        "needs_escalation": False,
        "source_text": str(source_text or ""),
        "source": _source(identity, operation_id),
        "created_at": now_iso(),
    }
    if not row["target_person"] or not row["question"]:
        return {"ok": False, "error": "target_and_question_required", "message": "缺少询问对象或问题。"}
    _append_jsonl(store, INFORMATION_REQUESTS_FILE, row)
    return {"ok": True, "request": row, "writeback_verified": True, "rendered_text": "已保存主动取数记录。"}


def query_information_requests(
    store: TuoguanStore,
    *,
    target_person: str = "",
    status: str = "",
    include_closed: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    requests = _fold_information_requests(store)
    target_person = str(target_person or "").strip()
    requested_status = str(status or "").strip()
    rows = []
    for row in requests.values():
        current_status = str(row.get("status") or "")
        if target_person and str(row.get("target_person") or "") != target_person:
            continue
        if requested_status and current_status != requested_status:
            continue
        if not include_closed and current_status in {"answered", "stopped", "closed"}:
            continue
        rows.append(row)
    rows.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""))
    rows = rows[-max(1, min(int(limit or 50), 200)):]
    waiting = [row for row in rows if str(row.get("status") or "") in {"asked", "waiting"}]
    return {
        "ok": True,
        "request_count": len(rows),
        "waiting_count": len(waiting),
        "requests": rows,
        "rendered_text": f"查到 {len(rows)} 条主动取数状态，其中 {len(waiting)} 条仍在等待。查询结果只是状态材料，不代表必须催问或升级。",
        "render_verified": True,
    }


def submit_information_request_update(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    request_id: str,
    status: str,
    update_text: str,
    response_text: str = "",
    needs_escalation: bool = False,
    source_text: str = "",
    operation_id: str = "",
) -> dict[str, Any]:
    request_id = str(request_id or "").strip()
    status = str(status or "").strip()
    allowed_statuses = {"waiting", "answered", "stopped", "closed", "escalation_candidate"}
    if status not in allowed_statuses:
        return {"ok": False, "error": "invalid_information_request_status", "message": "status 必须是 waiting、answered、stopped、closed 或 escalation_candidate。"}
    if not request_id or not str(update_text or "").strip():
        return {"ok": False, "error": "information_request_update_requires_text", "message": "缺少主动取数 request_id 或状态说明。"}
    requests = _fold_information_requests(store)
    if request_id not in requests:
        return {"ok": False, "error": "information_request_not_found", "message": "没有找到这条主动取数记录。"}
    row = {
        "event_id": _new_id("information_request_update"),
        "record_type": "information_request_update",
        "tenant_id": current_tenant_id(),
        "request_id": request_id,
        "status": status,
        "update_text": str(update_text or ""),
        "response_text": str(response_text or ""),
        "needs_escalation": bool(needs_escalation),
        "source_text": str(source_text or ""),
        "source": _source(identity, operation_id),
        "created_at": now_iso(),
    }
    _append_jsonl(store, INFORMATION_REQUESTS_FILE, row)
    return {
        "ok": True,
        "update": row,
        "writeback_verified": True,
        "rendered_text": "已保存主动取数状态更新；这只是事实状态，不会自动催问、升级或记入绩效。",
    }


def submit_profile_candidate(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    subject: str,
    subject_type: str,
    profile_text: str,
    evidence_text: str,
    confidence: float = 0.5,
    expires_at: str = "",
    source_text: str = "",
    operation_id: str = "",
) -> dict[str, Any]:
    row = {
        "candidate_id": _new_id("profile_candidate"),
        "tenant_id": current_tenant_id(),
        "subject": str(subject or "").strip(),
        "subject_type": str(subject_type or ""),
        "profile_text": str(profile_text or ""),
        "evidence_text": str(evidence_text or ""),
        "confidence": max(0.0, min(float(confidence or 0.0), 1.0)),
        "expires_at": str(expires_at or ""),
        "status": "candidate",
        "corrections": [],
        "source_text": str(source_text or ""),
        "source": _source(identity, operation_id),
        "created_at": now_iso(),
    }
    if not row["subject"] or not row["profile_text"] or not row["evidence_text"]:
        return {"ok": False, "error": "profile_candidate_requires_evidence", "message": "画像候选必须有对象、候选内容和事实依据。"}
    _append_jsonl(store, PROFILE_CANDIDATES_FILE, row)
    return {"ok": True, "candidate": row, "writeback_verified": True, "rendered_text": "已保存画像候选；它不是永久标签，重要使用前要核对证据和有效期。"}


def submit_profile_candidate_correction(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    candidate_id: str,
    correction_text: str,
    decision: str = "correct",
    source_text: str = "",
    operation_id: str = "",
) -> dict[str, Any]:
    candidate_id = str(candidate_id or "").strip()
    decision = str(decision or "correct").strip()
    if decision not in {"correct", "retract", "expire"}:
        return {"ok": False, "error": "invalid_profile_correction_decision", "message": "decision 必须是 correct、retract 或 expire。"}
    if not candidate_id or not str(correction_text or "").strip():
        return {"ok": False, "error": "profile_correction_requires_text", "message": "画像候选纠错必须有候选 id 和纠错说明。"}
    existing = [
        row for row in _read_jsonl(store, PROFILE_CANDIDATES_FILE)
        if str(row.get("candidate_id") or "") == candidate_id
    ]
    if not existing:
        return {"ok": False, "error": "profile_candidate_not_found", "message": "没有找到这条画像候选。"}
    row = {
        "correction_id": _new_id("profile_correction"),
        "tenant_id": current_tenant_id(),
        "candidate_id": candidate_id,
        "decision": decision,
        "correction_text": str(correction_text or ""),
        "source_text": str(source_text or ""),
        "source": _source(identity, operation_id),
        "created_at": now_iso(),
    }
    _append_jsonl(store, PROFILE_CORRECTIONS_FILE, row)
    rendered = {
        "correct": "已保存画像候选纠错说明；后续使用该候选时必须同时看到这条纠错。",
        "retract": "已保存画像候选撤回记录；后续默认查询不会再使用该候选。",
        "expire": "已保存画像候选过期记录；后续默认查询不会再使用该候选。",
    }[decision]
    return {"ok": True, "correction": row, "writeback_verified": True, "rendered_text": rendered}


def submit_goal_evidence(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    goal_id: str,
    evidence_text: str,
    subject: str = "",
    source_text: str = "",
    operation_id: str = "",
) -> dict[str, Any]:
    row = {
        "evidence_id": _new_id("goal_evidence"),
        "tenant_id": current_tenant_id(),
        "goal_id": str(goal_id or "").strip(),
        "subject": str(subject or ""),
        "evidence_text": str(evidence_text or ""),
        "status": "candidate",
        "source_text": str(source_text or ""),
        "source": _source(identity, operation_id),
        "created_at": now_iso(),
    }
    if not row["evidence_text"]:
        return {"ok": False, "error": "evidence_text_required", "message": "缺少目标进度证据。"}
    _append_jsonl(store, GOAL_EVIDENCE_FILE, row)
    return {"ok": True, "evidence": row, "writeback_verified": True, "rendered_text": "已保存目标推进证据候选。"}


def submit_performance_evidence_candidate(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    staff_user_id: str,
    evidence_text: str,
    evidence_type: str = "execution",
    source_text: str = "",
    operation_id: str = "",
) -> dict[str, Any]:
    if identity.role not in {"boss", "manager"}:
        return {"ok": False, "error": "permission_denied", "message": "绩效证据候选第一阶段仅允许老板或店长提交；不自动扣分、不改工资。"}
    row = {
        "candidate_id": _new_id("performance_evidence"),
        "tenant_id": current_tenant_id(),
        "staff_user_id": str(staff_user_id or "").strip(),
        "evidence_type": str(evidence_type or "execution"),
        "evidence_text": str(evidence_text or ""),
        "status": "candidate",
        "score_effect": "not_scored",
        "salary_effect": "none",
        "dispute_status": "open_for_correction",
        "source_text": str(source_text or ""),
        "source": _source(identity, operation_id),
        "created_at": now_iso(),
    }
    if not row["staff_user_id"] or not row["evidence_text"]:
        return {"ok": False, "error": "staff_and_evidence_required", "message": "缺少员工或证据内容。"}
    _append_jsonl(store, PERFORMANCE_EVIDENCE_FILE, row)
    return {"ok": True, "candidate": row, "writeback_verified": True, "rendered_text": "已保存绩效证据候选；不会自动扣分、改工资或通知最终绩效。"}


def query_performance_evidence_candidates(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    staff_user_id: str = "",
    include_closed: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    requested_staff = str(staff_user_id or "").strip()
    if identity.role == "teacher":
        if requested_staff and requested_staff != identity.canonical_user_id:
            return {"ok": False, "error": "permission_denied", "message": "老师只能查看自己的绩效证据候选。"}
        requested_staff = identity.canonical_user_id
    elif identity.role not in {"boss", "manager"}:
        return {"ok": False, "error": "permission_denied", "message": "当前账号不能查看绩效证据候选。"}

    responses = _performance_responses_by_candidate(store)
    rows = []
    for row in _read_jsonl(store, PERFORMANCE_EVIDENCE_FILE):
        if str(row.get("record_type") or "") == "performance_evidence_response":
            continue
        if requested_staff and str(row.get("staff_user_id") or "") != requested_staff:
            continue
        item = deepcopy(row)
        candidate_id = str(item.get("candidate_id") or "")
        item_responses = responses.get(candidate_id, [])
        item["responses"] = item_responses
        if item_responses:
            latest = item_responses[-1]
            item["dispute_status"] = str(latest.get("status_after") or item.get("dispute_status") or "open_for_correction")
            item["latest_response_text"] = str(latest.get("response_text") or "")
            item["latest_response_type"] = str(latest.get("response_type") or "")
        if not include_closed and str(item.get("dispute_status") or "") in {"closed", "withdrawn"}:
            continue
        rows.append(item)
    rows.sort(key=lambda item: str(item.get("created_at") or ""))
    rows = rows[-max(1, min(int(limit or 50), 200)):]
    open_for_correction = [
        row for row in rows
        if str(row.get("dispute_status") or "") in {"open_for_correction", "disputed", "explained", "under_review"}
    ]
    return {
        "ok": True,
        "candidate_count": len(rows),
        "open_for_correction_count": len(open_for_correction),
        "candidates": rows,
        "rendered_text": f"查到 {len(rows)} 条绩效证据候选，其中 {len(open_for_correction)} 条仍开放说明或异议。候选不等于评分，不自动扣分或改工资。",
        "render_verified": True,
    }


def submit_performance_evidence_response(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    candidate_id: str,
    response_type: str,
    response_text: str,
    source_text: str = "",
    operation_id: str = "",
) -> dict[str, Any]:
    candidate_id = str(candidate_id or "").strip()
    response_type = str(response_type or "").strip()
    if response_type not in {"explanation", "dispute", "acknowledge", "manager_note", "boss_note"}:
        return {"ok": False, "error": "invalid_performance_response_type", "message": "response_type 必须是 explanation、dispute、acknowledge、manager_note 或 boss_note。"}
    candidates = [
        row for row in _read_jsonl(store, PERFORMANCE_EVIDENCE_FILE)
        if str(row.get("candidate_id") or "") == candidate_id
    ]
    if not candidates:
        return {"ok": False, "error": "performance_evidence_candidate_not_found", "message": "没有找到这条绩效证据候选。"}
    candidate = candidates[-1]
    if identity.role == "teacher":
        if str(candidate.get("staff_user_id") or "") != identity.canonical_user_id:
            return {"ok": False, "error": "permission_denied", "message": "老师只能回应自己的绩效证据候选。"}
        if response_type not in {"explanation", "dispute", "acknowledge"}:
            return {"ok": False, "error": "permission_denied", "message": "老师只能提交说明、异议或确认收到。"}
    elif identity.role == "manager":
        if response_type == "boss_note":
            return {"ok": False, "error": "permission_denied", "message": "店长不能提交老板备注。"}
    elif identity.role != "boss":
        return {"ok": False, "error": "permission_denied", "message": "当前账号不能回应绩效证据候选。"}
    if not str(response_text or "").strip():
        return {"ok": False, "error": "performance_response_requires_text", "message": "绩效证据回应必须有说明内容。"}
    status_after = {
        "explanation": "explained",
        "dispute": "disputed",
        "acknowledge": "acknowledged",
        "manager_note": "under_review",
        "boss_note": "under_review",
    }[response_type]
    row = {
        "response_id": _new_id("performance_response"),
        "record_type": "performance_evidence_response",
        "tenant_id": current_tenant_id(),
        "candidate_id": candidate_id,
        "staff_user_id": str(candidate.get("staff_user_id") or ""),
        "response_type": response_type,
        "response_text": str(response_text or ""),
        "status_after": status_after,
        "score_effect": "not_scored",
        "salary_effect": "none",
        "source_text": str(source_text or ""),
        "source": _source(identity, operation_id),
        "created_at": now_iso(),
    }
    _append_jsonl(store, PERFORMANCE_EVIDENCE_RESPONSES_FILE, row)
    return {
        "ok": True,
        "response": row,
        "writeback_verified": True,
        "rendered_text": "已保存绩效证据回应；这不是最终评分，不自动扣分、不改工资、不通知最终绩效。",
    }


def submit_value_ledger_entry(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    discovered: str,
    hermes_action: str,
    human_action: str = "",
    outcome: str = "",
    subject: str = "",
    attribution: str = "participated",
    source_text: str = "",
    operation_id: str = "",
) -> dict[str, Any]:
    if identity.role != "boss":
        return {"ok": False, "error": "permission_denied", "message": "价值账本第一阶段仅允许老板确认保存。"}
    normalized_attribution = str(attribution or "participated").strip()
    if normalized_attribution not in {"participated", "assisted", "observed", "unknown"}:
        normalized_attribution = "participated"
    row = {
        "entry_id": _new_id("value_ledger"),
        "tenant_id": current_tenant_id(),
        "subject": str(subject or ""),
        "discovered": str(discovered or ""),
        "hermes_action": str(hermes_action or ""),
        "human_action": str(human_action or ""),
        "outcome": str(outcome or ""),
        "attribution": normalized_attribution,
        "source_text": str(source_text or ""),
        "source": _source(identity, operation_id),
        "created_at": now_iso(),
    }
    if not row["discovered"] or not row["hermes_action"]:
        return {"ok": False, "error": "value_ledger_requires_action", "message": "价值账本至少要说明发现了什么以及 Hermes 做了什么。"}
    _append_jsonl(store, VALUE_LEDGER_FILE, row)
    return {"ok": True, "entry": row, "writeback_verified": True, "rendered_text": "已保存价值账本条目；仅记录参与推动，不夸大归因。"}


def query_gray_observations(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    scenario_id: str = "",
    outcome: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    if identity.role not in {"boss", "manager"}:
        return {"ok": False, "error": "permission_denied", "message": "灰度观察汇总第一阶段仅允许老板或店长查看。"}
    scenario_filter = str(scenario_id or "").strip()
    outcome_filter = str(outcome or "").strip()
    rows = []
    for row in _read_jsonl(store, GRAY_OBSERVATIONS_FILE):
        if scenario_filter and str(row.get("scenario_id") or "") != scenario_filter:
            continue
        if outcome_filter and str(row.get("outcome") or "") != outcome_filter:
            continue
        rows.append(deepcopy(row))
    rows.sort(key=lambda item: str(item.get("created_at") or ""))
    rows = rows[-max(1, min(int(limit or 50), 200)):]
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("outcome") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return {
        "ok": True,
        "observation_count": len(rows),
        "outcome_counts": counts,
        "observations": rows,
        "rendered_text": f"查到 {len(rows)} 条灰度观察记录。这些记录只用于人工验收和后续优化，不限制模型下一步，不自动进入绩效、工资或长期记忆。",
        "render_verified": True,
    }


def submit_gray_observation(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    scenario_id: str,
    observation_text: str,
    outcome: str = "note",
    conversation_ref: str = "",
    actor_user_id: str = "",
    source_text: str = "",
    operation_id: str = "",
) -> dict[str, Any]:
    if identity.role not in {"boss", "manager", "teacher"}:
        return {"ok": False, "error": "permission_denied", "message": "当前账号不能提交灰度观察。"}
    normalized_outcome = str(outcome or "note").strip()
    if normalized_outcome not in {"success", "issue", "unclear", "note"}:
        return {"ok": False, "error": "invalid_gray_observation_outcome", "message": "outcome 必须是 success、issue、unclear 或 note。"}
    row = {
        "observation_id": _new_id("gray_observation"),
        "tenant_id": current_tenant_id(),
        "scenario_id": str(scenario_id or "").strip(),
        "outcome": normalized_outcome,
        "observation_text": str(observation_text or "").strip(),
        "conversation_ref": str(conversation_ref or "").strip(),
        "actor_user_id": str(actor_user_id or identity.canonical_user_id).strip(),
        "source_text": str(source_text or ""),
        "source": _source(identity, operation_id),
        "created_at": now_iso(),
        "effects": {
            "limits_model": False,
            "changes_router": False,
            "enters_long_term_memory": False,
            "creates_performance_evidence": False,
            "changes_salary": False,
            "sends_notifications": False,
        },
    }
    if not row["scenario_id"] or not row["observation_text"]:
        return {"ok": False, "error": "gray_observation_requires_content", "message": "灰度观察必须包含场景和观察内容。"}
    _append_jsonl(store, GRAY_OBSERVATIONS_FILE, row)
    return {
        "ok": True,
        "observation": row,
        "writeback_verified": True,
        "rendered_text": "已保存灰度观察记录；它只用于人工验收和后续优化，不限制模型能力，不自动进入绩效、工资或长期记忆。",
    }


def query_gray_rollout_decisions(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    decision_type: str = "",
    limit: int = 30,
) -> dict[str, Any]:
    if identity.role not in {"boss", "manager"}:
        return {"ok": False, "error": "permission_denied", "message": "灰度放量决策记录第一阶段仅允许老板或店长查看。"}
    decision_filter = str(decision_type or "").strip()
    rows = []
    for row in _read_jsonl(store, GRAY_ROLLOUT_DECISIONS_FILE):
        if decision_filter and str(row.get("decision_type") or "") != decision_filter:
            continue
        rows.append(deepcopy(row))
    rows.sort(key=lambda item: str(item.get("created_at") or ""))
    rows = rows[-max(1, min(int(limit or 30), 100)):]
    return {
        "ok": True,
        "decision_count": len(rows),
        "decisions": rows,
        "rendered_text": f"查到 {len(rows)} 条灰度放量决策记录。记录只是老板拍板材料，不会自动扩大发布、修改手册、创建任务或改变权限。",
        "render_verified": True,
    }


def submit_gray_rollout_decision(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    decision_type: str,
    decision_text: str,
    scope: str = "",
    reason: str = "",
    source_report_path: str = "",
    source_text: str = "",
    operation_id: str = "",
) -> dict[str, Any]:
    if identity.role != "boss":
        return {"ok": False, "error": "permission_denied", "message": "灰度放量决策必须由老板确认保存；保存后也不会自动执行。"}
    normalized_type = str(decision_type or "").strip()
    if normalized_type not in {"continue_small_gray", "pause", "expand_candidate", "defer_item", "rollback_candidate", "note"}:
        return {
            "ok": False,
            "error": "invalid_gray_rollout_decision_type",
            "message": "decision_type 必须是 continue_small_gray、pause、expand_candidate、defer_item、rollback_candidate 或 note。",
        }
    row = {
        "decision_id": _new_id("gray_decision"),
        "tenant_id": current_tenant_id(),
        "decision_type": normalized_type,
        "decision_text": str(decision_text or "").strip(),
        "scope": str(scope or "").strip(),
        "reason": str(reason or "").strip(),
        "source_report_path": str(source_report_path or "").strip(),
        "source_text": str(source_text or ""),
        "source": _source(identity, operation_id),
        "created_at": now_iso(),
        "auto_effects": {
            "expands_rollout": False,
            "changes_permissions": False,
            "updates_handbook": False,
            "creates_tasks": False,
            "sends_notifications": False,
            "creates_learning_candidate": False,
            "changes_router": False,
        },
    }
    if not row["decision_text"]:
        return {"ok": False, "error": "gray_rollout_decision_requires_text", "message": "灰度放量决策必须有老板明确决定内容。"}
    _append_jsonl(store, GRAY_ROLLOUT_DECISIONS_FILE, row)
    return {
        "ok": True,
        "decision": row,
        "writeback_verified": True,
        "rendered_text": "已保存灰度放量决策记录；这只是老板拍板材料，不会自动扩大灰度、改权限、改手册或派任务。",
    }


def query_gray_optimization_decisions(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    candidate_id: str = "",
    decision_type: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    if identity.role not in {"boss", "manager"}:
        return {"ok": False, "error": "permission_denied", "message": "灰度优化确认记录第一阶段仅允许老板或店长查看。"}
    candidate_filter = str(candidate_id or "").strip()
    decision_filter = str(decision_type or "").strip()
    rows = []
    for row in _read_jsonl(store, GRAY_OPTIMIZATION_DECISIONS_FILE):
        if candidate_filter and str(row.get("candidate_id") or "") != candidate_filter:
            continue
        if decision_filter and str(row.get("decision_type") or "") != decision_filter:
            continue
        rows.append(deepcopy(row))
    rows.sort(key=lambda item: str(item.get("created_at") or ""))
    rows = rows[-max(1, min(int(limit or 50), 200)):]
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("decision_type") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return {
        "ok": True,
        "decision_count": len(rows),
        "decision_type_counts": counts,
        "decisions": rows,
        "rendered_text": f"查到 {len(rows)} 条灰度优化确认记录。记录只保存老板选择，不自动改手册、学习、工具、权限或任务。",
        "render_verified": True,
    }


def submit_gray_optimization_decision(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    candidate_id: str,
    decision_type: str,
    decision_text: str,
    source_observation_id: str = "",
    candidate_type: str = "",
    reason: str = "",
    source_text: str = "",
    operation_id: str = "",
) -> dict[str, Any]:
    if identity.role != "boss":
        return {"ok": False, "error": "permission_denied", "message": "灰度优化确认必须由老板保存；保存后也不会自动应用。"}
    normalized_type = str(decision_type or "").strip()
    allowed_types = {
        "handbook_candidate",
        "learning_candidate",
        "tool_fix_candidate",
        "permission_boundary_review",
        "response_style_candidate",
        "defer",
        "reject",
        "note",
    }
    if normalized_type not in allowed_types:
        return {
            "ok": False,
            "error": "invalid_gray_optimization_decision_type",
            "message": "decision_type 必须是 handbook_candidate、learning_candidate、tool_fix_candidate、permission_boundary_review、response_style_candidate、defer、reject 或 note。",
        }
    row = {
        "decision_id": _new_id("gray_optimization_decision"),
        "tenant_id": current_tenant_id(),
        "candidate_id": str(candidate_id or "").strip(),
        "source_observation_id": str(source_observation_id or "").strip(),
        "candidate_type": str(candidate_type or "").strip(),
        "decision_type": normalized_type,
        "decision_text": str(decision_text or "").strip(),
        "reason": str(reason or "").strip(),
        "source_text": str(source_text or ""),
        "source": _source(identity, operation_id),
        "created_at": now_iso(),
        "auto_effects": {
            "updates_handbook": False,
            "creates_learning_candidate": False,
            "patches_tools": False,
            "changes_permissions": False,
            "creates_tasks": False,
            "sends_notifications": False,
            "changes_salary": False,
            "limits_model": False,
            "changes_router": False,
        },
    }
    if not row["candidate_id"] or not row["decision_text"]:
        return {"ok": False, "error": "gray_optimization_decision_requires_content", "message": "灰度优化确认必须包含候选 ID 和老板明确决定内容。"}
    _append_jsonl(store, GRAY_OPTIMIZATION_DECISIONS_FILE, row)
    return {
        "ok": True,
        "decision": row,
        "writeback_verified": True,
        "rendered_text": "已保存灰度优化确认记录；这只是老板对候选方向的选择，不会自动改手册、学习、工具、权限、任务、工资或通知。",
    }



_AUTONOMOUS_FORBIDDEN_KEYS = {"model_intent", "next_tool", "workflow_step", "expected_reply"}
_WORK_ITEM_STATUSES = {"active", "waiting", "blocked", "closed", "superseded"}
_WORK_ITEM_OPEN_STATUSES = {"active", "waiting", "blocked"}
_WAKEUP_STATUSES = {"pending", "handled", "ignored", "superseded"}
_ACTION_EXECUTION_STATUSES = {
    "not_started",
    "success",
    "failed",
    "partial_success",
    "result_unknown",
    "permission_blocked",
    "manual_takeover",
}


def _limit_text(value: Any, max_len: int = 1200) -> str:
    text = str(value or "").strip()
    return text[:max_len]


def _strip_forbidden(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _strip_forbidden(item) for key, item in value.items() if str(key) not in _AUTONOMOUS_FORBIDDEN_KEYS}
    if isinstance(value, list):
        return [_strip_forbidden(item) for item in value]
    return deepcopy(value)


def _autonomous_source(identity: UserIdentity, operation_id: str = "", source_message_id: str = "") -> dict[str, Any]:
    source = _source(identity, operation_id)
    source["source_message_id"] = str(source_message_id or "")
    return source


def _read_state_dict(store: TuoguanStore, filename: str) -> dict[str, Any]:
    payload = store.read_json(filename, {})
    return payload if isinstance(payload, dict) else {}


def _write_state_dict(store: TuoguanStore, filename: str, payload: dict[str, Any]) -> None:
    from .write_guard import assert_business_write_allowed

    assert_business_write_allowed(store.data_dir, filename)
    store.write_json(filename, payload)


def _count_list_json(store: TuoguanStore, filename: str) -> int:
    payload = store.read_json(filename, [])
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        return len(payload)
    return 0


def _institution_onboarding_milestones() -> list[dict[str, Any]]:
    return [
        {"deadline": "first_run", "must_know": ["老板/授权人", "机构主营业务", "安全红线", "当前数据源", "当前最重要目标"]},
        {"deadline": "24h", "must_know": ["人员角色", "业务线", "学生大盘", "主要风险", "当前进行中的目标"]},
        {"deadline": "3d", "must_know": ["服务关系", "家校沟通规则", "记录标准", "老师分工", "常见业务流程"]},
        {"deadline": "7d", "must_know": ["机构画像", "老师画像候选", "学生风险清单", "经营机会清单", "首次自我胜任度报告"]},
        {"deadline": "30d", "must_know": ["续费机会", "增项机会", "老师协作模式", "老板决策偏好", "可复盘价值账本"]},
    ]


def _institution_understanding_audit(store: TuoguanStore, identity: UserIdentity) -> dict[str, Any]:
    operating_model = read_institution_operating_model(store)
    students = _students(store)
    active_regular = _active_regular_students(store, identity)
    staff_count = _count_list_json(store, "staff.json")
    goals_doc = store.read_json("goal_operator_goals.json", {})
    goals = goals_doc.get("goals") if isinstance(goals_doc, dict) else []
    confirmed_goals = [item for item in (goals or []) if isinstance(item, dict) and str(item.get("status") or "") == "confirmed"]
    goal_student_counts = [
        int(((item.get("review_snapshot") or {}).get("student_count") or 0))
        for item in confirmed_goals
        if isinstance(item.get("review_snapshot"), dict) and int(((item.get("review_snapshot") or {}).get("student_count") or 0)) > 0
    ]
    current_goal_student_count = max(goal_student_counts) if goal_student_counts else 0
    service_relations = query_student_service_relations(store, identity=identity, program_id="regular_tuoguan")
    weekly_coverage = query_weekly_record_coverage(store, identity=identity, days=14, program_id="regular_tuoguan")
    parent_coverage = query_parent_communication_coverage(store, identity=identity, days=30, program_id="regular_tuoguan")
    knowledge_latest = _read_state_dict(store, "knowledge_research_latest.json")
    competitor_latest = _read_state_dict(store, "competitor_research_latest.json")
    gaps: list[dict[str, Any]] = []
    if service_relations.get("missing_count"):
        gaps.append({
            "gap_key": "student_service_relation",
            "status": "deferred_until_new_term",
            "ask_role": "boss_or_regular_manager",
            "reason": "正式托管学生服务类型和主责老师仍有缺口；当前 7-8 月学生名单不稳定，先保留为 8 月底至 9 月初待确认。",
            "target_time": "2026-08-25/2026-09-10",
            "missing_count": service_relations.get("missing_count"),
        })
    if weekly_coverage.get("missing_count"):
        gaps.append({
            "gap_key": "weekly_record_evidence",
            "status": "active_observation",
            "ask_role": "boss_or_manager",
            "reason": "近 14 天仍有学生缺少表现记录证据；先作为续费目标证据缺口，不自动派老师。",
            "missing_count": weekly_coverage.get("missing_count"),
        })
    if parent_coverage.get("missing_count"):
        gaps.append({
            "gap_key": "parent_communication_coverage",
            "status": "active_goal_gap",
            "ask_role": "boss_or_manager",
            "reason": "家校沟通覆盖不足，会影响九月份续费目标推进。",
            "missing_count": parent_coverage.get("missing_count"),
        })
    if not knowledge_latest.get("evidence_count"):
        gaps.append({
            "gap_key": "public_industry_learning_source",
            "status": "needs_search_provider",
            "ask_role": "system_owner",
            "reason": "公网行业学习最近没有可用证据；需要可用搜索源，否则只能记录失败，不能编造趋势。",
        })
    return {
        "known_facts": {
            "tenant_id": current_tenant_id(),
            "program_scope": (operating_model.get("scope_note") or "当前主要覆盖正式托管班"),
            "active_regular_student_count": len(active_regular),
            "current_goal_student_count": current_goal_student_count,
            "student_profile_count": len(students),
            "staff_count": staff_count,
            "confirmed_goal_count": len(confirmed_goals),
            "active_goal_texts": [str(item.get("goal_text") or "") for item in confirmed_goals[:5]],
            "regular_manager_names": (((operating_model.get("programs") or {}).get("regular_tuoguan") or {}).get("manager_names") or []),
            "competitor_research_evidence_count": competitor_latest.get("evidence_count", 0),
            "industry_research_evidence_count": knowledge_latest.get("evidence_count", 0),
        },
        "gaps": gaps,
        "gap_count": len(gaps),
        "milestones": _institution_onboarding_milestones(),
    }


def query_institution_understanding(store: TuoguanStore, *, identity: UserIdentity) -> dict[str, Any]:
    if identity.role not in {"boss", "manager"}:
        return {"ok": False, "error": "permission_denied", "message": "只有老板或店长可以查看机构认知状态。"}
    state = _read_state_dict(store, INSTITUTION_UNDERSTANDING_FILE)
    audit = _institution_understanding_audit(store, identity)
    latest_gaps = _read_jsonl(store, INSTITUTION_FACT_GAP_EVENTS_FILE)[-50:]
    goal_count = audit["known_facts"].get("current_goal_student_count") or audit["known_facts"].get("active_regular_student_count", 0)
    rendered = (
        f"机构认知：已知学生档案 {audit['known_facts'].get('student_profile_count', 0)} 个，当前目标口径学生约 {goal_count} 人，"
        f"确认目标 {audit['known_facts'].get('confirmed_goal_count', 0)} 个，当前识别缺口 {audit.get('gap_count', 0)} 类。"
        "这些是 Hermes 的入职/胜任材料，不规定模型下一步。"
    )
    return {
        "ok": True,
        "state": state,
        "audit": audit,
        "recent_gap_events": latest_gaps,
        "rendered_text": rendered,
        "render_verified": True,
    }


def update_institution_understanding(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    operation_id: str,
    known_facts: dict[str, Any] | None = None,
    missing_facts: list[Any] | None = None,
    pending_confirmations: list[Any] | None = None,
    stale_or_conflicting_facts: list[Any] | None = None,
    next_review_at: str = "",
    source_text: str = "",
    source_message_id: str = "",
) -> dict[str, Any]:
    if identity.role not in {"boss", "manager"}:
        return {"ok": False, "error": "permission_denied", "message": "只有老板或店长可以更新机构认知状态。"}
    state = _read_state_dict(store, INSTITUTION_UNDERSTANDING_FILE)
    now = now_iso()
    state.update({
        "schema_version": 1,
        "tenant_id": current_tenant_id(),
        "updated_at": now,
        "source_text": _limit_text(source_text),
        "source": _autonomous_source(identity, operation_id, source_message_id),
        "auto_effects": {"forces_next_action": False, "changes_router": False, "updates_handbook": False},
    })
    if known_facts is not None:
        state["known_facts"] = _strip_forbidden(known_facts)
    if missing_facts is not None:
        state["missing_facts"] = _strip_forbidden(missing_facts)
    if pending_confirmations is not None:
        state["pending_confirmations"] = _strip_forbidden(pending_confirmations)
    if stale_or_conflicting_facts is not None:
        state["stale_or_conflicting_facts"] = _strip_forbidden(stale_or_conflicting_facts)
    if next_review_at:
        state["next_review_at"] = str(next_review_at or "").strip()
    _write_state_dict(store, INSTITUTION_UNDERSTANDING_FILE, state)
    verified = _read_state_dict(store, INSTITUTION_UNDERSTANDING_FILE).get("updated_at") == now
    return {"ok": True, "state": state, "writeback_verified": verified, "rendered_text": "已更新机构认知状态；这只是事实材料，不限制 Hermes 自主判断。"}


def submit_institution_fact_gap(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    gap_key: str,
    gap_text: str,
    ask_role: str,
    operation_id: str,
    target_time: str = "",
    urgency: str = "normal",
    related_objects: list[Any] | None = None,
    source_text: str = "",
    source_message_id: str = "",
) -> dict[str, Any]:
    if not str(gap_key or "").strip() or not str(gap_text or "").strip():
        return {"ok": False, "error": "fact_gap_requires_content", "message": "机构事实缺口必须包含 key 和说明。"}
    normalized_key = str(gap_key or "").strip()
    normalized_key = {
        "owner_current_goal_priority": "owner_priority",
        "owner_goal_priority": "owner_priority",
        "safety_issue_details": "safety_event_detail",
        "safety_event_details": "safety_event_detail",
        "safety_task_data_source": "safety_event_detail",
        "safety_task_discrepancy": "safety_event_detail",
        "safety_task_detail": "safety_event_detail",
    }.get(normalized_key, normalized_key)
    normalized_gap = {
        "gap_key": normalized_key,
        "gap_text": _limit_text(gap_text),
        "ask_role": str(ask_role or "").strip(),
        "target_time": str(target_time or "").strip(),
        "urgency": str(urgency or "normal").strip(),
        "related_objects": _strip_forbidden(related_objects or []),
    }

    def semantic_value(value: Any) -> Any:
        if isinstance(value, str):
            return re.sub(r"[\s，。；：、,.!！?？（）()\[\]【】]+", "", value).lower()
        if isinstance(value, list):
            return [semantic_value(item) for item in value]
        if isinstance(value, dict):
            return {key: semantic_value(item) for key, item in sorted(value.items())}
        return value

    autonomous_source = str(identity.canonical_user_id or "") == "autonomous_employee_loop"
    gap_numeric_facts = set(re.findall(r"\d+(?:\.\d+)?", normalized_gap["gap_text"]))
    gap_ratio_facts = set(re.findall(r"\d+\s*/\s*\d+", normalized_gap["gap_text"]))
    key_aliases = {
        "safety_issue_details": "safety_event_detail",
        "safety_event_details": "safety_event_detail",
        "safety_task_data_source": "safety_event_detail",
        "safety_task_discrepancy": "safety_event_detail",
        "safety_task_detail": "safety_event_detail",
    }
    for existing in reversed(_read_jsonl(store, INSTITUTION_FACT_GAP_EVENTS_FILE)[-500:]):
        existing_key = str(existing.get("gap_key") or "")
        existing_key = key_aliases.get(existing_key, existing_key)
        if existing_key != normalized_gap["gap_key"]:
            continue
        existing_text = str(existing.get("gap_text") or "")
        existing_numeric_facts = set(re.findall(r"\d+(?:\.\d+)?", existing_text))
        existing_ratio_facts = set(re.findall(r"\d+\s*/\s*\d+", existing_text))
        same_autonomous_fact = (
            autonomous_source
            and str(existing.get("created_at") or "")[:10] == now_iso()[:10]
            and (
                semantic_value(existing_text) == semantic_value(normalized_gap["gap_text"])
                or (
                    bool(gap_numeric_facts)
                    and existing_numeric_facts == gap_numeric_facts
                )
                or (
                    bool(gap_ratio_facts)
                    and bool(gap_ratio_facts & existing_ratio_facts)
                )
            )
        )
        if all(
            semantic_value(existing.get(key)) == semantic_value(value)
            for key, value in normalized_gap.items()
        ) or same_autonomous_fact:
            return {
                "ok": True,
                "gap_event": existing,
                "writeback_verified": True,
                "state_changed": False,
                "idempotent_replay": True,
                "rendered_text": "机构事实缺口没有实质变化，本轮未重复追加。",
            }
        break
    row = {
        "gap_event_id": _new_id("institution_gap"),
        "tenant_id": current_tenant_id(),
        "gap_key": normalized_key,
        "gap_text": _limit_text(gap_text),
        "ask_role": str(ask_role or "").strip(),
        "target_time": str(target_time or "").strip(),
        "urgency": str(urgency or "normal").strip(),
        "related_objects": _strip_forbidden(related_objects or []),
        "source_text": _limit_text(source_text),
        "source": _autonomous_source(identity, operation_id, source_message_id),
        "created_at": now_iso(),
        "auto_effects": {"sends_messages": False, "forces_next_action": False, "changes_router": False},
    }
    _append_jsonl(store, INSTITUTION_FACT_GAP_EVENTS_FILE, row)
    return {"ok": True, "gap_event": row, "writeback_verified": True, "state_changed": True, "rendered_text": "已保存机构事实缺口；它只是提醒 Hermes 缺什么事实，不自动询问或执行。"}


def query_hermes_employee_scorecard(store: TuoguanStore, *, identity: UserIdentity, limit: int = 30) -> dict[str, Any]:
    if identity.role not in {"boss", "manager"}:
        return {"ok": False, "error": "permission_denied", "message": "只有老板或店长可以查看 Hermes 员工自评。"}
    all_rows = _read_jsonl(store, HERMES_EMPLOYEE_SCORECARD_FILE)
    by_date: dict[str, dict[str, Any]] = {}
    undated: list[dict[str, Any]] = []
    for row in all_rows:
        review_date = str(row.get("review_date") or "").strip()
        if review_date:
            by_date[review_date] = row
        else:
            undated.append(row)
    rows = sorted(
        [*undated, *by_date.values()],
        key=lambda row: str(row.get("created_at") or ""),
    )[-max(1, min(int(limit or 30), 200)):]
    latest = rows[-1] if rows else {}
    rendered = f"查到 {len(rows)} 条 Hermes 员工自评；最近一次评分 {latest.get('quality_score', '无')}。自评只做内部复盘，不自动改变规则或记忆。"
    return {
        "ok": True,
        "review_count": len(rows),
        "historical_duplicate_count": max(0, len(all_rows) - len(rows)),
        "latest_review": latest,
        "reviews": rows,
        "rendered_text": rendered,
        "render_verified": True,
    }


def submit_employee_self_review(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    operation_id: str,
    review_date: str = "",
    institution_understanding: str = "",
    goal_progress: str = "",
    teacher_support: str = "",
    student_service_evidence: str = "",
    risk_detection: str = "",
    business_opportunity: str = "",
    learning_growth: str = "",
    tomorrow_focus: str = "",
    blocked_by: list[Any] | None = None,
    quality_score: int = 0,
    source_text: str = "",
    source_message_id: str = "",
) -> dict[str, Any]:
    normalized_review_date = str(review_date or now_iso()[:10]).strip()
    for existing in reversed(_read_jsonl(store, HERMES_EMPLOYEE_SCORECARD_FILE)[-60:]):
        if str(existing.get("review_date") or "") == normalized_review_date:
            return {
                "ok": True,
                "self_review": existing,
                "writeback_verified": True,
                "state_changed": False,
                "idempotent_replay": True,
                "rendered_text": "今天的正式员工自评已经保存，本轮未重复追加。",
            }
    row = {
        "review_id": _new_id("employee_review"),
        "tenant_id": current_tenant_id(),
        "review_date": normalized_review_date,
        "institution_understanding": _limit_text(institution_understanding, 800),
        "goal_progress": _limit_text(goal_progress, 800),
        "teacher_support": _limit_text(teacher_support, 800),
        "student_service_evidence": _limit_text(student_service_evidence, 800),
        "risk_detection": _limit_text(risk_detection, 800),
        "business_opportunity": _limit_text(business_opportunity, 800),
        "learning_growth": _limit_text(learning_growth, 800),
        "tomorrow_focus": _limit_text(tomorrow_focus, 800),
        "blocked_by": _strip_forbidden(blocked_by or []),
        "quality_score": max(0, min(int(quality_score or 0), 100)),
        "source_text": _limit_text(source_text),
        "source": _autonomous_source(identity, operation_id, source_message_id),
        "created_at": now_iso(),
        "auto_effects": {"updates_long_term_memory": False, "forces_next_action": False, "changes_router": False},
    }
    if not any(row.get(key) for key in ("institution_understanding", "goal_progress", "tomorrow_focus", "blocked_by")):
        return {"ok": False, "error": "self_review_requires_content", "message": "员工自评至少要包含推进、卡点或明日重点。"}
    _append_jsonl(store, HERMES_EMPLOYEE_SCORECARD_FILE, row)
    return {"ok": True, "self_review": row, "writeback_verified": True, "state_changed": True, "rendered_text": "已保存 Hermes 员工自评；只用于内部复盘，不自动写长期记忆或限制模型。"}


def query_industry_learning_candidates(store: TuoguanStore, *, identity: UserIdentity, status: str = "", limit: int = 30) -> dict[str, Any]:
    if identity.role not in {"boss", "manager"}:
        return {"ok": False, "error": "permission_denied", "message": "只有老板或店长可以查看行业学习候选。"}
    status_filter = str(status or "").strip()
    rows = []
    for row in _read_jsonl(store, INDUSTRY_LEARNING_CANDIDATES_FILE):
        if status_filter and str(row.get("status") or "") != status_filter:
            continue
        rows.append(deepcopy(row))
    rows.sort(key=lambda item: str(item.get("created_at") or ""))
    rows = rows[-max(1, min(int(limit or 30), 200)):]
    rendered = f"查到 {len(rows)} 条行业学习候选。公网资料只能作为建议材料，老板审核前不进入正式知识库。"
    return {"ok": True, "candidate_count": len(rows), "candidates": rows, "rendered_text": rendered, "render_verified": True}


def query_external_research_runs(store: TuoguanStore, *, identity: UserIdentity, mode: str = "", limit: int = 20) -> dict[str, Any]:
    if identity.role not in {"boss", "manager"}:
        return {"ok": False, "error": "permission_denied", "message": "只有老板或店长可以查看外部学习运行记录。"}
    mode_filter = str(mode or "").strip()
    rows = []
    for row in _read_jsonl(store, EXTERNAL_RESEARCH_RUNS_FILE):
        if mode_filter and str(row.get("mode") or "") != mode_filter:
            continue
        rows.append(deepcopy(row))
    rows.sort(key=lambda item: str(item.get("created_at") or ""))
    rows = rows[-max(1, min(int(limit or 20), 100)):]
    return {
        "ok": True,
        "run_count": len(rows),
        "runs": rows,
        "rendered_text": f"查到 {len(rows)} 条外部学习运行记录。它们只说明 Hermes 学习过什么，不代表已经采纳为机构事实。",
        "render_verified": True,
    }


def query_market_research_candidates(store: TuoguanStore, *, identity: UserIdentity, status: str = "", limit: int = 30) -> dict[str, Any]:
    if identity.role not in {"boss", "manager"}:
        return {"ok": False, "error": "permission_denied", "message": "只有老板或店长可以查看市场调研候选。"}
    status_filter = str(status or "").strip()
    rows = []
    for row in _read_jsonl(store, MARKET_RESEARCH_CANDIDATES_FILE):
        if status_filter and str(row.get("status") or "") != status_filter:
            continue
        rows.append(deepcopy(row))
    rows.sort(key=lambda item: str(item.get("created_at") or ""))
    rows = rows[-max(1, min(int(limit or 30), 100)):]
    return {
        "ok": True,
        "candidate_count": len(rows),
        "candidates": rows,
        "rendered_text": f"查到 {len(rows)} 条市场调研候选。公开竞品资料需要人工核验，不能直接当成事实。",
        "render_verified": True,
    }


def query_competitor_profiles(store: TuoguanStore, *, identity: UserIdentity, limit: int = 30) -> dict[str, Any]:
    if identity.role not in {"boss", "manager"}:
        return {"ok": False, "error": "permission_denied", "message": "只有老板或店长可以查看竞品公开画像候选。"}
    rows = _read_jsonl(store, COMPETITOR_PROFILES_FILE)
    rows.sort(key=lambda item: str(item.get("created_at") or ""))
    rows = rows[-max(1, min(int(limit or 30), 100)):]
    return {
        "ok": True,
        "profile_count": len(rows),
        "profiles": rows,
        "rendered_text": f"查到 {len(rows)} 条竞品公开画像候选。它们是公开线索，不是已确认竞品结论。",
        "render_verified": True,
    }


def query_external_learning_brief(store: TuoguanStore, *, identity: UserIdentity, limit: int = 5) -> dict[str, Any]:
    if identity.role not in {"boss", "manager"}:
        return {"ok": False, "error": "permission_denied", "message": "只有老板或店长可以查看外部学习摘要。"}
    runs = query_external_research_runs(store, identity=identity, limit=limit)
    industry = query_industry_learning_candidates(store, identity=identity, limit=limit)
    market = query_market_research_candidates(store, identity=identity, limit=limit)
    competitors = query_competitor_profiles(store, identity=identity, limit=limit)
    report_runs = _read_jsonl(store, WEEKLY_MARKET_REPORT_RUNS_FILE)
    report_runs.sort(key=lambda item: str(item.get("created_at") or ""))
    recent_reports = report_runs[-max(1, min(int(limit or 5), 30)):]
    return {
        "ok": True,
        "external_research_runs": runs,
        "industry_learning_candidates": industry,
        "market_research_candidates": market,
        "competitor_profiles": competitors,
        "recent_report_runs": recent_reports,
        "rendered_text": (
            f"外部学习摘要：运行 {runs.get('run_count', 0)} 次，"
            f"行业候选 {industry.get('candidate_count', 0)} 条，"
            f"市场候选 {market.get('candidate_count', 0)} 条，"
            f"竞品线索 {competitors.get('profile_count', 0)} 条。"
            "这些都是建议材料，是否采纳由 Hermes 结合老板审核和业务事实判断。"
        ),
        "render_verified": True,
    }


def submit_industry_learning_candidate(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    topic: str,
    summary: str,
    operation_id: str,
    sources: list[Any] | None = None,
    applicability: str = "",
    status: str = "pending_review",
    source_text: str = "",
    source_message_id: str = "",
) -> dict[str, Any]:
    if not str(topic or "").strip() or not str(summary or "").strip():
        return {"ok": False, "error": "industry_candidate_requires_content", "message": "行业学习候选必须包含主题和摘要。"}
    normalized_sources = _strip_forbidden(sources or [])
    if not isinstance(normalized_sources, list):
        normalized_sources = []
    source_count = len([item for item in normalized_sources if isinstance(item, dict) and str(item.get("url") or "").startswith(("http://", "https://"))])
    normalized_status = str(status or "pending_review").strip()
    if normalized_status not in {"pending_review", "approved", "rejected", "needs_more_evidence", "source_failed"}:
        normalized_status = "pending_review"
    row = {
        "candidate_id": _new_id("industry_learn"),
        "tenant_id": current_tenant_id(),
        "topic": _limit_text(topic, 200),
        "summary": _limit_text(summary),
        "sources": normalized_sources[:12],
        "source_count": source_count,
        "applicability": _limit_text(applicability, 800),
        "status": normalized_status,
        "source_text": _limit_text(source_text),
        "source": _autonomous_source(identity, operation_id, source_message_id),
        "created_at": now_iso(),
        "auto_effects": {"updates_handbook": False, "updates_institution_facts": False, "forces_next_action": False, "changes_router": False},
    }
    _append_jsonl(store, INDUSTRY_LEARNING_CANDIDATES_FILE, row)
    return {"ok": True, "candidate": row, "writeback_verified": True, "rendered_text": "已保存行业学习候选；审核前不会进入正式手册或机构事实。"}


def query_value_progress_ledger(store: TuoguanStore, *, identity: UserIdentity, subject: str = "", limit: int = 30) -> dict[str, Any]:
    if identity.role not in {"boss", "manager"}:
        return {"ok": False, "error": "permission_denied", "message": "只有老板或店长可以查看价值推进账本。"}
    subject_filter = str(subject or "").strip()
    rows = []
    for row in _read_jsonl(store, VALUE_PROGRESS_LEDGER_FILE):
        if not _value_progress_is_current_material(row) and not subject_filter:
            continue
        if subject_filter and subject_filter not in json.dumps(row, ensure_ascii=False):
            continue
        rows.append(deepcopy(row))
    rows.sort(key=lambda item: str(item.get("created_at") or ""))
    rows = rows[-max(1, min(int(limit or 30), 200)):]
    return {"ok": True, "entry_count": len(rows), "entries": rows, "rendered_text": f"查到 {len(rows)} 条价值推进账本。它只记录参与推动和证据，不夸大归因。", "render_verified": True}


def _value_progress_is_current_material(row: dict[str, Any]) -> bool:
    status = str(
        row.get("decision_material_status")
        or row.get("recall_status")
        or row.get("current_material_status")
        or ""
    ).strip()
    if status in {"historical_snapshot", "not_current_decision_material", "superseded"}:
        return False
    text = json.dumps(row, ensure_ascii=False)
    old_roster_terms = ("122", "服务类型", "主责老师", "旧学生", "旧名单", "历史快照")
    if any(term in text for term in old_roster_terms) and any(term in text for term in ("等待", "确认", "缺少")):
        created = str(row.get("created_at") or "")
        if created and created[:10] < "2026-08-01":
            return False
    if "等待中" in text and "创造价值" in text:
        return False
    if "未送达" in text and ("attention_thread" in text or "提醒" in text or "boss_attention" in text):
        return False
    return True


def submit_value_progress_entry(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    subject: str,
    discovered: str,
    hermes_action: str,
    operation_id: str,
    human_action: str = "",
    outcome: str = "",
    evidence: list[Any] | None = None,
    attribution: str = "participated",
    source_text: str = "",
    source_message_id: str = "",
) -> dict[str, Any]:
    if not str(discovered or "").strip() or not str(hermes_action or "").strip():
        return {"ok": False, "error": "value_progress_requires_content", "message": "价值推进账本至少要说明发现了什么以及 Hermes 做了什么。"}
    normalized_attribution = str(attribution or "participated").strip()
    if normalized_attribution not in {"participated", "assisted", "observed", "unknown"}:
        normalized_attribution = "participated"
    normalized_evidence = _strip_forbidden(evidence or [])
    semantic_fingerprint = json.dumps(
        {
            "subject": re.sub(r"\s+", "", str(subject or "")).lower(),
            "discovered": re.sub(r"\s+", "", str(discovered or "")).lower(),
            "hermes_action": re.sub(r"\s+", "", str(hermes_action or "")).lower(),
            "human_action": re.sub(r"\s+", "", str(human_action or "")).lower(),
            "outcome": re.sub(r"\s+", "", str(outcome or "")).lower(),
            "evidence": normalized_evidence,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    for existing in reversed(_read_jsonl(store, VALUE_PROGRESS_LEDGER_FILE)[-500:]):
        existing_fingerprint = str(existing.get("semantic_fingerprint") or "")
        if not existing_fingerprint:
            existing_fingerprint = json.dumps(
                {
                    "subject": re.sub(r"\s+", "", str(existing.get("subject") or "")).lower(),
                    "discovered": re.sub(r"\s+", "", str(existing.get("discovered") or "")).lower(),
                    "hermes_action": re.sub(r"\s+", "", str(existing.get("hermes_action") or "")).lower(),
                    "human_action": re.sub(r"\s+", "", str(existing.get("human_action") or "")).lower(),
                    "outcome": re.sub(r"\s+", "", str(existing.get("outcome") or "")).lower(),
                    "evidence": existing.get("evidence") or [],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        if existing_fingerprint == semantic_fingerprint:
            return {
                "ok": True,
                "entry": existing,
                "writeback_verified": True,
                "state_changed": False,
                "idempotent_replay": True,
                "rendered_text": "相同事实和证据的价值推进已经记录，本轮未重复追加。",
            }
    row = {
        "entry_id": _new_id("value_progress"),
        "tenant_id": current_tenant_id(),
        "subject": _limit_text(subject, 200),
        "discovered": _limit_text(discovered),
        "hermes_action": _limit_text(hermes_action),
        "human_action": _limit_text(human_action),
        "outcome": _limit_text(outcome),
        "evidence": normalized_evidence,
        "semantic_fingerprint": semantic_fingerprint,
        "attribution": normalized_attribution,
        "source_text": _limit_text(source_text),
        "source": _autonomous_source(identity, operation_id, source_message_id),
        "created_at": now_iso(),
        "auto_effects": {"claims_full_revenue_attribution": False, "forces_next_action": False, "changes_router": False},
    }
    _append_jsonl(store, VALUE_PROGRESS_LEDGER_FILE, row)
    return {
        "ok": True,
        "entry": row,
        "writeback_verified": True,
        "state_changed": True,
        "rendered_text": "已保存价值推进账本；仅记录参与推动，不夸大归因。",
    }


_AGENT_TYPES = {"institution_audit_agent", "goal_review_agent", "industry_research_agent"}
_DELEGATION_STATUSES = {"queued", "running", "completed", "failed", "discarded", "superseded"}
_DELEGATION_DECISIONS = {"pending", "adopted", "partially_adopted", "rejected", "needs_more_evidence", "deferred"}


def _fold_agent_delegations(store: TuoguanStore) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    decisions: dict[str, list[dict[str, Any]]] = {}
    for row in _read_jsonl(store, AGENT_DELEGATIONS_FILE):
        record_type = str(row.get("record_type") or "")
        delegation_id = str(row.get("delegation_id") or "")
        if not delegation_id:
            continue
        if record_type == "agent_delegation_decision":
            decisions.setdefault(delegation_id, []).append(deepcopy(row))
            continue
        item = deepcopy(row)
        item.setdefault("record_type", "agent_delegation")
        item.setdefault("status", "queued")
        item.setdefault("main_hermes_decision", "pending")
        item.setdefault("updated_at", item.get("created_at") or "")
        item.setdefault("decision_updates", [])
        result[delegation_id] = item
    for delegation_id, rows in decisions.items():
        if delegation_id not in result:
            continue
        rows.sort(key=lambda item: str(item.get("created_at") or ""))
        latest = rows[-1]
        result[delegation_id]["decision_updates"] = rows
        result[delegation_id]["main_hermes_decision"] = str(latest.get("main_hermes_decision") or "pending")
        result[delegation_id]["decision_note"] = str(latest.get("decision_note") or "")
        result[delegation_id]["updated_at"] = str(latest.get("created_at") or result[delegation_id].get("updated_at") or "")
    return result


def query_agent_delegations(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    agent_type: str = "",
    status: str = "",
    parent_focus_key: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    if identity.role not in {"boss", "manager"}:
        return {"ok": False, "error": "permission_denied", "message": "只有老板或店长可以查看多 Agent 委派记录。"}
    rows = []
    type_filter = str(agent_type or "").strip()
    status_filter = str(status or "").strip()
    focus_filter = str(parent_focus_key or "").strip()
    for row in _fold_agent_delegations(store).values():
        if type_filter and str(row.get("agent_type") or "") != type_filter:
            continue
        if status_filter and str(row.get("status") or "") != status_filter:
            continue
        if focus_filter and str(row.get("parent_focus_key") or "") != focus_filter:
            continue
        rows.append(deepcopy(row))
    rows.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""))
    rows = rows[-max(1, min(int(limit or 50), 200)):]
    return {
        "ok": True,
        "delegation_count": len(rows),
        "delegations": rows,
        "rendered_text": f"查到 {len(rows)} 条多 Agent 影子委派。子 Agent 只提供参谋材料，不能直接改变业务事实或现实动作。",
        "render_verified": True,
    }


def submit_agent_delegation(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    agent_type: str,
    delegation_reason: str,
    question: str,
    operation_id: str,
    parent_work_item_id: str = "",
    parent_goal_id: str = "",
    parent_focus_key: str = "",
    fact_scope: dict[str, Any] | None = None,
    allowed_read_tools: list[Any] | None = None,
    forbidden_actions: list[Any] | None = None,
    expected_output: list[Any] | None = None,
    source_text: str = "",
    source_message_id: str = "",
    status: str = "queued",
) -> dict[str, Any]:
    normalized_type = str(agent_type or "").strip()
    if normalized_type not in _AGENT_TYPES:
        return {"ok": False, "error": "invalid_agent_type", "message": "子 Agent 类型必须是 institution_audit_agent、goal_review_agent 或 industry_research_agent。"}
    normalized_status = str(status or "queued").strip()
    if normalized_status not in _DELEGATION_STATUSES:
        normalized_status = "queued"
    if not str(delegation_reason or "").strip() or not str(question or "").strip():
        return {"ok": False, "error": "delegation_requires_reason", "message": "多 Agent 委派必须包含委派原因和具体问题。"}
    row = {
        "record_type": "agent_delegation",
        "delegation_id": _new_id("agent_delegate"),
        "tenant_id": current_tenant_id(),
        "agent_type": normalized_type,
        "status": normalized_status,
        "parent_work_item_id": str(parent_work_item_id or "").strip(),
        "parent_goal_id": str(parent_goal_id or "").strip(),
        "parent_focus_key": str(parent_focus_key or "").strip(),
        "delegation_reason": _limit_text(delegation_reason),
        "question": _limit_text(question),
        "fact_scope": _strip_forbidden(fact_scope or {}),
        "allowed_read_tools": _strip_forbidden(allowed_read_tools or []),
        "forbidden_actions": _strip_forbidden(forbidden_actions or [
            "contact_people", "write_business_data", "create_tasks", "send_messages",
            "change_salary", "change_permissions", "update_handbook", "write_memory",
            "decide_for_main_hermes", "create_child_agents",
        ]),
        "expected_output": _strip_forbidden(expected_output or ["facts", "evidence", "inferences", "uncertainties", "recommendations", "confidence"]),
        "main_hermes_decision": "pending",
        "source_text": _limit_text(source_text),
        "source": _autonomous_source(identity, operation_id, source_message_id),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "auto_effects": {
            "writes_business_data": False,
            "sends_messages": False,
            "creates_tasks": False,
            "changes_salary": False,
            "changes_permissions": False,
            "updates_handbook": False,
            "updates_memory": False,
            "forces_next_action": False,
            "changes_router": False,
        },
    }
    _append_jsonl(store, AGENT_DELEGATIONS_FILE, row)
    return {"ok": True, "delegation": row, "writeback_verified": True, "rendered_text": "已保存多 Agent 影子委派；它只是内部参谋任务，不会直接影响生产业务。"}


def query_agent_delegation_results(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    delegation_id: str = "",
    agent_type: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    if identity.role not in {"boss", "manager"}:
        return {"ok": False, "error": "permission_denied", "message": "只有老板或店长可以查看多 Agent 结果。"}
    id_filter = str(delegation_id or "").strip()
    type_filter = str(agent_type or "").strip()
    rows = []
    for row in _read_jsonl(store, AGENT_DELEGATION_RESULTS_FILE):
        if id_filter and str(row.get("delegation_id") or "") != id_filter:
            continue
        if type_filter and str(row.get("agent_type") or "") != type_filter:
            continue
        rows.append(deepcopy(row))
    rows.sort(key=lambda item: str(item.get("created_at") or ""))
    rows = rows[-max(1, min(int(limit or 50), 200)):]
    return {
        "ok": True,
        "result_count": len(rows),
        "results": rows,
        "rendered_text": f"查到 {len(rows)} 条子 Agent 影子结果。结果是参谋材料，必须由主 Hermes 基于真实事实决定是否采纳。",
        "render_verified": True,
    }


def submit_agent_delegation_result(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    delegation_id: str,
    agent_type: str,
    summary: str,
    operation_id: str,
    facts: list[Any] | None = None,
    evidence: list[Any] | None = None,
    inferences: list[Any] | None = None,
    uncertainties: list[Any] | None = None,
    recommendations: list[Any] | None = None,
    confidence: str = "",
    status: str = "completed",
    raw_output_ref: str = "",
    source_text: str = "",
    source_message_id: str = "",
) -> dict[str, Any]:
    normalized_id = str(delegation_id or "").strip()
    normalized_type = str(agent_type or "").strip()
    if not normalized_id or normalized_type not in _AGENT_TYPES or not str(summary or "").strip():
        return {"ok": False, "error": "delegation_result_requires_content", "message": "子 Agent 结果必须包含委派 id、类型和摘要。"}
    normalized_status = str(status or "completed").strip()
    if normalized_status not in _DELEGATION_STATUSES:
        normalized_status = "completed"
    row = {
        "result_id": _new_id("agent_result"),
        "tenant_id": current_tenant_id(),
        "delegation_id": normalized_id,
        "agent_type": normalized_type,
        "status": normalized_status,
        "summary": _limit_text(summary),
        "facts": _strip_forbidden(facts or []),
        "evidence": _strip_forbidden(evidence or []),
        "inferences": _strip_forbidden(inferences or []),
        "uncertainties": _strip_forbidden(uncertainties or []),
        "recommendations": _strip_forbidden(recommendations or []),
        "confidence": str(confidence or "").strip(),
        "raw_output_ref": str(raw_output_ref or "").strip(),
        "source_text": _limit_text(source_text),
        "source": _autonomous_source(identity, operation_id, source_message_id),
        "created_at": now_iso(),
        "auto_effects": {
            "writes_business_data": False,
            "sends_messages": False,
            "creates_tasks": False,
            "changes_salary": False,
            "changes_permissions": False,
            "updates_handbook": False,
            "updates_memory": False,
            "forces_next_action": False,
            "changes_router": False,
        },
    }
    _append_jsonl(store, AGENT_DELEGATION_RESULTS_FILE, row)
    return {"ok": True, "result": row, "writeback_verified": True, "rendered_text": "已保存子 Agent 影子结果；结果只供主 Hermes 判断，不自动生效。"}


def update_agent_delegation_decision(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    delegation_id: str,
    main_hermes_decision: str,
    decision_note: str,
    operation_id: str,
    adopted_points: list[Any] | None = None,
    rejected_points: list[Any] | None = None,
    source_text: str = "",
    source_message_id: str = "",
) -> dict[str, Any]:
    normalized_id = str(delegation_id or "").strip()
    if normalized_id not in _fold_agent_delegations(store):
        return {"ok": False, "error": "delegation_not_found", "message": "没有找到要更新的多 Agent 委派记录。"}
    decision = str(main_hermes_decision or "pending").strip()
    if decision not in _DELEGATION_DECISIONS:
        return {"ok": False, "error": "invalid_delegation_decision", "message": "采纳决策必须是 pending、adopted、partially_adopted、rejected、needs_more_evidence 或 deferred。"}
    if not str(decision_note or "").strip():
        return {"ok": False, "error": "delegation_decision_requires_note", "message": "主 Hermes 必须说明为什么采纳、部分采纳、延后或拒绝。"}
    row = {
        "record_type": "agent_delegation_decision",
        "decision_id": _new_id("agent_decision"),
        "tenant_id": current_tenant_id(),
        "delegation_id": normalized_id,
        "main_hermes_decision": decision,
        "decision_note": _limit_text(decision_note),
        "adopted_points": _strip_forbidden(adopted_points or []),
        "rejected_points": _strip_forbidden(rejected_points or []),
        "source_text": _limit_text(source_text),
        "source": _autonomous_source(identity, operation_id, source_message_id),
        "created_at": now_iso(),
        "auto_effects": {"forces_next_action": False, "changes_router": False, "writes_business_data": False},
    }
    _append_jsonl(store, AGENT_DELEGATIONS_FILE, row)
    folded = _fold_agent_delegations(store).get(normalized_id, {})
    return {"ok": True, "delegation": folded, "decision": row, "writeback_verified": folded.get("main_hermes_decision") == decision, "rendered_text": "已记录主 Hermes 对子 Agent 结果的采纳判断；不会自动改变业务事实。"}


def query_multi_agent_brief(store: TuoguanStore, *, identity: UserIdentity, limit: int = 20) -> dict[str, Any]:
    delegations = query_agent_delegations(store, identity=identity, limit=limit)
    results = query_agent_delegation_results(store, identity=identity, limit=limit)
    if not delegations.get("ok"):
        return delegations
    rows = delegations.get("delegations") or []
    pending = [row for row in rows if str(row.get("main_hermes_decision") or "pending") == "pending"]
    by_type: dict[str, int] = {}
    for row in rows:
        key = str(row.get("agent_type") or "unknown")
        by_type[key] = by_type.get(key, 0) + 1
    return {
        "ok": True,
        "delegation_count": len(rows),
        "result_count": int(results.get("result_count") or 0) if isinstance(results, dict) else 0,
        "pending_decision_count": len(pending),
        "by_type": by_type,
        "recent_delegations": rows[-10:],
        "recent_results": (results.get("results") or [])[-10:] if isinstance(results, dict) else [],
        "rendered_text": f"多 Agent 影子简报：委派 {len(rows)} 条，结果 {int(results.get('result_count') or 0) if isinstance(results, dict) else 0} 条，待主 Hermes 判断 {len(pending)} 条。",
        "render_verified": True,
    }


def relationship_touch_policy(store: TuoguanStore) -> dict[str, Any]:
    loaded = store.read_json(RELATIONSHIP_TOUCH_POLICY_FILE, {})
    policy = deepcopy(DEFAULT_RELATIONSHIP_TOUCH_POLICY)
    if isinstance(loaded, dict):
        for key, value in loaded.items():
            if isinstance(value, dict) and isinstance(policy.get(key), dict):
                merged = deepcopy(policy[key])
                merged.update(value)
                policy[key] = merged
            else:
                policy[key] = value
    return policy


def query_relationship_touch_candidates(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    target_user_id: str = "",
    target_role: str = "",
    include_closed: bool = False,
    limit: int = 30,
) -> dict[str, Any]:
    requested_user = str(target_user_id or "").strip()
    requested_role = str(target_role or "").strip()
    rows: list[dict[str, Any]] = []
    for row in _fold_relationship_touch_candidates(store).values():
        status = str(row.get("status") or "candidate")
        if not include_closed and status not in _OPEN_RELATIONSHIP_TOUCH_STATUSES:
            continue
        if requested_user and str(row.get("target_user_id") or "") not in {requested_user, ""}:
            continue
        if requested_role and str(row.get("target_role") or "") != requested_role:
            continue
        if not _identity_can_view_relationship_touch(identity, row):
            continue
        rows.append(deepcopy(row))
    rows.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""))
    rows = rows[-max(1, min(int(limit or 30), 100)):]
    return {
        "ok": True,
        "candidate_count": len(rows),
        "candidates": rows,
        "policy": relationship_touch_policy(store),
        "rendered_text": f"查到 {len(rows)} 条 Hermes 关系经营候选。候选只是材料，不自动联系老师、店长或家长。",
        "render_verified": True,
    }


def submit_relationship_touch_candidate(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    target_role: str,
    touch_type: str,
    message: str,
    reason: str,
    operation_id: str,
    target_user_id: str = "",
    target_name: str = "",
    value: str = "",
    work_related: bool = False,
    private_emotional_support: bool = False,
    requires_authorization: bool = True,
    external_send_allowed: bool = False,
    suggested_send_at: str = "",
    status: str = "candidate",
    source_text: str = "",
    source_message_id: str = "",
) -> dict[str, Any]:
    role = str(target_role or "").strip()
    touch = str(touch_type or "").strip()
    if role not in {"boss", "manager", "teacher"}:
        return {"ok": False, "error": "invalid_relationship_target_role", "message": "关系经营对象必须是老板、店长或老师。"}
    if touch not in _RELATIONSHIP_TOUCH_TYPES:
        touch = "care" if role == "teacher" else ("manager_assist" if role == "manager" else "presence_report")
    normalized_status = str(status or "candidate").strip()
    if normalized_status not in _RELATIONSHIP_TOUCH_STATUSES:
        normalized_status = "candidate"
    clean_message = _safe_relationship_message(message, role=role, work_related=bool(work_related))
    clean_reason = _limit_text(reason, 500)
    if not clean_message or not clean_reason:
        return {"ok": False, "error": "relationship_touch_requires_message", "message": "关系经营候选必须有自然内容和原因。"}
    policy = relationship_touch_policy(store)
    role_policy = policy.get(role) if isinstance(policy.get(role), dict) else {}
    if role != "boss":
        external_send_allowed = False
        requires_authorization = True
    elif str(role_policy.get("mode") or "candidate") != "direct":
        external_send_allowed = False
    semantic_fingerprint = json.dumps({
        "day": now_iso()[:10],
        "target_role": role,
        "target_user_id": str(target_user_id or ""),
        "touch_type": touch,
        "message": re.sub(r"\s+", "", clean_message).lower(),
    }, ensure_ascii=False, sort_keys=True)
    for existing in reversed(_read_jsonl(store, RELATIONSHIP_TOUCH_CANDIDATES_FILE)[-500:]):
        if str(existing.get("semantic_fingerprint") or "") == semantic_fingerprint:
            return {
                "ok": True,
                "candidate": existing,
                "writeback_verified": True,
                "state_changed": False,
                "idempotent_replay": True,
                "rendered_text": "相同关系经营候选今天已存在，本轮未重复追加。",
            }
    row = {
        "candidate_id": _new_id("relationship_touch"),
        "tenant_id": current_tenant_id(),
        "target_role": role,
        "target_user_id": str(target_user_id or "").strip(),
        "target_name": _limit_text(target_name, 80),
        "touch_type": touch,
        "message": clean_message,
        "reason": clean_reason,
        "value": _limit_text(value, 500),
        "work_related": bool(work_related),
        "private_emotional_support": bool(private_emotional_support),
        "requires_authorization": bool(requires_authorization),
        "external_send_allowed": bool(external_send_allowed),
        "suggested_send_at": str(suggested_send_at or "").strip(),
        "status": normalized_status,
        "semantic_fingerprint": semantic_fingerprint,
        "source_text": _limit_text(source_text),
        "source": _autonomous_source(identity, operation_id, source_message_id),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "auto_effects": {
            "sends_parent_messages": False,
            "sends_teacher_messages": False,
            "sends_manager_messages": False,
            "changes_salary": False,
            "changes_performance_conclusion": False,
            "reports_private_chat_to_boss": False,
            "forces_next_action": False,
            "changes_router": False,
        },
    }
    _append_jsonl(store, RELATIONSHIP_TOUCH_CANDIDATES_FILE, row)
    return {
        "ok": True,
        "candidate": row,
        "writeback_verified": True,
        "state_changed": True,
        "rendered_text": "已保存 Hermes 关系经营候选；未授权老师/店长不会自动外发。",
    }


def update_relationship_touch_candidate_status(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    candidate_id: str,
    status: str,
    operation_id: str,
    delivery_receipt: dict[str, Any] | None = None,
    failure_reason: str = "",
    source_text: str = "",
    source_message_id: str = "",
) -> dict[str, Any]:
    normalized_id = str(candidate_id or "").strip()
    if normalized_id not in _fold_relationship_touch_candidates(store):
        return {"ok": False, "error": "relationship_touch_not_found", "message": "没有找到这条关系经营候选。"}
    normalized_status = str(status or "").strip()
    if normalized_status not in _RELATIONSHIP_TOUCH_STATUSES:
        return {"ok": False, "error": "invalid_relationship_touch_status", "message": "关系经营状态不合法。"}
    row = {
        "record_type": "relationship_touch_update",
        "update_id": _new_id("relationship_touch_update"),
        "tenant_id": current_tenant_id(),
        "candidate_id": normalized_id,
        "status": normalized_status,
        "delivery_receipt": _strip_forbidden(delivery_receipt or {}),
        "failure_reason": _limit_text(failure_reason, 500),
        "source_text": _limit_text(source_text),
        "source": _autonomous_source(identity, operation_id, source_message_id),
        "created_at": now_iso(),
        "auto_effects": {
            "sends_parent_messages": False,
            "sends_teacher_messages": False,
            "sends_manager_messages": False,
            "changes_salary": False,
            "reports_private_chat_to_boss": False,
            "forces_next_action": False,
            "changes_router": False,
        },
    }
    _append_jsonl(store, RELATIONSHIP_TOUCH_CANDIDATES_FILE, row)
    folded = _fold_relationship_touch_candidates(store).get(normalized_id, {})
    return {
        "ok": True,
        "candidate": folded,
        "writeback_verified": folded.get("status") == normalized_status,
        "state_changed": True,
        "rendered_text": "已更新关系经营候选状态；它不会扩大现实外发权限。",
    }


def _fold_relationship_touch_candidates(store: TuoguanStore) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    updates: dict[str, list[dict[str, Any]]] = {}
    for row in _read_jsonl(store, RELATIONSHIP_TOUCH_CANDIDATES_FILE):
        candidate_id = str(row.get("candidate_id") or "")
        if not candidate_id:
            continue
        if str(row.get("record_type") or "") == "relationship_touch_update":
            updates.setdefault(candidate_id, []).append(deepcopy(row))
            continue
        item = deepcopy(row)
        item.setdefault("record_type", "relationship_touch_candidate")
        item.setdefault("status", "candidate")
        item.setdefault("updated_at", item.get("created_at") or "")
        item.setdefault("updates", [])
        result[candidate_id] = item
    for candidate_id, rows in updates.items():
        if candidate_id not in result:
            continue
        rows.sort(key=lambda item: str(item.get("created_at") or ""))
        latest = rows[-1]
        result[candidate_id]["updates"] = rows
        result[candidate_id]["status"] = str(latest.get("status") or result[candidate_id].get("status") or "")
        result[candidate_id]["updated_at"] = str(latest.get("created_at") or result[candidate_id].get("updated_at") or "")
        if latest.get("delivery_receipt"):
            result[candidate_id]["delivery_receipt"] = latest.get("delivery_receipt")
        if latest.get("failure_reason"):
            result[candidate_id]["failure_reason"] = latest.get("failure_reason")
    return result


def _identity_can_view_relationship_touch(identity: UserIdentity, row: dict[str, Any]) -> bool:
    if identity.role == "boss":
        return True
    role = str(row.get("target_role") or "")
    target_user = str(row.get("target_user_id") or "")
    current_user = str(identity.canonical_user_id or "")
    if identity.role == "manager":
        return role in {"manager", "teacher"} and (not target_user or target_user == current_user or role == "teacher")
    if identity.role == "teacher":
        return role == "teacher" and (not target_user or target_user == current_user)
    return False


def _safe_relationship_message(message: Any, *, role: str, work_related: bool) -> str:
    text = _limit_text(message, 700)
    if len(text) < 8:
        return ""
    forbidden = (
        "家长已发送", "已通知家长", "发给家长", "批量派", "扣工资", "改工资",
        "绩效结论", "删除", "修改权限", "责任绑定", "必须回复", "马上回复",
        "不回复就", "监控", "老板让我盯着你", "汇报给老板",
    )
    if any(term in text for term in forbidden):
        return ""
    private_terms = ("家里", "婚姻", "收入", "隐私", "私生活", "身体隐私")
    if role == "teacher" and not work_related and any(term in text for term in private_terms):
        return ""
    return text


def _identity_can_view_autonomous_item(identity: UserIdentity, row: dict[str, Any]) -> bool:
    if identity.role in {"boss", "manager"}:
        return True
    current_user = str(identity.canonical_user_id or "")
    if not current_user:
        return False
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    if str(row.get("actor_user_id") or "") == current_user:
        return True
    if str(source.get("actor_user_id") or "") == current_user:
        return True
    related_staff = row.get("related_staff_user_ids") or []
    if isinstance(related_staff, list) and current_user in {str(item) for item in related_staff}:
        return True
    waiting = row.get("current_waiting") if isinstance(row.get("current_waiting"), dict) else {}
    if str(waiting.get("target_user_id") or "") == current_user:
        return True
    if identity.person_name and str(waiting.get("target_person") or "") == str(identity.person_name):
        return True
    return False


def _fold_hermes_work_items(store: TuoguanStore) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    updates: dict[str, list[dict[str, Any]]] = {}
    verified_human_contact_times = {
        str(row.get("occurred_at") or "")
        for row in _read_jsonl(store, BUSINESS_EVENTS_FILE)
        if str(row.get("event_type") or "") == "owner_inbound_message"
        and str((row.get("source") or {}).get("actor_user_id") or "")
        not in {"autonomous_employee_loop", "autonomous_wakeup_runner"}
    }
    for row in _read_jsonl(store, HERMES_WORK_ITEMS_FILE):
        record_type = str(row.get("record_type") or "")
        work_item_id = str(row.get("work_item_id") or "")
        if not work_item_id:
            continue
        if record_type == "work_item_update":
            updates.setdefault(work_item_id, []).append(deepcopy(row))
            continue
        item = deepcopy(row)
        item.setdefault("status", "active")
        item.setdefault("updates", [])
        item.setdefault("updated_at", item.get("created_at") or "")
        result[work_item_id] = item
    for work_item_id, rows in updates.items():
        if work_item_id not in result:
            continue
        rows.sort(key=lambda item: str(item.get("created_at") or ""))
        result[work_item_id]["updates"] = rows
        mutable_keys = (
            "status",
            "focus_summary",
            "execution_plan",
            "current_phase",
            "next_actions",
            "progress_evidence",
            "confirmed_facts",
            "pending_judgements",
            "completed_actions",
            "current_waiting",
            "blocked_by",
            "ask_candidates",
            "last_human_contact_at",
            "next_contact_after",
            "owner_escalation_reason",
            "value_progress_note",
            "next_attention_at",
            "stop_reason",
            "closed_at",
        )
        # Updates are patches, not snapshots. Apply every patch in time order so
        # an omitted field keeps the most recently confirmed value instead of
        # falling back to the original, possibly obsolete work item.
        for update in rows:
            for key in mutable_keys:
                if key in update:
                    value = deepcopy(update.get(key))
                    if key == "last_human_contact_at":
                        update_actor = str((update.get("source") or {}).get("actor_user_id") or "")
                        if (
                            update_actor in {"autonomous_employee_loop", "autonomous_wakeup_runner"}
                            and str(value or "") not in verified_human_contact_times
                        ):
                            continue
                        previous_contact = str(result[work_item_id].get(key) or "")
                        if previous_contact and str(value or "") < previous_contact:
                            continue
                    if (
                        key in {"current_phase", "current_waiting"}
                        and isinstance(value, dict)
                        and value
                        and isinstance(result[work_item_id].get(key), dict)
                    ):
                        previous = deepcopy(result[work_item_id].get(key) or {})
                        same_phase = (
                            key != "current_phase"
                            or not value.get("phase_key")
                            or value.get("phase_key") == previous.get("phase_key")
                        )
                        if same_phase:
                            value = {**previous, **value}
                    result[work_item_id][key] = value
        latest = rows[-1]
        result[work_item_id]["updated_at"] = str(latest.get("created_at") or result[work_item_id].get("updated_at") or "")
        result[work_item_id]["latest_update_text"] = str(latest.get("update_text") or "")
    return result


def _compact_work_item_view(item: dict[str, Any], *, update_count: int = 0) -> dict[str, Any]:
    view = deepcopy(item)
    updates = view.get("updates")
    if isinstance(updates, list):
        view["updates"] = _strip_forbidden(deepcopy(updates[-10:]))
        if len(updates) > 10:
            view["updates_total_count"] = len(updates)
    else:
        view.pop("updates", None)
    plan = view.get("execution_plan")
    if isinstance(plan, list):
        compact_plan = []
        for phase in plan[:8]:
            if not isinstance(phase, dict):
                compact_plan.append(phase)
                continue
            names = phase.get("student_names")
            compact_phase = {
                key: deepcopy(value)
                for key, value in phase.items()
                if key not in {"student_names", "students", "missing_students", "no_recent_record_students"}
            }
            if isinstance(names, list):
                compact_phase["student_count"] = len(names)
            compact_plan.append(compact_phase)
        view["execution_plan"] = compact_plan
        view["execution_plan_phase_count"] = len(plan)
    for key in (
        "next_actions",
        "progress_evidence",
        "confirmed_facts",
        "pending_judgements",
        "completed_actions",
        "blocked_by",
        "ask_candidates",
    ):
        value = view.get(key)
        if isinstance(value, list) and len(value) > 20:
            view[key] = deepcopy(value[-20:])
            view[f"{key}_total_count"] = len(value)
    view["update_count"] = int(update_count)
    return view


def _find_open_work_item_by_focus(store: TuoguanStore, focus_key: str) -> dict[str, Any] | None:
    normalized_focus = str(focus_key or "").strip()
    if not normalized_focus:
        return None
    for item in _fold_hermes_work_items(store).values():
        if str(item.get("focus_key") or "") == normalized_focus and str(item.get("status") or "active") in _WORK_ITEM_OPEN_STATUSES:
            return item
    return None


def query_hermes_work_items(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    status: str = "",
    focus_key: str = "",
    include_closed: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    normalized_status = str(status or "").strip()
    normalized_focus = str(focus_key or "").strip()
    items = []
    for item in _fold_hermes_work_items(store).values():
        item_status = str(item.get("status") or "active")
        if normalized_status and item_status != normalized_status:
            continue
        if normalized_focus and str(item.get("focus_key") or "") != normalized_focus:
            continue
        if not include_closed and item_status not in _WORK_ITEM_OPEN_STATUSES:
            continue
        if not _identity_can_view_autonomous_item(identity, item):
            continue
        updates = item.get("updates") if isinstance(item.get("updates"), list) else []
        view = _compact_work_item_view(item, update_count=len(updates))
        items.append(view)
    items.sort(key=lambda row: str(row.get("next_attention_at") or row.get("updated_at") or row.get("created_at") or ""))
    max_items = max(1, min(int(limit or 50), 200))
    items = items[-max_items:]
    waiting = [item for item in items if str(item.get("status") or "") == "waiting"]
    return {
        "ok": True,
        "work_item_count": len(items),
        "waiting_count": len(waiting),
        "items": items,
        "rendered_text": f"查到 {len(items)} 条 Hermes 自主工作事项，其中等待中 {len(waiting)} 条。它们只是状态材料，不规定 Hermes 下一步必须做什么。",
        "render_verified": True,
    }


def submit_hermes_work_item(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    focus_key: str,
    title: str,
    focus_summary: str,
    related_objects: list[Any] | None = None,
    related_staff_user_ids: list[str] | None = None,
    execution_plan: list[Any] | None = None,
    current_phase: dict[str, Any] | None = None,
    next_actions: list[Any] | None = None,
    progress_evidence: list[Any] | None = None,
    confirmed_facts: list[Any] | None = None,
    pending_judgements: list[Any] | None = None,
    completed_actions: list[Any] | None = None,
    current_waiting: dict[str, Any] | None = None,
    blocked_by: list[Any] | None = None,
    ask_candidates: list[Any] | None = None,
    last_human_contact_at: str = "",
    next_contact_after: str = "",
    owner_escalation_reason: str = "",
    value_progress_note: str = "",
    next_attention_at: str = "",
    status: str = "active",
    source_text: str = "",
    source_message_id: str = "",
    operation_id: str = "",
) -> dict[str, Any]:
    normalized_status = str(status or "active").strip()
    if normalized_status not in _WORK_ITEM_STATUSES:
        return {"ok": False, "error": "invalid_work_item_status", "message": "工作事项状态必须是 active、waiting、blocked、closed 或 superseded。"}
    normalized_focus = str(focus_key or "").strip()
    if not normalized_focus or not str(title or "").strip() or not str(focus_summary or "").strip():
        return {"ok": False, "error": "work_item_requires_focus", "message": "工作事项必须包含 focus_key、title 和 focus_summary。"}
    existing = _find_open_work_item_by_focus(store, normalized_focus)
    if existing:
        return update_hermes_work_item(
            store,
            identity=identity,
            work_item_id=str(existing.get("work_item_id") or ""),
            focus_key=normalized_focus,
            status=normalized_status,
            focus_summary=focus_summary,
            execution_plan=execution_plan,
            current_phase=current_phase,
            next_actions=next_actions,
            progress_evidence=progress_evidence,
            confirmed_facts=confirmed_facts,
            pending_judgements=pending_judgements,
            completed_actions=completed_actions,
            current_waiting=current_waiting,
            blocked_by=blocked_by,
            ask_candidates=ask_candidates,
            last_human_contact_at=last_human_contact_at,
            next_contact_after=next_contact_after,
            owner_escalation_reason=owner_escalation_reason,
            value_progress_note=value_progress_note,
            next_attention_at=next_attention_at,
            update_text=f"同一焦点合并更新：{title}",
            source_text=source_text,
            source_message_id=source_message_id,
            operation_id=operation_id,
        )
    now = now_iso()
    row = {
        "record_type": "work_item",
        "work_item_id": _new_id("hermes_work"),
        "tenant_id": current_tenant_id(),
        "focus_key": normalized_focus,
        "title": _limit_text(title, 200),
        "focus_summary": _limit_text(focus_summary),
        "related_objects": _strip_forbidden(related_objects or []),
        "related_staff_user_ids": [str(item) for item in (related_staff_user_ids or []) if str(item or "").strip()],
        "execution_plan": _strip_forbidden(execution_plan or []),
        "current_phase": _strip_forbidden(current_phase or {}),
        "next_actions": _strip_forbidden(next_actions or []),
        "progress_evidence": _strip_forbidden(progress_evidence or []),
        "confirmed_facts": _strip_forbidden(confirmed_facts or []),
        "pending_judgements": _strip_forbidden(pending_judgements or []),
        "completed_actions": _strip_forbidden(completed_actions or []),
        "current_waiting": _strip_forbidden(current_waiting or {}),
        "blocked_by": _strip_forbidden(blocked_by or []),
        "ask_candidates": _strip_forbidden(ask_candidates or []),
        "last_human_contact_at": str(last_human_contact_at or "").strip(),
        "next_contact_after": str(next_contact_after or "").strip(),
        "owner_escalation_reason": _limit_text(owner_escalation_reason, 500),
        "value_progress_note": _limit_text(value_progress_note, 500),
        "next_attention_at": str(next_attention_at or "").strip(),
        "status": normalized_status,
        "stop_reason": "",
        "source_text": _limit_text(source_text),
        "source": _autonomous_source(identity, operation_id, source_message_id),
        "created_at": now,
        "updated_at": now,
        "auto_effects": {
            "sends_parent_messages": False,
            "sends_teacher_messages": False,
            "creates_teacher_tasks": False,
            "changes_salary": False,
            "changes_permissions": False,
            "changes_router": False,
            "forces_next_action": False,
        },
    }
    _append_jsonl(store, HERMES_WORK_ITEMS_FILE, row)
    return {
        "ok": True,
        "work_item": _compact_work_item_view(row),
        "writeback_verified": True,
        "rendered_text": "已保存 Hermes 自主工作事项；这是内部状态材料，不会自动发人、派任务、改工资或规定模型下一步。",
    }


def update_hermes_work_item(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    operation_id: str,
    work_item_id: str = "",
    focus_key: str = "",
    status: str = "",
    focus_summary: str = "",
    execution_plan: list[Any] | None = None,
    current_phase: dict[str, Any] | None = None,
    next_actions: list[Any] | None = None,
    progress_evidence: list[Any] | None = None,
    confirmed_facts: list[Any] | None = None,
    pending_judgements: list[Any] | None = None,
    completed_actions: list[Any] | None = None,
    current_waiting: dict[str, Any] | None = None,
    blocked_by: list[Any] | None = None,
    ask_candidates: list[Any] | None = None,
    last_human_contact_at: str = "",
    next_contact_after: str = "",
    owner_escalation_reason: str = "",
    value_progress_note: str = "",
    next_attention_at: str = "",
    stop_reason: str = "",
    update_text: str = "",
    source_text: str = "",
    source_message_id: str = "",
) -> dict[str, Any]:
    items = _fold_hermes_work_items(store)
    item = items.get(str(work_item_id or ""))
    if not item and focus_key:
        item = _find_open_work_item_by_focus(store, str(focus_key or ""))
    if not item:
        return {"ok": False, "error": "work_item_not_found", "message": "没有找到要更新的 Hermes 工作事项。"}
    if not _identity_can_view_autonomous_item(identity, item):
        return {"ok": False, "error": "permission_denied", "message": "当前账号无权更新这个 Hermes 工作事项。"}
    normalized_status = str(status or item.get("status") or "active").strip()
    normalized_waiting = _strip_forbidden(current_waiting) if current_waiting is not None else None
    normalized_blocked_by = _strip_forbidden(blocked_by) if blocked_by is not None else item.get("blocked_by")
    if (
        normalized_status == "waiting"
        and isinstance(normalized_waiting, dict)
        and normalized_waiting.get("not_a_goal_blocker") is True
        and not normalized_blocked_by
        and str(item.get("status") or "") == "active"
    ):
        normalized_status = "active"
    if normalized_status not in _WORK_ITEM_STATUSES:
        return {"ok": False, "error": "invalid_work_item_status", "message": "工作事项状态必须是 active、waiting、blocked、closed 或 superseded。"}
    row: dict[str, Any] = {
        "record_type": "work_item_update",
        "update_id": _new_id("hermes_work_update"),
        "work_item_id": str(item.get("work_item_id") or ""),
        "tenant_id": current_tenant_id(),
        "focus_key": str(item.get("focus_key") or focus_key or ""),
        "status": normalized_status,
        "update_text": _limit_text(update_text or source_text or "Hermes 更新了工作事项状态。"),
        "source_text": _limit_text(source_text),
        "source": _autonomous_source(identity, operation_id, source_message_id),
        "created_at": now_iso(),
        "auto_effects": {
            "sends_parent_messages": False,
            "sends_teacher_messages": False,
            "creates_teacher_tasks": False,
            "changes_salary": False,
            "changes_permissions": False,
            "changes_router": False,
            "forces_next_action": False,
        },
    }
    if focus_summary:
        row["focus_summary"] = _limit_text(focus_summary)
    if execution_plan is not None:
        row["execution_plan"] = _strip_forbidden(execution_plan)
    if current_phase:
        row["current_phase"] = _strip_forbidden(current_phase)
    if next_actions is not None:
        row["next_actions"] = _strip_forbidden(next_actions)
    if progress_evidence is not None:
        row["progress_evidence"] = _strip_forbidden(progress_evidence)
    if confirmed_facts is not None:
        row["confirmed_facts"] = _strip_forbidden(confirmed_facts)
    if pending_judgements is not None:
        row["pending_judgements"] = _strip_forbidden(pending_judgements)
    if completed_actions is not None:
        row["completed_actions"] = _strip_forbidden(completed_actions)
    if current_waiting is not None:
        row["current_waiting"] = normalized_waiting
    if blocked_by is not None:
        row["blocked_by"] = _strip_forbidden(blocked_by)
    if ask_candidates is not None:
        row["ask_candidates"] = _strip_forbidden(ask_candidates)
    if last_human_contact_at:
        normalized_contact_at = str(last_human_contact_at or "").strip()
        existing_contact_at = str(item.get("last_human_contact_at") or "").strip()
        if not existing_contact_at or normalized_contact_at >= existing_contact_at:
            row["last_human_contact_at"] = normalized_contact_at
    if next_contact_after:
        row["next_contact_after"] = str(next_contact_after or "").strip()
    if owner_escalation_reason:
        row["owner_escalation_reason"] = _limit_text(owner_escalation_reason, 500)
    if value_progress_note:
        row["value_progress_note"] = _limit_text(value_progress_note, 500)
    if next_attention_at:
        row["next_attention_at"] = str(next_attention_at or "").strip()
    if stop_reason:
        row["stop_reason"] = _limit_text(stop_reason, 500)
    material_keys = (
        "status",
        "current_phase",
        "progress_evidence",
        "completed_actions",
        "current_waiting",
        "blocked_by",
        "last_human_contact_at",
        "next_contact_after",
        "next_attention_at",
        "stop_reason",
    )
    def material_value(source: dict[str, Any], key: str) -> Any:
        value = source.get(key)
        if key == "current_phase" and isinstance(value, dict):
            return value.get("phase_key") or value.get("phase_name") or value
        return value

    if all(
        material_value(item, key) == material_value(row, key)
        for key in material_keys
        if key in row
    ):
        return {
            "ok": True,
            "work_item": _compact_work_item_view(
                item,
                update_count=len(item.get("updates") or []) if isinstance(item.get("updates"), list) else 0,
            ),
            "writeback_verified": True,
            "state_changed": False,
            "idempotent_replay": True,
            "rendered_text": "工作事项没有阶段、卡点、等待、证据或关注时间变化，本轮未重复追加。",
        }
    if normalized_status in {"closed", "superseded"}:
        row["closed_at"] = row["created_at"]
    _append_jsonl(store, HERMES_WORK_ITEMS_FILE, row)
    folded = _fold_hermes_work_items(store).get(str(item.get("work_item_id") or ""), {})
    return {
        "ok": True,
        "work_item": _compact_work_item_view(
            folded,
            update_count=len(folded.get("updates") or []) if isinstance(folded.get("updates"), list) else 0,
        ),
        "update": _compact_work_item_view(row),
        "writeback_verified": True,
        "state_changed": True,
        "rendered_text": "已更新 Hermes 自主工作事项；更新只保存状态和证据，不自动执行外部动作。",
    }


def _fold_wakeup_requests(store: TuoguanStore) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    updates: dict[str, list[dict[str, Any]]] = {}
    for row in _read_jsonl(store, WAKEUP_REQUESTS_FILE):
        record_type = str(row.get("record_type") or "")
        request_id = str(row.get("wakeup_request_id") or "")
        if not request_id:
            continue
        if record_type == "wakeup_request_update":
            updates.setdefault(request_id, []).append(deepcopy(row))
            continue
        item = deepcopy(row)
        item.setdefault("record_type", "wakeup_request")
        item.setdefault("status", "pending")
        item.setdefault("updates", [])
        item.setdefault("updated_at", item.get("created_at") or "")
        result[request_id] = item
    for request_id, rows in updates.items():
        if request_id not in result:
            continue
        rows.sort(key=lambda item: str(item.get("created_at") or ""))
        result[request_id]["updates"] = rows
        latest = rows[-1]
        result[request_id]["status"] = str(latest.get("status") or result[request_id].get("status") or "")
        result[request_id]["updated_at"] = str(latest.get("created_at") or result[request_id].get("updated_at") or "")
        result[request_id]["latest_update_text"] = str(latest.get("update_text") or "")
        result[request_id]["handled_at"] = str(latest.get("handled_at") or result[request_id].get("handled_at") or "")
    return result


def query_wakeup_requests(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    status: str = "",
    wakeup_source: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    normalized_status = str(status or "").strip()
    normalized_source = str(wakeup_source or "").strip()
    rows = []
    for row in _fold_wakeup_requests(store).values():
        if normalized_status and str(row.get("status") or "") != normalized_status:
            continue
        if normalized_source and str(row.get("wakeup_source") or "") != normalized_source:
            continue
        if identity.role not in {"boss", "manager"} and str((row.get("source") or {}).get("actor_user_id") or "") != str(identity.canonical_user_id or ""):
            continue
        rows.append(deepcopy(row))
    rows.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""))
    rows = rows[-max(1, min(int(limit or 50), 200)):]
    pending = [row for row in rows if str(row.get("status") or "") == "pending"]
    return {
        "ok": True,
        "request_count": len(rows),
        "pending_count": len(pending),
        "requests": rows,
        "rendered_text": f"查到 {len(rows)} 条唤醒请求，其中待处理 {len(pending)} 条。唤醒只说明 Hermes 为什么重新看这件事，不预设动作。",
        "render_verified": True,
    }


def submit_wakeup_request(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    wakeup_source: str,
    reason: str,
    operation_id: str,
    related_work_item_id: str = "",
    related_objects: list[Any] | None = None,
    scheduled_for: str = "",
    status: str = "pending",
    source_text: str = "",
    source_message_id: str = "",
) -> dict[str, Any]:
    normalized_status = str(status or "pending").strip()
    if normalized_status not in _WAKEUP_STATUSES:
        return {"ok": False, "error": "invalid_wakeup_status", "message": "唤醒请求状态必须是 pending、handled、ignored 或 superseded。"}
    if not str(wakeup_source or "").strip() or not str(reason or "").strip():
        return {"ok": False, "error": "wakeup_request_requires_reason", "message": "唤醒请求必须包含来源和原因。"}
    row = {
        "wakeup_request_id": _new_id("wakeup"),
        "tenant_id": current_tenant_id(),
        "wakeup_source": str(wakeup_source or "").strip(),
        "reason": _limit_text(reason),
        "related_work_item_id": str(related_work_item_id or "").strip(),
        "related_objects": _strip_forbidden(related_objects or []),
        "scheduled_for": str(scheduled_for or "").strip(),
        "status": normalized_status,
        "source_text": _limit_text(source_text),
        "source": _autonomous_source(identity, operation_id, source_message_id),
        "created_at": now_iso(),
        "auto_effects": {"executes_business_action": False, "forces_next_action": False, "changes_router": False},
    }
    _append_jsonl(store, WAKEUP_REQUESTS_FILE, row)
    return {"ok": True, "wakeup_request": row, "writeback_verified": True, "rendered_text": "已保存唤醒请求；它只会让 Hermes 重新查看事实，不直接执行业务动作。"}



def update_wakeup_request(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    wakeup_request_id: str,
    status: str,
    update_text: str,
    operation_id: str,
    source_text: str = "",
    source_message_id: str = "",
) -> dict[str, Any]:
    normalized_id = str(wakeup_request_id or "").strip()
    normalized_status = str(status or "").strip()
    if normalized_status not in _WAKEUP_STATUSES:
        return {"ok": False, "error": "invalid_wakeup_status", "message": "唤醒请求状态必须是 pending、handled、ignored 或 superseded。"}
    if not normalized_id or not str(update_text or "").strip():
        return {"ok": False, "error": "wakeup_update_requires_content", "message": "唤醒请求更新必须包含 request id 和说明。"}
    folded = _fold_wakeup_requests(store)
    current = folded.get(normalized_id)
    if not current:
        return {"ok": False, "error": "wakeup_request_not_found", "message": "没有找到要更新的唤醒请求。"}
    if identity.role not in {"boss", "manager"} and str((current.get("source") or {}).get("actor_user_id") or "") != str(identity.canonical_user_id or ""):
        return {"ok": False, "error": "permission_denied", "message": "当前账号无权更新这个唤醒请求。"}
    row = {
        "record_type": "wakeup_request_update",
        "update_id": _new_id("wakeup_update"),
        "wakeup_request_id": normalized_id,
        "tenant_id": current_tenant_id(),
        "status": normalized_status,
        "update_text": _limit_text(update_text),
        "source_text": _limit_text(source_text),
        "source": _autonomous_source(identity, operation_id, source_message_id),
        "created_at": now_iso(),
        "auto_effects": {
            "executes_business_action": False,
            "sends_parent_messages": False,
            "sends_teacher_messages": False,
            "creates_tasks": False,
            "changes_salary": False,
            "closes_work_items": False,
            "retries_action": False,
            "forces_next_action": False,
            "changes_router": False,
        },
    }
    if normalized_status in {"handled", "ignored", "superseded"}:
        row["handled_at"] = row["created_at"]
    _append_jsonl(store, WAKEUP_REQUESTS_FILE, row)
    folded_after = _fold_wakeup_requests(store).get(normalized_id, {})
    return {
        "ok": True,
        "wakeup_request": folded_after,
        "update": row,
        "writeback_verified": str(folded_after.get("status") or "") == normalized_status,
        "rendered_text": "已更新内部唤醒请求状态；这只表示 Hermes 已重新看过或决定暂不处理，不会发消息、派任务、关闭工作事项或规定下一步。",
    }


_BUSINESS_EVENT_TYPE_ALIASES = {
    "data_inconsistency_found": "data_inconsistency",
    "safety_data_inconsistency": "data_inconsistency",
    "risk_detection": "data_inconsistency",
    "record_coverage_gap": "record_coverage_low",
    "weekly_record_coverage_low": "record_coverage_low",
    "data_quality": "record_coverage_low",
}


def query_business_events(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    event_type: str = "",
    related_object: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    normalized_type = str(event_type or "").strip()
    normalized_type = _BUSINESS_EVENT_TYPE_ALIASES.get(normalized_type, normalized_type)
    normalized_related = str(related_object or "").strip()
    rows = []
    for row in _read_jsonl(store, BUSINESS_EVENTS_FILE):
        row_type = str(row.get("event_type") or "")
        row_type = _BUSINESS_EVENT_TYPE_ALIASES.get(row_type, row_type)
        if normalized_type and row_type != normalized_type:
            continue
        if normalized_related and normalized_related not in json.dumps(row.get("related_objects") or [], ensure_ascii=False):
            continue
        if identity.role not in {"boss", "manager"} and str((row.get("source") or {}).get("actor_user_id") or "") != str(identity.canonical_user_id or ""):
            continue
        rows.append(deepcopy(row))
    rows.sort(key=lambda item: str(item.get("created_at") or ""))
    rows = rows[-max(1, min(int(limit or 50), 200)):]
    return {
        "ok": True,
        "event_count": len(rows),
        "events": rows,
        "rendered_text": f"查到 {len(rows)} 条业务事件。事件只记录现实发生了什么，不代表 Hermes 必须采取固定动作。",
        "render_verified": True,
    }


def submit_business_event(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    event_type: str,
    event_text: str,
    operation_id: str,
    related_objects: list[Any] | None = None,
    occurred_at: str = "",
    source_text: str = "",
    source_message_id: str = "",
) -> dict[str, Any]:
    if not str(event_type or "").strip() or not str(event_text or "").strip():
        return {"ok": False, "error": "business_event_requires_content", "message": "业务事件必须包含类型和内容。"}
    normalized_type = str(event_type or "").strip()
    normalized_type = _BUSINESS_EVENT_TYPE_ALIASES.get(normalized_type, normalized_type)
    normalized_text = _limit_text(event_text)
    normalized_related = _strip_forbidden(related_objects or [])
    normalized_source_message_id = str(source_message_id or "").strip()
    autonomous_source = str(identity.canonical_user_id or "") == "autonomous_employee_loop"
    semantic_text = re.sub(
        r"[\s，。；：、,.!！?？（）()\[\]【】]+",
        "",
        normalized_text,
    ).lower()
    numeric_facts = set(re.findall(r"\d+(?:\.\d+)?", normalized_text))
    ratio_facts = set(re.findall(r"\d+\s*/\s*\d+", normalized_text))
    for existing in reversed(_read_jsonl(store, BUSINESS_EVENTS_FILE)[-500:]):
        existing_source = existing.get("source") if isinstance(existing.get("source"), dict) else {}
        if (
            normalized_source_message_id
            and str(existing_source.get("source_message_id") or "") == normalized_source_message_id
            and str(existing.get("event_type") or "") == normalized_type
        ):
            return {"ok": True, "business_event": existing, "writeback_verified": True, "state_changed": False, "idempotent_replay": True}
        existing_type = _BUSINESS_EVENT_TYPE_ALIASES.get(
            str(existing.get("event_type") or ""),
            str(existing.get("event_type") or ""),
        )
        existing_semantic_text = re.sub(
            r"[\s，。；：、,.!！?？（）()\[\]【】]+",
            "",
            str(existing.get("event_text") or ""),
        ).lower()
        existing_numeric_facts = set(
            re.findall(r"\d+(?:\.\d+)?", str(existing.get("event_text") or ""))
        )
        existing_ratio_facts = set(
            re.findall(r"\d+\s*/\s*\d+", str(existing.get("event_text") or ""))
        )
        if (
            (normalized_type.startswith("autonomous_") or autonomous_source)
            and existing_type == normalized_type
            and (
                existing_semantic_text == semantic_text
                or (bool(numeric_facts) and existing_numeric_facts == numeric_facts)
                or (bool(ratio_facts) and bool(ratio_facts & existing_ratio_facts))
            )
            and existing.get("related_objects") == normalized_related
            and str(existing.get("created_at") or "")[:10] == now_iso()[:10]
        ):
            return {
                "ok": True,
                "business_event": existing,
                "writeback_verified": True,
                "state_changed": False,
                "idempotent_replay": True,
                "rendered_text": "相同自主观察今天已经记录，本轮未重复追加。",
            }
    row = {
        "business_event_id": _new_id("business_event"),
        "tenant_id": current_tenant_id(),
        "event_type": normalized_type,
        "event_text": normalized_text,
        "related_objects": normalized_related,
        "occurred_at": str(occurred_at or "").strip() or now_iso(),
        "source_text": _limit_text(source_text),
        "source": _autonomous_source(identity, operation_id, source_message_id),
        "created_at": now_iso(),
        "auto_effects": {"executes_business_action": False, "forces_next_action": False, "changes_router": False},
    }
    _append_jsonl(store, BUSINESS_EVENTS_FILE, row)
    return {"ok": True, "business_event": row, "writeback_verified": True, "state_changed": True, "rendered_text": "已保存业务事件；它只是现实变化材料，不会自动触发固定流程。"}


def _fold_attention_threads(store: TuoguanStore) -> dict[str, dict[str, Any]]:
    threads: dict[str, dict[str, Any]] = {}
    updates: dict[str, list[dict[str, Any]]] = {}
    for row in _read_jsonl(store, ATTENTION_THREADS_FILE):
        attention_id = str(row.get("attention_id") or "").strip()
        if not attention_id:
            continue
        if str(row.get("record_type") or "") == "attention_thread_update":
            updates.setdefault(attention_id, []).append(row)
            continue
        item = deepcopy(row)
        item.setdefault("status", "candidate")
        item.setdefault("updates", [])
        item.setdefault("updated_at", item.get("created_at") or "")
        threads[attention_id] = item
    for attention_id, rows in updates.items():
        if attention_id not in threads:
            continue
        rows.sort(key=lambda item: str(item.get("created_at") or ""))
        item = threads[attention_id]
        item["updates"] = deepcopy(rows)
        for row in rows:
            for key in (
                "status",
                "question_text",
                "needed_facts",
                "delivery_receipt",
                "owner_message_id",
                "owner_message_text",
                "reply_relevance",
                "reply_sufficiency",
                "model_judgment",
                "resolution_note",
                "failure_reason",
            ):
                if key in row:
                    item[key] = deepcopy(row.get(key))
            item["updated_at"] = str(row.get("created_at") or item.get("updated_at") or "")
            status = str(row.get("status") or "")
            if status == "queued":
                item["queued_at"] = str(row.get("created_at") or "")
            elif status == "sent":
                item["sent_at"] = str(row.get("created_at") or "")
            elif status == "replied":
                item["replied_at"] = str(row.get("created_at") or "")
            elif status == "resolved":
                item["resolved_at"] = str(row.get("created_at") or "")
            elif status == "failed":
                item["failed_at"] = str(row.get("created_at") or "")
            elif status == "superseded":
                item["superseded_at"] = str(row.get("created_at") or "")
    return threads


def query_attention_threads(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    status: str = "",
    focus_key: str = "",
    include_closed: bool = False,
    limit: int = 30,
) -> dict[str, Any]:
    if identity.role not in {"boss", "manager"}:
        return {"ok": False, "error": "permission_denied", "message": "只有老板或店长可以查看内部提醒线程。"}
    status_filter = str(status or "").strip()
    focus_filter = str(focus_key or "").strip()
    rows: list[dict[str, Any]] = []
    for item in _fold_attention_threads(store).values():
        item_status = str(item.get("status") or "")
        if status_filter and item_status != status_filter:
            continue
        if not include_closed and item_status not in _OPEN_ATTENTION_STATUSES:
            continue
        if focus_filter and str(item.get("focus_key") or "") != focus_filter:
            continue
        rows.append(deepcopy(item))
    rows.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""))
    rows = rows[-max(1, min(int(limit or 30), 100)):]
    return {
        "ok": True,
        "attention_count": len(rows),
        "attention_threads": rows,
        "rendered_text": f"查到 {len(rows)} 条提醒线程。它们只是未解决问题和消息事实，是否与本轮回复相关仍由 Hermes 判断。",
        "render_verified": True,
    }


def submit_attention_thread(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    focus_key: str,
    question_text: str,
    operation_id: str,
    target_user_id: str,
    needed_facts: list[Any] | None = None,
    related_goal_id: str = "",
    related_work_item_id: str = "",
    status: str = "candidate",
    attention_id: str = "",
    source_text: str = "",
    source_decision_summary: str = "",
    source_message_id: str = "",
) -> dict[str, Any]:
    normalized_status = str(status or "candidate").strip()
    if normalized_status not in {"candidate", "queued"}:
        return {"ok": False, "error": "invalid_initial_attention_status", "message": "新提醒只能从 candidate 或 queued 开始。"}
    if not str(focus_key or "").strip() or not str(question_text or "").strip():
        return {"ok": False, "error": "attention_requires_content", "message": "提醒线程必须包含焦点和问题内容。"}
    normalized_id = str(attention_id or "").strip() or _new_id("attention")
    existing = _fold_attention_threads(store).get(normalized_id)
    if existing:
        return {"ok": True, "attention_thread": existing, "writeback_verified": True, "idempotent_replay": True}
    row = {
        "record_type": "attention_thread",
        "attention_id": normalized_id,
        "tenant_id": current_tenant_id(),
        "focus_key": str(focus_key or "").strip(),
        "related_goal_id": str(related_goal_id or "").strip(),
        "related_work_item_id": str(related_work_item_id or "").strip(),
        "target_user_id": str(target_user_id or "").strip(),
        "question_text": _limit_text(question_text, 1200),
        "needed_facts": _strip_forbidden(needed_facts or []),
        "status": normalized_status,
        "source_text": _limit_text(source_text),
        "source_decision_summary": _limit_text(source_decision_summary, 1200),
        "source": _autonomous_source(identity, operation_id, source_message_id),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "auto_effects": {"decides_reply_relevance": False, "forces_next_action": False, "changes_router": False},
    }
    if normalized_status == "queued":
        row["queued_at"] = row["created_at"]
    _append_jsonl(store, ATTENTION_THREADS_FILE, row)
    folded = _fold_attention_threads(store).get(normalized_id, {})
    return {"ok": True, "attention_thread": folded, "writeback_verified": bool(folded)}


def update_attention_thread(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    attention_id: str,
    status: str,
    operation_id: str,
    owner_message_id: str = "",
    owner_message_text: str = "",
    reply_relevance: str = "",
    reply_sufficiency: str = "",
    model_judgment: str = "",
    resolution_note: str = "",
    failure_reason: str = "",
    delivery_receipt: dict[str, Any] | None = None,
    source_text: str = "",
    source_message_id: str = "",
) -> dict[str, Any]:
    if identity.role not in {"boss", "manager"}:
        return {"ok": False, "error": "permission_denied", "message": "只有老板或店长可以更新老板提醒线程。"}
    normalized_id = str(attention_id or "").strip()
    normalized_status = str(status or "").strip()
    if normalized_status not in _ATTENTION_STATUSES:
        return {"ok": False, "error": "invalid_attention_status", "message": "提醒状态无效。"}
    before = _fold_attention_threads(store).get(normalized_id)
    if not before:
        return {"ok": False, "error": "attention_not_found", "message": "没有找到对应提醒线程。"}
    if normalized_status == "sent" and not (delivery_receipt or before.get("delivery_receipt")):
        return {"ok": False, "error": "delivery_receipt_required", "message": "只有收到真实发送回执后才能标记为已发送。"}
    row: dict[str, Any] = {
        "record_type": "attention_thread_update",
        "attention_id": normalized_id,
        "status": normalized_status,
        "source": _autonomous_source(identity, operation_id, source_message_id),
        "source_text": _limit_text(source_text),
        "created_at": now_iso(),
        "auto_effects": {"decides_reply_relevance": False, "forces_next_action": False, "changes_router": False},
    }
    optional_values = {
        "owner_message_id": str(owner_message_id or "").strip(),
        "owner_message_text": _limit_text(owner_message_text, 1200),
        "reply_relevance": str(reply_relevance or "").strip(),
        "reply_sufficiency": str(reply_sufficiency or "").strip(),
        "model_judgment": _limit_text(model_judgment, 1200),
        "resolution_note": _limit_text(resolution_note, 1200),
        "failure_reason": _limit_text(failure_reason, 1200),
    }
    for key, value in optional_values.items():
        if value:
            row[key] = value
    if delivery_receipt:
        row["delivery_receipt"] = _strip_forbidden(delivery_receipt)
    _append_jsonl(store, ATTENTION_THREADS_FILE, row)
    after = _fold_attention_threads(store).get(normalized_id, {})
    return {
        "ok": True,
        "attention_thread": after,
        "update": row,
        "writeback_verified": str(after.get("status") or "") == normalized_status,
        "rendered_text": "已更新提醒线程状态；系统只保存 Hermes 的判断和真实消息证据，不替 Hermes 判断老板回复是否相关。",
    }


def query_action_executions(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    status: str = "",
    action_type: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    normalized_status = str(status or "").strip()
    normalized_type = str(action_type or "").strip()
    rows = []
    for row in _read_jsonl(store, ACTION_EXECUTIONS_FILE):
        if normalized_status and str(row.get("status") or "") != normalized_status:
            continue
        if normalized_type and str(row.get("action_type") or "") != normalized_type:
            continue
        if identity.role not in {"boss", "manager"} and str((row.get("source") or {}).get("actor_user_id") or "") != str(identity.canonical_user_id or ""):
            continue
        rows.append(deepcopy(row))
    rows.sort(key=lambda item: str(item.get("created_at") or ""))
    rows = rows[-max(1, min(int(limit or 50), 200)):]
    unknown = [row for row in rows if str(row.get("status") or "") == "result_unknown"]
    return {
        "ok": True,
        "execution_count": len(rows),
        "result_unknown_count": len(unknown),
        "executions": rows,
        "rendered_text": f"查到 {len(rows)} 条动作账本，其中结果未知 {len(unknown)} 条。结果未知时下一次应先核验最新事实，而不是盲目重试。",
        "render_verified": True,
    }


def submit_action_execution(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    action_type: str,
    action_summary: str,
    status: str,
    operation_id: str,
    related_work_item_id: str = "",
    idempotency_key: str = "",
    receipt: dict[str, Any] | None = None,
    result_text: str = "",
    source_text: str = "",
    source_message_id: str = "",
) -> dict[str, Any]:
    normalized_status = str(status or "").strip()
    if normalized_status not in _ACTION_EXECUTION_STATUSES:
        return {"ok": False, "error": "invalid_action_execution_status", "message": "动作状态必须是 not_started、success、failed、partial_success、result_unknown、permission_blocked 或 manual_takeover。"}
    if not str(action_type or "").strip() or not str(action_summary or "").strip():
        return {"ok": False, "error": "action_execution_requires_content", "message": "动作账本必须包含动作类型和摘要。"}
    row = {
        "action_execution_id": _new_id("action_exec"),
        "tenant_id": current_tenant_id(),
        "action_type": str(action_type or "").strip(),
        "action_summary": _limit_text(action_summary),
        "status": normalized_status,
        "related_work_item_id": str(related_work_item_id or "").strip(),
        "idempotency_key": str(idempotency_key or operation_id or "").strip(),
        "receipt": _strip_forbidden(receipt or {}),
        "result_text": _limit_text(result_text),
        "source_text": _limit_text(source_text),
        "source": _autonomous_source(identity, operation_id, source_message_id),
        "created_at": now_iso(),
        "auto_effects": {"retries_action": False, "forces_next_action": False, "changes_router": False},
    }
    _append_jsonl(store, ACTION_EXECUTIONS_FILE, row)
    return {"ok": True, "action_execution": row, "writeback_verified": True, "rendered_text": "已保存动作执行账本；它保存结果证据，不会自动重试或扩大现实动作权限。"}


def query_autonomous_work_brief(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    limit: int = 20,
) -> dict[str, Any]:
    work = query_hermes_work_items(store, identity=identity, include_closed=False, limit=limit)
    wakeups = query_wakeup_requests(store, identity=identity, status="pending", limit=limit)
    events = query_business_events(store, identity=identity, limit=limit)
    executions = query_action_executions(store, identity=identity, limit=limit)
    institution = query_institution_understanding(store, identity=identity) if identity.role in {"boss", "manager"} else {}
    scorecard = query_hermes_employee_scorecard(store, identity=identity, limit=5) if identity.role in {"boss", "manager"} else {}
    industry = query_industry_learning_candidates(store, identity=identity, limit=5) if identity.role in {"boss", "manager"} else {}
    external_learning = query_external_learning_brief(store, identity=identity, limit=5) if identity.role in {"boss", "manager"} else {}
    value_progress = query_value_progress_ledger(store, identity=identity, limit=5) if identity.role in {"boss", "manager"} else {}
    multi_agent = query_multi_agent_brief(store, identity=identity, limit=5) if identity.role in {"boss", "manager"} else {}
    waiting = [item for item in work.get("items", []) if str(item.get("status") or "") == "waiting"]
    institution_gap_count = int(((institution.get("audit") or {}).get("gap_count") or 0)) if isinstance(institution, dict) else 0
    rendered = (
        f"自主工作摘要：活跃/等待事项 {work.get('work_item_count', 0)} 条，"
        f"等待 {len(waiting)} 条，待处理唤醒 {wakeups.get('pending_count', 0)} 条，"
        f"结果未知动作 {executions.get('result_unknown_count', 0)} 条，"
        f"机构认知缺口 {institution_gap_count} 类，行业学习候选 {industry.get('candidate_count', 0) if isinstance(industry, dict) else 0} 条，"
        f"外部学习运行 {((external_learning.get('external_research_runs') or {}).get('run_count', 0) if isinstance(external_learning, dict) else 0)} 次，"
        f"多 Agent 影子结果 {multi_agent.get('result_count', 0) if isinstance(multi_agent, dict) else 0} 条。"
        "这些只是事实材料和状态材料，不规定 Hermes 下一步必须做什么。"
    )
    return {
        "ok": True,
        "work_item_count": work.get("work_item_count", 0),
        "waiting_count": len(waiting),
        "pending_wakeup_count": wakeups.get("pending_count", 0),
        "recent_business_event_count": events.get("event_count", 0),
        "recent_action_execution_count": executions.get("execution_count", 0),
        "result_unknown_action_count": executions.get("result_unknown_count", 0),
        "waiting_items": waiting[:10],
        "pending_wakeups": wakeups.get("requests", [])[:10],
        "recent_events": events.get("events", [])[-10:],
        "recent_executions": executions.get("executions", [])[-10:],
        "institution_understanding": institution,
        "employee_scorecard": scorecard,
        "industry_learning_candidates": industry,
        "external_learning_brief": external_learning,
        "value_progress_ledger": value_progress,
        "multi_agent_brief": multi_agent,
        "rendered_text": rendered,
        "render_verified": True,
    }




def submit_due_wakeup_candidate(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    candidate_id: str,
    operation_id: str,
    now_at: str = "",
    source_text: str = "",
    source_message_id: str = "",
) -> dict[str, Any]:
    """Persist one read-only due candidate as an internal wakeup request.

    This is still only an internal reminder. It does not send messages, assign
    tasks, retry actions, close work items, or decide what Hermes must do next.
    """

    normalized_id = str(candidate_id or "").strip()
    if not normalized_id:
        return {"ok": False, "error": "candidate_id_required", "message": "必须提供要落账的到期唤醒候选 id。"}
    candidates = generate_due_wakeup_candidates(store, identity=identity, now_at=now_at, limit=200)
    if not candidates.get("ok"):
        return candidates
    matched = None
    for item in candidates.get("candidates", []):
        if str(item.get("candidate_id") or "") == normalized_id:
            matched = item
            break
    if not matched:
        return {"ok": False, "error": "due_wakeup_candidate_not_found", "message": "没有找到这个到期唤醒候选；请先重新读取候选材料。"}

    work_item_id = str(matched.get("work_item_id") or "")
    if work_item_id:
        existing = query_wakeup_requests(store, identity=identity, status="pending", limit=200)
        for row in existing.get("requests", []) if isinstance(existing.get("requests"), list) else []:
            if str(row.get("related_work_item_id") or "") == work_item_id:
                return {
                    "ok": True,
                    "already_exists": True,
                    "wakeup_request": row,
                    "writeback_verified": True,
                    "rendered_text": "该工作事项已经有待处理唤醒请求，本次没有重复创建。它仍只是内部提醒，不会自动执行外部动作。",
                }

    candidate_type = str(matched.get("candidate_type") or "")
    if candidate_type == "due_work_item_attention":
        reason = (
            f"工作事项到关注时间：{matched.get('title') or matched.get('focus_key') or '未命名事项'}。"
            "恢复前先核验是否已有新事实，再由 Hermes 自主判断继续、等待、追问、写入状态或停止。"
        )
        related_objects = [{
            "candidate_id": normalized_id,
            "candidate_type": candidate_type,
            "focus_key": str(matched.get("focus_key") or ""),
            "work_item_id": work_item_id,
            "due_at": str(matched.get("due_at") or ""),
        }]
        related_work_item_id = work_item_id
    elif candidate_type == "result_unknown_recheck":
        reason = (
            f"结果未知动作需要核验：{matched.get('action_summary') or matched.get('action_type') or '未命名动作'}。"
            "恢复前先核验回执、幂等键和最新业务事实，不得直接说成成功或盲目重试。"
        )
        related_objects = [{
            "candidate_id": normalized_id,
            "candidate_type": candidate_type,
            "action_execution_id": str(matched.get("action_execution_id") or ""),
            "idempotency_key": str(matched.get("idempotency_key") or ""),
        }]
        related_work_item_id = ""
    else:
        return {"ok": False, "error": "unsupported_due_wakeup_candidate_type", "message": "该候选类型暂不支持落账为唤醒请求。"}

    result = submit_wakeup_request(
        store,
        identity=identity,
        wakeup_source="due_wakeup_candidate",
        reason=reason,
        related_work_item_id=related_work_item_id,
        related_objects=related_objects,
        scheduled_for=str(matched.get("due_at") or now_at or ""),
        status="pending",
        source_text=source_text,
        source_message_id=source_message_id,
        operation_id=operation_id,
    )
    if not result.get("ok"):
        return result
    wakeup_id = str((result.get("wakeup_request") or {}).get("wakeup_request_id") or "")
    verified = False
    for row in _read_jsonl(store, WAKEUP_REQUESTS_FILE):
        if str(row.get("wakeup_request_id") or "") == wakeup_id:
            verified = True
            break
    result["writeback_verified"] = verified
    result["rendered_text"] = "已把到期候选保存为内部唤醒请求；这只是让 Hermes 之后重新查看事实，不发送消息、不派任务、不重试动作、不规定下一步。"
    return result


def generate_autonomous_recovery_report(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    focus_key: str = "",
    limit: int = 30,
) -> dict[str, Any]:
    """Build a read-only recovery brief for Hermes autonomous work.

    This report is only material for the model and the owner. It does not mark
    wakeups handled, close work items, retry actions, send messages, assign
    tasks, or change any business record.
    """

    max_items = max(1, min(int(limit or 30), 100))
    work = query_hermes_work_items(store, identity=identity, focus_key=focus_key, include_closed=False, limit=max_items)
    wakeups = query_wakeup_requests(store, identity=identity, status="pending", limit=max_items)
    executions = query_action_executions(store, identity=identity, limit=max_items)
    events = query_business_events(store, identity=identity, limit=max_items)

    items = work.get("items", []) if isinstance(work.get("items"), list) else []
    waiting_items = [item for item in items if str(item.get("status") or "") == "waiting"]
    active_items = [item for item in items if str(item.get("status") or "") == "active"]
    blocked_items = [item for item in items if str(item.get("status") or "") == "blocked"]
    unknown_actions = [
        item for item in (executions.get("executions", []) if isinstance(executions.get("executions"), list) else [])
        if str(item.get("status") or "") == "result_unknown"
    ]

    recovery_questions: list[str] = []
    if waiting_items:
        recovery_questions.append("哪些等待事项已经有新事实，哪些仍应继续等待？")
    if unknown_actions:
        recovery_questions.append("哪些结果未知动作需要先核验幂等键、回执或最新业务事实？")
    if blocked_items:
        recovery_questions.append("哪些阻塞事项需要老板、店长或老师补充授权/事实？")
    if not recovery_questions:
        recovery_questions.append("当前没有明显等待或结果未知卡点；如继续推进，也应先按需查真实业务事实。")

    sections = {
        "work_items": work,
        "pending_wakeups": wakeups,
        "action_executions": executions,
        "business_events": events,
    }
    counts = {
        "visible_work_item_count": len(items),
        "active_count": len(active_items),
        "waiting_count": len(waiting_items),
        "blocked_count": len(blocked_items),
        "pending_wakeup_count": int(wakeups.get("pending_count") or 0),
        "result_unknown_action_count": len(unknown_actions),
        "recent_business_event_count": int(events.get("event_count") or 0),
    }
    lines = [
        "# Hermes 自主工作恢复报告",
        "",
        "状态：只读内部恢复材料。未发送家长消息，未发送老师消息，未批量派任务，未修改工资，未删除数据，未自动关闭工作事项。",
        "",
        "## 摘要",
        "",
        f"- 可见工作事项：{counts['visible_work_item_count']} 条；活跃 {counts['active_count']} 条；等待 {counts['waiting_count']} 条；阻塞 {counts['blocked_count']} 条。",
        f"- 待处理唤醒：{counts['pending_wakeup_count']} 条；结果未知动作：{counts['result_unknown_action_count']} 条；近期业务事件：{counts['recent_business_event_count']} 条。",
        "",
        "## 恢复前应先判断",
        "",
    ]
    lines.extend(f"- {question}" for question in recovery_questions)
    lines.extend(["", "## 等待事项", ""])
    if waiting_items:
        for item in waiting_items[:10]:
            waiting = item.get("current_waiting") if isinstance(item.get("current_waiting"), dict) else {}
            title = str(item.get("title") or item.get("focus_key") or "未命名事项")
            target = str(waiting.get("target_person") or waiting.get("target_user_id") or "未指定")
            reason = str(waiting.get("reason") or item.get("focus_summary") or "")
            next_time = str(item.get("next_attention_at") or "")
            lines.append(f"- {title}：等待 {target}；原因：{reason[:160]}；下一关注：{next_time or '未设置'}。")
    else:
        lines.append("- 当前没有可见等待事项。")
    lines.extend(["", "## 结果未知动作", ""])
    if unknown_actions:
        for action in unknown_actions[:10]:
            lines.append(
                f"- {action.get('action_type') or 'action'}：{action.get('action_summary') or ''}"
                f"；幂等键：{action.get('idempotency_key') or '未记录'}。"
            )
    else:
        lines.append("- 当前没有可见结果未知动作。")
    lines.extend(["", "## 边界", ""])
    lines.extend([
        "- 这份报告只提供事实材料和恢复问题，不规定 Hermes 下一步必须做什么。",
        "- 恢复时应由模型重新判断：查事实、追问、等待、写入状态、停止或建议人工确认。",
        "- 任何真实写入仍必须通过权限、operation_id、ledger、audit、幂等和写后反查。",
    ])
    rendered = "\n".join(lines).rstrip() + "\n"
    return {
        "ok": True,
        "report_type": "autonomous_recovery_report_v1",
        "tenant_id": current_tenant_id(),
        "read_only": True,
        "actions_taken": [],
        "forbidden_actions_confirmed_absent": [
            "no_parent_messages_sent",
            "no_teacher_messages_sent",
            "no_tasks_created",
            "no_salary_or_payroll_changed",
            "no_data_deleted",
            "no_work_items_closed",
            "no_action_retried",
            "no_router_changed",
        ],
        "counts": counts,
        "recovery_questions": recovery_questions,
        "sections": sections,
        "rendered_text": rendered,
        "render_verified": True,
    }



def generate_due_wakeup_candidates(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    now_at: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """Return read-only candidates that may deserve a new Hermes look.

    This does not create wakeup_requests, mark requests handled, update work
    items, retry actions, send messages, or choose the model's next step.
    """

    now_value = _parse_time(now_at) or datetime.now().astimezone()
    if now_value.tzinfo is None:
        now_value = now_value.astimezone()
    max_items = max(1, min(int(limit or 50), 200))
    work = query_hermes_work_items(store, identity=identity, include_closed=False, limit=max_items)
    wakeups = query_wakeup_requests(store, identity=identity, status="pending", limit=max_items)
    executions = query_action_executions(store, identity=identity, status="result_unknown", limit=max_items)

    pending_by_work_id = {
        str(row.get("related_work_item_id") or "")
        for row in (wakeups.get("requests", []) if isinstance(wakeups.get("requests"), list) else [])
        if str(row.get("related_work_item_id") or "")
    }
    candidates: list[dict[str, Any]] = []
    for item in (work.get("items", []) if isinstance(work.get("items"), list) else []):
        status = str(item.get("status") or "")
        if status not in {"waiting", "blocked", "active"}:
            continue
        attention_at = str(item.get("next_attention_at") or "")
        due_at = _parse_time(attention_at)
        if due_at and due_at.tzinfo is None:
            due_at = due_at.astimezone()
        if not due_at or due_at > now_value:
            continue
        work_item_id = str(item.get("work_item_id") or "")
        waiting = item.get("current_waiting") if isinstance(item.get("current_waiting"), dict) else {}
        candidates.append({
            "candidate_id": f"due_attention:{work_item_id or item.get('focus_key')}",
            "candidate_type": "due_work_item_attention",
            "work_item_id": work_item_id,
            "focus_key": str(item.get("focus_key") or ""),
            "title": str(item.get("title") or item.get("focus_key") or "未命名事项"),
            "status": status,
            "due_at": attention_at,
            "waiting_target": str(waiting.get("target_person") or waiting.get("target_user_id") or ""),
            "reason": str(waiting.get("reason") or item.get("focus_summary") or ""),
            "existing_pending_wakeup_exists": work_item_id in pending_by_work_id,
            "suggested_recovery_question": "到关注时间后，应先核验是否已有新事实，再由 Hermes 判断继续、等待、追问、写入状态或停止。",
        })

    unknown_actions = executions.get("executions", []) if isinstance(executions.get("executions"), list) else []
    for action in unknown_actions[:10]:
        candidates.append({
            "candidate_id": f"result_unknown:{action.get('action_execution_id') or action.get('idempotency_key')}",
            "candidate_type": "result_unknown_recheck",
            "action_execution_id": str(action.get("action_execution_id") or ""),
            "action_type": str(action.get("action_type") or ""),
            "action_summary": str(action.get("action_summary") or ""),
            "idempotency_key": str(action.get("idempotency_key") or ""),
            "suggested_recovery_question": "结果未知不能说成成功；恢复时先核验回执、幂等键和最新业务事实。",
        })

    lines = [
        "# Hermes 到期唤醒候选",
        "",
        "状态：只读候选材料。未创建唤醒请求，未发送消息，未派任务，未修改业务数据，未自动重试动作。",
        "",
        f"- 当前时间：{now_value.isoformat(timespec='seconds')}",
        f"- 候选数量：{len(candidates)}",
        "",
        "## 候选",
        "",
    ]
    if candidates:
        for item in candidates[:20]:
            if item.get("candidate_type") == "due_work_item_attention":
                duplicate = "；已有待处理唤醒" if item.get("existing_pending_wakeup_exists") else ""
                lines.append(f"- {item.get('title')}：{item.get('due_at')} 到关注时间{duplicate}；建议先核验新事实。")
            else:
                lines.append(f"- 结果未知动作：{item.get('action_summary') or item.get('action_type')}；幂等键：{item.get('idempotency_key') or '未记录'}。")
    else:
        lines.append("- 当前没有到期等待事项或结果未知动作候选。")
    lines.extend(["", "## 边界", ""])
    lines.extend([
        "- 候选只提醒 Hermes 可以重新看，不规定下一步。",
        "- 是否创建唤醒请求、继续等待、追问或停止，仍由模型结合最新事实自主判断。",
        "- 任何内部状态写入仍必须走模型主链路、operation_id、ledger、audit、幂等和写后反查。",
    ])
    return {
        "ok": True,
        "report_type": "due_wakeup_candidates_v1",
        "tenant_id": current_tenant_id(),
        "read_only": True,
        "generated_at": now_value.isoformat(timespec="seconds"),
        "candidate_count": len(candidates),
        "candidates": candidates[:max_items],
        "source_counts": {
            "visible_work_item_count": int(work.get("work_item_count") or 0),
            "pending_wakeup_count": int(wakeups.get("pending_count") or 0),
            "result_unknown_action_count": int(executions.get("result_unknown_count") or 0),
        },
        "actions_taken": [],
        "forbidden_actions_confirmed_absent": [
            "no_wakeup_requests_created",
            "no_parent_messages_sent",
            "no_teacher_messages_sent",
            "no_tasks_created",
            "no_salary_or_payroll_changed",
            "no_data_deleted",
            "no_work_items_closed",
            "no_action_retried",
            "no_router_changed",
        ],
        "rendered_text": "\n".join(lines).rstrip() + "\n",
        "render_verified": True,
    }


_AUTONOMOUS_LOG_TOOL_NAMES = {
    "tuoguan_query_hermes_work_items",
    "tuoguan_query_wakeup_requests",
    "tuoguan_query_business_events",
    "tuoguan_query_action_executions",
    "tuoguan_query_autonomous_work_brief",
    "tuoguan_query_institution_understanding",
    "tuoguan_query_hermes_employee_scorecard",
    "tuoguan_query_industry_learning_candidates",
    "tuoguan_query_external_research_runs",
    "tuoguan_query_market_research_candidates",
    "tuoguan_query_competitor_profiles",
    "tuoguan_query_external_learning_brief",
    "tuoguan_query_value_progress_ledger",
    "tuoguan_generate_autonomous_recovery_report",
    "tuoguan_generate_due_wakeup_candidates",
    "tuoguan_generate_autonomous_acceptance_pack",
    "tuoguan_submit_hermes_work_item",
    "tuoguan_update_hermes_work_item",
    "tuoguan_submit_wakeup_request",
    "tuoguan_submit_due_wakeup_candidate",
    "tuoguan_update_wakeup_request",
    "tuoguan_submit_business_event",
    "tuoguan_submit_action_execution",
    "tuoguan_submit_institution_fact_gap",
    "tuoguan_update_institution_understanding",
    "tuoguan_submit_employee_self_review",
    "tuoguan_submit_industry_learning_candidate",
    "tuoguan_submit_value_progress_entry",
    "tuoguan_query_agent_delegations",
    "tuoguan_query_agent_delegation_results",
    "tuoguan_query_multi_agent_brief",
    "tuoguan_submit_agent_delegation",
    "tuoguan_submit_agent_delegation_result",
    "tuoguan_update_agent_delegation_decision",
}

_AUTONOMOUS_LOG_TERMS = (
    "自主", "唤醒", "等待", "恢复", "到期", "结果未知", "推进到哪一步", "还在等什么",
    "不要发", "不要派", "内部复盘", "续费", "目标", "老师没回", "明天再看",
)

_ISSUE_SIGNAL_TERMS = (
    "系统校验未通过", "校验未通过", "被拦截", "拦截", "权限", "写入失败",
    "工具失败", "失败", "error", "result_unknown", "内部机制", "查不到",
)


def _compact_text(value: Any, *, limit: int = 240) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _extract_tool_names(row: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for key in ("tool_calls", "tool_results", "selected_tools", "tools"):
        value = row.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    name = str(item.get("tool") or item.get("name") or item.get("tool_name") or "").strip()
                    if name:
                        names.append(name)
                elif isinstance(item, str):
                    names.append(item)
        elif isinstance(value, dict):
            for item in value.values():
                if isinstance(item, dict):
                    name = str(item.get("tool") or item.get("name") or item.get("tool_name") or "").strip()
                    if name:
                        names.append(name)
    return sorted(set(names))


def _row_has_tool_error(row: dict[str, Any]) -> bool:
    values = []
    for key in ("tool_results", "tool_result", "write_result", "result"):
        value = row.get(key)
        if isinstance(value, list):
            values.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            values.append(value)
    for item in values:
        if item.get("ok") is False or item.get("success") is False or item.get("error"):
            return True
        status = str(item.get("status") or "").lower()
        if status in {"failed", "error", "result_unknown", "blocked"}:
            return True
    return False


def _classify_log_review_candidate(text: str, tool_names: list[str] | None = None) -> str:
    lower = text.lower()
    tools = set(tool_names or [])
    if any(term in text for term in ("权限", "校验", "拦截", "不能写", "无权")):
        return "permission_boundary_candidate"
    if any(term in lower for term in ("error", "result_unknown", "ok false")) or any(term in text for term in ("写入", "工具", "查不到", "失败", "数据")):
        return "tool_or_data_candidate"
    if tools & _AUTONOMOUS_LOG_TOOL_NAMES or any(term in text for term in ("自主", "等待", "唤醒", "恢复", "到期", "推进到哪一步", "还在等什么", "结果未知")):
        return "autonomous_work_candidate"
    if any(term in text for term in ("回复", "表达", "不自然", "啰嗦", "语气", "话术", "内部机制")):
        return "response_style_candidate"
    if any(term in text for term in ("手册", "应该", "不知道", "怎么做", "身份")):
        return "handbook_candidate"
    return "manual_review_candidate"


def _is_autonomous_related_ledger_row(row: dict[str, Any], text: str, tool_names: list[str]) -> bool:
    if any(name in _AUTONOMOUS_LOG_TOOL_NAMES for name in tool_names):
        return True
    if any(term in text for term in _AUTONOMOUS_LOG_TERMS):
        return True
    if any(term in text for term in _ISSUE_SIGNAL_TERMS):
        return True
    guard = str(row.get("guard_result") or row.get("write_guard_result") or "").lower()
    if guard and guard not in {"allowed", "ok", "success", "none"}:
        return True
    return _row_has_tool_error(row)


def generate_autonomous_log_review(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    limit: int = 80,
    include_gray_observations: bool = True,
) -> dict[str, Any]:
    """Review recent real logs into optimization candidates without changing state.

    The report is evidence for humans and for Hermes to inspect. It does not
    update handbooks, create learning candidates, patch tools, change
    permissions, send messages, create tasks, or prescribe the model's next step.
    """

    if identity.role not in {"boss", "manager"}:
        return {"ok": False, "error": "permission_denied", "message": "真实日志复盘第一阶段仅允许老板或店长查看。"}

    max_items = max(1, min(int(limit or 80), 300))
    ledger_rows = _read_jsonl(store, "reply_ledger.jsonl")
    recent_rows = ledger_rows[-max_items:]
    candidates: list[dict[str, Any]] = []
    evidence_samples: list[dict[str, Any]] = []

    for index, row in enumerate(recent_rows, start=max(0, len(ledger_rows) - len(recent_rows)) + 1):
        tool_names = _extract_tool_names(row)
        text_parts = [
            row.get("raw_text"),
            row.get("user_text"),
            row.get("source_text"),
            row.get("final_reply"),
            row.get("reply"),
            row.get("error"),
            row.get("guard_result"),
            row.get("write_guard_result"),
        ]
        text = " ".join(str(part or "") for part in text_parts if part is not None)
        if not _is_autonomous_related_ledger_row(row, text, tool_names):
            continue
        candidate_type = _classify_log_review_candidate(text, tool_names)
        issue_signal = any(term in text for term in _ISSUE_SIGNAL_TERMS) or _row_has_tool_error(row)
        candidate = {
            "candidate_id": f"autonomous_log:{row.get('ledger_id') or row.get('message_id') or index}",
            "candidate_type": candidate_type,
            "source_type": "reply_ledger",
            "ledger_id": str(row.get("ledger_id") or ""),
            "message_id": str(row.get("message_id") or row.get("source_message_id") or ""),
            "created_at": str(row.get("timestamp") or row.get("created_at") or row.get("time") or ""),
            "actor_user_id": str(row.get("user_id") or row.get("canonical_user_id") or row.get("from_user") or ""),
            "tool_names": tool_names,
            "issue_signal": bool(issue_signal),
            "evidence_summary": _compact_text(text, limit=260),
            "review_note": "只作为人工和 Hermes 复盘候选；不能自动改手册、学习、权限或工具，也不能规定模型下一步。",
        }
        candidates.append(candidate)
        if len(evidence_samples) < 10:
            evidence_samples.append(deepcopy(candidate))

    gray_rows: list[dict[str, Any]] = []
    if include_gray_observations:
        for row in _read_jsonl(store, GRAY_OBSERVATIONS_FILE)[-max_items:]:
            outcome = str(row.get("outcome") or "")
            if outcome not in {"issue", "unclear"}:
                continue
            text = " ".join(str(row.get(key) or "") for key in ("observation_text", "source_text", "scenario_id"))
            candidate_type = _classify_log_review_candidate(text, [])
            candidate = {
                "candidate_id": f"gray_observation:{row.get('observation_id') or len(gray_rows) + 1}",
                "candidate_type": candidate_type,
                "source_type": "gray_observation",
                "observation_id": str(row.get("observation_id") or ""),
                "scenario_id": str(row.get("scenario_id") or ""),
                "outcome": outcome,
                "created_at": str(row.get("created_at") or ""),
                "actor_user_id": str(row.get("actor_user_id") or ""),
                "tool_names": [],
                "issue_signal": outcome == "issue",
                "evidence_summary": _compact_text(text, limit=260),
                "review_note": "灰度观察只作为优化候选；老板确认前不进入手册、长期记忆、绩效或工具改造。",
            }
            gray_rows.append(candidate)
            candidates.append(candidate)

    type_counts: dict[str, int] = {}
    for item in candidates:
        key = str(item.get("candidate_type") or "manual_review_candidate")
        type_counts[key] = type_counts.get(key, 0) + 1

    lines = [
        "# Hermes 自主工作真实日志复盘",
        "",
        "状态：只读复盘材料。未写入记忆，未改手册，未改权限，未修工具，未发通知，未派任务，未关闭事项。",
        "",
        "## 摘要",
        "",
        f"- 扫描最近对话账本：{len(recent_rows)} 条；相关轮次：{len([c for c in candidates if c.get('source_type') == 'reply_ledger'])} 条。",
        f"- 纳入灰度观察候选：{len(gray_rows)} 条。",
        f"- 优化候选合计：{len(candidates)} 条。",
        "",
        "## 候选分类",
        "",
    ]
    if type_counts:
        for key in sorted(type_counts):
            lines.append(f"- {key}：{type_counts[key]} 条。")
    else:
        lines.append("- 暂未发现明显候选；仍建议老板继续用真实渠道观察。")
    lines.extend(["", "## 边界", ""])
    lines.extend([
        "- 复盘只告诉 Hermes 和人工：哪里可能需要进一步看事实、看手册、看权限或看工具。",
        "- 它不是 Router，不限制模型表达，不要求固定流程，不自动把候选变成长期记忆。",
        "- 下一步是否优化手册、学习候选、工具或权限边界，必须由老板/负责人确认后另行执行。",
    ])

    return {
        "ok": True,
        "report_type": "autonomous_log_review_v1",
        "tenant_id": current_tenant_id(),
        "read_only": True,
        "ledger_scanned_count": len(recent_rows),
        "related_turn_count": len([c for c in candidates if c.get("source_type") == "reply_ledger"]),
        "gray_observation_count": len(gray_rows),
        "issue_candidate_count": len(candidates),
        "candidate_type_counts": type_counts,
        "candidates": candidates[:max_items],
        "evidence_samples": evidence_samples,
        "remaining_stages_after_this": [
            {
                "stage_id": "owner_decision_and_small_rollout",
                "title": "老板确认与小范围放量",
                "description": "老板根据复盘候选决定哪些进入手册优化、学习候选、工具修复或继续暂缓。",
            },
            {
                "stage_id": "autonomous_work_v2_hardening",
                "title": "自主工作 V2 加固",
                "description": "基于真实试用结果补强多机构、更多账号、稳定恢复和更细权限边界。",
            },
        ],
        "boundary": {
            "review_only": True,
            "limits_model": False,
            "auto_executes": False,
            "updates_handbook": False,
            "creates_learning_candidates": False,
            "patches_tools": False,
            "changes_permissions": False,
            "sends_notifications": False,
            "creates_tasks": False,
            "changes_salary": False,
            "stores_model_next_step": False,
        },
        "actions_taken": [],
        "forbidden_actions_confirmed_absent": [
            "no_memory_written",
            "no_handbook_changed",
            "no_learning_candidates_created",
            "no_tools_patched",
            "no_permissions_changed",
            "no_parent_messages_sent",
            "no_teacher_messages_sent",
            "no_tasks_created",
            "no_salary_or_payroll_changed",
            "no_router_changed",
            "no_model_next_step_stored",
        ],
        "rendered_text": "\n".join(lines).rstrip() + "\n",
        "render_verified": True,
    }


def _source(identity: UserIdentity, operation_id: str = "") -> dict[str, Any]:
    return {
        "actor_user_id": identity.canonical_user_id,
        "actor_name": identity.person_name,
        "actor_role": identity.role,
        "operation_id": str(operation_id or ""),
    }


def _profile_corrections_by_candidate(store: TuoguanStore) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for row in _read_jsonl(store, PROFILE_CORRECTIONS_FILE):
        candidate_id = str(row.get("candidate_id") or "")
        if not candidate_id:
            continue
        result.setdefault(candidate_id, []).append(row)
    for rows in result.values():
        rows.sort(key=lambda item: str(item.get("created_at") or ""))
    return result

def _fold_information_requests(store: TuoguanStore) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    updates: dict[str, list[dict[str, Any]]] = {}
    for row in _read_jsonl(store, INFORMATION_REQUESTS_FILE):
        record_type = str(row.get("record_type") or "")
        if record_type == "information_request_update":
            request_id = str(row.get("request_id") or "")
            if request_id:
                updates.setdefault(request_id, []).append(row)
            continue
        request_id = str(row.get("request_id") or "")
        if not request_id:
            continue
        item = deepcopy(row)
        item.setdefault("status", "asked")
        item.setdefault("updates", [])
        item.setdefault("updated_at", item.get("created_at") or "")
        result[request_id] = item
    for request_id, rows in updates.items():
        if request_id not in result:
            continue
        rows.sort(key=lambda item: str(item.get("created_at") or ""))
        result[request_id]["updates"] = rows
        latest = rows[-1]
        result[request_id]["status"] = str(latest.get("status") or result[request_id].get("status") or "")
        result[request_id]["updated_at"] = str(latest.get("created_at") or result[request_id].get("updated_at") or "")
        result[request_id]["latest_update_text"] = str(latest.get("update_text") or "")
        result[request_id]["latest_response_text"] = str(latest.get("response_text") or "")
        result[request_id]["needs_escalation"] = bool(latest.get("needs_escalation"))
    return result


def _performance_responses_by_candidate(store: TuoguanStore) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for row in _read_jsonl(store, PERFORMANCE_EVIDENCE_RESPONSES_FILE):
        candidate_id = str(row.get("candidate_id") or "")
        if not candidate_id:
            continue
        result.setdefault(candidate_id, []).append(row)
    for rows in result.values():
        rows.sort(key=lambda item: str(item.get("created_at") or ""))
    return result
