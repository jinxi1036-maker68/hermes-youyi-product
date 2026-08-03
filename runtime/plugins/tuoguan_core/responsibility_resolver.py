
"""Youyi operating model and student responsibility resolution.

This is factual context for the Hermes Agent and tools. It is not an intent
router. It never decides what the user means; it only answers: if a student and
business purpose are known, which staff responsibility is supported by current
business data, and which facts are still missing.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .store import TuoguanStore

OPERATING_MODEL_FILE = "youyi_operating_model.json"
REGULAR_PROGRAM_ID = "regular_tuoguan"
SUMMER_PROGRAM_ID = "summer_2026"


def load_operating_model(store: TuoguanStore) -> dict[str, Any]:
    data = store.read_json(OPERATING_MODEL_FILE, {})
    return data if isinstance(data, dict) else {}


def regular_manager_names(store: TuoguanStore) -> list[str]:
    model = load_operating_model(store)
    regular = (model.get("programs") or {}).get(REGULAR_PROGRAM_ID)
    if isinstance(regular, dict):
        names = [str(x) for x in regular.get("manager_names") or [] if str(x)]
        if names:
            return names
    return ["崔老师"]


def staff_display_name(store: TuoguanStore, user_id: str) -> str:
    user = str(user_id or "").strip()
    if not user:
        return ""
    model = load_operating_model(store)
    override = (model.get("known_staff_overrides") or {}).get(user)
    if isinstance(override, dict) and override.get("display_name"):
        return str(override.get("display_name"))
    staff = store.read_json("staff.json", {})
    if isinstance(staff, dict) and isinstance(staff.get(user), dict):
        return str(staff[user].get("name") or staff[user].get("display_name") or user)
    maps = store.read_json("teacher_wecom_map.json", {})
    if isinstance(maps, dict):
        for name, mapped in maps.items():
            if str(mapped) == user:
                return str(name)
    return user


def operating_staff_role(store: TuoguanStore, user_id: str) -> str:
    user = str(user_id or "").strip()
    if not user:
        return "unknown"
    model = load_operating_model(store)
    override = (model.get("known_staff_overrides") or {}).get(user)
    if isinstance(override, dict) and override.get("operating_role"):
        return str(override.get("operating_role"))
    staff = store.read_json("staff.json", {})
    role = ""
    if isinstance(staff, dict) and isinstance(staff.get(user), dict):
        item = staff[user]
        if str(item.get("role") or "") == "manager":
            return "manager"
        if SUMMER_PROGRAM_ID in {str(x) for x in item.get("program_ids") or []} and REGULAR_PROGRAM_ID not in {str(x) for x in item.get("program_ids") or []}:
            return "summer_teacher"
        role = str(item.get("operating_role") or item.get("work_role") or "")
    if role:
        return role
    return str(model.get("default_regular_teacher_role") or "part_time_lunch_teacher")


def _student_program_ids(profile: dict[str, Any]) -> set[str]:
    ids = {str(profile.get("program_id") or ""), str(profile.get("program") or "")}
    for row in profile.get("program_enrollments") or []:
        if isinstance(row, dict):
            ids.add(str(row.get("program_id") or row.get("program") or ""))
    if str(profile.get("campus_id") or "") == "main":
        ids.add(REGULAR_PROGRAM_ID)
    if profile.get("is_summer") or str(profile.get("summer_status") or ""):
        ids.add(SUMMER_PROGRAM_ID)
    return {x for x in ids if x}


def _is_regular_student(profile: dict[str, Any]) -> bool:
    ids = _student_program_ids(profile)
    if REGULAR_PROGRAM_ID in ids:
        return True
    if SUMMER_PROGRAM_ID in ids and len(ids) == 1:
        return False
    return str(profile.get("campus_id") or "") == "main"


def _service_mode(profile: dict[str, Any]) -> str:
    raw = str(
        profile.get("service_mode")
        or profile.get("care_type")
        or profile.get("tuoguan_type")
        or profile.get("attendance_mode")
        or profile.get("enrollment_type")
        or ""
    ).strip().lower()
    if raw in {"lunch_only", "午托", "只午托", "noon", "noon_only"}:
        return "lunch_only"
    if raw in {"evening_only", "晚托", "只晚托", "after_school", "homework"}:
        return "evening_only"
    if raw in {"full_day", "full", "all_day", "全托", "全天", "午托+晚托", "午晚托"}:
        return "full_day"
    return "unknown"


def _field(profile: dict[str, Any], *names: str) -> str:
    for name in names:
        value = str(profile.get(name) or "").strip()
        if value:
            return value
    return ""


def resolve_student_responsibility(store: TuoguanStore, student_name: str, *, purpose: str = "parent_communication") -> dict[str, Any]:
    students = store.read_json("students.json", {})
    if not isinstance(students, dict):
        return {"ok": False, "error": "students_unavailable", "message": "学生主库不可用。"}
    name = str(student_name or "").strip()
    profile = students.get(name)
    if not isinstance(profile, dict):
        return {"ok": False, "error": "student_not_found", "message": f"没有找到{name}的学生档案。"}
    profile = deepcopy(profile)
    program_ids = sorted(_student_program_ids(profile))
    if not _is_regular_student(profile):
        return {
            "ok": True,
            "student_name": name,
            "program_ids": program_ids,
            "in_scope": False,
            "resolution_status": "out_of_scope",
            "reason": "当前责任解析 V1 先覆盖正式托管班，暑假班暂不纳入家长沟通目标推进。",
            "missing_fields": [],
        }
    service_mode = _service_mode(profile)
    lunch_teacher = _field(profile, "lunch_teacher_user_id", "lunch_teacher", "noon_teacher_user_id", "noon_teacher")
    evening_teacher = _field(profile, "evening_teacher_user_id", "evening_teacher", "homework_teacher_user_id", "homework_teacher")
    current_teacher = _field(profile, "primary_teacher_user_id", "main_teacher_user_id", "teacher_user_id", "teacher")
    missing: list[str] = []
    primary = ""
    primary_reason = ""
    if service_mode == "lunch_only":
        primary = lunch_teacher or current_teacher
        primary_reason = "只午托孩子的家长沟通由午托负责老师推进。"
        if not lunch_teacher:
            missing.append("lunch_teacher")
    elif service_mode == "evening_only":
        primary = evening_teacher or current_teacher
        primary_reason = "只晚托孩子的家长沟通由晚托/作业负责老师推进。"
        if not evening_teacher:
            missing.append("evening_teacher")
    elif service_mode == "full_day":
        primary = evening_teacher or current_teacher
        primary_reason = "全托孩子优先由全职/晚托主责老师推进家长沟通。"
        if not evening_teacher:
            role = operating_staff_role(store, current_teacher)
            if role != "full_time_teacher":
                missing.append("evening_teacher_or_full_time_primary")
    else:
        primary = current_teacher
        primary_reason = "学生档案有当前 teacher 字段，但缺少午托/晚托/全托服务模式，不能确认最终主责。"
        missing.append("service_mode")
    if not primary:
        missing.append("primary_teacher")
    if purpose in {"meal", "nap", "pickup", "safety", "meal_nap_pickup_safety"}:
        target = lunch_teacher or primary
        target_reason = "吃饭、午休、接送和现场安全优先找午托老师。"
        if not lunch_teacher:
            missing.append("lunch_teacher")
    elif purpose in {"homework", "learning", "evening", "homework_learning_evening"}:
        target = evening_teacher or primary
        target_reason = "作业、学习和晚间表现优先找晚托/主责老师。"
        if service_mode == "full_day" and not evening_teacher and operating_staff_role(store, primary) != "full_time_teacher":
            missing.append("evening_teacher")
    else:
        target = primary
        target_reason = primary_reason
    missing = sorted(set(x for x in missing if x))
    status = "resolved" if target and not missing else "needs_manager_or_boss_confirmation"
    question_owners = regular_manager_names(store) + ["老板"] if missing else []
    return {
        "ok": True,
        "student_name": name,
        "program_ids": program_ids,
        "in_scope": True,
        "service_mode": service_mode,
        "known_teacher_user_id": current_teacher,
        "known_teacher_name": staff_display_name(store, current_teacher),
        "lunch_teacher_user_id": lunch_teacher,
        "lunch_teacher_name": staff_display_name(store, lunch_teacher),
        "evening_teacher_user_id": evening_teacher,
        "evening_teacher_name": staff_display_name(store, evening_teacher),
        "responsible_user_id": target,
        "responsible_name": staff_display_name(store, target),
        "responsible_operating_role": operating_staff_role(store, target),
        "purpose": purpose,
        "reason": target_reason,
        "missing_fields": missing,
        "resolution_status": status,
        "manager_should_confirm": bool(missing),
        "question_owner_names": question_owners,
        "autonomy_note": "责任解析只提供事实边界；Hermes Agent 自己决定如何自然询问和推进。",
    }


def summarize_responsibility_coverage(store: TuoguanStore, students: dict[str, dict[str, Any]], *, purpose: str = "parent_communication") -> dict[str, Any]:
    rows = [resolve_student_responsibility(store, name, purpose=purpose) for name in sorted(students)]
    in_scope = [row for row in rows if row.get("in_scope")]
    resolved = [row for row in in_scope if row.get("resolution_status") == "resolved"]
    unresolved = [row for row in in_scope if row.get("resolution_status") != "resolved"]
    by_teacher: dict[str, dict[str, Any]] = {}
    for row in in_scope:
        teacher = str(row.get("responsible_user_id") or "")
        key = teacher or "unresolved"
        bucket = by_teacher.setdefault(key, {
            "teacher_user_id": teacher,
            "teacher_name": staff_display_name(store, teacher) if teacher else "待店长/老板确认",
            "student_count": 0,
            "students": [],
            "unresolved_students": [],
        })
        bucket["student_count"] += 1
        bucket["students"].append(row["student_name"])
        if row.get("resolution_status") != "resolved":
            bucket["unresolved_students"].append(row["student_name"])
    return {
        "total": len(in_scope),
        "resolved_count": len(resolved),
        "unresolved_count": len(unresolved),
        "unresolved_students": unresolved,
        "by_responsible_teacher": sorted(by_teacher.values(), key=lambda item: (-int(item.get("student_count") or 0), str(item.get("teacher_name") or ""))),
    }
