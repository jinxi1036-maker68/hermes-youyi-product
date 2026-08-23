"""Evidence-backed project opportunity candidates for Xiaoyou.

This module deliberately separates deterministic evidence aggregation from
model judgement.  Records can make an evidence bundle eligible for review,
but only Xiaoyou can turn that bundle into an opportunity candidate.  The
result remains a reversible candidate until the owner approves validation.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import math
from typing import Any

from .programs import REGULAR_PROGRAM_ID, canonical_program_id, load_programs, student_program_ids
from .record_evaluation import evaluate_record
from .store import TuoguanStore
from .tenant_context import current_tenant_id


PROJECT_OPPORTUNITY_EVENTS_FILE = "project_opportunity_events.jsonl"
WINDOW_DAYS = 30
RECENT_EVIDENCE_DAYS = 14
MIN_COVERAGE_RATE = 0.60
MIN_EVIDENCE_DATES = 3
DISMISS_COOLDOWN_DAYS = 30
STALE_AFTER_DAYS = 30

ACTIVE_STUDENT_STATUSES = {"", "active", "enrolled", "serving", "normal", "unknown", "在读", "正常", "在托", "服务中"}
INACTIVE_STUDENT_STATUSES = {
    "paused", "pause", "suspended", "churned", "inactive", "left", "trial", "test", "demo",
    "停课", "暂停", "流失", "退费", "测试", "试听", "试托",
}
ACTIVE_STAFF_STATUSES = {"", "active", "approved", "employed", "在职", "正常"}
INACTIVE_STAFF_STATUSES = {"inactive", "left", "offboarded", "离职", "停用", "removed", "deleted"}

VISIBLE_STATUSES = {
    "evidence_ready", "decision_pending", "validation_approved", "validating", "proposal_ready", "accepted",
}
DECISION_STATUSES = {"decision_pending"}
TERMINAL_STATUSES = {"dismissed", "stale", "superseded", "failed"}

DIMENSIONS: dict[str, dict[str, Any]] = {
    "math_foundation": {
        "title": "数学基础提升",
        "tags": {"数学计算弱"},
        "record_types": {"academic_issue"},
        "support_terms": ("数学", "计算", "口算", "应用题", "错题"),
        "negative_terms": ("薄弱", "不会", "错误", "错题", "困难", "跟不上", "需要加强", "基础差", "计算慢"),
    },
    "reading_comprehension": {
        "title": "阅读理解提升",
        "tags": {"阅读理解弱"},
        "record_types": {"academic_issue"},
        "support_terms": ("阅读", "理解", "概括", "表达"),
        "negative_terms": ("薄弱", "不会", "困难", "读不懂", "答不到点", "需要加强", "理解弱"),
    },
    "writing_fluency": {
        "title": "书写能力提升",
        "tags": {"语文书写慢"},
        "record_types": {"academic_issue"},
        "support_terms": ("书写", "写字", "字迹", "卷面"),
        "negative_terms": ("慢", "潦草", "不规范", "困难", "需要加强", "跟不上"),
    },
    "english_foundation": {
        "title": "英语基础提升",
        "tags": {"英语基础弱"},
        "record_types": {"academic_issue"},
        "support_terms": ("英语", "单词", "拼读", "朗读", "背诵"),
        "negative_terms": ("薄弱", "不会", "困难", "记不住", "跟不上", "需要加强", "基础差"),
    },
    "learning_habits": {
        "title": "学习习惯提升",
        "tags": {"注意力不集中", "粗心"},
        "record_types": {"learning_habit"},
        "support_terms": ("注意力", "走神", "粗心", "习惯", "专注"),
        "negative_terms": ("不集中", "走神", "粗心", "拖拉", "需要提醒", "反复"),
    },
    "homework_efficiency": {
        "title": "作业效率提升",
        "tags": {"作业效率低"},
        "record_types": {"learning_habit"},
        "support_terms": ("作业", "效率", "拖拉", "磨蹭", "完成慢"),
        "negative_terms": ("慢", "效率低", "拖拉", "磨蹭", "超时", "需要催"),
    },
    "explicit_service_demand": {
        "title": "家长明确服务需求",
        "tags": {"家长需求"},
        "record_types": {"parent_anxiety", "parent_communication"},
        "support_terms": ("家长", "妈妈", "爸爸", "希望", "需要", "想要", "建议开", "能不能"),
        "negative_terms": ("希望", "需要", "想要", "建议开", "能不能", "想报名"),
    },
}


def scan_project_opportunity_evidence(
    store: TuoguanStore,
    *,
    now: datetime | None = None,
    project_id: str = "",
    limit: int = 3,
) -> dict[str, Any]:
    """Build deterministic evidence bundles without writing any state."""

    timestamp = _aware_now(now)
    start = timestamp - timedelta(days=WINDOW_DAYS)
    students = _student_map(store.read_json("students.json", {}))
    records = _record_rows(store.read_json("records.json", []))
    programs = load_programs(store)
    selected_ids = [canonical_program_id(project_id)] if str(project_id or "").strip() else list(programs)
    bundles: list[dict[str, Any]] = []
    for selected_id in selected_ids:
        active_students = {
            name: profile
            for name, profile in students.items()
            if _student_is_active(profile) and selected_id in student_program_ids(profile)
        }
        if not active_students:
            continue
        recent_records = [
            row for row in records
            if _record_student(row) in active_students
            and (recorded_at := _recorded_at(row)) is not None
            and start <= recorded_at <= timestamp
            and _record_is_valid(row)
        ]
        covered_students = {_record_student(row) for row in recent_records}
        coverage_rate = len(covered_students) / len(active_students) if active_students else 0.0
        active_teachers = _active_project_teachers(store, selected_id, active_students)
        for dimension, definition in DIMENSIONS.items():
            evidence = _dimension_evidence(
                recent_records,
                dimension=dimension,
                definition=definition,
                students=active_students,
            )
            affected = sorted({item["student_name"] for item in evidence})
            evidence_dates = sorted({item["evidence_date"] for item in evidence})
            teachers = sorted({item["teacher_id"] for item in evidence if item["teacher_id"]})
            latest_at = max((item["recorded_at"] for item in evidence), default="")
            minimum_students = max(5, math.ceil(len(active_students) * 0.08))
            single_teacher_project = len(active_teachers) == 1
            teacher_gate = (
                bool(teachers)
                and ((single_teacher_project and set(teachers) == set(active_teachers)) or (not single_teacher_project and len(teachers) >= 2))
            )
            latest_dt = _parse_time(latest_at)
            freshness_gate = bool(latest_dt and latest_dt >= timestamp - timedelta(days=RECENT_EVIDENCE_DAYS))
            gates = {
                "coverage": coverage_rate >= MIN_COVERAGE_RATE,
                "affected_students": len(affected) >= minimum_students,
                "evidence_dates": len(evidence_dates) >= MIN_EVIDENCE_DATES,
                "teachers": teacher_gate,
                "freshness": freshness_gate,
            }
            strong = bool(evidence) and all(gates.values())
            fingerprint = _fingerprint(current_tenant_id(), selected_id, dimension)
            bundle_id = f"opportunity_evidence:{fingerprint}:{timestamp.strftime('%Y%m%d')}"
            bundles.append({
                "bundle_id": bundle_id,
                "fingerprint": fingerprint,
                "tenant_id": current_tenant_id(),
                "project_id": selected_id,
                "project_name": str(programs.get(selected_id, {}).get("name") or selected_id),
                "dimension": dimension,
                "title": str(definition["title"]),
                "window_days": WINDOW_DAYS,
                "window_start": start.isoformat(timespec="seconds"),
                "window_end": timestamp.isoformat(timespec="seconds"),
                "total_student_count": len(active_students),
                "covered_student_count": len(covered_students),
                "coverage_rate": round(coverage_rate, 4),
                "minimum_affected_student_count": minimum_students,
                "affected_student_count": len(affected),
                "affected_student_names": affected,
                "teacher_count": len(teachers),
                "teacher_ids": teachers,
                "active_teacher_count": len(active_teachers),
                "evidence_dates": evidence_dates,
                "latest_evidence_at": latest_at,
                "evidence_refs": [item["evidence_ref"] for item in evidence],
                "evidence_type_counts": _count_values(item["evidence_type"] for item in evidence),
                "gates": gates,
                "strong_evidence": strong,
                "maturity": "strong" if strong else "observing",
                "missing_gates": [name for name, passed in gates.items() if not passed],
                "scanned_at": timestamp.isoformat(timespec="seconds"),
            })
    bundles.sort(key=_bundle_sort_key, reverse=True)
    safe_limit = max(1, min(int(limit or 3), 10))
    strongest = bundles[:safe_limit]
    return {
        "ok": True,
        "dry_run": True,
        "scan_due": True,
        "scanned_at": timestamp.isoformat(timespec="seconds"),
        "project_count": len(selected_ids),
        "evaluated_bundle_count": len(bundles),
        "strong_bundle_count": sum(1 for item in bundles if item["strong_evidence"]),
        "bundles": deepcopy(strongest),
    }


def record_project_opportunity_assessment(
    store: TuoguanStore,
    *,
    evidence_bundle: dict[str, Any],
    judgement: dict[str, Any],
    actor_user_id: str,
    operation_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist one model-reviewed opportunity candidate with writeback proof."""

    timestamp = _aware_now(now)
    if not isinstance(evidence_bundle, dict) or not str(evidence_bundle.get("fingerprint") or ""):
        return _error("invalid_evidence_bundle", "机会证据包无效，本轮没有写入。")
    if not isinstance(judgement, dict):
        return _error("invalid_model_judgement", "小优判断不是结构化结果，本轮没有写入。")
    existing = _by_fingerprint(store).get(str(evidence_bundle["fingerprint"]))
    affected_count = int(evidence_bundle.get("affected_student_count") or 0)
    if existing and str(existing.get("status") or "") == "dismissed":
        dismissed_at = _parse_time(existing.get("decided_at") or existing.get("updated_at"))
        previous_count = int(existing.get("affected_student_count") or 0)
        cooldown_active = bool(dismissed_at and timestamp < dismissed_at + timedelta(days=DISMISS_COOLDOWN_DAYS))
        significant_growth = affected_count >= max(previous_count + 1, math.ceil(previous_count * 1.5))
        if cooldown_active and not significant_growth:
            return {
                "ok": True,
                "suppressed": True,
                "reason": "dismissed_cooldown_active",
                "candidate": deepcopy(existing),
                "writeback_verified": True,
            }
    worth_validating = bool(judgement.get("worth_validating"))
    strong = bool(evidence_bundle.get("strong_evidence"))
    validation_plan = _validation_plan(judgement.get("validation_plan"))
    if strong and worth_validating:
        status = "decision_pending" if _validation_plan_complete(validation_plan) else "evidence_ready"
    else:
        status = "observing"
    opportunity_id = str((existing or {}).get("opportunity_id") or f"project_opportunity:{evidence_bundle['fingerprint']}")
    row = {
        "record_type": "project_opportunity_event",
        "event_id": f"project_opportunity_event:{hashlib.sha256(f'{operation_id}:{status}'.encode('utf-8')).hexdigest()[:20]}",
        "event_type": "assessment_recorded",
        "opportunity_id": opportunity_id,
        "fingerprint": str(evidence_bundle["fingerprint"]),
        "tenant_id": str(evidence_bundle.get("tenant_id") or current_tenant_id()),
        "project_id": str(evidence_bundle.get("project_id") or REGULAR_PROGRAM_ID),
        "project_name": str(evidence_bundle.get("project_name") or ""),
        "dimension": str(evidence_bundle.get("dimension") or ""),
        "title": str(evidence_bundle.get("title") or "新项目机会"),
        "hypothesis": _limit(judgement.get("hypothesis"), 300),
        "xiaoyou_judgement": _limit(judgement.get("reasoning"), 600),
        "missing_facts": _string_list(judgement.get("missing_facts"), 8, 180),
        "validation_plan": validation_plan,
        "status": status,
        "evidence_bundle_id": str(evidence_bundle.get("bundle_id") or ""),
        "affected_student_count": affected_count,
        "total_student_count": int(evidence_bundle.get("total_student_count") or 0),
        "teacher_count": int(evidence_bundle.get("teacher_count") or 0),
        "evidence_dates": deepcopy(evidence_bundle.get("evidence_dates") or []),
        "coverage_rate": float(evidence_bundle.get("coverage_rate") or 0),
        "evidence_strength": "strong" if strong else "observing",
        "evidence_refs": deepcopy(evidence_bundle.get("evidence_refs") or [])[:200],
        "latest_evidence_at": str(evidence_bundle.get("latest_evidence_at") or ""),
        "gates": deepcopy(evidence_bundle.get("gates") or {}),
        "missing_gates": deepcopy(evidence_bundle.get("missing_gates") or []),
        "affected_student_names": deepcopy(evidence_bundle.get("affected_student_names") or [])[:200],
        "source_kind": "internal_operating_evidence",
        "model_judgement": True,
        "operation_id": str(operation_id or ""),
        "actor_user_id": str(actor_user_id or "system"),
        "created_at": str((existing or {}).get("created_at") or timestamp.isoformat(timespec="seconds")),
        "updated_at": timestamp.isoformat(timespec="seconds"),
        "writeback_verified": True,
    }
    _append_if_new(store, row)
    verified = _latest_by_id(store).get(opportunity_id)
    ok = bool(verified and str(verified.get("event_id") or "") == row["event_id"])
    return {
        "ok": ok,
        "candidate": deepcopy(verified or row),
        "writeback_verified": ok,
        "error": "" if ok else "writeback_failed",
    }


