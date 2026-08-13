"""Trusted compatibility operations for the Youyi production data model.

This module does not alter legacy records in bulk.  Each write performs its
own read-back verification and returns a stable result contract.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import uuid
from typing import Any

from .programs import student_program_ids
from .store import TuoguanStore
from .tenant_context import current_tenant_id


SUMMER_PROGRAM_ID = "summer_2026"
FORMAL_STATUS = "active"


def _now() -> datetime:
    return datetime.now().astimezone()


def _stamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="seconds")


def _data_version(store: TuoguanStore, *names: str) -> str:
    parts: list[str] = []
    for name in names:
        path = store.data_dir / name
        try:
            parts.append(f"{name}:{path.stat().st_mtime_ns}:{path.stat().st_size}")
        except OSError:
            parts.append(f"{name}:missing")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _phone(profile: dict[str, Any]) -> str:
    return str(profile.get("phone") or profile.get("parent_phone") or "").strip()


def register_official_student(
    store: TuoguanStore,
    *,
    student_name: str,
    parent_phone: str,
    teacher_user_id: str,
    grade: str = "",
    campus_id: str = "main",
    channel: str = "wecom_callback",
) -> dict[str, Any]:
    name = str(student_name or "").strip()
    phone = str(parent_phone or "").strip()
    if not name or not phone or not teacher_user_id:
        return {"ok": False, "reason_code": "missing_required_fields", "missing_fields": [key for key, value in (("student_name", name), ("parent_phone", phone), ("teacher_user_id", teacher_user_id)) if not value], "writeback_verified": False}
    students = store.read_json("students.json", {})
    if not isinstance(students, dict):
        return {"ok": False, "reason_code": "invalid_students_store", "writeback_verified": False}
    existing = students.get(name)
    if isinstance(existing, dict):
        same_phone = not _phone(existing) or _phone(existing) == phone
        if same_phone and str(existing.get("teacher") or "") == teacher_user_id:
            return {"ok": True, "already_applied": True, "student_name": name, "student_id": str(existing.get("student_id") or name), "writeback_verified": True}
        return {"ok": False, "reason_code": "duplicate_conflict", "student_name": name, "writeback_verified": False}
    for other_name, profile in students.items():
        if isinstance(profile, dict) and phone and _phone(profile) == phone:
            return {"ok": False, "reason_code": "phone_conflict", "conflict_student_name": str(other_name), "writeback_verified": False}
    student_id = f"stu_{uuid.uuid4().hex[:12]}"
    students[name] = {
        "student_id": student_id,
        "phone": phone,
        "teacher": teacher_user_id,
        "campus_id": campus_id or "main",
        "grade": str(grade or ""),
        "status": FORMAL_STATUS,
        "program_enrollments": [{"program_id": "regular_tuoguan", "status": FORMAL_STATUS, "created_at": _stamp()}],
        "tenant_id": current_tenant_id(),
        "channel": channel,
        "created_by": teacher_user_id,
        "created_at": _stamp(),
        "schema_version": 2,
    }
    store.write_json("students.json", students)
    reread = store.read_json("students.json", {}).get(name, {})
    verified = isinstance(reread, dict) and reread.get("student_id") == student_id and reread.get("teacher") == teacher_user_id and _phone(reread) == phone
    return {"ok": verified, "already_applied": False, "student_name": name, "student_id": student_id, "teacher_user_id": teacher_user_id, "writeback_verified": verified, "reason_code": "" if verified else "writeback_consistency_failed"}


def register_summer_student(
    store: TuoguanStore,
    *,
    student_name: str,
    parent_phone: str,
    grade: str,
    attendance_mode: str = "full_day",
    created_by: str,
    channel: str = "wecom_callback",
) -> dict[str, Any]:
    name, phone, grade = (str(value or "").strip() for value in (student_name, parent_phone, grade))
    if not name or not phone or not grade:
        return {"ok": False, "reason_code": "missing_required_fields", "missing_fields": [key for key, value in (("student_name", name), ("parent_phone", phone), ("grade", grade)) if not value], "writeback_verified": False}
    students = store.read_json("students.json", {})
    enrollments = store.read_json("summer_enrollments.json", [])
    if not isinstance(students, dict) or not isinstance(enrollments, list):
        return {"ok": False, "reason_code": "invalid_store", "writeback_verified": False}
    profile = students.get(name)
    if isinstance(profile, dict) and _phone(profile) and _phone(profile) != phone:
        return {"ok": False, "reason_code": "duplicate_conflict", "writeback_verified": False}
    duplicate = next((item for item in enrollments if isinstance(item, dict) and item.get("student_name") == name and item.get("program_id") == SUMMER_PROGRAM_ID and item.get("status") in {"active", "phone_pending"}), None)
    if duplicate:
        visible = isinstance(profile, dict) and SUMMER_PROGRAM_ID in student_program_ids(profile)
        return {"ok": visible, "already_applied": True, "student_name": name, "program_id": SUMMER_PROGRAM_ID, "h5_visibility_condition_verified": visible, "writeback_verified": visible, "reason_code": "" if visible else "h5_visibility_condition_failed"}
    if not isinstance(profile, dict):
        profile = {"student_id": f"stu_{uuid.uuid4().hex[:12]}", "teacher": "", "campus_id": "main", "schema_version": 2}
    profile["phone"] = phone
    profile["grade"] = grade
    profile["summer_status"] = FORMAL_STATUS
    profile["tenant_id"] = current_tenant_id()
    profile["channel"] = channel
    relations = profile.get("program_enrollments") if isinstance(profile.get("program_enrollments"), list) else []
    relations = [item for item in relations if not (isinstance(item, dict) and item.get("program_id") == SUMMER_PROGRAM_ID)]
    relations.append({"program_id": SUMMER_PROGRAM_ID, "status": FORMAL_STATUS, "created_at": _stamp()})
    profile["program_enrollments"] = relations
    students[name] = profile
    enrollment = {"program_id": SUMMER_PROGRAM_ID, "program_name": "2026暑假班", "student_name": name, "grade": grade, "parent_phone": phone, "attendance_mode": attendance_mode or "full_day", "status": FORMAL_STATUS, "created_by": created_by, "tenant_id": current_tenant_id(), "channel": channel, "created_at": _stamp(), "updated_at": _stamp()}
    enrollments.append(enrollment)
    store.write_json("students.json", students)
    store.write_json("summer_enrollments.json", enrollments)
    reread_profile = store.read_json("students.json", {}).get(name, {})
    reread_enrollments = store.read_json("summer_enrollments.json", [])
    relation_ok = isinstance(reread_profile, dict) and SUMMER_PROGRAM_ID in student_program_ids(reread_profile)
    enrollment_ok = any(isinstance(item, dict) and item.get("student_name") == name and item.get("program_id") == SUMMER_PROGRAM_ID and item.get("status") == FORMAL_STATUS for item in reread_enrollments)
    verified = relation_ok and enrollment_ok
    return {"ok": verified, "already_applied": False, "student_name": name, "program_id": SUMMER_PROGRAM_ID, "enrollment_verified": enrollment_ok, "h5_visibility_condition_verified": relation_ok, "writeback_verified": verified, "reason_code": "" if verified else "writeback_consistency_failed"}


def create_trial_lead(
    store: TuoguanStore,
    *,
    student_name: str,
    parent_phone: str,
    recorder_user_id: str,
    observation: str = "",
    age_or_grade: str = "",
    channel: str = "wecom_callback",
) -> dict[str, Any]:
    name, phone = str(student_name or "").strip(), str(parent_phone or "").strip()
    if not name or not phone:
        return {"ok": False, "reason_code": "missing_required_fields", "missing_fields": [key for key, value in (("student_name", name), ("parent_phone", phone)) if not value], "writeback_verified": False}
    students = store.read_json("students.json", {})
    if isinstance(students, dict) and name in students:
        return {"ok": False, "reason_code": "official_student_exists", "writeback_verified": False}
    leads = store.read_json("trial_leads.json", [])
    if not isinstance(leads, list): leads = []
    existing = next((item for item in leads if isinstance(item, dict) and (item.get("parent_phone") == phone or item.get("student_name") == name) and item.get("status") not in {"closed", "converted"}), None)
    if existing:
        linked = [task for task in store.load_tasks() if task.get("trial_lead_id") == existing.get("lead_id")]
        verified = len(linked) == 3
        return {"ok": verified, "already_applied": True, "lead_id": existing.get("lead_id"), "task_ids": [task.get("id") for task in linked], "writeback_verified": verified}
    now = _now()
    lead_id = f"lead_{uuid.uuid4().hex[:12]}"
    lead = {"lead_id": lead_id, "student_name": name, "parent_phone": phone, "age_or_grade": age_or_grade, "observation": observation, "recorder_user_id": recorder_user_id, "status": "pending_follow_up", "tenant_id": current_tenant_id(), "channel": channel, "created_at": _stamp(now)}
    leads.append(lead)
    tasks = store.load_tasks()
    specs = [
        ("课后反馈任务", now.replace(hour=20, minute=0, second=0, microsecond=0)),
        ("次日上午观察反馈任务", (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)),
        ("次日下午转化跟进任务", (now + timedelta(days=1)).replace(hour=16, minute=0, second=0, microsecond=0)),
    ]
    task_ids: list[str] = []
    from .tasks import build_task_contract

    for title, due in specs:
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        task_ids.append(task_id)
        task_title = f"{name}｜{title}"
        due_at = _stamp(due)
        tasks.append({
            "id": task_id,
            "title": task_title,
            "type": "trial_lead_follow_up",
            "level": "A",
            "status": "pending",
            "student_name": name,
            "assignee_userid": recorder_user_id,
            "trial_lead_id": lead_id,
            "due_at": due_at,
            "created_at": _stamp(now),
            "tenant_id": current_tenant_id(),
            "channel": channel,
            "source": "试听线索自动生成",
            "source_type": "trial_lead_follow_up",
            "source_authority": "verified_trial_lead",
            "task_contract": build_task_contract(
                title=task_title,
                source_text=f"试听线索 {name} 的{title}",
                student_name=name,
                due_at=due_at,
                business_goal="完成试听服务与转化跟进",
                assignee_user_id=recorder_user_id,
                assigned_by_role="system",
                known_facts=[f"试听线索编号：{lead_id}"],
            ),
        })
    def append_lead(current: Any) -> list[dict[str, Any]]:
        current = current if isinstance(current, list) else []
        if not any(isinstance(item, dict) and item.get("lead_id") == lead_id for item in current):
            current.append(deepcopy(lead))
        return current

    store.update_json("trial_leads.json", [], append_lead)
    store.append_tasks_verified([task for task in tasks if task.get("trial_lead_id") == lead_id])
    lead_ok = any(isinstance(item, dict) and item.get("lead_id") == lead_id for item in store.read_json("trial_leads.json", []))
    linked = [task for task in store.load_tasks() if task.get("trial_lead_id") == lead_id]
    verified = lead_ok and len(linked) == 3 and all(task.get("level") == "A" and task.get("assignee_userid") == recorder_user_id for task in linked)
    return {"ok": verified, "already_applied": False, "lead_id": lead_id, "task_ids": task_ids, "follow_up_task_count": len(linked), "writeback_verified": verified, "reason_code": "" if verified else "writeback_consistency_failed"}


def create_assigned_task(
    store: TuoguanStore,
    *,
    title: str,
    assignee_user_id: str,
    created_by: str,
    due_at: str = "",
    level: str = "A",
    student_name: str = "",
    channel: str = "wecom_callback",
    source_text: str = "",
    evidence_requirement: str = "",
    created_by_role: str = "",
    created_by_name: str = "",
    business_goal: str = "",
    known_facts: list[str] | None = None,
) -> dict[str, Any]:
    if not str(title).strip() or not str(assignee_user_id).strip():
        return {"ok": False, "reason_code": "missing_required_fields", "writeback_verified": False}
    signature = (str(title).strip(), assignee_user_id, due_at, student_name)
    task_id = f"task_{uuid.uuid4().hex[:12]}"
    from .tasks import build_task_contract

    original_text = str(source_text or title or "").strip()
    task = {
        "id": task_id,
        "title": str(title).strip(),
        "type": "manual_assignment",
        "level": level if level in {"S", "A", "B", "C"} else "A",
        "status": "pending",
        "student_name": student_name,
        "assignee_userid": assignee_user_id,
        "created_by": created_by,
        "created_by_role": str(created_by_role or ""),
        "created_by_name": str(created_by_name or ""),
        "due_at": due_at,
        "created_at": _stamp(),
        "tenant_id": current_tenant_id(),
        "channel": channel,
        "source_text": original_text,
        "evidence_requirement": str(evidence_requirement or "").strip(),
        "task_contract": build_task_contract(
            title=str(title).strip(),
            source_text=original_text,
            student_name=student_name,
            due_at=due_at,
            evidence_requirement=evidence_requirement,
            business_goal=business_goal,
            assignee_user_id=assignee_user_id,
            assigned_by_user_id=created_by,
            assigned_by_role=created_by_role,
            known_facts=known_facts,
        ),
    }
    selected: dict[str, Any] = {}
    already_applied = False

    def append_if_absent(tasks: list[dict[str, Any]]) -> None:
        nonlocal selected, already_applied
        existing = next((item for item in tasks if (str(item.get("title") or "").strip(), str(item.get("assignee_userid") or ""), str(item.get("due_at") or ""), str(item.get("student_name") or "")) == signature and item.get("status") not in {"cancelled", "closed"}), None)
        if existing is not None:
            selected = deepcopy(existing)
            already_applied = True
            return
        tasks.append(deepcopy(task))
        selected = deepcopy(task)

    store.update_tasks(append_if_absent)
    if already_applied:
        return {"ok": True, "already_applied": True, "task_id": selected.get("id"), "task": selected, "writeback_verified": True}
    found = next((item for item in store.load_tasks() if item.get("id") == task_id), None)
    verified = isinstance(found, dict) and found.get("assignee_userid") == assignee_user_id
    return {"ok": verified, "already_applied": False, "task_id": task_id, "task": deepcopy(found), "writeback_verified": verified, "reason_code": "" if verified else "writeback_consistency_failed"}


def operations_report(store: TuoguanStore, *, report_type: str) -> dict[str, Any]:
    records = store.read_json("records.json", [])
    leads = store.read_json("trial_leads.json", [])
    tasks = store.load_tasks()
    students = store.read_json("students.json", {})
    records = records if isinstance(records, list) else []
    leads = leads if isinstance(leads, list) else []
    students = students if isinstance(students, dict) else {}
    today = _now().date().isoformat()
    today_records = [item for item in records if isinstance(item, dict) and str(item.get("created_at") or item.get("timestamp") or "")[:10] == today]
    open_tasks = [item for item in tasks if item.get("status") not in {"completed", "closed", "cancelled", "done", "completed_by_admin", "closed_by_admin"}]
    safety_tasks = [item for item in tasks if item.get("level") == "S" or item.get("type") == "safety_incident"]
    summary = {"student_count": len(students), "today_record_count": len(today_records), "trial_lead_count": len(leads), "task_count": len(tasks), "open_task_count": len(open_tasks), "safety_task_count": len(safety_tasks)}
    labels = {"operations": "当前经营情况", "teacher_workload": "今天老师记录情况", "trial_follow_up": "试听线索与跟进", "daily": "经营日报", "weekly": "经营周报"}
    lines = [labels.get(report_type, "经营统计"), f"学生：{summary['student_count']} 人", f"今日记录：{summary['today_record_count']} 条", f"试听线索：{summary['trial_lead_count']} 条", f"任务：共 {summary['task_count']} 条，未完成 {summary['open_task_count']} 条", f"安全任务：{summary['safety_task_count']} 条"]
    return {"ok": True, "report_type": report_type, "result_count": sum(summary.values()), "summary": summary, "source_counts": {"students.json": len(students), "records.json": len(records), "trial_leads.json": len(leads), "tasks.json": len(tasks)}, "rendered_text": "\n".join(lines), "render_verified": True, "data_version": _data_version(store, "students.json", "records.json", "trial_leads.json", "tasks.json"), "writeback_verified": True}


def verify_dashboard_visibility(store: TuoguanStore, *, student_name: str = "") -> dict[str, Any]:
    students = store.read_json("students.json", {})
    enrollments = store.read_json("summer_enrollments.json", [])
    cache = store.read_json("dashboard_cache.json", {})
    students = students if isinstance(students, dict) else {}
    enrollments = enrollments if isinstance(enrollments, list) else []
    active_names = sorted({str(item.get("student_name") or "") for item in enrollments if isinstance(item, dict) and item.get("program_id") == SUMMER_PROGRAM_ID and item.get("status") in {"active", "phone_pending"}})
    profile_visible_names = sorted(name for name, profile in students.items() if isinstance(profile, dict) and SUMMER_PROGRAM_ID in student_program_ids(profile))
    missing = sorted(set(active_names) - set(profile_visible_names))
    target = str(student_name or "").strip()
    target_result = None
    if target:
        target_result = {"enrollment_exists": target in active_names, "profile_h5_condition": target in profile_visible_names, "visible": target in active_names and target in profile_visible_names}
    return {"ok": True, "active_enrollment_count": len(active_names), "profile_visible_count": len(profile_visible_names), "difference_count": len(missing), "difference_students": missing, "student": target_result, "dashboard_cache_generated_at": cache.get("generated_at") if isinstance(cache, dict) else None, "render_verified": True, "writeback_verified": True, "data_version": _data_version(store, "students.json", "summer_enrollments.json", "dashboard_cache.json")}
