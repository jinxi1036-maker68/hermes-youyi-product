"""Deterministic operating analytics for tutoring-center data."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Iterable

from .store import TuoguanStore


_CLOSED_STATUSES = {"completed", "cancelled", "closed", "done", "closed_by_admin", "completed_by_admin"}
_RISK_ORDER = {"high": 0, "medium": 1, "low": 2, "unknown": 3}


def _parse_datetime(value: Any) -> datetime | None:
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
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def build_student_business_signals(
    store: TuoguanStore,
    *,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Compute explainable signals without overwriting user-entered facts."""
    timestamp = now or datetime.now().astimezone()
    students = store.read_json("students.json", {})
    records = store.read_json("records.json", [])
    tasks = store.load_tasks()
    if not isinstance(students, dict):
        students = {}
    if not isinstance(records, list):
        records = []

    records_by_student: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if isinstance(record, dict):
            records_by_student[str(record.get("student_name") or "")].append(record)
    tasks_by_student: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for task in tasks:
        if (
            isinstance(task, dict)
            and str(task.get("status") or "") not in _CLOSED_STATUSES
        ):
            tasks_by_student[str(task.get("student_name") or "")].append(task)

    seven_days_ago = timestamp - timedelta(days=7)
    signals: dict[str, dict[str, Any]] = {}
    for name, profile in students.items():
        if not isinstance(profile, dict):
            continue
        student_records = records_by_student.get(str(name), [])
        student_tasks = tasks_by_student.get(str(name), [])
        dated_records = []
        for record in student_records:
            created = _parse_datetime(record.get("created_at") or record.get("timestamp"))
            if created is None:
                continue
            if created.tzinfo is None and timestamp.tzinfo is not None:
                created = created.replace(tzinfo=timestamp.tzinfo)
            dated_records.append(created)
        latest = max(dated_records, default=None)
        recent_count = sum(created >= seven_days_ago for created in dated_records)

        renewal_reasons: list[str] = []
        renewal_level = "unknown"
        renewal_status = str(profile.get("renewal_status") or "unknown")
        due = _parse_datetime(profile.get("renewal_due_date"))
        if due is not None:
            if due.tzinfo is None and timestamp.tzinfo is not None:
                due = due.replace(tzinfo=timestamp.tzinfo)
            days_to_due = (due.date() - timestamp.date()).days
        else:
            days_to_due = None
        renewal_tasks = [
            task for task in student_tasks if task.get("type") == "renewal_risk"
        ]
        if renewal_status == "declined":
            renewal_level = "high"
            renewal_reasons.append("人工记录为不续费")
        if renewal_tasks:
            renewal_level = "high"
            renewal_reasons.append("存在未闭环续费风险任务")
        if days_to_due is not None and days_to_due < 0 and renewal_status != "renewed":
            renewal_level = "high"
            renewal_reasons.append("续费日期已过且未记录续费完成")
        elif (
            days_to_due is not None
            and days_to_due <= 30
            and renewal_level != "high"
            and renewal_status != "renewed"
        ):
            renewal_level = "medium"
            renewal_reasons.append("30天内到续费日期")
        elif renewal_status == "communicating" and renewal_level == "unknown":
            renewal_level = "medium"
            renewal_reasons.append("人工记录为沟通中")
        elif renewal_status == "likely" and renewal_level == "unknown":
            renewal_level = "low"
            renewal_reasons.append("人工记录为较大概率续费")
        elif renewal_status in {"renewed", "not_due"} and renewal_level == "unknown":
            renewal_level = "low"
            renewal_reasons.append("人工记录为已续费或暂未到期")

        relationship_reasons: list[str] = []
        relationship_level = {
            "at_risk": "high",
            "concerned": "medium",
            "stable": "low",
        }.get(str(profile.get("relationship_temperature") or ""), "unknown")
        if relationship_level != "unknown":
            relationship_reasons.append("来自人工记录的家长关系状态")
        if any(
            task.get("type") in {"parent_complaint", "safety_incident"}
            for task in student_tasks
        ):
            relationship_level = "high"
            relationship_reasons.append("存在投诉或安全事件未闭环")
        elif (
            any(task.get("type") == "parent_anxiety" for task in student_tasks)
            and relationship_level != "high"
        ):
            relationship_level = "medium"
            relationship_reasons.append("存在家长焦虑沟通任务")

        signals[str(name)] = {
            "renewal_risk": {
                "level": renewal_level,
                "reasons": renewal_reasons,
                "days_to_due": days_to_due,
            },
            "relationship_risk": {
                "level": relationship_level,
                "reasons": relationship_reasons,
            },
            "record_coverage": {
                "status": "current" if recent_count else "stale",
                "records_last_7_days": recent_count,
                "last_record_at": latest.isoformat() if latest else "",
            },
            "open_task_count": len(student_tasks),
        }
    return signals


