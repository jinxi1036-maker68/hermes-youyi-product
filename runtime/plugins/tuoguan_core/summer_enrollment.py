"""Summer-class student import and enrollment helpers."""

from __future__ import annotations

import csv
import difflib
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .store import TuoguanStore


SUMMER_PROGRAM_ID = "summer_2026"
SUMMER_PROGRAM_NAME = "2026暑假班"
SUMMER_ENROLLMENTS_FILE = "summer_enrollments.json"
SUMMER_IMPORT_ISSUES_FILE = "summer_import_issues.json"
PENDING_UNKNOWN_RECORDS_FILE = "pending_unknown_summer_records.json"
SUMMER_IMPORT_BATCHES_FILE = "summer_import_batches.json"
SUMMER_CLASS_GRADE_1 = "一年级班"
SUMMER_CLASS_GRADE_2_3 = "二三年级班"
SUMMER_CLASS_GRADE_4_6 = "四五六年级班"

_PHONE_RE = re.compile(r"1\d{10}")
_PHONE_KEYS = ("家长联系电话", "联系电话", "电话", "手机号", "手机", "parent_phone", "phone")
_NAME_KEYS = ("孩子姓名", "学生姓名", "姓名", "student_name", "name")
_GRADE_KEYS = ("年级", "grade")
_GROUP_KEYS = ("暑假班分组", "分组", "班级", "group", "summer_group")
_OLD_STUDENT_KEYS = ("是否托管老生", "托管老生", "是否老生", "old_student")
_DATE_KEYS = ("报名日期", "报名时间", "enrolled_at", "signup_date")
_NOTE_KEYS = ("备注", "note", "notes")
_ATTENTION_KEYS = ("特殊注意事项", "特殊事项", "注意事项", "attention", "special_attention")
_ATTENDANCE_MODE_KEYS = ("上课时段", "到课时段", "班型", "attendance_mode", "attendance_type")

_SAFETY_ATTENTION_WORDS = (
    "过敏",
    "哮喘",
    "不能剧烈运动",
    "身体不适",
    "流鼻血",
    "不吃辣",
    "接送",
    "只能妈妈接",
    "陌生人不可接",
    "电话确认",
    "花生",
    "鸡蛋",
)
_NORMAL_ATTENTION_WORDS = (
    "写字慢",
    "性格内向",
    "午休",
    "慢热",
    "多反馈",
    "走神",
    "挑食",
)
_MEAL_WORDS = ("午餐", "吃饭", "进餐", "餐食", "米饭", "青菜", "辣", "加饭")
_ACTIVITY_WORDS = ("体育", "活动", "跑步", "运动", "科学实验", "实验", "户外", "跳绳")
_PICKUP_WORDS = ("接送", "接孩子", "放学", "接走", "来接", "陌生人", "妈妈接", "电话确认")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _is_summer_manager(actor_role: str, actor_userid: str, store) -> bool:
    """判断是否为暑假班店长或老板。

    优先检查独立的暑假班店长列表 summer_manager_ids，
    如果没有配置则 fallback 到原有的 manager_ids 逻辑。
    老板（boss/super_admin）始终可以操作。
    """
    if actor_role in {"boss", "super_admin"}:
        return True
    
    whitelist = store.read_json("wecom_whitelist.json", {})
    if not isinstance(whitelist, dict):
        return False
    
    # 优先使用独立的暑假班店长列表
    summer_managers = whitelist.get("summer_manager_ids", [])
    if isinstance(summer_managers, list) and len(summer_managers) > 0:
        return str(actor_userid) in summer_managers
    
    # 如果没有配置暑假班店长列表，fallback 到原有逻辑
    managers = whitelist.get("manager_ids", [])
    if isinstance(managers, list):
        return str(actor_userid) in managers
    
    # 再检查角色
    roles = whitelist.get("user_roles", {})
    if isinstance(roles, dict):
        if str(roles.get(actor_userid)) == "manager":
            return True
        if str(roles.get(actor_userid)) == "boss":
            return True
    
    return False