def query_project_opportunities(
    store: TuoguanStore,
    *,
    project_id: str = "",
    status: str = "",
    include_internal: bool = False,
    limit: int = 20,
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = _aware_now(now)
    selected = []
    wanted_status = str(status or "").strip()
    wanted_project = canonical_program_id(project_id) if str(project_id or "").strip() else ""
    for item in _latest_by_id(store).values():
        current = deepcopy(item)
        current_status = _effective_status(current, timestamp)
        current["status"] = current_status
        if wanted_project and str(current.get("project_id") or "") != wanted_project:
            continue
        if wanted_status and current_status != wanted_status:
            continue
        if not include_internal and current_status not in VISIBLE_STATUSES:
            continue
        selected.append(current)
    selected.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    selected = selected[: max(1, min(int(limit or 20), 100))]
    return {
        "ok": True,
        "data_state": _data_state(selected, timestamp),
        "source_updated_at": max((str(item.get("updated_at") or "") for item in selected), default=""),
        "visible_count": len(selected),
        "items": [_public_item(item, include_people=include_internal) for item in selected],
        "rendered_text": _render_query(selected),
        "boundary": "项目机会是内部证据与小优判断形成的候选，不等于正式立项。",
    }


def review_project_opportunity(
    store: TuoguanStore,
    *,
    opportunity_id: str,
    decision: str,
    actor_user_id: str,
    operation_id: str,
    note: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = _aware_now(now)
    candidate = _latest_by_id(store).get(str(opportunity_id or "").strip())
    if not candidate:
        return _error("opportunity_not_found", "没有找到这个新项目机会候选。")
    normalized = str(decision or "").strip().lower()
    transitions = {
        "approve_validation": "validation_approved",
        "defer": "deferred",
        "dismiss": "dismissed",
        "reopen": "decision_pending" if _validation_plan_complete(candidate.get("validation_plan")) else "evidence_ready",
    }
    if normalized not in transitions:
        return _error("invalid_decision", "decision 只支持 approve_validation、defer、dismiss 或 reopen。")
    if normalized == "approve_validation" and str(candidate.get("status") or "") != "decision_pending":
        return _error("opportunity_not_ready", "该候选还没有完整验证方案，不能批准验证。")
    status = transitions[normalized]
    row = {
        **deepcopy(candidate),
        "record_type": "project_opportunity_event",
        "event_id": f"project_opportunity_event:{hashlib.sha256(f'{operation_id}:{normalized}'.encode('utf-8')).hexdigest()[:20]}",
        "event_type": "owner_reviewed",
        "status": status,
        "owner_decision": normalized,
        "decision_note": _limit(note, 300),
        "decided_by": str(actor_user_id or ""),
        "decided_at": timestamp.isoformat(timespec="seconds"),
        "operation_id": str(operation_id or ""),
        "updated_at": timestamp.isoformat(timespec="seconds"),
        "writeback_verified": True,
    }
    _append_if_new(store, row)
    verified = _latest_by_id(store).get(str(candidate["opportunity_id"]))
    ok = bool(verified and str(verified.get("event_id") or "") == row["event_id"])
    return {
        "ok": ok,
        "candidate": deepcopy(verified or row),
        "writeback_verified": ok,
        "error": "" if ok else "writeback_failed",
    }


def mark_opportunity_report_notified(
    store: TuoguanStore,
    *,
    opportunity_id: str,
    report_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = _aware_now(now)
    candidate = _latest_by_id(store).get(str(opportunity_id or "").strip())
    if not candidate:
        return _error("opportunity_not_found", "没有找到项目机会候选。")
    row = {
        **deepcopy(candidate),
        "record_type": "project_opportunity_event",
        "event_id": f"project_opportunity_event:{hashlib.sha256(f'{report_id}:{opportunity_id}'.encode('utf-8')).hexdigest()[:20]}",
        "event_type": "fixed_report_notified",
        "last_report_notified_id": str(report_id or ""),
        "last_report_notified_at": timestamp.isoformat(timespec="seconds"),
        "updated_at": str(candidate.get("updated_at") or timestamp.isoformat(timespec="seconds")),
        "writeback_verified": True,
    }
    _append_if_new(store, row)
    verified = _latest_by_id(store).get(str(candidate["opportunity_id"]))
    ok = bool(verified and str(verified.get("event_id") or "") == row["event_id"])
    return {"ok": ok, "writeback_verified": ok, "candidate": deepcopy(verified or row)}


def pending_report_opportunity(store: TuoguanStore, *, now: datetime | None = None) -> dict[str, Any] | None:
    timestamp = _aware_now(now)
    candidates = []
    for item in _latest_by_id(store).values():
        status = _effective_status(item, timestamp)
        if status not in {"evidence_ready", "decision_pending"}:
            continue
        notified = _parse_time(item.get("last_report_notified_at"))
        updated = _parse_time(item.get("updated_at"))
        if notified and updated and notified >= updated:
            continue
        candidates.append(item)
    candidates.sort(key=lambda row: (str(row.get("status")) == "decision_pending", str(row.get("updated_at") or "")), reverse=True)
    return deepcopy(candidates[0]) if candidates else None


def project_opportunity_scan_due(store: TuoguanStore, *, now: datetime | None = None) -> bool:
    timestamp = _aware_now(now)
    day = timestamp.date().isoformat()
    return not any(
        str(row.get("record_type") or "") == "project_opportunity_scan_run"
        and str(row.get("scan_date") or "") == day
        for row in _read_rows(store)
    )


def record_project_opportunity_scan_run(
    store: TuoguanStore,
    *,
    status: str,
    operation_id: str,
    evaluated_bundle_count: int,
    candidate_count: int,
    error: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = _aware_now(now)
    day = timestamp.date().isoformat()
    row = {
        "record_type": "project_opportunity_scan_run",
        "event_id": f"project_opportunity_scan:{day}",
        "scan_date": day,
        "status": str(status or "failed"),
        "evaluated_bundle_count": max(0, int(evaluated_bundle_count or 0)),
        "candidate_count": max(0, int(candidate_count or 0)),
        "error": _limit(error, 500),
        "operation_id": str(operation_id or ""),
        "created_at": timestamp.isoformat(timespec="seconds"),
        "writeback_verified": True,
    }
    _append_if_new(store, row)
    verified = any(str(item.get("event_id") or "") == row["event_id"] for item in _read_rows(store))
    return {"ok": verified, "run": row, "writeback_verified": verified}


def mark_stale_project_opportunities(
    store: TuoguanStore,
    *,
    operation_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = _aware_now(now)
    changed: list[str] = []
    for candidate in list(_latest_by_id(store).values()):
        if str(candidate.get("status") or "") in TERMINAL_STATUSES | {"accepted", "stale"}:
            continue
        if _effective_status(candidate, timestamp) != "stale":
            continue
        opportunity_id = str(candidate.get("opportunity_id") or "")
        row = {
            **deepcopy(candidate),
            "record_type": "project_opportunity_event",
            "event_id": f"project_opportunity_event:{hashlib.sha256(f'{operation_id}:{opportunity_id}:stale'.encode('utf-8')).hexdigest()[:20]}",
            "event_type": "auto_stale",
            "status": "stale",
            "stale_reason": "no_new_internal_evidence_for_30_days",
            "operation_id": str(operation_id or ""),
            "updated_at": timestamp.isoformat(timespec="seconds"),
            "writeback_verified": True,
        }
        _append_if_new(store, row)
        changed.append(opportunity_id)
    verified = _latest_by_id(store)
    ok = all(str(verified.get(item, {}).get("status") or "") == "stale" for item in changed)
    return {"ok": ok, "stale_count": len(changed), "opportunity_ids": changed, "writeback_verified": ok}


def _dimension_evidence(
    records: list[dict[str, Any]],
    *,
    dimension: str,
    definition: dict[str, Any],
    students: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for record in records:
        tags = set(_record_tags(record))
        record_types = set(_record_types(record))
        content = _record_content(record)
        compact = "".join(content.split())
        explicit_tag = bool(tags & set(definition["tags"]))
        structured_type = bool(record_types & set(definition["record_types"]))
        support = any(term in compact for term in definition["support_terms"])
        negative = any(term in compact for term in definition["negative_terms"])
        positive = "positive_progress" in record_types or "正向成长" in tags
        if dimension == "explicit_service_demand":
            matched = structured_type and support and negative
        else:
            matched = explicit_tag or (structured_type and support and negative)
        if not matched or (positive and not explicit_tag):
            continue
        student = _record_student(record)
        recorded_at = _recorded_at(record)
        if not student or recorded_at is None:
            continue
        day = recorded_at.date().isoformat()
        key = (student, dimension, day)
        if key in seen:
            continue
        seen.add(key)
        teacher = _record_teacher(record, students)
        record_id = str(record.get("id") or record.get("record_id") or "").strip()
        if not record_id:
            record_id = "record:" + hashlib.sha256(
                f"{student}|{day}|{teacher}|{content}".encode("utf-8")
            ).hexdigest()[:20]
        evidence.append({
            "evidence_ref": record_id,
            "student_name": student,
            "teacher_id": teacher,
            "evidence_date": day,
            "recorded_at": recorded_at.isoformat(timespec="seconds"),
            "evidence_type": "structured_tag" if explicit_tag else "structured_type_with_text_support",
        })
    return evidence


def _public_item(item: dict[str, Any], *, include_people: bool) -> dict[str, Any]:
    output = {
        key: deepcopy(item.get(key))
        for key in (
            "opportunity_id", "project_id", "project_name", "dimension", "title", "hypothesis",
            "status", "affected_student_count", "total_student_count", "teacher_count", "evidence_dates",
            "coverage_rate", "evidence_strength", "latest_evidence_at", "xiaoyou_judgement",
            "missing_facts", "validation_plan", "source_kind", "created_at", "updated_at",
        )
    }
    output["evidence_period"] = _evidence_period(item.get("evidence_dates") or [])
    output["requires_owner_decision"] = str(item.get("status") or "") in DECISION_STATUSES
    output["boundary"] = "待验证候选，不等于正式项目"
    if include_people:
        output["affected_student_names"] = deepcopy(item.get("affected_student_names") or [])
        output["evidence_refs"] = deepcopy(item.get("evidence_refs") or [])
    return output


def _latest_by_id(store: TuoguanStore) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    path = store.path_for(PROJECT_OPPORTUNITY_EVENTS_FILE)
    if not path.exists():
        return latest
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return latest
    for line in lines:
        try:
            row = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(row, dict) or str(row.get("record_type") or "") != "project_opportunity_event":
            continue
        opportunity_id = str(row.get("opportunity_id") or "").strip()
        if opportunity_id:
            latest[opportunity_id] = row
    return latest


def _by_fingerprint(store: TuoguanStore) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("fingerprint") or ""): item
        for item in _latest_by_id(store).values()
        if str(item.get("fingerprint") or "")
    }


def _append_if_new(store: TuoguanStore, row: dict[str, Any]) -> None:
    event_id = str(row.get("event_id") or "")
    if any(str(item.get("event_id") or "") == event_id for item in _read_rows(store)):
        return
    store.append_jsonl_verified(PROJECT_OPPORTUNITY_EVENTS_FILE, row)


def _read_rows(store: TuoguanStore) -> list[dict[str, Any]]:
    path = store.path_for(PROJECT_OPPORTUNITY_EVENTS_FILE)
    if not path.exists():
        return []
    rows = []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            item = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _student_map(raw: Any) -> dict[str, dict[str, Any]]:
    if isinstance(raw, dict):
        source = raw.get("students") if isinstance(raw.get("students"), dict) else raw
        return {str(name): row for name, row in source.items() if isinstance(row, dict)}
    if isinstance(raw, list):
        return {
            str(row.get("student_name") or row.get("name") or ""): row
            for row in raw if isinstance(row, dict) and str(row.get("student_name") or row.get("name") or "").strip()
        }
    return {}


def _record_rows(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [row for row in raw if isinstance(row, dict)]
    if isinstance(raw, dict):
        source = raw.get("records") if isinstance(raw.get("records"), list) else raw.values()
        return [row for row in source if isinstance(row, dict)]
    return []


def _record_is_valid(record: dict[str, Any]) -> bool:
    evaluation = record.get("record_evaluation")
    if not isinstance(evaluation, dict):
        evaluation = evaluate_record(record)
    return bool(evaluation.get("accepted"))


def _student_is_active(profile: dict[str, Any]) -> bool:
    status = str(profile.get("status") or profile.get("student_status") or profile.get("service_status") or "").strip().lower()
    if status in INACTIVE_STUDENT_STATUSES:
        return False
    return status in ACTIVE_STUDENT_STATUSES or not status


def _active_project_teachers(
    store: TuoguanStore,
    project_id: str,
    students: dict[str, dict[str, Any]],
) -> list[str]:
    staff = store.read_json("staff.json", {})
    active: set[str] = set()
    if isinstance(staff, dict):
        for user_id, profile in staff.items():
            if not isinstance(profile, dict) or str(profile.get("role") or "").strip().lower() != "teacher":
                continue
            status = str(profile.get("status") or profile.get("employment_status") or "").strip().lower()
            if status in INACTIVE_STAFF_STATUSES or (status and status not in ACTIVE_STAFF_STATUSES):
                continue
            programs = {canonical_program_id(item) for item in profile.get("program_ids") or []}
            if not programs or project_id in programs:
                active.add(str(user_id))
    if not active:
        active.update(_student_teacher(profile) for profile in students.values() if _student_teacher(profile))
    return sorted(active)


def _record_student(record: dict[str, Any]) -> str:
    return str(record.get("student_name") or record.get("student") or "").strip()


def _record_teacher(record: dict[str, Any], students: dict[str, dict[str, Any]]) -> str:
    direct = str(record.get("teacher_id") or record.get("teacher") or record.get("created_by") or "").strip()
    return direct or _student_teacher(students.get(_record_student(record), {}))


def _student_teacher(profile: dict[str, Any]) -> str:
    return str(profile.get("teacher_id") or profile.get("teacher_user_id") or profile.get("teacher") or "").strip()


def _record_content(record: dict[str, Any]) -> str:
    return str(record.get("content") or record.get("source_text") or record.get("text") or "").strip()


def _record_tags(record: dict[str, Any]) -> list[str]:
    value = record.get("tags") or []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value or "").strip() else []


def _record_types(record: dict[str, Any]) -> list[str]:
    value = record.get("record_types") or record.get("types") or []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value or "").strip() else []


def _recorded_at(record: dict[str, Any]) -> datetime | None:
    for key in ("timestamp", "created_at", "recorded_at", "date"):
        parsed = _parse_time(record.get(key))
        if parsed is not None:
            return parsed
    return None


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed.astimezone()


def _aware_now(value: datetime | None) -> datetime:
    timestamp = value or datetime.now().astimezone()
    return timestamp.astimezone() if timestamp.tzinfo else timestamp.astimezone()


def _fingerprint(tenant_id: str, project_id: str, dimension: str) -> str:
    return hashlib.sha256(f"{tenant_id}|{project_id}|{dimension}".encode("utf-8")).hexdigest()[:20]


def _bundle_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    gates = item.get("gates") if isinstance(item.get("gates"), dict) else {}
    return (
        bool(item.get("strong_evidence")),
        sum(1 for passed in gates.values() if passed),
        int(item.get("affected_student_count") or 0),
        float(item.get("coverage_rate") or 0),
        str(item.get("latest_evidence_at") or ""),
    )


def _effective_status(item: dict[str, Any], now: datetime) -> str:
    status = str(item.get("status") or "observing")
    if status in {"accepted", "dismissed", "superseded", "failed"}:
        return status
    latest = _parse_time(item.get("latest_evidence_at") or item.get("updated_at"))
    if latest and latest < now - timedelta(days=STALE_AFTER_DAYS):
        return "stale"
    return status


def _validation_plan(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {
        "objective": _limit(source.get("objective"), 240),
        "method": _limit(source.get("method"), 500),
        "sample_scope": _limit(source.get("sample_scope"), 240),
        "success_evidence": _string_list(source.get("success_evidence"), 6, 180),
        "estimated_days": max(0, min(int(source.get("estimated_days") or 0), 90)),
        "requires_staff_contact": bool(source.get("requires_staff_contact")),
    }


def _validation_plan_complete(plan: Any) -> bool:
    return bool(
        isinstance(plan, dict)
        and str(plan.get("objective") or "").strip()
        and str(plan.get("method") or "").strip()
        and list(plan.get("success_evidence") or [])
    )


def _data_state(items: list[dict[str, Any]], now: datetime) -> str:
    if not items:
        return "empty"
    if all(_effective_status(item, now) == "stale" for item in items):
        return "stale"
    return "current"


def _render_query(items: list[dict[str, Any]]) -> str:
    if not items:
        return "当前没有达到展示门槛的新项目机会，弱信号仍在内部观察。"
    lines = []
    for item in items[:5]:
        lines.append(
            f"{item.get('title')}：涉及{int(item.get('affected_student_count') or 0)}名学生、"
            f"{int(item.get('teacher_count') or 0)}名老师，状态{item.get('status')}。"
        )
    return "\n".join(lines)


def _evidence_period(values: list[Any]) -> str:
    dates = sorted(str(item) for item in values if str(item or "").strip())
    if not dates:
        return ""
    return dates[0] if len(dates) == 1 else f"{dates[0]} 至 {dates[-1]}"


def _count_values(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value or "").strip()
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


def _string_list(value: Any, limit: int, item_limit: int) -> list[str]:
    source = value if isinstance(value, list) else ([value] if str(value or "").strip() else [])
    return [_limit(item, item_limit) for item in source[:limit] if _limit(item, item_limit)]


def _limit(value: Any, length: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= length else text[: length - 1].rstrip() + "…"


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": code, "message": message, "writeback_verified": False}