def refresh_student_business_signals(
    store: TuoguanStore,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    timestamp = now or datetime.now().astimezone()
    students = store.read_json("students.json", {})
    if not isinstance(students, dict):
        students = {}
    signals = build_student_business_signals(store, now=timestamp)
    changed = 0
    for name, signal in signals.items():
        profile = students.get(name)
        if not isinstance(profile, dict):
            continue
        if profile.get("business_signals") == signal:
            continue
        profile["business_signals"] = signal
        profile["business_signals_updated_at"] = timestamp.isoformat(timespec="seconds")
        changed += 1
    if changed:
        store.write_json("students.json", students)
    return {"students": len(signals), "changed": changed}


def build_business_overview(
    store: TuoguanStore,
    *,
    student_names: Iterable[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = now or datetime.now().astimezone()
    students = store.read_json("students.json", {})
    records = store.read_json("records.json", [])
    growth_reports = store.read_json("growth_reports.json", [])
    tasks = store.load_tasks()
    if not isinstance(students, dict):
        students = {}
    if not isinstance(records, list):
        records = []
    if not isinstance(growth_reports, list):
        growth_reports = []

    allowed = set(student_names) if student_names is not None else set(students)
    scoped_students = {
        str(name): profile
        for name, profile in students.items()
        if str(name) in allowed and isinstance(profile, dict)
    }
    scoped_records = [
        record
        for record in records
        if isinstance(record, dict)
        and str(record.get("student_name") or "") in scoped_students
    ]
    scoped_tasks = [
        task
        for task in tasks
        if str(task.get("student_name") or "") in scoped_students
    ]

    seven_days_ago = timestamp - timedelta(days=7)
    latest_record: dict[str, datetime] = {}
    records_last_7_days = 0
    teacher_records: Counter[str] = Counter()
    for record in scoped_records:
        created = _parse_datetime(record.get("created_at") or record.get("timestamp"))
        if created is None:
            continue
        if created.tzinfo is None and timestamp.tzinfo is not None:
            created = created.replace(tzinfo=timestamp.tzinfo)
        student = str(record.get("student_name") or "")
        if student and (student not in latest_record or created > latest_record[student]):
            latest_record[student] = created
        if created >= seven_days_ago:
            records_last_7_days += 1
            teacher = str(
                record.get("teacher_userid")
                or record.get("sender_id")
                or record.get("teacher")
                or scoped_students.get(student, {}).get("teacher")
                or "未识别"
            )
            teacher_records[teacher] += 1

    students_by_teacher: dict[str, list[str]] = defaultdict(list)
    for name, profile in scoped_students.items():
        students_by_teacher[str(profile.get("teacher") or "未分配")].append(name)

    growth_cutoff = timestamp - timedelta(days=30)
    latest_approved_growth: dict[str, datetime] = {}
    for report in growth_reports:
        if not isinstance(report, dict) or report.get("status") != "approved":
            continue
        student = str(report.get("student_name") or "")
        if student not in scoped_students:
            continue
        approved_at = _parse_datetime(report.get("approved_at"))
        if approved_at is None:
            continue
        if approved_at.tzinfo is None and timestamp.tzinfo is not None:
            approved_at = approved_at.replace(tzinfo=timestamp.tzinfo)
        if (
            student not in latest_approved_growth
            or approved_at > latest_approved_growth[student]
        ):
            latest_approved_growth[student] = approved_at
    missing_growth_report_students = sorted(
        name
        for name in scoped_students
        if name not in latest_approved_growth
        or latest_approved_growth[name] < growth_cutoff
    )

    open_tasks = [
        task
        for task in scoped_tasks
        if str(task.get("status") or "") not in _CLOSED_STATUSES
    ]
    open_by_teacher = Counter(
        str(task.get("assignee_userid") or "未分配") for task in open_tasks
    )
    teacher_execution = []
    for teacher, names in sorted(students_by_teacher.items()):
        stale = [
            name
            for name in names
            if name not in latest_record or latest_record[name] < seven_days_ago
        ]
        missing_growth = [
            name for name in names if name in missing_growth_report_students
        ]
        teacher_execution.append(
            {
                "teacher_userid": teacher,
                "student_count": len(names),
                "records_last_7_days": teacher_records.get(teacher, 0),
                "students_without_record_last_7_days": len(stale),
                "open_task_count": open_by_teacher.get(teacher, 0),
                "stale_students": sorted(stale),
                "students_without_approved_growth_report_30_days": len(
                    missing_growth
                ),
                "missing_growth_report_students": sorted(missing_growth),
            }
        )

    renewal_status = Counter(
        str(profile.get("renewal_status") or "unknown")
        for profile in scoped_students.values()
    )
    missing_due_students = sorted(
        name
        for name, profile in scoped_students.items()
        if not str(profile.get("renewal_due_date") or "").strip()
    )
    missing_due_by_teacher: dict[str, list[str]] = defaultdict(list)
    for name in missing_due_students:
        teacher = str(scoped_students[name].get("teacher") or "未分配")
        missing_due_by_teacher[teacher].append(name)
    due_within_30_days = []
    overdue = []
    for name, profile in scoped_students.items():
        due = _parse_datetime(profile.get("renewal_due_date"))
        if due is None:
            continue
        if due.tzinfo is None and timestamp.tzinfo is not None:
            due = due.replace(tzinfo=timestamp.tzinfo)
        days = (due.date() - timestamp.date()).days
        item = {"student_name": name, "due_date": due.date().isoformat(), "days": days}
        if days < 0:
            overdue.append(item)
        elif days <= 30:
            due_within_30_days.append(item)

    relationship_temperature = Counter(
        str(profile.get("relationship_temperature") or "unknown")
        for profile in scoped_students.values()
    )
    signals = build_student_business_signals(store, now=timestamp)
    scoped_signals = {
        name: signal for name, signal in signals.items() if name in scoped_students
    }
    renewal_risk_counts = Counter(
        signal["renewal_risk"]["level"] for signal in scoped_signals.values()
    )
    relationship_risk_counts = Counter(
        signal["relationship_risk"]["level"] for signal in scoped_signals.values()
    )
    priority_students = sorted(
        (
            {
                "student_name": name,
                "renewal_risk": signal["renewal_risk"],
                "relationship_risk": signal["relationship_risk"],
                "record_coverage": signal["record_coverage"],
                "open_task_count": signal["open_task_count"],
            }
            for name, signal in scoped_signals.items()
            if signal["renewal_risk"]["level"] == "high"
            or signal["relationship_risk"]["level"] == "high"
        ),
        key=lambda item: (
            _RISK_ORDER[item["renewal_risk"]["level"]],
            _RISK_ORDER[item["relationship_risk"]["level"]],
            item["student_name"],
        ),
    )
    return {
        "generated_at": timestamp.isoformat(timespec="seconds"),
        "student_count": len(scoped_students),
        "record_count": len(scoped_records),
        "records_last_7_days": records_last_7_days,
        "students_without_record_last_7_days": sum(
            1
            for name in scoped_students
            if name not in latest_record or latest_record[name] < seven_days_ago
        ),
        "open_task_count": len(open_tasks),
        "open_tasks_by_level": dict(
            Counter(str(task.get("level") or "未分级") for task in open_tasks)
        ),
        "growth_reports": {
            "approved_within_30_days": (
                len(scoped_students) - len(missing_growth_report_students)
            ),
            "missing_approved_within_30_days": len(
                missing_growth_report_students
            ),
            "missing_students": missing_growth_report_students,
        },
        "renewal": {
            "status_counts": dict(renewal_status),
            "missing_due_date": len(missing_due_students),
            "missing_due_students": missing_due_students,
            "missing_due_by_teacher": {
                teacher: sorted(names)
                for teacher, names in sorted(missing_due_by_teacher.items())
            },
            "due_within_30_days": sorted(
                due_within_30_days, key=lambda item: item["days"]
            ),
            "overdue": sorted(overdue, key=lambda item: item["days"]),
        },
        "parent_relationship": {
            "temperature_counts": dict(relationship_temperature),
        },
        "risk_signals": {
            "renewal_counts": dict(renewal_risk_counts),
            "relationship_counts": dict(relationship_risk_counts),
            "priority_students": priority_students,
        },
        "teacher_execution": teacher_execution,
    }
