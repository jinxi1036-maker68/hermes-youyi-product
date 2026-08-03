"""Summer course roster, attendance, evidence coverage and reminder helpers."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
import re
from typing import Any

from .programs import SUMMER_PROGRAM_ID
from .store import TuoguanStore


SCHEDULE_FILE = "summer_course_schedule.json"
ATTENDANCE_FILE = "summer_attendance.json"
COVERAGE_FILE = "summer_course_coverage.json"
REMINDER_STATE_FILE = "summer_recording_reminder_state.json"
DISPLAY_COURSES = ("语文", "数学", "英语", "练字", "实验")
_COURSE_ALIASES = {
    "阅读": "语文",
    "写作": "语文",
    "科学": "实验",
    "科学实验": "实验",
    "实验": "实验",
}
_PRESENT = {"present", "attended", "到课", "出勤", "正常"}
_ABSENT = {"absent", "leave", "缺勤", "请假", "未到"}


def canonical_session_period(session: dict[str, Any]) -> str:
    values = (
        session.get("time_period"),
        session.get("session_period"),
        session.get("period"),
        session.get("time_slot"),
        session.get("上课时段"),
    )
    text = " ".join(str(value or "") for value in values).lower()
    if any(word in text for word in ("下午", "午后", "afternoon", "pm")):
        return "afternoon"
    if any(word in text for word in ("上午", "早上", "morning", "am")):
        return "morning"
    start_time = str(session.get("start_time") or session.get("starts_at") or "").strip()
    match = re.search(r"(?:T|\s|^)(\d{1,2}):\d{2}", start_time)
    if match:
        return "afternoon" if int(match.group(1)) >= 12 else "morning"
    return ""


def _student_attendance_mode(store: TuoguanStore, student_name: str) -> str:
    students = store.read_json("students.json", {})
    profile = students.get(student_name, {}) if isinstance(students, dict) else {}
    if not isinstance(profile, dict):
        return ""
    direct = str(profile.get("summer_attendance_mode") or profile.get("attendance_mode") or "").strip()
    if direct:
        return direct
    for enrollment in profile.get("program_enrollments") or []:
        if (
            isinstance(enrollment, dict)
            and str(enrollment.get("program_id") or "") in {SUMMER_PROGRAM_ID, "2026_summer"}
        ):
            return str(enrollment.get("attendance_mode") or "").strip()
    return ""


def _student_attendance_mode_pending(store: TuoguanStore, student_name: str) -> bool:
    students = store.read_json("students.json", {})
    profile = students.get(student_name, {}) if isinstance(students, dict) else {}
    return isinstance(profile, dict) and bool(profile.get("summer_attendance_mode_pending"))


def _eligible_roster_for_session(
    store: TuoguanStore,
    session: dict[str, Any],
    roster: list[str],
) -> tuple[list[str], list[str]]:
    if canonical_session_period(session) != "afternoon":
        return roster, []
    excluded = [
        name
        for name in roster
        if _student_attendance_mode(store, name) == "morning_only"
        or _student_attendance_mode_pending(store, name)
    ]
    excluded_set = set(excluded)
    return [name for name in roster if name not in excluded_set], sorted(set(excluded))


def canonical_course(value: Any) -> str:
    text = str(value or "").strip()
    if text in DISPLAY_COURSES:
        return text
    for alias, target in _COURSE_ALIASES.items():
        if alias in text:
            return target
    return text


def _items(store: TuoguanStore, name: str) -> list[dict[str, Any]]:
    raw = store.read_json(name, [])
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _date(value: Any) -> str:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else text


def _active_summer_students(store: TuoguanStore, group: str = "") -> list[str]:
    result: list[str] = []
    enrollments_file = store.read_json("summer_enrollments.json", [])
    if isinstance(enrollments_file, list):
        for item in enrollments_file:
            if not isinstance(item, dict):
                continue
            if str(item.get("program_id") or SUMMER_PROGRAM_ID) not in {SUMMER_PROGRAM_ID, "2026_summer"}:
                continue
            if str(item.get("status") or "active") not in {"active", "phone_pending"}:
                continue
            student_group = str(item.get("summer_group") or "")
            if group and student_group != group:
                continue
            name = str(item.get("student_name") or "").strip()
            if name:
                result.append(name)
    students = store.read_json("students.json", {})
    if isinstance(students, dict):
        for name, profile in students.items():
            if not isinstance(profile, dict):
                continue
            enrollments = profile.get("program_enrollments") or []
            active = str(profile.get("summer_status") or "") in {"active", "phone_pending"} or any(
                isinstance(item, dict)
                and str(item.get("program_id") or "") in {SUMMER_PROGRAM_ID, "2026_summer"}
                and str(item.get("status") or "active") in {"active", "phone_pending"}
                for item in enrollments
            )
            student_group = str(profile.get("summer_group") or "")
            if active and (not group or student_group == group):
                result.append(str(name))
    return sorted(set(result))


def scheduled_sessions(
    store: TuoguanStore,
    *,
    on_date: date | str,
    teacher_userid: str = "",
    course: str = "",
    summer_group: str = "",
) -> list[dict[str, Any]]:
    target_date = on_date.isoformat() if isinstance(on_date, date) else _date(on_date)
    target_course = canonical_course(course)
    result = []
    for item in _items(store, SCHEDULE_FILE):
        if _date(item.get("date")) != target_date:
            continue
        if str(item.get("program_id") or SUMMER_PROGRAM_ID) not in {SUMMER_PROGRAM_ID, "2026_summer"}:
            continue
        if str(item.get("status") or "scheduled") in {"cancelled", "closed", "停课"}:
            continue
        if teacher_userid and str(item.get("teacher_userid") or "") != teacher_userid:
            continue
        if target_course and canonical_course(item.get("course")) != target_course:
            continue
        if summer_group and str(item.get("summer_group") or "") != summer_group:
            continue
        result.append(dict(item))
    return result


def session_attendance(
    store: TuoguanStore,
    session: dict[str, Any],
) -> dict[str, Any]:
    session_date = _date(session.get("date"))
    course = canonical_course(session.get("course"))
    group = str(session.get("summer_group") or "")
    roster = [str(name) for name in session.get("student_names") or [] if str(name)]
    if not roster:
        roster = _active_summer_students(store, group)
    roster, excluded_by_attendance_mode = _eligible_roster_for_session(store, session, roster)
    if "present_students" in session or "absent_students" in session:
        present = [str(name) for name in session.get("present_students") or [] if str(name)]
        absent = [str(name) for name in session.get("absent_students") or [] if str(name)]
        eligible = set(roster)
        present = [name for name in present if name in eligible]
        absent = [name for name in absent if name in eligible]
        return {
            "confirmed": True,
            "roster": roster,
            "present": sorted(set(present)),
            "absent": sorted(set(absent)),
            "excluded_by_attendance_mode": excluded_by_attendance_mode,
            "session_period": canonical_session_period(session),
        }
    attendance = [
        item
        for item in _items(store, ATTENDANCE_FILE)
        if _date(item.get("date")) == session_date
        and (not item.get("course") or canonical_course(item.get("course")) == course)
        and (not item.get("summer_group") or str(item.get("summer_group")) == group)
        and str(item.get("student_name") or "") in roster
    ]
    if not attendance:
        return {
            "confirmed": False,
            "roster": roster,
            "present": [],
            "absent": [],
            "excluded_by_attendance_mode": excluded_by_attendance_mode,
            "session_period": canonical_session_period(session),
        }
    statuses = {str(item.get("student_name") or ""): str(item.get("status") or "").strip() for item in attendance}
    present = [name for name in roster if statuses.get(name) in _PRESENT]
    absent = [name for name in roster if statuses.get(name) in _ABSENT]
    unconfirmed = [name for name in roster if name not in statuses]
    return {
        "confirmed": not unconfirmed,
        "roster": roster,
        "present": sorted(set(present)),
        "absent": sorted(set(absent)),
        "unconfirmed": unconfirmed,
        "excluded_by_attendance_mode": excluded_by_attendance_mode,
        "session_period": canonical_session_period(session),
    }


def resolve_lesson_scope(
    store: TuoguanStore,
    *,
    on_date: date | str,
    teacher_userid: str,
    course: str,
    summer_group: str,
    lesson: str = "",
) -> dict[str, Any]:
    if not course:
        return {"ok": False, "error": "course_missing", "clarification": "请补充课程名称和课节，例如‘今天数学第1节课’。"}
    target_date = on_date.isoformat() if isinstance(on_date, date) else _date(on_date)
    target_course = canonical_course(course)
    sessions = scheduled_sessions(
        store,
        on_date=on_date,
        teacher_userid=teacher_userid,
        course=course,
        summer_group=summer_group,
    )
    if lesson:
        exact = [item for item in sessions if str(item.get("lesson") or "") == lesson]
        if exact:
            sessions = exact
    if not sessions:
        configured = bool(_items(store, SCHEDULE_FILE))
        if not configured and summer_group:
            roster = _active_summer_students(store, summer_group)
            if roster:
                return {
                    "ok": True,
                    "legacy_unscoped": False,
                    "fallback_without_schedule": True,
                    "present_students": roster,
                    "absent_students": [],
                    "roster": roster,
                    "session_period": "",
                    "session": {
                        "date": target_date,
                        "course": target_course,
                        "summer_group": summer_group,
                        "student_names": roster,
                        "source": "summer_enrollment_roster_fallback",
                    },
                }
        return {
            "ok": not configured,
            "legacy_unscoped": not configured,
            "error": "schedule_not_found" if configured else "",
            "clarification": "",
            "present_students": [],
            "absent_students": [],
        }
    if len(sessions) > 1:
        return {"ok": False, "error": "multiple_sessions", "clarification": "今天有多节相同课程，请补充第几课时或具体班级。"}
    attendance = session_attendance(store, sessions[0])
    if not attendance.get("confirmed"):
        return {
            "ok": False,
            "error": "attendance_not_confirmed",
            "clarification": "本节课出勤名单还没有确认，暂不能生成课程覆盖，请先补齐到课和缺勤情况。",
            "session": sessions[0],
            **attendance,
        }
    return {
        "ok": True,
        "session": sessions[0],
        "present_students": attendance.get("present") or [],
        "absent_students": attendance.get("absent") or [],
        "roster": attendance.get("roster") or [],
        "excluded_by_attendance_mode": attendance.get("excluded_by_attendance_mode") or [],
        "session_period": attendance.get("session_period") or "",
    }


def save_course_coverage(
    store: TuoguanStore,
    *,
    lesson_record: dict[str, Any],
    present_students: list[str],
    absent_students: list[str],
) -> dict[str, Any]:
    focus = {
        str(item.get("student_name") or ""): str(item.get("observation") or "")
        for item in lesson_record.get("focus_students") or []
        if isinstance(item, dict) and str(item.get("student_name") or "")
    }
    present_set = set(present_students)
    rows = _items(store, COVERAGE_FILE)
    created: list[dict[str, Any]] = []
    for student_name in present_students:
        individual = student_name in focus
        created.append({
            "id": f"{lesson_record.get('id')}:{student_name}",
            "program_id": SUMMER_PROGRAM_ID,
            "lesson_record_id": str(lesson_record.get("id") or ""),
            "date": _date(lesson_record.get("created_at")),
            "course": canonical_course(lesson_record.get("course")),
            "summer_group": str(lesson_record.get("summer_group") or ""),
            "student_name": student_name,
            "teacher_userid": str(lesson_record.get("teacher_userid") or ""),
            "teacher_name": str(lesson_record.get("teacher_name") or ""),
            "coverage_level": "individual" if individual else "overall",
            "evidence_strength": "strong" if individual else "ordinary",
            "observation": focus.get(student_name, ""),
            "class_overall": str(lesson_record.get("class_overall") or ""),
            "created_at": str(lesson_record.get("created_at") or ""),
        })
    rows.extend(created)
    store.write_json(COVERAGE_FILE, rows[-20000:])
    return {
        "created": created,
        "individual_students": sorted(name for name in focus if name in present_set),
        "overall_students": sorted(name for name in present_students if name not in focus),
        "ignored_absent_focus_students": sorted(name for name in focus if name in set(absent_students)),
    }


def current_teaching_cycle(on_date: date) -> tuple[date, date]:
    days_since_wednesday = (on_date.weekday() - 2) % 7
    start = on_date - timedelta(days=days_since_wednesday)
    return start, start + timedelta(days=4)


def student_course_evidence(
    store: TuoguanStore,
    *,
    student_name: str,
    start: date,
    end: date,
) -> dict[str, Any]:
    rows = [
        item for item in _items(store, COVERAGE_FILE)
        if str(item.get("student_name") or "") == student_name
        and start.isoformat() <= _date(item.get("date") or item.get("created_at")) <= end.isoformat()
    ]
    by_course: dict[str, dict[str, int]] = {}
    for course in DISPLAY_COURSES:
        selected = [item for item in rows if canonical_course(item.get("course")) == course]
        by_course[course] = {
            "overall": sum(1 for item in selected if item.get("coverage_level") == "overall"),
            "individual": sum(1 for item in selected if item.get("coverage_level") == "individual"),
        }
    return {
        "overall_count": sum(1 for item in rows if item.get("coverage_level") == "overall"),
        "individual_count": sum(1 for item in rows if item.get("coverage_level") == "individual"),
        "by_course": by_course,
        "missing_courses": [course for course, counts in by_course.items() if counts["overall"] + counts["individual"] == 0],
    }


def summer_course_coverage_dashboard(store: TuoguanStore, now: datetime | None = None) -> dict[str, Any]:
    timestamp = now or datetime.now()
    target = timestamp.date()
    schedules = scheduled_sessions(store, on_date=target)
    coverage = [item for item in _items(store, COVERAGE_FILE) if _date(item.get("date") or item.get("created_at")) == target.isoformat()]
    cards: list[dict[str, Any]] = []
    for course in DISPLAY_COURSES:
        course_sessions = [item for item in schedules if canonical_course(item.get("course")) == course]
        expected: set[str] = set()
        absent: set[str] = set()
        teachers: set[str] = set()
        attendance_confirmed = True
        for session in course_sessions:
            attendance = session_attendance(store, session)
            attendance_confirmed = attendance_confirmed and bool(attendance.get("confirmed"))
            expected.update(attendance.get("present") or [])
            absent.update(attendance.get("absent") or [])
            teachers.add(str(session.get("teacher_name") or session.get("teacher_userid") or "待安排"))
        rows = [item for item in coverage if canonical_course(item.get("course")) == course]
        individual = sorted({str(item.get("student_name")) for item in rows if item.get("coverage_level") == "individual"})
        overall = sorted({str(item.get("student_name")) for item in rows if item.get("coverage_level") == "overall"})
        covered = set(individual) | set(overall)
        missing = sorted(expected - covered)
        cards.append({
            "course": course,
            "has_course": bool(course_sessions),
            "attendance_confirmed": attendance_confirmed if course_sessions else True,
            "expected_count": len(expected),
            "overall_count": len(overall),
            "individual_count": len(individual),
            "missing_count": len(missing),
            "teacher_names": sorted(teachers),
            "individual_students": individual,
            "overall_students": overall,
            "missing_students": missing,
            "absent_students": sorted(absent),
            "needs_record": bool(course_sessions) and (not rows or bool(missing)),
        })
    cycle_start, cycle_end = current_teaching_cycle(target)
    evidence_gaps = []
    for student_name in _active_summer_students(store):
        evidence = student_course_evidence(store, student_name=student_name, start=cycle_start, end=cycle_end)
        if evidence["individual_count"] < 1:
            evidence_gaps.append({
                "student_name": student_name,
                "overall_count": evidence["overall_count"],
                "individual_count": evidence["individual_count"],
                "missing_courses": evidence["missing_courses"],
                "reason": "本周期尚缺个别记录" if evidence["overall_count"] else "本周期证据不足",
            })
    missing_cards = [card for card in cards if card["needs_record"]]
    teacher_counts = Counter(name for card in missing_cards for name in card["teacher_names"])
    manager_actions = []
    for card in missing_cards:
        manager_actions.append({
            "priority": "A" if card["missing_count"] else "B",
            "title": f"提醒{('、'.join(card['teacher_names']) or '负责老师')}补{card['course']}记录",
            "detail": f"应覆盖{card['expected_count']}人，未覆盖{card['missing_count']}人。",
            "course": card["course"],
            "message_template": f"今天{card['course']}课还缺记录，请课后补一段整体情况和2-3名个别学生表现，不用逐个写。",
        })
    return {
        "program_id": SUMMER_PROGRAM_ID,
        "date": target.isoformat(),
        "is_teaching_day": target.weekday() in {2, 3, 4, 5, 6},
        "courses": cards,
        "summary": {
            "scheduled_course_count": sum(1 for card in cards if card["has_course"]),
            "recorded_course_count": sum(1 for card in cards if card["has_course"] and not card["needs_record"]),
            "missing_course_count": len(missing_cards),
            "evidence_gap_student_count": len(evidence_gaps),
        },
        "teacher_followup_counts": dict(teacher_counts),
        "evidence_gap_students": evidence_gaps,
        "manager_actions": manager_actions,
        "empty_state": not schedules,
    }


def teacher_recording_reminder(
    store: TuoguanStore,
    *,
    teacher_userid: str,
    teacher_name: str = "",
    now: datetime | None = None,
    after_class: bool = False,
) -> dict[str, Any]:
    timestamp = now or datetime.now()
    sessions = scheduled_sessions(store, on_date=timestamp.date(), teacher_userid=teacher_userid)
    coverage = summer_course_coverage_dashboard(store, timestamp)
    cards = [card for card in coverage["courses"] if teacher_name in card["teacher_names"] or teacher_userid in card["teacher_names"]]
    if not sessions:
        return {"due": False, "message": "今天没有已确认的暑假班课程，不需要补课程记录。", "sessions": []}
    course_labels = "、".join(dict.fromkeys(canonical_course(item.get("course")) for item in sessions))
    present_students: list[str] = []
    for session in sessions:
        present_students.extend(session_attendance(store, session).get("present") or [])
    start, end = current_teaching_cycle(timestamp.date())
    ranked = sorted(
        set(present_students),
        key=lambda name: student_course_evidence(store, student_name=name, start=start, end=end)["individual_count"],
    )
    focus = ranked[:3]
    missing = any(card.get("needs_record") for card in cards) if cards else after_class
    lead = "课后还没有完整记录，请补一段即可。" if after_class and missing else "课后不用逐个孩子写。"
    message = (
        f"{teacher_name or '老师'}，今天你有{course_labels}课。{lead}"
        "发一段课程整体情况，再点名2-3名有明显表现的孩子即可。"
    )
    if focus:
        message += f"\n今天建议重点关注：{'、'.join(focus)}。"
    message += f"\n记录示例：今天{course_labels}课整体……，个别{focus[0] if focus else '某同学'}……，另一位同学……。"
    return {"due": True, "message": message, "sessions": sessions, "focus_students": focus, "needs_followup": missing}


def lesson_recording_help(text: str) -> str:
    course = next((name for name in DISPLAY_COURSES if name in str(text or "")), "这节")
    return (
        f"不用逐个孩子写。请用一段话说明{course}课的整体情况，再点名2-3名有明显表现的孩子。\n"
        f"可以这样说：今天一二年级{course}课整体……，大部分孩子……。张三……，李四……，王五……。\n"
        "请带上年级组、课程、整体表现和点名孩子；系统会按课程表和出勤名单计算覆盖，缺勤孩子不会被覆盖。"
    )