def _norm(value: Any) -> str:
    return str(value or "").strip()


def _first(row: dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        if key in row and _norm(row.get(key)):
            return _norm(row.get(key))
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if _norm(value):
            return _norm(value)
    return ""


def normalize_phone(value: str) -> str:
    text = _norm(value)
    match = _PHONE_RE.search(text)
    return match.group(0) if match else text


def normalize_attendance_mode(value: Any) -> str:
    text = _norm(value).lower()
    if not text:
        return ""
    if "半天" in text or "仅上午" in text or text in {"morning_only", "morning", "half_day"}:
        return "morning_only"
    if "全天" in text or text in {"full_day", "full", "all_day"}:
        return "full_day"
    return ""


def summer_operating_class_for_grade(value: Any) -> str:
    grade = _norm(value).replace(" ", "")
    if any(token in grade for token in ("一年级", "一升二")):
        return SUMMER_CLASS_GRADE_1
    if any(token in grade for token in ("二年级", "三年级", "二升三", "三升四")):
        return SUMMER_CLASS_GRADE_2_3
    if any(token in grade for token in ("四年级", "五年级", "六年级", "四升五", "五升六", "六升七")):
        return SUMMER_CLASS_GRADE_4_6
    return ""


def _declares_any_key(row: dict[str, Any], keys: Iterable[str]) -> bool:
    lowered = {str(key).strip().lower() for key in row}
    return any(key.lower() in lowered for key in keys)


def student_phone(profile: dict[str, Any]) -> str:
    for key in ("phone", "parent_phone", "guardian_phone", "mobile"):
        phone = normalize_phone(_norm(profile.get(key)))
        if phone:
            return phone
    parents = profile.get("parents")
    if isinstance(parents, list):
        for parent in parents:
            if isinstance(parent, dict):
                phone = normalize_phone(_norm(parent.get("phone") or parent.get("mobile")))
                if phone:
                    return phone
    return ""


def split_attention(text: str) -> dict[str, list[str]]:
    raw_items = [
        item.strip()
        for item in re.split(r"[，,、;；\n]+", _norm(text))
        if item.strip()
    ]
    normal: list[str] = []
    safety: list[str] = []
    for item in raw_items:
        target = safety if any(word in item for word in _SAFETY_ATTENTION_WORDS) else normal
        target.append(item)
    return {"normal": normal, "safety": safety}


def normalize_import_row(row: dict[str, Any]) -> dict[str, Any]:
    phone = normalize_phone(_first(row, _PHONE_KEYS))
    attention = split_attention(_first(row, _ATTENTION_KEYS))
    attendance_mode_declared = _declares_any_key(row, _ATTENDANCE_MODE_KEYS)
    attendance_mode = normalize_attendance_mode(_first(row, _ATTENDANCE_MODE_KEYS))
    grade = _first(row, _GRADE_KEYS)
    summer_group = _first(row, _GROUP_KEYS) or summer_operating_class_for_grade(grade)
    return {
        "student_name": _first(row, _NAME_KEYS),
        "grade": grade,
        "summer_group": summer_group,
        "parent_phone": phone,
        "is_existing_tuoguan_student": _first(row, _OLD_STUDENT_KEYS) in {"是", "yes", "true", "1", "老生"},
        "signup_date": _first(row, _DATE_KEYS),
        "notes": _first(row, _NOTE_KEYS),
        "special_attention": attention,
        # Old imports without this column stay compatible. In the new template,
        # a declared but empty/invalid value is blocked before formal import.
        "attendance_mode": attendance_mode or ("full_day" if not attendance_mode_declared else ""),
        "attendance_mode_label": "半天（仅上午文化课）" if attendance_mode == "morning_only" else "全天" if attendance_mode else "",
        "attendance_mode_declared": attendance_mode_declared,
        "raw": dict(row),
    }


def read_summer_import_file(path: str | Path) -> list[dict[str, Any]]:
    """Read CSV/XLSX rows for summer import.

    XLSX support is intentionally optional; CSV remains available in minimal
    deployments. The returned rows are raw dicts, ready for normalize_import_row.
    """

    file_path = Path(path)
    suffix = file_path.suffix.lower()
    if suffix in {".csv", ".txt"}:
        with file_path.open("r", encoding="utf-8-sig", newline="") as fh:
            return [dict(row) for row in csv.DictReader(fh)]
    if suffix in {".xlsx", ".xlsm"}:
        try:
            from openpyxl import load_workbook  # type: ignore
        except Exception as exc:  # pragma: no cover - depends on optional package
            raise RuntimeError("读取 Excel 需要安装 openpyxl") from exc
        wb = load_workbook(file_path, read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [_norm(item) for item in rows[0]]
        output: list[dict[str, Any]] = []
        for values in rows[1:]:
            if not any(_norm(value) for value in values):
                continue
            output.append({headers[idx]: value for idx, value in enumerate(values) if idx < len(headers)})
        return output
    raise ValueError(f"不支持的名单文件格式：{file_path.suffix}")


def _load_list(store: TuoguanStore, name: str) -> list[dict[str, Any]]:
    data = store.read_json(name, [])
    return data if isinstance(data, list) else []


def _save_list(store: TuoguanStore, name: str, data: list[dict[str, Any]]) -> None:
    store.write_json(name, data)


def _similar_name(a: str, b: str) -> bool:
    if not a or not b or a == b:
        return False
    if a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.67


def duplicate_candidates(row: dict[str, Any], students: dict[str, Any]) -> list[dict[str, Any]]:
    name = _norm(row.get("student_name"))
    phone = normalize_phone(_norm(row.get("parent_phone")))
    candidates: list[dict[str, Any]] = []
    for existing_name, profile in students.items():
        if not isinstance(profile, dict):
            profile = {}
        existing_name_s = str(existing_name)
        existing_phone = student_phone(profile)
        reasons: list[str] = []
        if name and existing_name_s == name:
            reasons.append("姓名相同")
        elif _similar_name(name, existing_name_s):
            reasons.append("姓名相近")
        if phone and existing_phone and phone == existing_phone:
            if existing_name_s == name:
                reasons.append("姓名和电话都相同")
            elif existing_name_s != name:
                reasons.append("电话相同但姓名不同")
        if name and existing_name_s == name and phone and existing_phone and phone != existing_phone:
            reasons.append("姓名相同但电话不同")
        if reasons:
            candidates.append(
                {
                    "student_name": existing_name_s,
                    "phone": existing_phone,
                    "grade": _norm(profile.get("grade")),
                    "status": _norm(profile.get("status") or "当前学生"),
                    "reasons": list(dict.fromkeys(reasons)),
                }
            )
    return candidates


def _issue(
    row: dict[str, Any],
    *,
    issue_type: str,
    status: str,
    reason: str,
    candidates: list[dict[str, Any]] | None = None,
    actor_userid: str = "",
) -> dict[str, Any]:
    return {
        "id": f"summer_issue_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}",
        "program_id": SUMMER_PROGRAM_ID,
        "issue_type": issue_type,
        "status": status,
        "reason": reason,
        "import_student": row,
        "candidates": candidates or [],
        "created_by": actor_userid,
        "created_at": _now(),
        "updated_at": _now(),
    }


def _enrollment_payload(row: dict[str, Any], *, student_name: str, status: str = "active") -> dict[str, Any]:
    attendance_mode = normalize_attendance_mode(row.get("attendance_mode"))
    if not attendance_mode and not row.get("attendance_mode_declared"):
        attendance_mode = "full_day"
    return {
        "program_id": SUMMER_PROGRAM_ID,
        "program_name": SUMMER_PROGRAM_NAME,
        "student_name": student_name,
        "grade": _norm(row.get("grade")),
        "summer_group": _norm(row.get("summer_group")),
        "parent_phone": normalize_phone(_norm(row.get("parent_phone"))),
        "signup_date": _norm(row.get("signup_date")),
        "notes": _norm(row.get("notes")),
        "special_attention": row.get("special_attention") if isinstance(row.get("special_attention"), dict) else {"normal": [], "safety": []},
        "attendance_mode": attendance_mode,
        "status": status,
        "created_at": _now(),
        "updated_at": _now(),
    }


def _upsert_enrollment(enrollments: list[dict[str, Any]], payload: dict[str, Any]) -> None:
    for item in enrollments:
        if (
            isinstance(item, dict)
            and item.get("program_id") == payload.get("program_id")
            and item.get("student_name") == payload.get("student_name")
        ):
            item.update({**payload, "created_at": item.get("created_at") or payload["created_at"], "updated_at": _now()})
            return
    enrollments.append(payload)


def _attach_program(profile: dict[str, Any], enrollment: dict[str, Any]) -> None:
    programs = profile.get("program_enrollments")
    if not isinstance(programs, list):
        programs = []
    compact = {
        "program_id": enrollment["program_id"],
        "program_name": enrollment["program_name"],
        "grade": enrollment["grade"],
        "summer_group": enrollment["summer_group"],
        "attendance_mode": enrollment["attendance_mode"],
        "status": enrollment["status"],
        "signup_date": enrollment["signup_date"],
    }
    for item in programs:
        if isinstance(item, dict) and str(item.get("program_id") or "") in {SUMMER_PROGRAM_ID, "2026_summer"}:
            item.update(compact)
            break
    else:
        programs.append(compact)
    profile["program_enrollments"] = programs
    profile["summer_group"] = enrollment["summer_group"]
    profile["summer_grade"] = enrollment["grade"]
    profile["summer_status"] = enrollment["status"]
    profile["summer_attendance_mode"] = enrollment["attendance_mode"]
    profile["summer_attendance_mode_pending"] = not bool(enrollment["attendance_mode"])
    if enrollment["parent_phone"] and not _norm(profile.get("phone")):
        profile["phone"] = enrollment["parent_phone"]
    attention = enrollment.get("special_attention")
    if isinstance(attention, dict):
        profile["special_attention"] = attention


def _create_student_profile(row: dict[str, Any], *, actor_userid: str, temporary: bool = False) -> dict[str, Any]:
    enrollment = _enrollment_payload(row, student_name=row["student_name"], status="phone_pending" if temporary else "active")
    profile = {
        "name": row["student_name"],
        "grade": row["grade"],
        "phone": row.get("parent_phone") or "",
        "campus_id": "summer_2026",
        "status": "phone_pending" if temporary else "active",
        "source": "summer_import",
        "created_by": actor_userid,
        "created_at": _now(),
    }
    _attach_program(profile, enrollment)
    return profile


def bulk_import_summer_students(
    store: TuoguanStore,
    rows: list[dict[str, Any]],
    *,
    actor_userid: str,
    actor_role: str,
    confirmed: bool = False,
) -> dict[str, Any]:
    if not _is_summer_manager(actor_role, actor_userid, store):
        return {"ok": False, "error": "only_boss_or_summer_manager_can_import_summer_students"}
    if not confirmed:
        batches = _load_list(store, SUMMER_IMPORT_BATCHES_FILE)
        batch = {
            "id": f"summer_batch_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}",
            "program_id": SUMMER_PROGRAM_ID,
            "status": "pending_confirmation",
            "row_count": len(rows),
            "rows": [dict(item) for item in rows if isinstance(item, dict)],
            "created_by": actor_userid,
            "created_at": _now(),
            "expires_at": "",
        }
        batches.append(batch)
        _save_list(store, SUMMER_IMPORT_BATCHES_FILE, batches[-200:])
        return {
            "ok": True,
            "pending_confirmation": True,
            "batch_id": batch["id"],
            "row_count": len(rows),
            "message": f"已生成2026暑假班名单导入待确认批次，共{len(rows)}人；确认后才会写入正式学生库。",
        }
    students = store.read_json("students.json", {})
    if not isinstance(students, dict):
        students = {}
    enrollments = _load_list(store, SUMMER_ENROLLMENTS_FILE)
    issues = _load_list(store, SUMMER_IMPORT_ISSUES_FILE)
    created: list[dict[str, Any]] = []
    phone_missing: list[dict[str, Any]] = []
    grade_missing: list[dict[str, Any]] = []
    attendance_missing: list[dict[str, Any]] = []
    duplicate_pending: list[dict[str, Any]] = []
    invalid_rows: list[dict[str, Any]] = []
    for raw in rows:
        row = normalize_import_row(raw)
        missing_required = ["孩子姓名"] if not _norm(row.get("student_name")) else []
        if missing_required:
            issue = _issue(row, issue_type="missing_required", status="pending_required_fields", reason="缺少" + "、".join(missing_required), actor_userid=actor_userid)
            issues.append(issue)
            invalid_rows.append(issue)
            continue
        candidates = duplicate_candidates(row, students)
        if candidates:
            issue = _issue(row, issue_type="duplicate_candidate", status="duplicate_pending_confirmation", reason="发现疑似已有学生档案，需要人工确认", candidates=candidates, actor_userid=actor_userid)
            issues.append(issue)
            duplicate_pending.append(issue)
            continue
        if not row["grade"]:
            issue = _issue(row, issue_type="missing_grade", status="grade_pending", reason="缺少年级，已建档待店长补齐", actor_userid=actor_userid)
            issues.append(issue)
            grade_missing.append(issue)
        if not row["attendance_mode"]:
            issue = _issue(row, issue_type="missing_attendance_mode", status="attendance_mode_pending", reason="缺少上课时段，已建档待店长补齐", actor_userid=actor_userid)
            issues.append(issue)
            attendance_missing.append(issue)
        phone_pending = not bool(row["parent_phone"])
        if phone_pending:
            issue = _issue(row, issue_type="missing_phone", status="phone_pending", reason="缺少家长联系电话，已建档待店长补齐", actor_userid=actor_userid)
            issues.append(issue)
            phone_missing.append(issue)
        students[row["student_name"]] = _create_student_profile(row, actor_userid=actor_userid, temporary=phone_pending)
        enrollment = _enrollment_payload(
            row,
            student_name=row["student_name"],
            status="phone_pending" if phone_pending else "active",
        )
        _upsert_enrollment(enrollments, enrollment)
        _attach_program(students[row["student_name"]], enrollment)
        created.append(enrollment)
    store.write_json("students.json", students)
    _save_list(store, SUMMER_ENROLLMENTS_FILE, enrollments)
    _save_list(store, SUMMER_IMPORT_ISSUES_FILE, issues[-1000:])
    return {
        "ok": True,
        "created_count": len(created),
        "phone_missing_count": len(phone_missing),
        "grade_missing_count": len(grade_missing),
        "attendance_missing_count": len(attendance_missing),
        "duplicate_pending_count": len(duplicate_pending),
        "invalid_count": len(invalid_rows),
        "created": created,
        "phone_missing": phone_missing,
        "grade_missing": grade_missing,
        "attendance_missing": attendance_missing,
        "duplicate_pending": duplicate_pending,
        "invalid_rows": invalid_rows,
        "message": summer_import_result_message(created, phone_missing, grade_missing, attendance_missing, duplicate_pending, invalid_rows),
    }


def summer_import_result_message(
    created: list[dict[str, Any]],
    phone_missing: list[dict[str, Any]],
    grade_missing: list[dict[str, Any]],
    attendance_missing: list[dict[str, Any]],
    duplicate_pending: list[dict[str, Any]],
    invalid_rows: list[dict[str, Any]],
) -> str:
    lines = [f"暑假班名单导入检查完成：正式导入 {len(created)} 人。"]
    if phone_missing:
        names = "、".join(str(item.get("import_student", {}).get("student_name") or "") for item in phone_missing[:10])
        lines.append(f"以下学生已建档，家长电话待店长补齐：{names}")
    if grade_missing:
        names = "、".join(str(item.get("import_student", {}).get("student_name") or "") for item in grade_missing[:10])
        lines.append(f"以下学生已建档，年级待店长补齐：{names}")
    if attendance_missing:
        names = "、".join(str(item.get("import_student", {}).get("student_name") or "") for item in attendance_missing[:10])
        lines.append(f"以下学生已建档，上课时段待店长补齐：{names}")
    if duplicate_pending:
        names = "、".join(str(item.get("import_student", {}).get("student_name") or "") for item in duplicate_pending[:10])
        lines.append(f"发现疑似重复学生，需人工确认后再导入：{names}")
    if invalid_rows:
        lines.append(f"另有 {len(invalid_rows)} 条缺少必填字段，已进入待处理。")
    return "\n".join(lines)


def temporary_add_summer_student(
    store: TuoguanStore,
    row: dict[str, Any],
    *,
    actor_userid: str,
    actor_role: str,
) -> dict[str, Any]:
    if not _is_summer_manager(actor_role, actor_userid, store):
        return {"ok": False, "error": "only_boss_or_summer_manager_can_add_temporary_summer_student"}
    normalized = normalize_import_row(row)
    students = store.read_json("students.json", {})
    if not isinstance(students, dict):
        students = {}
    enrollments = _load_list(store, SUMMER_ENROLLMENTS_FILE)
    students[normalized["student_name"]] = _create_student_profile(normalized, actor_userid=actor_userid, temporary=not bool(normalized["parent_phone"]))
    enrollment = _enrollment_payload(normalized, student_name=normalized["student_name"], status="phone_pending" if not normalized["parent_phone"] else "active")
    _upsert_enrollment(enrollments, enrollment)
    _attach_program(students[normalized["student_name"]], enrollment)
    store.write_json("students.json", students)
    _save_list(store, SUMMER_ENROLLMENTS_FILE, enrollments)
    return {
        "ok": True,
        "student_name": normalized["student_name"],
        "phone_pending": not bool(normalized["parent_phone"]),
        "reply": (
            f"已暂存{normalized['student_name']}为暑假班学生，当前缺少家长联系电话。\n"
            "请尽快补齐，未补齐前不建议生成家长反馈或结业汇报。"
            if not normalized["parent_phone"]
            else f"已补录{normalized['student_name']}为暑假班学生。"
        ),
    }


def resolve_duplicate_issue(
    store: TuoguanStore,
    issue_id: str,
    *,
    action: str,
    actor_userid: str,
    existing_student_name: str = "",
) -> dict[str, Any]:
    students = store.read_json("students.json", {})
    if not isinstance(students, dict):
        students = {}
    issues = _load_list(store, SUMMER_IMPORT_ISSUES_FILE)
    enrollments = _load_list(store, SUMMER_ENROLLMENTS_FILE)
    issue = next((item for item in issues if isinstance(item, dict) and item.get("id") == issue_id), None)
    if issue is None:
        return {"ok": False, "error": "issue_not_found"}
    row = issue.get("import_student") if isinstance(issue.get("import_student"), dict) else {}
    if action == "link_existing":
        target_name = existing_student_name or _norm((issue.get("candidates") or [{}])[0].get("student_name"))
        if target_name not in students:
            return {"ok": False, "error": "existing_student_not_found"}
        enrollment = _enrollment_payload(row, student_name=target_name)
        _upsert_enrollment(enrollments, enrollment)
        _attach_program(students[target_name], enrollment)
        issue["status"] = "linked_existing"
        issue["resolved_student_name"] = target_name
    elif action == "create_new":
        students[row["student_name"]] = _create_student_profile(row, actor_userid=actor_userid)
        enrollment = _enrollment_payload(row, student_name=row["student_name"])
        _upsert_enrollment(enrollments, enrollment)
        _attach_program(students[row["student_name"]], enrollment)
        issue["status"] = "created_new_student"
        issue["resolved_student_name"] = row["student_name"]
    elif action == "skip":
        issue["status"] = "skipped"
    else:
        return {"ok": False, "error": "unsupported_action"}
    issue["resolved_by"] = actor_userid
    issue["resolved_at"] = _now()
    issue["updated_at"] = _now()
    store.write_json("students.json", students)
    _save_list(store, SUMMER_ENROLLMENTS_FILE, enrollments)
    _save_list(store, SUMMER_IMPORT_ISSUES_FILE, issues[-1000:])
    return {"ok": True, "issue": issue}


def remember_unknown_summer_record(
    store: TuoguanStore,
    *,
    student_name: str,
    text: str,
    teacher_userid: str,
    teacher_name: str = "",
    source: str = "teacher_record",
) -> dict[str, Any]:
    items = _load_list(store, PENDING_UNKNOWN_RECORDS_FILE)
    item = {
        "id": f"pending_unknown_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}",
        "program_id": SUMMER_PROGRAM_ID,
        "student_name": student_name,
        "text": text,
        "teacher_userid": teacher_userid,
        "teacher_name": teacher_name,
        "source": source,
        "status": "waiting_manager_confirmation",
        "created_at": _now(),
        "updated_at": _now(),
    }
    items.append(item)
    _save_list(store, PENDING_UNKNOWN_RECORDS_FILE, items[-1000:])
    return item


def guess_unknown_student_name(text: str) -> str:
    raw = _norm(text)
    match = re.match(r"([\u4e00-\u9fa5]{2,5}?)(?:今天|在|上课|数学|语文|英语|午餐|午休|活动|科学|写字)", raw)
    if not match:
        return ""
    candidate = match.group(1)
    if candidate.endswith(("老师", "店长")) or candidate in {"金总", "老板"}:
        return ""
    if candidate in {"我是", "我在", "你是", "你在", "他是", "他在", "她是", "她在", "这是", "这在", "那个"}:
        return ""
    if candidate.startswith(("我", "你", "他", "她", "这", "那")):
        return ""
    return candidate


def safety_attention_reminders(store: TuoguanStore, student_name: str, text: str) -> list[str]:
    students = store.read_json("students.json", {})
    profile = students.get(student_name, {}) if isinstance(students, dict) else {}
    if not isinstance(profile, dict):
        return []
    attention = profile.get("special_attention")
    safety = []
    if isinstance(attention, dict):
        safety = [str(item) for item in attention.get("safety") or [] if str(item)]
    if not safety:
        for enrollment in _load_list(store, SUMMER_ENROLLMENTS_FILE):
            if isinstance(enrollment, dict) and enrollment.get("student_name") == student_name:
                att = enrollment.get("special_attention")
                if isinstance(att, dict):
                    safety = [str(item) for item in att.get("safety") or [] if str(item)]
                break
    if not safety:
        return []
    compact = str(text or "").replace(" ", "")
    reminders: list[str] = []
    if any(word in compact for word in _MEAL_WORDS):
        for item in safety:
            if any(word in item for word in ("花生", "鸡蛋", "不吃辣", "过敏")):
                reminders.append(f"注意：{student_name}有“{item}”备注，请确认今日餐食无相关风险。")
    if any(word in compact for word in _ACTIVITY_WORDS):
        for item in safety:
            if any(word in item for word in ("哮喘", "不能剧烈运动", "身体不适")):
                reminders.append(f"注意：{student_name}有“{item}”备注，请活动中降低强度并持续观察状态。")
    if any(word in compact for word in _PICKUP_WORDS):
        for item in safety:
            if any(word in item for word in ("接送", "妈妈接", "陌生人", "电话确认")):
                reminders.append(f"注意：{student_name}有接送特殊要求“{item}”，请按备注核对接送人。")
    return list(dict.fromkeys(reminders))


def summer_import_dashboard(store: TuoguanStore) -> dict[str, Any]:
    enrollments = [item for item in _load_list(store, SUMMER_ENROLLMENTS_FILE) if isinstance(item, dict)]
    issues = [item for item in _load_list(store, SUMMER_IMPORT_ISSUES_FILE) if isinstance(item, dict)]
    pending_unknown = [item for item in _load_list(store, PENDING_UNKNOWN_RECORDS_FILE) if isinstance(item, dict) and item.get("status") != "resolved"]
    active_enrollments = [item for item in enrollments if str(item.get("status") or "") in {"active", "phone_pending"}]
    group_counts: dict[str, int] = {}
    safety_students: list[dict[str, Any]] = []
    attention_students: list[dict[str, Any]] = []
    for item in active_enrollments:
        group = _norm(item.get("summer_group"))
        if group:
            group_counts[group] = group_counts.get(group, 0) + 1
        attention = item.get("special_attention")
        if isinstance(attention, dict):
            normal = [str(v) for v in attention.get("normal") or [] if str(v)]
            safety = [str(v) for v in attention.get("safety") or [] if str(v)]
            if normal:
                attention_students.append({"student_name": item.get("student_name"), "items": normal})
            if safety:
                safety_students.append({"student_name": item.get("student_name"), "items": safety})
    def _student_key(item: dict[str, Any]) -> str:
        imported = item.get("import_student") if isinstance(item.get("import_student"), dict) else {}
        return str(imported.get("student_name") or item.get("student_name") or item.get("id") or "")

    phone_pending_map: dict[str, dict[str, Any]] = {}
    for item in issues:
        if item.get("issue_type") == "missing_phone" and str(item.get("status") or "") == "phone_pending":
            phone_pending_map[_student_key(item)] = item
    for item in active_enrollments:
        if str(item.get("status") or "") == "phone_pending":
            phone_pending_map.setdefault(_student_key(item), item)
    phone_pending = list(phone_pending_map.values())
    grade_pending = [
        item
        for item in issues
        if item.get("issue_type") == "missing_grade" and str(item.get("status") or "") == "grade_pending"
    ]
    attendance_pending = [
        item
        for item in issues
        if item.get("issue_type") == "missing_attendance_mode" and str(item.get("status") or "") == "attendance_mode_pending"
    ]
    duplicate_pending = [
        item
        for item in issues
        if item.get("issue_type") == "duplicate_candidate"
        and str(item.get("status") or "") == "duplicate_pending_confirmation"
    ]
    summary = {
        "total_students": len(active_enrollments),
        "group_counts": group_counts,
        "phone_pending_count": len(phone_pending),
        "grade_pending_count": len(grade_pending),
        "attendance_mode_pending_count": len(attendance_pending),
        "duplicate_pending_count": len(duplicate_pending),
        "unknown_record_count": len(pending_unknown),
        "special_attention_count": len(attention_students),
        "safety_attention_count": len(safety_students),
        "open_issue_count": len(phone_pending) + len(grade_pending) + len(attendance_pending) + len(duplicate_pending) + len(pending_unknown),
    }
    return {
        "program_id": SUMMER_PROGRAM_ID,
        "program_name": SUMMER_PROGRAM_NAME,
        "summary": summary,
        "students": [
            {
                "student_name": str(item.get("student_name") or ""),
                "grade": str(item.get("grade") or ""),
                "summer_group": str(item.get("summer_group") or ""),
                "status": str(item.get("status") or ""),
            }
            for item in active_enrollments[:200]
        ],
        "phone_pending_students": phone_pending[:50],
        "grade_pending_students": grade_pending[:50],
        "attendance_mode_pending_students": attendance_pending[:50],
        "duplicate_pending_students": duplicate_pending[:50],
        "unknown_record_students": pending_unknown[:50],
        "special_attention_students": attention_students[:50],
        "safety_attention_students": safety_students[:50],
    }
