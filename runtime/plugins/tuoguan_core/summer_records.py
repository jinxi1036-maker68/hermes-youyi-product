"""Structured 2026 summer lesson records."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from .programs import SUMMER_PROGRAM_ID
from .store import TuoguanStore
from .summer_course_coverage import canonical_course, resolve_lesson_scope, save_course_coverage


SUMMER_LESSON_RECORDS_FILE = "summer_lesson_records.json"
_GROUP_PATTERNS = (
    (re.compile(r"一升二(?:年级)?(?:班|组)?"), "一年级班"),
    (re.compile(r"二升三(?:年级)?(?:和|、|及)?三升四(?:年级)?(?:班|组)?"), "二三年级班"),
    (re.compile(r"\u4e8c\u5347\u4e09(?:\u5e74\u7ea7)?(?:\u73ed|\u7ec4)?|\u4e09\u5347\u56db(?:\u5e74\u7ea7)?(?:\u73ed|\u7ec4)?"), "\u4e8c\u4e09\u5e74\u7ea7\u73ed"),
    (re.compile(r"\u4e09\u5e74\u7ea7(?:\u73ed|\u7ec4)?"), "\u4e8c\u4e09\u5e74\u7ea7\u73ed"),
    (re.compile(r"四升五(?:年级)?(?:和|、|及)?五升六(?:年级)?(?:班|组)?"), "四五六年级班"),
    (re.compile(r"三四五六年级(?:班|组)?|三至六年级(?:班|组)?"), "三四五六年级班"),
    (re.compile(r"四五六年级(?:班|组)?|四至六年级(?:班|组)?"), "四五六年级班"),
    (re.compile(r"二三年级(?:班|组)?|二至三年级(?:班|组)?"), "二三年级班"),
    (re.compile(r"一年级(?:班|组)?"), "一年级班"),
    (re.compile(r"二年级(?:班|组)?"), "二年级班"),
    (re.compile(r"一二年级(?:班|组)?"), "一二年级组"),
    (re.compile(r"三四年级(?:班|组)?"), "三四年级组"),
    (re.compile(r"五六年级(?:班|组)?"), "五六年级组"),
)
_LESSON_RE = re.compile(r"第\s*([一二三四五六七八九十\d]+)\s*(?:课时|节)")
_COURSES = ("科学实验", "数学", "语文", "英语", "阅读", "写作", "练字", "实验", "科学", "美术", "体育", "活动")


def looks_like_summer_lesson_record(text: str) -> bool:
    compact = str(text or "").replace(" ", "")
    if "暑假班每日总评" in compact or "暑假班今日总评" in compact:
        return True
    has_scope = (
        any(pattern.search(compact) for pattern, _group in _GROUP_PATTERNS)
        or bool(_LESSON_RE.search(compact))
        or "暑假班" in compact
        or ("今天" in compact and any(word in compact for word in ("课堂", "整体")))
    )
    return bool(
        has_scope
        and any(course in compact for course in _COURSES)
        and any(word in compact for word in ("课", "课时", "课堂", "整体", "重点孩子", "重点学生"))
    )


def parse_summer_lesson_record(text: str, store: TuoguanStore) -> dict[str, Any]:
    raw = str(text or "").strip()
    compact = raw.replace(" ", "")
    summer_group = next((group for pattern, group in _GROUP_PATTERNS if pattern.search(compact)), "")
    lesson_match = _LESSON_RE.search(compact)
    course = canonical_course(next((item for item in _COURSES if item in compact), ""))
    students = store.read_json("students.json", {})
    focus_students = []
    if isinstance(students, dict):
        for name, profile in students.items():
            enrollments = profile.get("program_enrollments") if isinstance(profile, dict) else []
            is_summer = str((profile or {}).get("summer_status") or "") in {"active", "phone_pending"} or any(
                isinstance(item, dict)
                and str(item.get("program_id") or "") in {SUMMER_PROGRAM_ID, "2026_summer"}
                and str(item.get("status") or "active") in {"active", "phone_pending"}
                for item in (enrollments or [])
            )
            if is_summer and str(name) in raw:
                clauses = [part.strip() for part in re.split(r"[，,。；;！!？?\n]+", raw) if part.strip()]
                observations = [part for part in clauses if str(name) in part]
                observation = "；".join(observations).strip() or raw
                focus_students.append({"student_name": str(name), "observation": observation})
    overall = raw
    for marker in ("课堂整体：", "课堂整体:", "整体：", "整体:"):
        if marker in raw:
            overall = raw.split(marker, 1)[1]
            overall = re.split(r"重点孩子[：:]|重点学生[：:]", overall, maxsplit=1)[0].strip(" ，,。")
            break
    else:
        focus_names = {str(item.get("student_name") or "") for item in focus_students}
        overall_parts = [
            part.strip()
            for part in re.split(r"[。；;！!？?\n]+", raw)
            if part.strip() and not any(name and name in part for name in focus_names)
        ]
        overall = "。".join(overall_parts).strip(" ，,。") or raw
    return {
        "program_id": SUMMER_PROGRAM_ID,
        "summer_group": summer_group,
        "course": course,
        "lesson": f"第{lesson_match.group(1)}课时" if lesson_match else "",
        "class_overall": overall,
        "focus_students": focus_students,
        "raw_text": raw,
        "record_kind": "daily_summary" if "总评" in compact else "lesson",
    }


def save_summer_lesson_record(
    store: TuoguanStore,
    *,
    text: str,
    teacher_userid: str,
    teacher_name: str,
    actor_is_manager: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    from .records import analyze_teacher_record, save_analysis

    parsed = parse_summer_lesson_record(text, store)
    if parsed["record_kind"] == "daily_summary" and not actor_is_manager:
        return {"ok": False, "error": "only_summer_manager_can_submit_daily_summary"}
    timestamp = now or datetime.now()
    scope = {
        "ok": True,
        "legacy_unscoped": True,
        "present_students": [],
        "absent_students": [],
    }
    if parsed["record_kind"] == "lesson":
        scope = resolve_lesson_scope(
            store,
            on_date=timestamp.date(),
            teacher_userid=teacher_userid,
            course=parsed["course"],
            summer_group=parsed["summer_group"],
            lesson=parsed["lesson"],
        )
        if not scope.get("ok"):
            return {
                "ok": False,
                "error": str(scope.get("error") or "lesson_scope_unclear"),
                "clarification": str(scope.get("clarification") or "课程覆盖范围不明确，请补充课程、年级组和课节。"),
            }
        if scope.get("legacy_unscoped") and not parsed["summer_group"]:
            return {
                "ok": False,
                "error": "class_missing",
                "clarification": "请先告诉我这节课是哪个班：一年级班、二三年级班，还是四五六年级班。确认班级后再记录整体表现。",
            }
        session = scope.get("session") if isinstance(scope.get("session"), dict) else {}
        if not parsed["summer_group"] and session:
            parsed["summer_group"] = str(session.get("summer_group") or "")
    created_at = timestamp.isoformat(timespec="seconds")
    item = {
        "id": f"summer_lesson_{uuid.uuid4().hex[:12]}",
        **parsed,
        "teacher_userid": teacher_userid,
        "teacher_name": teacher_name,
        "coverage_scope": "legacy_unscoped" if scope.get("legacy_unscoped") else "schedule_attendance",
        "present_students": list(scope.get("present_students") or []),
        "absent_students": list(scope.get("absent_students") or []),
        "created_at": created_at,
        "updated_at": created_at,
    }
    items = store.read_json(SUMMER_LESSON_RECORDS_FILE, [])
    items = items if isinstance(items, list) else []
    items.append(item)
    store.write_json(SUMMER_LESSON_RECORDS_FILE, items[-3000:])
    coverage_result = {"created": [], "individual_students": [], "overall_students": [], "ignored_absent_focus_students": []}
    if parsed["record_kind"] == "lesson" and not scope.get("legacy_unscoped"):
        coverage_result = save_course_coverage(
            store,
            lesson_record=item,
            present_students=list(scope.get("present_students") or []),
            absent_students=list(scope.get("absent_students") or []),
        )
    student_records = []
    for focus in parsed["focus_students"]:
        name = str(focus.get("student_name") or "")
        if not scope.get("legacy_unscoped") and name not in set(scope.get("present_students") or []):
            continue
        try:
            observation = str(focus.get("observation") or "").strip()
            analysis = analyze_teacher_record(f"{name} {observation}", store)
        except ValueError:
            continue
        analysis.update({
            "program_id": SUMMER_PROGRAM_ID,
            "summer_group": parsed["summer_group"],
            "course": parsed["course"],
            "lesson": parsed["lesson"],
            "class_overall": parsed["class_overall"],
            "focus_student": True,
            "course_coverage_level": "individual",
            "evidence_strength": "strong",
        })
        saved = save_analysis(
            analysis,
            teacher_userid,
            store,
            source_meta={"program_id": SUMMER_PROGRAM_ID, "kind": "summer_lesson", "lesson_record_id": item["id"]},
        )
        student_records.append(saved["record"])
    return {
        "ok": True,
        "lesson_record": item,
        "student_records": student_records,
        "coverage": coverage_result,
    }


def summer_lesson_dashboard(store: TuoguanStore) -> dict[str, Any]:
    items = store.read_json(SUMMER_LESSON_RECORDS_FILE, [])
    items = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    return {
        "program_id": SUMMER_PROGRAM_ID,
        "lesson_record_count": len([item for item in items if item.get("record_kind") == "lesson"]),
        "daily_summary_count": len([item for item in items if item.get("record_kind") == "daily_summary"]),
        "recent_records": items[-20:][::-1],
    }
