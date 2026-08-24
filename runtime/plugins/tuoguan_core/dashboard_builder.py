"""Build precomputed, role-scoped dashboard snapshots for tutoring operations."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import os
import uuid
from typing import Any

from .analytics import build_business_overview
from .digital_employee_state import (
    AUTONOMOUS_WORK_ITEM_FRESHNESS_HOURS,
    query_attention_threads,
    query_hermes_work_items,
    query_institution_work,
)
from .knowledge import list_pending_learning_candidates
from .operations_focus import active_operations_focus
from .payroll import build_payroll_snapshot, load_payroll_rules
from .record_evaluation import evaluate_monthly_record_performance, evaluate_record
from .store import TuoguanStore
from .summer_enrollment import summer_import_dashboard
from .summer_records import summer_lesson_dashboard
from .summer_reports import ensure_friday_summer_weekly_feedbacks, summer_report_dashboard
from .summer_course_coverage import summer_course_coverage_dashboard
from .models import UserIdentity
from .programs import (
    REGULAR_PROGRAM_ID,
    SUMMER_PROGRAM_ID,
    filter_program_items,
    filter_program_students,
    load_programs,
    program_status,
    student_program_ids,
    user_program_ids,
)
from .project_opportunities import query_project_opportunities
from .tasks import build_task_contract, closure_missing_fields, task_is_open


CACHE_FILE = "dashboard_cache.json"
RECORD_RULE_VERSION = "record_payroll_v3"
COUPON_RULE_VERSION = "growth_coupon_v2"
_POSITIVE_HINTS = ("进步", "认真", "主动", "完成好", "表扬", "优秀", "专注")
_ISSUE_HINTS = ("退步", "不认真", "拖拉", "冲突", "哭", "风险", "投诉", "安全")
_ACTIVE_STUDENT_STATUSES = {"", "active", "enrolled", "serving", "在读", "正常", "服务中"}
_INACTIVE_STUDENT_STATUSES = {"paused", "trial", "churned", "test", "停课", "暂停", "试托", "流失", "测试"}
_PRIORITY_TEMPERATURES = {"at_risk", "risk", "cold", "高风险", "风险", "偏冷", "流失风险"}
_PRIORITY_RENEWAL_STATUSES = {"declined", "risk", "at_risk", "hesitating", "流失风险", "拒绝", "犹豫"}
_PRIORITY_FOLLOWUP_TASK_TYPE = "parent_anxiety"
_PRIORITY_FOLLOWUP_TRIGGER = "priority_student_2day_followup"
_PERIODIC_SOURCE_TYPE = "periodic_operation"
_WEEKLY_SERVICE_TRIGGER = "weekly_service_quality"
_MONTHLY_PARENT_TRIGGER = "monthly_parent_communication"
_RENEWAL_30_TRIGGER = "renewal_30_evidence"
_RENEWAL_15_TRIGGER = "renewal_15_communication"
_RENEWAL_7_TRIGGER = "renewal_7_confirmation"
_ACTION_PRIORITY_RANK = {"S": 0, "A": 1, "B": 2, "C": 3}
_BUSINESS_TIMEZONE = timezone(timedelta(hours=8))


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _periodic_task_generation_enabled() -> bool:
    """Legacy fixed task generation is disabled by default.

    Hermes now works from boss goals and model-led follow-up. Dashboard refresh
    must remain read-mostly and must not repopulate a fixed task pool unless an
    operator explicitly opts back in for migration/debugging.
    """

    return os.getenv("HERMES_TUOGUAN_LEGACY_PERIODIC_TASKS_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def _performance_rules() -> dict[str, Any]:
    target_points = max(1.0, _float_env("HERMES_TUOGUAN_PERFORMANCE_TARGET_POINTS", 100.0))
    base_amount = max(0.0, _float_env("HERMES_TUOGUAN_PERFORMANCE_BASE_AMOUNT", 200.0))
    top_bonus = max(0.0, _float_env("HERMES_TUOGUAN_PERFORMANCE_TOP_BONUS", 50.0))
    return {
        "period": "自然月",
        "target_points": round(target_points, 2),
        "base_amount": round(base_amount, 2),
        "top_bonus_amount": round(top_bonus, 2),
        "required_weight": 0.75,
        "quality_weight": 0.25,
        "description": "记录绩效按月度必达项完成度为主、优质记录为辅预估，最终发放以老板确认规则为准",
    }


def _business_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(_BUSINESS_TIMEZONE).replace(tzinfo=None)


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _business_naive(parsed)


def _timestamp(item: dict[str, Any]) -> datetime | None:
    return _parse_dt(item.get("timestamp") or item.get("created_at") or item.get("updated_at"))


def _same_date(item: dict[str, Any], now: datetime) -> bool:
    timestamp = _timestamp(item)
    return timestamp is not None and timestamp.date() == now.date()


def _record_sort_key(item: dict[str, Any]) -> datetime:
    return _timestamp(item) or datetime.min


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _load_students(store: TuoguanStore) -> dict[str, dict[str, Any]]:
    raw = store.read_json("students.json", {})
    if not isinstance(raw, dict):
        return {}
    return {str(name): profile for name, profile in raw.items() if isinstance(profile, dict)}


def _load_records(store: TuoguanStore) -> list[dict[str, Any]]:
    raw = store.read_json("records.json", [])
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _read_jsonl(store: TuoguanStore, name: str) -> list[dict[str, Any]]:
    path = store.path_for(name)
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


def _student_teacher(profile: dict[str, Any]) -> str:
    return str(profile.get("teacher") or profile.get("teacher_id") or "").strip()


def _student_campus(profile: dict[str, Any]) -> str:
    return str(profile.get("campus_id") or profile.get("campus") or "未分校区").strip() or "未分校区"


def _record_student(record: dict[str, Any]) -> str:
    return str(record.get("student_name") or record.get("student") or "").strip()


def _record_teacher(record: dict[str, Any], students: dict[str, dict[str, Any]]) -> str:
    direct = str(record.get("teacher") or record.get("teacher_id") or record.get("created_by") or "").strip()
    if direct:
        return direct
    profile = students.get(_record_student(record), {})
    return _student_teacher(profile)


def _record_content(record: dict[str, Any]) -> str:
    return str(record.get("content") or record.get("source_text") or record.get("text") or "").strip()


def _record_tags(record: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    for key in ("tags", "record_types", "types"):
        value = record.get(key)
        if isinstance(value, list):
            tags.extend(str(item) for item in value if item)
        elif isinstance(value, str) and value.strip():
            tags.append(value.strip())
    category = str(record.get("category") or record.get("type") or "").strip()
    if category:
        tags.append(category)
    return tags


def _is_valid_record(record: dict[str, Any]) -> bool:
    return bool(_record_evaluation(record).get("accepted"))


def _is_quality_record(record: dict[str, Any]) -> bool:
    return str(_record_evaluation(record).get("quality_level") or "") in {"quality", "excellent"}


def _record_points(record: dict[str, Any]) -> int:
    return int(float(_record_evaluation(record).get("score") or 0))


def _record_evaluation(record: dict[str, Any]) -> dict[str, Any]:
    evaluation = record.get("record_evaluation")
    return evaluation if isinstance(evaluation, dict) and evaluation else evaluate_record(record)


def _record_improvement_tip(record: dict[str, Any]) -> str:
    evaluation = _record_evaluation(record)
    reason_codes = {str(item) for item in evaluation.get("reason_codes") or []}
    if bool(evaluation.get("payroll_eligible")):
        if str(evaluation.get("quality_level") or "") in {"quality", "excellent"}:
            return "这条已经是优质记录，可以作为以后记录的参考。"
        return "这条已计入绩效，后续补上结果变化会更接近优质记录。"
    if "duplicate_same_day_type" in reason_codes or "similar_duplicate" in reason_codes:
        return "这类同日相似记录已入档，绩效不会重复累计。"
    if "too_generic" in reason_codes:
        return "补充具体场景、孩子行为、老师处理和下一步，就能更接近有效记录。"
    if "missing_teacher_action" in reason_codes:
        return "补一句老师当时如何提醒、引导、沟通或后续跟进。"
    if "missing_next_step" in reason_codes:
        return "补一句后续观察、家校配合或明天继续怎么做。"
    if "missing_closure" in reason_codes:
        return "风险类记录要补充处理结果、家长是否知情和后续观察。"
    if not bool(evaluation.get("accepted")):
        return "先补清楚学生、时间、老师和事情经过，系统才能稳定入档。"
    return "这条已入档，但还缺少可计绩效的细节。"


def _student_status(profile: dict[str, Any]) -> str:
    return str(profile.get("status") or profile.get("student_status") or profile.get("service_status") or "").strip()


def _is_active_payroll_student(profile: dict[str, Any]) -> bool:
    status = _student_status(profile)
    if status in _INACTIVE_STUDENT_STATUSES:
        return False
    return status in _ACTIVE_STUDENT_STATUSES or not status


def _is_priority_student(profile: dict[str, Any]) -> bool:
    if not _is_active_payroll_student(profile):
        return False
    temperature = str(profile.get("relationship_temperature") or "").strip().lower()
    renewal = str(profile.get("renewal_status") or "").strip().lower()
    focus = str(profile.get("focus_status") or profile.get("priority_status") or profile.get("重点状态") or "").strip().lower()
    tags = profile.get("tags")
    tag_text = " ".join(str(item) for item in tags if item) if isinstance(tags, list) else str(tags or "")
    return (
        temperature in _PRIORITY_TEMPERATURES
        or renewal in _PRIORITY_RENEWAL_STATUSES
        or focus in {"重点", "重点学生", "priority", "key", "focus", "risk"}
        or any(word in tag_text for word in ("重点", "风险", "续费", "投诉"))
    )


def _student_last_valid_followup_at(
    records: list[dict[str, Any]],
    students: dict[str, dict[str, Any]],
    student_name: str,
) -> datetime | None:
    latest: datetime | None = None
    for record in records:
        if _record_student(record) != student_name:
            continue
        evaluation = _record_evaluation(record)
        if not bool(evaluation.get("payroll_eligible")):
            evaluation = evaluate_record(record, previous_records=[])
        if not bool(evaluation.get("payroll_eligible")):
            continue
        bucket = str(evaluation.get("required_bucket") or "")
        if bucket not in {"growth_observation", "parent_communication", "risk_closure", "safety_closure"}:
            continue
        timestamp = _timestamp(record)
        if timestamp is not None and (latest is None or timestamp > latest):
            latest = timestamp
    return latest


def _priority_followup_reason(profile: dict[str, Any]) -> str:
    temperature = str(profile.get("relationship_temperature") or "").strip()
    renewal = str(profile.get("renewal_status") or "").strip()
    if renewal:
        return f"续费状态：{renewal}"
    if temperature:
        return f"关系温度：{temperature}"
    focus = str(profile.get("focus_status") or profile.get("priority_status") or profile.get("重点状态") or "").strip()
    if focus:
        return f"重点状态：{focus}"
    return "重点学生需要持续跟进"


def _priority_followup_due(now: datetime) -> str:
    due = now.replace(hour=20, minute=0, second=0, microsecond=0)
    if due <= now:
        due = now + timedelta(hours=4)
    return due.isoformat(timespec="seconds")


def _task_due_at(now: datetime, *, days: int = 0, hour: int = 20) -> str:
    due = (now + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
    if due <= now:
        due = now + timedelta(hours=4)
    return due.isoformat(timespec="seconds")


def _has_open_task_for_trigger(
    open_tasks: list[dict[str, Any]],
    *,
    student_name: str,
    trigger: str,
) -> bool:
    return any(
        _task_student(task) == student_name
        and str(task.get("trigger_reason") or "") == trigger
        for task in open_tasks
    )


def _has_task_for_trigger_since(
    tasks: list[dict[str, Any]],
    *,
    student_name: str,
    trigger: str,
    since: datetime,
) -> bool:
    for task in tasks:
        if _task_student(task) != student_name:
            continue
        if str(task.get("trigger_reason") or "") != trigger:
            continue
        if str(task.get("status") or "") == "cancelled":
            continue
        task_time = _parse_dt(task.get("completed_at")) or _parse_dt(task.get("created_at")) or _parse_dt(task.get("updated_at"))
        if task_time is None:
            continue
        if task_time >= since:
            return True
    return False


def _create_periodic_task(
    tasks: list[dict[str, Any]],
    open_tasks: list[dict[str, Any]],
    created: list[dict[str, Any]],
    *,
    now: datetime,
    student_name: str,
    profile: dict[str, Any],
    title: str,
    level: str,
    task_type: str,
    trigger: str,
    source_label: str,
    source_text: str,
    due_at: str,
    payroll_impact: bool,
    close_requirement: str,
    periodic_stage: str,
) -> None:
    if _has_open_task_for_trigger(open_tasks, student_name=student_name, trigger=trigger):
        return
    stamp = now.isoformat(timespec="seconds")
    task = {
        "id": f"task_periodic_{now.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}",
        "title": title,
        "type": task_type,
        "level": level,
        "status": "pending",
        "student_name": student_name,
        "campus_id": _student_campus(profile),
        "program_id": sorted(student_program_ids(profile))[0],
        "assignee_userid": _student_teacher(profile),
        "assignee_role": "teacher",
        "source_text": source_text,
        "source_type": _PERIODIC_SOURCE_TYPE,
        "source_label": source_label,
        "trigger_reason": trigger,
        "periodic_stage": periodic_stage,
        "payroll_impact": payroll_impact,
        "closure_requirement": close_requirement,
        "due_at": due_at,
        "next_remind_at": (now + timedelta(hours=6)).isoformat(timespec="seconds"),
        "defer_count": 0,
        "escalation_count": 0,
        "coach_stage": "waiting_start",
        "evidence_summary": "",
        "closure_summary": "",
        "created_at": stamp,
        "updated_at": stamp,
    }
    task["task_contract"] = build_task_contract(
        title=title,
        source_text=source_text,
        student_name=student_name,
        due_at=due_at,
        evidence_requirement=close_requirement,
        business_goal=source_label,
        assignee_user_id=_student_teacher(profile),
        assigned_by_role="system",
        known_facts=[source_text],
    )
    tasks.append(task)
    open_tasks.append(task)
    created.append(task)


def ensure_priority_followup_tasks(
    store: TuoguanStore,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    if not _periodic_task_generation_enabled():
        return []
    timestamp = now or datetime.now()
    students = _load_students(store)
    records = _load_records(store)
    tasks = store.load_tasks()
    cutoff = timestamp - timedelta(days=2)
    open_tasks = [task for task in tasks if _task_open(task)]
    changed = False
    created: list[dict[str, Any]] = []
    for student_name, profile in sorted(students.items()):
        teacher_id = _student_teacher(profile)
        if not teacher_id or not _is_priority_student(profile):
            continue
        if not any(program_status(store, program_id) == "active" for program_id in student_program_ids(profile)):
            continue
        last_followup = _student_last_valid_followup_at(records, students, student_name)
        if last_followup is not None and last_followup >= cutoff:
            continue
        has_open_priority_or_risk = any(
            _task_student(task) == student_name
            and (
                str(task.get("trigger_reason") or "") == _PRIORITY_FOLLOWUP_TRIGGER
                or str(task.get("level") or "") in {"S", "A"}
                or str(task.get("type") or "") in {"safety_incident", "parent_complaint", "renewal_risk"}
            )
            for task in open_tasks
        )
        if has_open_priority_or_risk:
            continue
        reason = _priority_followup_reason(profile)
        stamp = timestamp.isoformat(timespec="seconds")
        task = {
            "id": f"task_priority_{timestamp.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}",
            "title": f"{student_name}重点学生跟进",
            "type": _PRIORITY_FOLLOWUP_TASK_TYPE,
            "level": "A",
            "status": "pending",
            "student_name": student_name,
            "campus_id": _student_campus(profile),
            "program_id": sorted(student_program_ids(profile))[0],
            "assignee_userid": teacher_id,
            "assignee_role": "teacher",
            "source_text": f"{student_name}已超过2天没有有效跟进记录。",
            "source_type": "system_risk",
            "source_label": "系统重点学生",
            "trigger_reason": _PRIORITY_FOLLOWUP_TRIGGER,
            "risk_reason": reason,
            "due_at": _priority_followup_due(timestamp),
            "next_remind_at": (timestamp + timedelta(hours=2)).isoformat(timespec="seconds"),
            "defer_count": 0,
            "escalation_count": 0,
            "coach_stage": "waiting_start",
            "evidence_summary": "",
            "closure_summary": "",
            "created_at": stamp,
            "updated_at": stamp,
        }
        task["task_contract"] = build_task_contract(
            title=str(task["title"]),
            source_text=str(task["source_text"]),
            student_name=student_name,
            due_at=str(task["due_at"]),
            evidence_requirement=reason,
            business_goal="及时识别并推进重点学生服务风险",
            assignee_user_id=teacher_id,
            assigned_by_role="system",
            known_facts=[str(task["source_text"]), reason],
        )
        tasks.append(task)
        open_tasks.append(task)
        created.append(task)
        changed = True
    if changed:
        store.append_tasks_verified(created)
    return created


def _requirement_by_bucket(
    monthly: dict[str, Any],
    bucket: str,
) -> dict[str, dict[str, Any]]:
    requirements = _as_dict(monthly.get("requirements"))
    items = requirements.get(bucket) or []
    result: dict[str, dict[str, Any]] = {}
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and str(item.get("student_name") or ""):
                result[str(item.get("student_name"))] = item
    return result


def ensure_periodic_operation_tasks(
    store: TuoguanStore,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    timestamp = now or datetime.now()
    students = _load_students(store)
    records = _load_records(store)
    tasks = store.load_tasks()
    open_tasks = [task for task in tasks if _task_open(task)]
    payroll_rules = load_payroll_rules(store)
    teacher_ids = {
        _student_teacher(profile)
        for profile in students.values()
        if _student_teacher(profile)
    }
    monthly_by_teacher = {
        teacher_id: evaluate_monthly_record_performance(
            records=records,
            students=students,
            teacher_id=teacher_id,
            now=timestamp,
            rules=payroll_rules,
        )
        for teacher_id in teacher_ids
    }
    created: list[dict[str, Any]] = []
    weekly_created_by_teacher: Counter[str] = Counter()
    week_start = timestamp - timedelta(days=7)
    is_weekly_check_day = timestamp.weekday() == 0
    is_parent_check_day = timestamp.day in {10, 20} or timestamp.day >= 25
    for student_name, profile in sorted(students.items()):
        teacher_id = _student_teacher(profile)
        if not teacher_id or not _is_active_payroll_student(profile):
            continue
        profile_programs = student_program_ids(profile)
        if REGULAR_PROGRAM_ID not in profile_programs or program_status(store, REGULAR_PROGRAM_ID) != "active":
            continue
        monthly = monthly_by_teacher.get(teacher_id, {})
        growth_item = _requirement_by_bucket(monthly, "growth_observation").get(student_name, {})
        parent_item = _requirement_by_bucket(monthly, "parent_communication").get(student_name, {})
        week_followup = _student_last_valid_followup_at(records, students, student_name)
        if (
            is_weekly_check_day
            and (
            week_followup is None or week_followup < week_start
            )
            and not _has_open_task_for_trigger(open_tasks, student_name=student_name, trigger=_WEEKLY_SERVICE_TRIGGER)
            and not _has_task_for_trigger_since(tasks, student_name=student_name, trigger=_WEEKLY_SERVICE_TRIGGER, since=week_start)
        ):
            if weekly_created_by_teacher[teacher_id] < 3:
                _create_periodic_task(
                    tasks,
                    open_tasks,
                    created,
                    now=timestamp,
                    student_name=student_name,
                    profile=profile,
                    title=f"{student_name}本周成长观察",
                    level="B",
                    task_type="student_daily",
                    trigger=_WEEKLY_SERVICE_TRIGGER,
                    source_label="每周服务质量",
                    source_text=f"{student_name}本周缺少有效成长观察或服务跟进。",
                    due_at=_task_due_at(timestamp, days=1),
                    payroll_impact=True,
                    close_requirement="补一条真实成长观察，写清场景、孩子表现、老师动作和下一步。",
                    periodic_stage="weekly_service_quality",
                )
                weekly_created_by_teacher[teacher_id] += 1
        if (
            is_parent_check_day
            and parent_item
            and not parent_item.get("done")
            and not _has_open_task_for_trigger(open_tasks, student_name=student_name, trigger=_MONTHLY_PARENT_TRIGGER)
        ):
            _create_periodic_task(
                tasks,
                open_tasks,
                created,
                now=timestamp,
                student_name=student_name,
                profile=profile,
                title=f"{student_name}本月家长沟通",
                level="A" if timestamp.day >= 20 else "B",
                task_type="parent_anxiety",
                trigger=_MONTHLY_PARENT_TRIGGER,
                source_label="每月家长沟通",
                source_text=f"{student_name}本月还没有有效家长沟通记录。",
                due_at=_task_due_at(timestamp, days=2 if timestamp.day < 20 else 1),
                payroll_impact=True,
                close_requirement="完成一次有效家长沟通，写清沟通对象、事实、建议和后续安排。",
                periodic_stage="monthly_parent_communication",
            )
        renewal_due = _parse_dt(profile.get("renewal_due_date"))
        if renewal_due is None:
            continue
        days_to_due = (renewal_due.date() - timestamp.date()).days
        renewal_status = str(profile.get("renewal_status") or "").strip().lower()
        renewal_done = renewal_status in {"renewed", "confirmed", "已续费", "已确认", "续费完成"}
        if renewal_done or days_to_due < 0:
            continue
        if 16 <= days_to_due <= 30 and growth_item and not growth_item.get("done"):
            _create_periodic_task(
                tasks,
                open_tasks,
                created,
                now=timestamp,
                student_name=student_name,
                profile=profile,
                title=f"{student_name}续费前成长证据",
                level="B",
                task_type="student_daily",
                trigger=_RENEWAL_30_TRIGGER,
                source_label="续费前30天",
                source_text=f"{student_name}距离续费约{days_to_due}天，成长证据不足。",
                due_at=_task_due_at(timestamp, days=3),
                payroll_impact=True,
                close_requirement="补齐近期成长证据，后续用于续费沟通和家长报告。",
                periodic_stage="renewal_30_evidence",
            )
        elif 8 <= days_to_due <= 15 and parent_item and not parent_item.get("done"):
            _create_periodic_task(
                tasks,
                open_tasks,
                created,
                now=timestamp,
                student_name=student_name,
                profile=profile,
                title=f"{student_name}续费沟通准备",
                level="A",
                task_type="parent_anxiety",
                trigger=_RENEWAL_15_TRIGGER,
                source_label="续费前15天",
                source_text=f"{student_name}距离续费约{days_to_due}天，缺少有效家长沟通。",
                due_at=_task_due_at(timestamp, days=1),
                payroll_impact=True,
                close_requirement="准备并完成续费前家长沟通，写清成长证据、家长态度和下一步。",
                periodic_stage="renewal_15_communication",
            )
        elif 0 <= days_to_due <= 7:
            _create_periodic_task(
                tasks,
                open_tasks,
                created,
                now=timestamp,
                student_name=student_name,
                profile=profile,
                title=f"{student_name}续费确认",
                level="A",
                task_type="renewal_risk",
                trigger=_RENEWAL_7_TRIGGER,
                source_label="续费前7天",
                source_text=f"{student_name}距离续费约{days_to_due}天，需确认续费状态。",
                due_at=_task_due_at(timestamp, days=1, hour=18),
                payroll_impact=True,
                close_requirement="确认家长态度、续费结果和下一步，需要店长推动时同步店长。",
                periodic_stage="renewal_7_confirmation",
            )
    if created:
        store.append_tasks_verified(created)
    return created


def _teacher_performance(
    month_records: list[dict[str, Any]],
    *,
    records: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    students: dict[str, dict[str, Any]],
    teacher_id: str,
    now: datetime,
    payroll_rules: dict[str, Any],
) -> dict[str, Any]:
    performance_rules = _performance_rules()
    month_start = _month_start(now)
    requirement_records = records + _task_growth_evidence_records(
        tasks,
        teacher_id=teacher_id,
        student_names={
            name
            for name, profile in students.items()
            if _student_teacher(profile) == teacher_id and _is_active_payroll_student(profile)
        },
        since=month_start,
    )
    monthly = evaluate_monthly_record_performance(
        records=requirement_records,
        students=students,
        teacher_id=teacher_id,
        now=now,
        rules=payroll_rules,
    )
    target = float(monthly.get("quality_score_target") or performance_rules["target_points"])
    base_amount = float(performance_rules["base_amount"])
    record_rate = float(monthly.get("completion_rate") or 0)
    month_points = sum(float(_record_evaluation(record).get("score") or 0) for record in month_records)
    month_valid = sum(1 for record in month_records if _is_valid_record(record))
    month_quality = sum(1 for record in month_records if _is_quality_record(record))
    progress_rate = int(monthly.get("completion_percent") or 0)
    estimated_base = round(base_amount * record_rate, 2)
    points_to_full = max(0, round(target - month_points, 2))
    missing_count = len(monthly.get("missing_required") or [])
    if progress_rate >= 100:
        status_text = "本月记录绩效已达标"
    elif missing_count:
        status_text = f"本月还有{missing_count}个记录必达项未完成"
    elif month_points > 0:
        status_text = f"距离拿满记录质量分还差{points_to_full:g}分"
    else:
        status_text = "本月还没有可计入绩效的记录"
    return {
        "rules": performance_rules,
        "month_points": month_points,
        "month_records": len(month_records),
        "month_valid_records": month_valid,
        "month_quality_records": month_quality,
        "target_points": target,
        "progress_rate": progress_rate,
        "base_amount": base_amount,
        "estimated_base_amount": estimated_base,
        "rank": None,
        "rank_bonus_amount": 0.0,
        "estimated_total_amount": estimated_base,
        "points_to_full_base": points_to_full,
        "status_text": status_text,
        "payroll_rule": "必达项完成度为主，优质记录加分；低质量记录入档但不计绩效",
        "monthly_requirements": monthly,
    }


def _task_growth_evidence_records(
    tasks: list[dict[str, Any]],
    *,
    teacher_id: str,
    student_names: set[str],
    since: datetime,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for task in tasks:
        if str(task.get("type") or "") != "student_daily":
            continue
        if str(task.get("status") or "") not in {"completed", "completed_by_admin"}:
            continue
        if str(task.get("assignee_userid") or "") != teacher_id:
            continue
        student_name = _task_student(task)
        if student_name not in student_names:
            continue
        timestamp = _parse_dt(task.get("completed_at")) or _parse_dt(task.get("updated_at"))
        if timestamp is None or timestamp < since:
            continue
        evidence = str(task.get("closure_summary") or task.get("evidence_summary") or "").strip()
        if not evidence:
            continue
        records.append(
            {
                "id": f"task_evidence::{task.get('id')}",
                "student_name": student_name,
                "teacher": teacher_id,
                "timestamp": timestamp.isoformat(timespec="seconds"),
                "content": evidence,
                "record_types": ["student_daily", "task_closure_evidence"],
                "source_meta": {
                    "source": "completed_growth_task",
                    "task_id": str(task.get("id") or ""),
                    "task_title": str(task.get("title") or ""),
                },
                "record_evaluation": {
                    "schema_version": 1,
                    "accepted": True,
                    "payroll_eligible": True,
                    "quality_level": "valid",
                    "score": 0.0,
                    "category": "growth_observation",
                    "required_bucket": "growth_observation",
                    "reason_codes": ["task_evidence_counts_for_requirement"],
                    "reason_texts": ["成长观察任务闭环证据计入覆盖，不重复计记录质量分"],
                },
            }
        )
    return records


def _apply_performance_rankings(teacher_dashboards: dict[str, dict[str, Any]]) -> None:
    ranked = sorted(
        teacher_dashboards.values(),
        key=lambda item: (
            int((item.get("performance") or {}).get("month_points") or 0),
            int((item.get("performance") or {}).get("month_quality_records") or 0),
            int((item.get("performance") or {}).get("month_records") or 0),
        ),
        reverse=True,
    )
    for index, dashboard in enumerate(ranked, start=1):
        performance = dashboard.get("performance")
        if not isinstance(performance, dict):
            continue
        performance["rank"] = index
        bonus = 0.0
        if index == 1 and int(performance.get("month_points") or 0) > 0:
            bonus = float((performance.get("rules") or {}).get("top_bonus_amount") or 0)
        performance["rank_bonus_amount"] = round(bonus, 2)
        performance["estimated_total_amount"] = round(float(performance.get("estimated_base_amount") or 0) + bonus, 2)


def _profile_completion(profile: dict[str, Any]) -> int:
    fields = (
        "phone",
        "grade",
        "school",
        "class",
        "teacher",
        "campus_id",
        "renewal_due_date",
        "relationship_temperature",
    )
    filled = sum(1 for field in fields if profile.get(field) not in (None, "", [], {}))
    return round(filled / len(fields) * 100)


def _task_open(task: dict[str, Any]) -> bool:
    return task_is_open(task)


def _task_freshness_state(task: dict[str, Any], now: datetime) -> str:
    """Classify an open task for display without changing its business status."""

    due_at = _parse_dt(task.get("due_at"))
    updated_at = _parse_dt(task.get("updated_at") or task.get("created_at"))
    if due_at is not None and due_at < now and now - due_at > timedelta(hours=36):
        return "overdue"
    if due_at is not None and due_at >= now:
        return "current"
    if updated_at is not None and now - updated_at <= timedelta(hours=36):
        return "current"
    return "stale"


def _task_is_today_action(task: dict[str, Any], now: datetime) -> bool:
    """Limit the teacher's daily prompt to work that is actually current today."""

    due_at = _parse_dt(task.get("due_at"))
    if due_at is not None:
        if due_at.date() == now.date():
            return True
        if due_at < now and now - due_at <= timedelta(hours=36):
            return True
    for key in ("updated_at", "created_at"):
        when = _parse_dt(task.get(key))
        if when is not None and when.date() == now.date():
            return True
    return False


def _task_display_sort_key(task: dict[str, Any], now: datetime) -> tuple[int, int, str, str]:
    freshness_rank = {"current": 0, "overdue": 1, "stale": 2}
    return (
        freshness_rank.get(_task_freshness_state(task, now), 3),
        _ACTION_PRIORITY_RANK.get(_task_action_priority(task), 9),
        str(task.get("due_at") or "9999-12-31"),
        str(task.get("updated_at") or task.get("created_at") or ""),
    )


def _task_action_priority(task: dict[str, Any]) -> str:
    level = str(task.get("level") or "C").upper()
    return level if level in _ACTION_PRIORITY_RANK else "C"


def _action_sort_key(action: dict[str, Any]) -> tuple[int, str, str]:
    type_rank = {
        "task_closure": "0",
        "monthly_requirement": "1",
        "coverage": "2",
        "keep_quality": "3",
    }.get(str(action.get("type") or ""), "9")
    return (
        _ACTION_PRIORITY_RANK.get(str(action.get("priority") or "C"), 9),
        type_rank,
        str(action.get("due_at") or action.get("title") or ""),
    )


def _after(value: datetime | None, floor: datetime) -> bool:
    return value is not None and value >= floor


def _task_student(task: dict[str, Any]) -> str:
    return str(task.get("student_name") or task.get("student") or "").strip()


def _task_teacher(task: dict[str, Any], students: dict[str, dict[str, Any]]) -> str:
    direct = str(task.get("assignee_userid") or task.get("assignee") or task.get("owner") or task.get("teacher") or "").strip()
    if direct:
        return direct
    return _student_teacher(students.get(_task_student(task), {}))


def _manager_team_teacher_ids(store: TuoguanStore, manager_id: str) -> set[str]:
    rules = load_payroll_rules(store)
    people = _as_dict(rules.get("people"))
    profile = _as_dict(people.get(manager_id))
    return {str(item).strip() for item in profile.get("team_teacher_ids") or [] if str(item).strip()}


def _task_campus(task: dict[str, Any], students: dict[str, dict[str, Any]]) -> str:
    direct = str(task.get("campus_id") or "").strip()
    if direct:
        return direct
    return _student_campus(students.get(_task_student(task), {}))


def _teacher_names(store: TuoguanStore) -> dict[str, str]:
    mapping = store.read_json("teacher_wecom_map.json", {})
    if not isinstance(mapping, dict):
        return {}
    result: dict[str, str] = {}
    score_by_user: dict[str, int] = {}
    blocked = {"未分配", "优益托管", "执行校长"}

    def score(name: str) -> int:
        if not name or name in blocked:
            return -100
        if "?" in name or "\ufffd" in name:
            return -80
        value = 0
        if any("\u4e00" <= ch <= "\u9fff" for ch in name):
            value += 50
        if name.endswith("老师"):
            value += 20
        if 2 <= len(name) <= 6:
            value += 10
        return value

    for name, user_id in mapping.items():
        uid = str(user_id)
        label = str(name)
        item_score = score(label)
        if item_score > score_by_user.get(uid, -999):
            result[uid] = label
            score_by_user[uid] = item_score
    return result


def _manager_campuses(store: TuoguanStore) -> dict[str, set[str]]:
    staff = store.read_json("staff.json", {})
    if not isinstance(staff, dict):
        staff = {}
    result: dict[str, set[str]] = {}
    for user_id, profile in staff.items():
        if not isinstance(profile, dict):
            continue
        if str(profile.get("role") or "") != "manager":
            continue
        campuses = {str(item) for item in profile.get("campus_ids") or [] if str(item)}
        result[str(user_id)] = campuses
    whitelist = store.read_json("wecom_whitelist.json", {})
    if isinstance(whitelist, dict):
        for user_id in whitelist.get("summer_manager_ids") or []:
            if str(user_id):
                result[str(user_id)] = {SUMMER_PROGRAM_ID}
    return result


def _dashboard_identity(user_id: str, role: str, display_name: str = "") -> UserIdentity:
    return UserIdentity(
        platform="wecom",
        platform_user_id=user_id,
        canonical_user_id=user_id,
        person_name=display_name,
        role=role,
        approval_state="approved",
    )


def _sanitize_manager_dashboard(data: dict[str, Any], program_ids: set[str] | None) -> dict[str, Any]:
    data["program_scope"] = sorted(program_ids) if program_ids is not None else ["global"]
    if program_ids == {SUMMER_PROGRAM_ID}:
        data["program_role"] = "summer_manager"
        data["default_program_id"] = SUMMER_PROGRAM_ID
        data["visible_modules"] = ["今日运行", "课节记录", "孩子覆盖", "待处理事项", "安全关注", "反馈进度"]
        data["performance"] = {}
        data["payroll"] = {}
        data["renewal_funnel"] = {}
        data["rule_center"] = {}
        data["opportunities"] = []
        data["business_focus"] = {}
        data["learning"] = {}
        execution = _as_dict(data.get("execution"))
        execution["managers"] = []
        data["execution"] = execution
        summer = _as_dict(data.get("summer_import"))
        summer_summary = _as_dict(summer.get("summary"))
        summer_reports = _as_dict(data.get("summer_reports"))
        course_coverage = _as_dict(data.get("summer_course_coverage"))
        coverage_summary = _as_dict(course_coverage.get("summary"))
        summary = _as_dict(data.get("summary"))
        data["daily_brief"] = {
            "headline": "2026暑假班今日运行",
            "cards": [
                {"label": "今日记录", "value": summary.get("today_records", 0)},
                {"label": "今日课程", "value": coverage_summary.get("scheduled_course_count", 0)},
                {"label": "缺记录科目", "value": coverage_summary.get("missing_course_count", 0)},
                {"label": "未闭环任务", "value": summary.get("open_task_count", 0)},
                {"label": "安全关注", "value": summer_summary.get("safety_attention_count", 0)},
                {"label": "待审核反馈", "value": summer_reports.get("draft_count", 0)},
            ],
            "top_actions": (course_coverage.get("manager_actions") or [])[:5] or [
                {
                    "priority": "A" if int(summary.get("high_risk_count", 0) or 0) else "C",
                    "title": "先处理安全关注" if int(summary.get("high_risk_count", 0) or 0) else "保持暑假班日常巡检",
                    "detail": (
                        f"当前有{int(summary.get('high_risk_count', 0) or 0)}项风险事项，请优先确认孩子状态和处理进展。"
                        if int(summary.get("high_risk_count", 0) or 0)
                        else "今天重点看课节记录、孩子覆盖和待处理事项。"
                    ),
                    "source": "暑假班运行",
                }
            ],
        }
        data["denied_capabilities"] = ["payroll", "performance", "coupon", "renewal", "regular_history", "owner_operations", "owner_permissions"]
        data.pop("restricted_modules", None)
    elif program_ids == {REGULAR_PROGRAM_ID}:
        prior_actions = _as_list(_as_dict(data.get("daily_brief")).get("top_actions"))
        data["program_role"] = "regular_manager"
        data["default_program_id"] = REGULAR_PROGRAM_ID
        data["visible_modules"] = ["托管班历史", "托管班遗留任务", "托管班学生", "托管班记录", "冻结只读提示"]
        data["readonly_hint"] = "托管班已进入冻结只读状态；历史数据保留，新操作需明确项目归属。"
        data["performance"] = {}
        data["payroll"] = {}
        data["renewal_funnel"] = {}
        data["rule_center"] = {}
        data["opportunities"] = []
        data["business_focus"] = {}
        data["learning"] = {}
        execution = _as_dict(data.get("execution"))
        execution["managers"] = []
        data["execution"] = execution
        data["summer_import"] = {}
        data["summer_lessons"] = {}
        data["summer_reports"] = {}
        data["summer_course_coverage"] = {}
        summary = _as_dict(data.get("summary"))
        data["daily_brief"] = {
            "headline": "托管班冻结只读概况",
            "cards": [
                {"label": "托管学生", "value": summary.get("student_count", 0)},
                {"label": "历史记录", "value": summary.get("week_records", 0)},
                {"label": "遗留任务", "value": summary.get("open_task_count", 0)},
                {"label": "风险事项", "value": summary.get("high_risk_count", 0)},
            ],
            "top_actions": [
                item
                for item in prior_actions
                if not any(
                    word in json.dumps(item, ensure_ascii=False)
                    for word in ("工资", "绩效", "券", "续费", "经营")
                )
            ][:6],
        }
        data["denied_capabilities"] = ["summer_operations", "owner_dashboard", "owner_operations", "owner_permissions", "payroll", "coupon", "performance"]
        data.pop("restricted_modules", None)
    return data


def _recent_record_card(record: dict[str, Any]) -> dict[str, Any]:
    tags = _record_tags(record)
    content = _record_content(record)
    evaluation = _record_evaluation(record)
    use_cases = []
    text = " ".join(tags) + " " + content
    if _is_quality_record(record):
        use_cases.append("成长报告")
    elif any(word in text for word in ("进步", "认真", "主动", "优秀", "positive_progress")):
        use_cases.append("成长报告")
    if any(word in text for word in ("家长", "沟通", "提醒", "建议", "parent")):
        use_cases.append("家长沟通")
    if any(word in text for word in ("安全", "磕", "碰", "流血", "不舒服", "safety")):
        use_cases.append("安全闭环")
    if any(word in text for word in ("数学", "计算", "阅读", "英语", "书写", "习惯", "专注", "拖拉")):
        use_cases.append("学生标签")
    if not use_cases and _is_valid_record(record):
        use_cases.append("日常观察")
    return {
        "student_name": _record_student(record),
        "time": str(record.get("timestamp") or record.get("created_at") or ""),
        "summary": content[:80],
        "tags": tags[:4],
        "valid": _is_valid_record(record),
        "quality": _is_quality_record(record),
        "points": _record_points(record),
        "payroll_eligible": bool(evaluation.get("payroll_eligible")),
        "quality_level": str(evaluation.get("quality_level") or "low"),
        "required_bucket": str(evaluation.get("required_bucket") or ""),
        "payroll_reasons": list(evaluation.get("reason_texts") or []),
        "improvement_tip": _record_improvement_tip(record),
        "evaluation_version": str(evaluation.get("evaluation_version") or RECORD_RULE_VERSION),
        "use_cases": use_cases[:4],
        "record_type_label": _record_type_label(record),
    }


def _record_type_label(record: dict[str, Any]) -> str:
    types = set(record.get("record_types") or record.get("types") or [])
    if "safety_incident" in types:
        return "安全事件"
    if {"parent_anxiety", "parent_complaint"} & types:
        return "家长沟通"
    if "meal_care" in types:
        return "午餐/生活照护"
    if "nap_care" in types:
        return "午休/行为表现"
    if "life_care" in types:
        return "生活照护"
    if "behavior_observation" in types:
        return "行为表现"
    if {"academic_issue", "learning_habit"} & types:
        return "学习"
    return "日常观察"


def _teacher_record_review(month_records: list[dict[str, Any]]) -> dict[str, Any]:
    cards = [
        _recent_record_card(record)
        for record in sorted(
            month_records,
            key=lambda item: str(item.get("timestamp") or item.get("created_at") or ""),
            reverse=True,
        )
    ]
    effective = [card for card in cards if card.get("payroll_eligible")]
    not_payroll = [card for card in cards if not card.get("payroll_eligible")]
    quality = [
        card
        for card in cards
        if str(card.get("quality_level") or "") in {"quality", "excellent"}
    ]
    archived_only = [
        card
        for card in cards
        if card.get("valid") and not card.get("payroll_eligible")
    ]
    return {
        "counts": {
            "total": len(cards),
            "effective": len(effective),
            "not_payroll": len(not_payroll),
            "quality": len(quality),
            "archived_only": len(archived_only),
        },
        "effective_records": effective[:50],
        "not_payroll_records": not_payroll[:50],
        "quality_records": quality[:50],
        "archived_only_records": archived_only[:50],
    }


def _task_card(task: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    source_type = str(task.get("source_type") or "")
    source_label = str(task.get("source_label") or "")
    if not source_label:
        source_label = {
            "manual_assignment": "手动安排",
            "record_triggered": "记录触发",
            "system_risk": "系统风险",
        }.get(source_type, "系统生成" if source_type else "")
    card = {
        "id": str(task.get("id") or ""),
        "title": str(task.get("title") or task.get("summary") or "未命名任务")[:80],
        "student_name": _task_student(task),
        "level": str(task.get("level") or "C"),
        "status": str(task.get("status") or ""),
        "due_at": str(task.get("due_at") or ""),
        "type": str(task.get("type") or ""),
        "source_type": source_type,
        "source_label": source_label,
        "assigned_by": str(task.get("assigned_by") or ""),
        "assigned_by_name": str(task.get("assigned_by_name") or ""),
    }
    if now is not None:
        card["freshness_state"] = _task_freshness_state(task, now)
        card["today_eligible"] = _task_is_today_action(task, now)
    return card


def _closure_event_card(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": str(event.get("task_id") or ""),
        "task_title": str(event.get("task_title") or "未命名任务")[:80],
        "student_name": str(event.get("student_name") or ""),
        "level": str(event.get("level") or "C"),
        "source_type": str(event.get("source_type") or ""),
        "source_label": str(event.get("source_label") or ""),
        "assigned_by": str(event.get("assigned_by") or ""),
        "action": str(event.get("action") or ""),
        "by": str(event.get("by") or ""),
        "at": str(event.get("at") or ""),
        "text": str(event.get("text") or "")[:120],
        "missing_fields": list(event.get("missing_fields") or [])[:8],
    }


def _closure_gap_card(task: dict[str, Any]) -> dict[str, Any] | None:
    missing = closure_missing_fields(task, str(task.get("evidence_summary") or ""))
    if not missing:
        return None
    return {
        "task_id": str(task.get("id") or ""),
        "task_title": str(task.get("title") or "未命名任务")[:80],
        "student_name": _task_student(task),
        "level": str(task.get("level") or "C"),
        "status": str(task.get("status") or ""),
        "assignee_userid": str(task.get("assignee_userid") or ""),
        "source_type": str(task.get("source_type") or ""),
        "source_label": str(task.get("source_label") or ""),
        "assigned_by": str(task.get("assigned_by") or ""),
        "assigned_by_name": str(task.get("assigned_by_name") or ""),
        "missing_fields": missing[:8],
        "evidence_summary": str(task.get("evidence_summary") or "")[:160],
    }


def _record_templates(material_records: list[dict[str, Any]]) -> dict[str, Any]:
    examples = [
        {
            "student_name": item.get("student_name"),
            "summary": item.get("summary"),
            "quality_level": item.get("quality_level"),
            "use_cases": item.get("use_cases", []),
        }
        for item in material_records[:6]
    ]
    return {
        "principle": "模板只能提醒结构，不能复制粘贴刷分；有效记录必须写出真实场景、行为、老师动作和下一步。",
        "templates": [
            {
                "scenario": "日常成长",
                "title": "观察 + 引导 + 下一步",
                "template": "今天[学生]在[场景]出现[具体行为/进步]，老师[提醒/引导/陪伴]，后续建议[下一步]。",
                "required_parts": ["具体场景", "孩子行为", "老师动作", "下一步"],
            },
            {
                "scenario": "家长沟通",
                "title": "问题 + 沟通 + 家校配合",
                "template": "关于[学生]的[问题/进步]，老师已和[家长]沟通，说明[事实]，建议家里[配合方式]，明天继续跟进。",
                "required_parts": ["沟通对象", "沟通事实", "家校建议", "跟进时间"],
            },
            {
                "scenario": "安全闭环",
                "title": "事件 + 处理 + 告知 + 观察",
                "template": "[学生]在[时间/地点]发生[安全事件]，老师已[处理动作]，并[告知家长/负责人]，后续[观察安排]。",
                "required_parts": ["事件经过", "处理动作", "告知对象", "后续观察"],
            },
            {
                "scenario": "续费/投诉风险",
                "title": "风险 + 证据 + 动作 + 结果",
                "template": "[学生/家长]出现[风险信号]，老师基于[证据]已[沟通/跟进]，当前结果[变化]，下一步[安排]。",
                "required_parts": ["风险信号", "证据", "老师动作", "结果/下一步"],
            },
        ],
        "quality_examples": examples,
    }


def _teacher_daily_coach(
    *,
    open_tasks: list[dict[str, Any]],
    uncovered_students: list[dict[str, Any]],
    performance: dict[str, Any],
) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    for task in sorted(open_tasks, key=lambda item: (_ACTION_PRIORITY_RANK.get(_task_action_priority(item), 9), str(item.get("due_at") or ""))):
        level = _task_action_priority(task)
        actions.append(
            {
                "priority": level,
                "type": "task_closure",
                "student_name": _task_student(task),
                "title": f"闭环{level}级任务",
                "reason": str(task.get("title") or task.get("summary") or "有任务未完成"),
                "action_hint": "补齐处理结果、家长是否知情和下一步观察，闭环后才会进入服务/安全绩效。",
                "due_at": str(task.get("due_at") or ""),
            }
        )
        if len(actions) >= 3:
            break
    monthly = _as_dict(performance.get("monthly_requirements"))
    for item in monthly.get("missing_required") or []:
        if len(actions) >= 3:
            break
        if not isinstance(item, dict):
            continue
        bucket = str(item.get("bucket") or item.get("required_bucket") or "")
        title = "补一条有效家长沟通" if bucket == "parent_communication" else "补一条有效成长观察"
        actions.append(
            {
                "priority": "A" if bucket == "parent_communication" else "B",
                "type": "monthly_requirement",
                "student_name": str(item.get("student_name") or ""),
                "title": title,
                "reason": str(item.get("reason") or "本月必达项未完成"),
                "action_hint": "记录时写清场景、孩子表现、老师动作和后续建议，泛泛一句只入档不计绩效。",
            }
        )
    for item in uncovered_students:
        if len(actions) >= 3:
            break
        actions.append(
            {
                "priority": "C",
                "type": "coverage",
                "student_name": str(item.get("student_name") or ""),
                "title": "补本周成长证据",
                "reason": str(item.get("reason") or "本周还没有新的成长记录"),
                "action_hint": "用今天的真实观察补一条，避免月底才发现覆盖不足。",
            }
        )
    if not actions:
        actions.append(
            {
                "priority": "C",
                "type": "keep_quality",
                "student_name": "",
                "title": "保持今天的记录节奏",
                "reason": "当前没有紧急缺项。",
                "action_hint": "继续记录具体场景和老师动作，优质记录会提高家长信任和绩效解释力。",
            }
        )
    return {
        "title": "今天最该做的3件事",
        "top_actions": sorted(actions, key=_action_sort_key)[:3],
        "payroll_preview": {
            "estimated_amount": performance.get("estimated_total_amount", 0),
            "progress_rate": performance.get("progress_rate", 0),
            "status_text": performance.get("status_text", ""),
        },
    }


def _today_prefix(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def _row_today(row: dict[str, Any], now: datetime) -> bool:
    for key in ("created_at", "updated_at", "queued_at", "sent_at", "timestamp"):
        value = str(row.get(key) or "")
        if value.startswith(_today_prefix(now)):
            return True
    return False


def _latest_rows(rows: list[dict[str, Any]], *, limit: int = 3) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda item: str(item.get("updated_at") or item.get("created_at") or item.get("timestamp") or ""))[-limit:]


def _current_state_row(row: dict[str, Any], *, now: datetime) -> bool:
    updated_at = _parse_dt(row.get("updated_at") or row.get("created_at"))
    if updated_at is None:
        return False
    reference = _business_naive(now)
    return updated_at >= reference - timedelta(hours=AUTONOMOUS_WORK_ITEM_FRESHNESS_HOURS)


def _short_text(value: Any, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _work_item_card(item: dict[str, Any]) -> dict[str, Any]:
    current_waiting = _as_dict(item.get("current_waiting"))
    phase = _as_dict(item.get("current_phase"))
    card = {
        "work_item_id": str(item.get("work_item_id") or ""),
        "focus_key": str(item.get("focus_key") or ""),
        "title": str(item.get("title") or item.get("focus_summary") or item.get("focus_key") or "小优工作事项"),
        "status": str(item.get("status") or "active"),
        "phase": _short_text(phase.get("name") or phase.get("phase") or item.get("stage") or ""),
        "blocker": _short_text(current_waiting.get("reason") or item.get("owner_escalation_reason") or item.get("focus_summary") or ""),
        "next_attention_at": str(item.get("next_attention_at") or item.get("next_contact_after") or ""),
        "next_action": _short_text(_first_string(item.get("next_actions"))),
    }
    if str(item.get("work_kind") or "") == "institution_change":
        artifacts = _as_list(item.get("artifacts"))
        current_version = str(item.get("current_artifact_version_id") or "")
        artifact = next((row for row in artifacts if isinstance(row, dict) and str(row.get("version_id") or "") == current_version), {})
        card.update({
            "work_kind": "institution_change",
            "institution_stage": str(item.get("institution_stage") or "discovered"),
            "artifact_version_id": current_version,
            "artifact_title": _short_text(_as_dict(artifact).get("title") or ""),
            "artifact_status": str(_as_dict(artifact).get("status") or ""),
            "evidence_count": len(_as_list(item.get("evidence"))),
            "pending_items": _as_list(_as_dict(artifact).get("pending_items"))[:3],
            "decision_type": str(current_waiting.get("decision_type") or ""),
        })
    return card


def _boss_focus_title(card: dict[str, Any]) -> str:
    title = str(card.get("title") or "").strip()
    focus_key = str(card.get("focus_key") or "").strip().lower()
    combined = f"{focus_key} {title} {card.get('blocker') or ''} {card.get('next_action') or ''}"
    if "sept" in combined or "九月" in combined or "续费" in combined:
        return "九月份续费率更稳"
    if focus_key.startswith("goal:") and any(word in combined for word in ("新学期", "服务关系", "2026-08-25", "8月25")):
        return "九月份续费率更稳"
    if len(title) > 32 and not any(word in title for word in ("目标", "风险", "日报", "巡检")):
        return _short_text(title, 24)
    return _short_text(title or "暂无活跃目标", 32)


def _first_string(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            text = _first_string(item)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        for key in ("text", "title", "summary", "reason", "action", "next_action", "description"):
            text = str(value.get(key) or "").strip()
            if text:
                return text
        return _short_text(json.dumps(value, ensure_ascii=False))
    return str(value or "").strip()


def _daily_report_status(store: TuoguanStore, now: datetime) -> dict[str, Any]:
    outbox = store.read_json("notification_outbox.json", [])
    outbox_rows = [item for item in outbox if isinstance(item, dict)] if isinstance(outbox, list) else []
    runs = _read_jsonl(store, "daily_report_runs.jsonl")
    result: dict[str, Any] = {}
    for kind, label in (("morning", "早报"), ("evening", "晚报")):
        matched = [
            row for row in outbox_rows
            if str(row.get("notification_type") or "") == "autonomous_daily_report"
            and str(row.get("id") or "").endswith(f":{kind}")
            and _row_today(row, now)
        ]
        latest = _latest_rows(matched, limit=1)
        run_rows = [
            row for row in runs
            if str(row.get("report_kind") or "") == kind and _row_today(row, now)
        ]
        item = latest[-1] if latest else {}
        result[kind] = {
            "label": label,
            "status": str(item.get("status") or ("queued" if run_rows else "not_generated")),
            "sent_at": str(item.get("sent_at") or ""),
            "queued_at": str(item.get("created_at") or (run_rows[-1].get("queued_at") if run_rows else "")),
            "message": "已发送" if str(item.get("status") or "") == "sent" else ("已入队" if item or run_rows else "未生成"),
        }
    return result


def _relationship_touch_snapshot(
    store: TuoguanStore,
    *,
    now: datetime,
    role: str,
    user_id: str,
    limit: int = 3,
) -> dict[str, Any]:
    rows = []
    for row in _read_jsonl(store, "relationship_touch_candidates.jsonl"):
        if not _relationship_touch_visible(row, role=role, user_id=user_id):
            continue
        if str(row.get("status") or "candidate") not in {"candidate", "queued", "sent", "failed"}:
            continue
        rows.append(row)
    today = [row for row in rows if _row_today(row, now)]
    latest = list(reversed(_latest_rows(today or rows, limit=limit)))
    return {
        "candidate_count": len(rows),
        "today_count": len(today),
        "items": [
            {
                "target_role": str(row.get("target_role") or ""),
                "target_user_id": str(row.get("target_user_id") or ""),
                "target_name": str(row.get("target_name") or ""),
                "touch_type": str(row.get("touch_type") or ""),
                "message": _short_text(row.get("message") or "", 160),
                "reason": _short_text(row.get("reason") or "", 140),
                "value": _short_text(row.get("value") or "", 140),
                "status": str(row.get("status") or "candidate"),
                "work_related": bool(row.get("work_related")),
                "private_emotional_support": bool(row.get("private_emotional_support")),
                "external_send_allowed": bool(row.get("external_send_allowed")),
                "suggested_send_at": str(row.get("suggested_send_at") or ""),
            }
            for row in latest
        ],
        "boundary_note": "这些是小优的关系经营候选；老师/店长未授权前只展示，不自动外发。",
    }


def _relationship_touch_visible(row: dict[str, Any], *, role: str, user_id: str) -> bool:
    target_role = str(row.get("target_role") or "")
    target_user = str(row.get("target_user_id") or "")
    if role == "boss":
        return True
    if role == "manager":
        return target_role in {"manager", "teacher"} and (not target_user or target_user == user_id or target_role == "teacher")
    if role == "teacher":
        return target_role == "teacher" and (not target_user or target_user == user_id)
    return False


def _hermes_employee_snapshot(
    store: TuoguanStore,
    *,
    now: datetime,
    role: str,
    user_id: str,
    summary: dict[str, Any],
    task_status: dict[str, Any],
    teacher_execution: list[dict[str, Any]],
    value_limit: int = 3,
) -> dict[str, Any]:
    dashboard_identity = UserIdentity(
        "dashboard",
        user_id,
        user_id,
        user_id,
        role,
        "approved",
    )
    work_result = query_hermes_work_items(
        store,
        identity=dashboard_identity,
        include_closed=False,
        limit=100,
    )
    work_rows = [
        row for row in _as_list(work_result.get("items"))
        if isinstance(row, dict) and _current_state_row(row, now=now)
    ]
    institution_result = query_institution_work(
        store,
        identity=dashboard_identity,
        include_closed=False,
        limit=20,
    )
    institution_rows = [
        row for row in _as_list(institution_result.get("items"))
        if isinstance(row, dict) and _current_state_row(row, now=now)
    ]
    attention_rows: list[dict[str, Any]] = []
    if role in {"boss", "manager"}:
        attention_result = query_attention_threads(
            store,
            identity=dashboard_identity,
            include_closed=False,
            limit=100,
        )
        attention_rows = [
            row for row in _as_list(attention_result.get("attention_threads"))
            if isinstance(row, dict) and _current_state_row(row, now=now)
        ]
    values = list(reversed(_latest_rows(_read_jsonl(store, "value_progress_ledger.jsonl"), limit=value_limit)))
    action_rows = _read_jsonl(store, "action_executions.jsonl")
    today_actions = [row for row in action_rows if _row_today(row, now)]
    work_items = [_work_item_card(item) for item in reversed(_latest_rows(work_rows, limit=5))]
    institution_items = [_work_item_card(item) for item in institution_rows[:3]] if role == "boss" else []
    current = work_items[0] if work_items else {}
    open_questions = [
        {
            "question": _short_text(row.get("question_text") or row.get("source_decision_summary") or "待确认事项"),
            "status": str(row.get("status") or ""),
            "focus_key": str(row.get("focus_key") or ""),
        }
        for row in reversed(_latest_rows(attention_rows, limit=3))
        if role == "boss" or str(row.get("target_user_id") or "") == user_id
    ][:3]
    institution_decisions = [
        {
            "question": _short_text(
                f"确认《{row.get('artifact_title') or row.get('title') or '机构方案'}》"
                if str(row.get("institution_stage") or "") == "awaiting_content_approval"
                else f"是否授权落实《{row.get('artifact_title') or row.get('title') or '机构方案'}》"
            ),
            "status": str(row.get("institution_stage") or ""),
            "focus_key": str(row.get("focus_key") or ""),
            "work_item_id": str(row.get("work_item_id") or ""),
            "artifact_version_id": str(row.get("artifact_version_id") or ""),
            "decision_type": str(row.get("decision_type") or ""),
        }
        for row in institution_items
        if str(row.get("institution_stage") or "") in {"awaiting_content_approval", "awaiting_implementation_authorization"}
    ]
    if role == "boss":
        open_questions = (institution_decisions + open_questions)[:3]
    report_status = _daily_report_status(store, now)
    relationship_touch = _relationship_touch_snapshot(store, now=now, role=role, user_id=user_id, limit=4)
    decision_count = len(open_questions)
    risk_summary = {
        "safety_count": len(_as_list(task_status.get("safety_risks"))),
        "teacher_support_count": len([
            row for row in teacher_execution
            if int(row.get("open_task_count") or 0) or int(row.get("coverage_rate_7d") or 0) < 60
        ]),
    }
    focus_brief = {
        "title": _boss_focus_title(current) if current else "暂无活跃目标",
        "status": str(current.get("status") or ("active" if current else "巡检中")),
        "phase": _short_text(current.get("phase") or ("持续推进" if current else "日常巡检"), 42),
        "blocker": _short_text(current.get("blocker") or ("暂无明确卡点" if current else "暂无需要老板立即拍板的事项"), 72),
        "next_action": _short_text(current.get("next_action") or ("等待新事实后继续判断" if current else "继续巡检记录覆盖、风险信号和目标缺口"), 72),
        "next_attention_at": str(current.get("next_attention_at") or ""),
    }
    today_status = {
        "summary": (
            f"今日有 {len(today_actions)} 条动作账本，{decision_count} 个事项等待老板确认。"
            if role == "boss"
            else f"今日记录 {summary.get('today_records', 0)} 条，现场待确认 {decision_count} 个。"
        ),
        "morning_report": report_status.get("morning", {}).get("message", "未生成"),
        "evening_report": report_status.get("evening", {}).get("message", "未生成"),
    }
    cards = [
        {"label": "当前焦点", "value": "1个" if current else "巡检中"},
        {"label": "待我确认", "value": decision_count},
        {"label": "今日日报", "value": report_status.get("evening", {}).get("message", "未生成")},
        {"label": "风险", "value": risk_summary["safety_count"]},
    ]
    if role == "manager":
        cards = [
            {"label": "今日记录", "value": summary.get("today_records", 0)},
            {"label": "老师覆盖", "value": f"{summary.get('coverage_rate_7d', 0)}%"},
            {"label": "未闭环", "value": summary.get("open_task_count", 0)},
            {"label": "待确认", "value": len(open_questions)},
        ]
    return {
        "headline": (
            "小优正在推进：" + str(current.get("title"))
            if current
            else "暂无活跃目标，小优会继续巡检机构事实、记录覆盖和风险信号。"
        ),
        "role_tone": "professional_operator" if role == "boss" else "store_assistant",
        "cards": cards,
        "daily_reports": report_status,
        "relationship_presence": relationship_touch,
        "today_status": today_status,
        "focus_brief": focus_brief,
        "decision_brief": open_questions[:2],
        "current_focus": current,
        "work_items": work_items[:3],
        "other_work_count": max(0, len(work_items) - 3),
        "institution_work_items": institution_items,
        "open_questions": open_questions,
        "value_entries": [
            {
                "subject": str(row.get("subject") or "价值推进"),
                "discovered": _short_text(row.get("discovered") or ""),
                "hermes_action": _short_text(row.get("hermes_action") or ""),
                "outcome": _short_text(row.get("outcome") or "等待真实结果"),
            }
            for row in values
        ][:2],
        "risk_summary": risk_summary,
        "boundary_note": "看板只展示小优的状态、证据和建议，不替小优决定下一步，也不扩大外发权限。",
    }


def _manager_assistant_snapshot(
    *,
    summary: dict[str, Any],
    teacher_execution: list[dict[str, Any]],
    hermes_employee: dict[str, Any],
    task_status: dict[str, Any],
) -> dict[str, Any]:
    support = []
    for item in sorted(
        teacher_execution,
        key=lambda row: (int(row.get("coverage_rate_7d") or 0), -int(row.get("open_task_count") or 0)),
    ):
        if len(support) >= 3:
            break
        if int(item.get("open_task_count") or 0):
            support.append({
                "title": f"帮 {item.get('teacher_name') or item.get('teacher_id')} 看一下未闭环事项",
                "detail": f"当前还有 {item.get('open_task_count')} 个事项，先看安全、家长和续费相关。",
            })
        elif int(item.get("coverage_rate_7d") or 0) < 60:
            support.append({
                "title": f"给 {item.get('teacher_name') or item.get('teacher_id')} 一个记录模板",
                "detail": f"近7天覆盖 {item.get('coverage_rate_7d')}%，更像是需要示范，不是简单催促。",
            })
    if not support:
        support.append({"title": "今天先保持现场巡检", "detail": "当前没有明显老师支持缺口，重点看安全、记录覆盖和老板目标落地。"})
    return {
        "headline": "小优今天帮你盯现场执行和老师支持。",
        "store_status": {
            "today_records": summary.get("today_records", 0),
            "coverage_rate_7d": summary.get("coverage_rate_7d", 0),
            "open_task_count": summary.get("open_task_count", 0),
            "safety_count": len(_as_list(task_status.get("safety_risks"))),
        },
        "top_suggestions": support[:3],
        "pending_confirmations": _as_list(hermes_employee.get("open_questions"))[:3],
        "relationship_support": _as_dict(hermes_employee.get("relationship_presence")),
        "goal_focus": hermes_employee.get("current_focus") or {},
    }


def _position_cap_from_payroll(payroll_item: dict[str, Any] | None, payroll_rules: dict[str, Any]) -> tuple[float, str]:
    item = payroll_item if isinstance(payroll_item, dict) else {}
    position = str(item.get("position") or "").strip() or "part_time"
    positions = _as_dict(payroll_rules.get("positions"))
    configured = _as_dict(positions.get(position))
    if position == "manager":
        cap = float(configured.get("hermes_performance_total") or configured.get("performance_total") or 600)
    elif position == "full_time_teacher":
        cap = float(configured.get("hermes_performance_total") or configured.get("hermes_collaboration_total") or 400)
    else:
        cap = float(configured.get("hermes_performance_total") or configured.get("hermes_collaboration_total") or 200)
    return max(0.0, cap), str(configured.get("label") or item.get("position_label") or position)


def _teacher_hermes_blocks(
    *,
    display_name: str,
    today_records: list[dict[str, Any]],
    week_records: list[dict[str, Any]],
    month_records: list[dict[str, Any]],
    material_records: list[dict[str, Any]],
    suggested_records: list[dict[str, Any]],
    performance: dict[str, Any],
    payroll_rules: dict[str, Any],
    payroll_item: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    cap, position_label = _position_cap_from_payroll(payroll_item, payroll_rules)
    timely = min(30, len(today_records) * 10 + len(week_records) * 3)
    completeness = min(30, int(performance.get("progress_rate") or 0) * 30 // 100)
    detail_quality = min(20, int(performance.get("month_quality_records") or 0) * 8 + len(material_records) * 2)
    material_value = min(15, len(material_records) * 5)
    stability = min(5, 2 + len([row for row in week_records if _is_valid_record(row)]) // 3)
    score = int(min(100, timely + completeness + detail_quality + material_value + stability))
    estimated = round(cap * score / 100, 2)
    tree_level = max(1, min(9, score // 12 + 1))
    companion = {
        "headline": (
            f"{display_name}，今天小优已经帮你整理了 {len(today_records)} 条成长证据。"
            if today_records
            else f"{display_name}，今天还没有新记录；你顺手说一句，小优就能帮你整理成孩子成长证据。"
        ),
        "helped_today": [
            f"整理今日记录 {len(today_records)} 条",
            f"沉淀可用于家长沟通/成长报告的素材 {len(material_records)} 条",
            "把本月绩效进度换成能看懂的成长树",
        ],
        "easy_next": [
            {
                "student_name": str(item.get("student_name") or ""),
                "hint": str(item.get("reason") or "补一句真实观察就能形成成长证据"),
            }
            for item in suggested_records[:3]
        ],
        "tone": "同事式提醒，不是监督；帮助老师省掉重复整理和不会写的压力。",
    }
    tree = {
        "score": score,
        "level": tree_level,
        "estimated_amount": estimated,
        "cap_amount": cap,
        "position_label": position_label,
        "progress_rate": score,
        "components": {
            "timely_response": timely,
            "answer_completeness": completeness,
            "child_detail_quality": detail_quality,
            "material_value": material_value,
            "stability": stability,
        },
        "growth_text": (
            "这棵树来自你和小优的配合：回复越及时、细节越具体、越能变成家校素材，树就长得越好。"
        ),
        "next_tip": (
            "再补一句孩子具体表现和老师怎么处理，会更容易长出果实。"
            if score < 90
            else "本月配合质量很稳，继续保持真实、具体、及时。"
        ),
        "final_note": "这是本月预估和激励展示，最终绩效以老板确认制度为准。",
    }
    return companion, tree


def _teacher_dashboard(
    *,
    store: TuoguanStore,
    user_id: str,
    display_name: str,
    students: dict[str, dict[str, Any]],
    records: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    now: datetime,
    payroll_rules: dict[str, Any],
) -> dict[str, Any]:
    week_start = now - timedelta(days=7)
    month_start = _month_start(now)
    own_students = {
        name: profile
        for name, profile in students.items()
        if _student_teacher(profile) == user_id
    }
    own_names = set(own_students)
    own_records = [
        record
        for record in records
        if _record_teacher(record, students) == user_id or _record_student(record) in own_names
    ]
    own_records = own_records + _task_growth_evidence_records(
        tasks,
        teacher_id=user_id,
        student_names=own_names,
        since=month_start,
    )
    today_records = [
        record
        for record in own_records
        if _same_date(record, now)
    ]
    week_records = [
        record
        for record in own_records
        if _after(_timestamp(record), week_start)
    ]
    month_records = [
        record
        for record in own_records
        if _after(_timestamp(record), month_start)
    ]
    covered_names = {_record_student(record) for record in week_records if _record_student(record)}
    completion_items = [
        {
            "student_name": name,
            "completion": _profile_completion(profile),
            "campus_id": _student_campus(profile),
        }
        for name, profile in own_students.items()
    ]
    open_tasks = [
        task
        for task in tasks
        if _task_open(task)
        and (_task_teacher(task, students) == user_id or _task_student(task) in own_names)
    ]
    ordered_open_tasks = sorted(open_tasks, key=lambda item: _task_display_sort_key(item, now))
    today_open_tasks = [task for task in ordered_open_tasks if _task_is_today_action(task, now)]
    week_points = sum(_record_points(record) for record in week_records)
    quality_week_records = [record for record in week_records if _is_quality_record(record)]
    material_records = [_recent_record_card(record) for record in quality_week_records]
    best_record = max(
        (_recent_record_card(record) for record in today_records),
        key=lambda item: (int(item.get("points") or 0), len(str(item.get("summary") or ""))),
        default=None,
    )
    student_count = len(own_students)
    coverage_rate = round(len(covered_names & own_names) / student_count * 100) if student_count else 0
    growth_value = week_points + coverage_rate
    growth_level = max(1, min(9, growth_value // 20 + 1))
    next_level_target = growth_level * 20
    avg_completion = (
        round(sum(item["completion"] for item in completion_items) / len(completion_items))
        if completion_items
        else 0
    )
    uncovered_students = [
        {"student_name": name, "completion": _profile_completion(profile)}
        for name, profile in own_students.items()
        if name not in covered_names
    ][:20]
    suggested_records = []
    for item in uncovered_students[:8]:
        reason = "本周还缺一条成长证据"
        if item["completion"] < 50:
            reason = "档案完整度偏低，顺手补一条观察会很有用"
        suggested_records.append({**item, "reason": reason})
    growth_report_materials = [
        item for item in material_records if "成长报告" in item.get("use_cases", [])
    ]
    parent_materials = [
        item for item in material_records if "家长沟通" in item.get("use_cases", [])
    ]
    tag_materials = [
        item for item in material_records if "学生标签" in item.get("use_cases", [])
    ]
    performance = _teacher_performance(
        month_records,
        records=records,
        tasks=tasks,
        students=students,
        teacher_id=user_id,
        now=now,
        payroll_rules=payroll_rules,
    )
    assistant_message = (
        f"今天你留下了{len(today_records)}条孩子成长证据，其中"
        f"{sum(1 for record in today_records if _is_quality_record(record))}条质量不错。"
        if today_records
        else "今天还没有新的记录。等你在企业微信顺手说一句，小优会帮你整理成孩子的成长证据。"
    )
    hermes_companion, hermes_tree = _teacher_hermes_blocks(
        display_name=display_name or user_id,
        today_records=today_records,
        week_records=week_records,
        month_records=month_records,
        material_records=material_records,
        suggested_records=suggested_records,
        performance=performance,
        payroll_rules=payroll_rules,
    )
    relationship_touch = _relationship_touch_snapshot(
        store,
        now=now,
        role="teacher",
        user_id=user_id,
        limit=3,
    )
    colleague_line = (
        relationship_touch.get("items", [{}])[0].get("message")
        if relationship_touch.get("items")
        else hermes_companion.get("headline")
    )
    return {
        "role": "teacher",
        "user_id": user_id,
        "display_name": display_name or user_id,
        "generated_at": now.isoformat(timespec="seconds"),
        "summary": {
            "today_records": len(today_records),
            "today_valid_records": sum(1 for record in today_records if _is_valid_record(record)),
            "today_quality_records": sum(1 for record in today_records if _is_quality_record(record)),
            "week_records": len(week_records),
            "week_points": week_points,
            "student_count": student_count,
            "coverage_rate_7d": coverage_rate,
            "average_profile_completion": avg_completion,
            "open_task_count": len(open_tasks),
        },
        "today_feedback": [_recent_record_card(record) for record in sorted(today_records, key=_record_sort_key, reverse=True)[:8]],
        "record_review": _teacher_record_review(month_records),
        "daily_coach": _teacher_daily_coach(
            open_tasks=today_open_tasks,
            uncovered_students=suggested_records,
            performance=performance,
        ),
        "week_contribution": {
            "records": len(week_records),
            "points": week_points,
            "quality_records": sum(1 for record in week_records if _is_quality_record(record)),
            "growth_value": growth_value,
            "growth_level": growth_level,
            "next_level_target": next_level_target,
            "points_to_next_level": max(0, next_level_target - growth_value),
        },
        "teacher_motivation": {
            "headline": f"{display_name or user_id}，本周你已经留下{len(week_records)}条孩子成长证据",
            "subtitle": f"{len(growth_report_materials)}条可用于成长报告，{len(parent_materials)}条可用于家长沟通",
            "assistant_message": assistant_message,
            "best_record": best_record,
        },
        "hermes_companion": hermes_companion,
        "hermes_colleague_touch": {
            "headline": "小优同事一句话",
            "message": colleague_line,
            "candidates": relationship_touch.get("items", []),
            "boundary_note": "这些候选用于陪伴、鼓励和减负；未授权前不会自动私聊老师，私人情绪聊天不默认进入老板绩效材料。",
        },
        "hermes_performance_tree": hermes_tree,
        "performance": performance,
        "uncovered_students": uncovered_students,
        "suggested_records": suggested_records,
        "materials": {
            "growth_reports": growth_report_materials[:12],
            "parent_communication": parent_materials[:12],
            "student_tags": tag_materials[:12],
            "quality_records": material_records[:12],
        },
        "record_templates": _record_templates(material_records),
        "today_tasks": [_task_card(task, now=now) for task in today_open_tasks[:12]],
        "open_tasks": [_task_card(task, now=now) for task in ordered_open_tasks[:12]],
        "student_completion": sorted(completion_items, key=lambda item: item["completion"])[:20],
        "readonly_hint": "记录请回企业微信直接说",
    }


def _data_quality_report(
    *,
    students: dict[str, dict[str, Any]],
    records: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    teacher_names: dict[str, str],
    now: datetime,
) -> dict[str, Any]:
    month_start = _month_start(now)
    known_students = set(students)
    known_teachers = set(teacher_names)
    known_teachers.update(_student_teacher(profile) for profile in students.values() if _student_teacher(profile))
    month_records = [record for record in records if _after(_timestamp(record), month_start)]
    missing_evaluation = [
        record
        for record in month_records
        if not isinstance(record.get("record_evaluation"), dict)
        or str(record.get("record_evaluation", {}).get("evaluation_version") or "") != RECORD_RULE_VERSION
    ]
    orphan_records = [
        record
        for record in month_records
        if _record_student(record) and _record_student(record) not in known_students
    ]
    teacher_anomalies = [
        record
        for record in month_records
        if _record_teacher(record, students) and _record_teacher(record, students) not in known_teachers
    ]
    inactive_students = [
        {"student_name": name, "status": _student_status(profile)}
        for name, profile in students.items()
        if not _is_active_payroll_student(profile)
    ]
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    duplicate_risks: list[dict[str, Any]] = []
    for record in month_records:
        timestamp = _timestamp(record)
        content = _record_content(record)
        fingerprint = "".join(content.split())[:36]
        if not fingerprint:
            continue
        key = (_record_student(record), timestamp.date().isoformat() if timestamp else "", fingerprint)
        if key in seen:
            duplicate_risks.append(record)
        else:
            seen[key] = record
    open_high_tasks = [
        task
        for task in tasks
        if _task_open(task) and str(task.get("level") or "") in {"S", "A"}
    ]
    def record_item(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(record.get("id") or ""),
            "student_name": _record_student(record),
            "teacher": _record_teacher(record, students),
            "time": str(record.get("timestamp") or record.get("created_at") or ""),
            "summary": _record_content(record)[:80],
        }
    return {
        "status": "needs_attention" if missing_evaluation or orphan_records else "ok",
        "summary": {
            "missing_evaluation_count": len(missing_evaluation),
            "orphan_record_count": len(orphan_records),
            "duplicate_risk_count": len(duplicate_risks),
            "teacher_anomaly_count": len(teacher_anomalies),
            "inactive_student_count": len(inactive_students),
            "open_high_task_count": len(open_high_tasks),
        },
        "missing_evaluation_records": [record_item(record) for record in missing_evaluation[:20]],
        "orphan_records": [record_item(record) for record in orphan_records[:20]],
        "duplicate_risk_records": [record_item(record) for record in duplicate_risks[:20]],
        "teacher_assignment_anomalies": [record_item(record) for record in teacher_anomalies[:20]],
        "inactive_students": inactive_students[:30],
        "settlement_guard": {
            "can_lock_snapshot": not missing_evaluation and not orphan_records,
            "blocking_reasons": [
                reason
                for condition, reason in (
                    (missing_evaluation, "存在未按最新规则评估的本月记录"),
                    (orphan_records, "存在无法匹配学生档案的本月记录"),
                )
                if condition
            ],
            "warnings": [
                reason
                for condition, reason in (
                    (duplicate_risks, "存在同日相似记录，需要抽查是否刷分"),
                    (teacher_anomalies, "存在老师归属异常记录"),
                    (open_high_tasks, "存在未闭环S/A任务"),
                    (inactive_students, "存在暂停/试托/流失/测试学生，必达项需按计薪口径过滤"),
                )
                if condition
            ],
        },
    }


def _renewal_funnel(
    *,
    students: dict[str, dict[str, Any]],
    store: TuoguanStore,
    now: datetime,
) -> dict[str, Any]:
    raw_reports = store.read_json("growth_reports.json", [])
    reports = [item for item in raw_reports if isinstance(item, dict)] if isinstance(raw_reports, list) else []
    approved_recent: set[str] = set()
    for report in reports:
        if str(report.get("status") or "") != "approved":
            continue
        approved_at = _parse_dt(report.get("approved_at") or report.get("created_at"))
        if approved_at is None or approved_at >= now - timedelta(days=30):
            approved_recent.add(str(report.get("student_name") or ""))
    items: list[dict[str, Any]] = []
    for name, profile in students.items():
        due = _parse_dt(profile.get("renewal_due_date"))
        if due is None:
            continue
        days = (due.date() - now.date()).days
        if days > 30:
            continue
        status = str(profile.get("renewal_status") or "待沟通")
        risk = str(profile.get("relationship_temperature") or "")
        items.append(
            {
                "student_name": name,
                "due_date": due.date().isoformat(),
                "days_to_due": days,
                "renewal_status": status,
                "relationship_temperature": risk,
                "has_recent_growth_report": name in approved_recent,
                "suggested_action": "先确认成长证据和家校沟通，再由老师/老板确认是否发送续费回顾。",
            }
        )
    return {
        "due_30_count": sum(1 for item in items if 0 <= int(item["days_to_due"]) <= 30),
        "due_15_count": sum(1 for item in items if 0 <= int(item["days_to_due"]) <= 15),
        "due_7_count": sum(1 for item in items if 0 <= int(item["days_to_due"]) <= 7),
        "overdue_count": sum(1 for item in items if int(item["days_to_due"]) < 0),
        "with_growth_report_count": sum(1 for item in items if item["has_recent_growth_report"]),
        "at_risk_count": sum(1 for item in items if str(item.get("relationship_temperature")) in {"at_risk", "cold", "risk", "流失风险"}),
        "students": sorted(items, key=lambda item: int(item["days_to_due"]))[:50],
    }


def _rule_center(payroll_rules: dict[str, Any], data_quality: dict[str, Any], now: datetime) -> dict[str, Any]:
    performance_rules = _performance_rules()
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "rules": [
            {
                "key": "record_evaluation",
                "name": "老师记录评估",
                "version": RECORD_RULE_VERSION,
                "basis": "先入档，再判断是否计绩效，再进入月度必达项和质量分。",
            },
            {
                "key": "payroll_record_performance",
                "name": "记录绩效工资",
                "version": str(_as_dict(payroll_rules.get("record_performance")).get("version") or "payroll_record_v1"),
                "basis": "记录绩效工资 = 记录绩效池金额 × 必达项完成率 × 质量系数，最终由老板确认。",
            },
            {
                "key": "growth_coupon",
                "name": "家长端成长激励券",
                "version": COUPON_RULE_VERSION,
                "basis": "最高200元，只按已审核证据、优质记录、家校沟通和老师跟进逐级增加。",
            },
            {
                "key": "settlement_guard",
                "name": "工资结算守门",
                "version": "settlement_guard_v1",
                "basis": "存在未评估记录或孤儿学生记录时，不建议锁定结算快照。",
            },
        ],
        "simulator": {
            "mode": "preview_only",
            "inputs_supported": ["记录绩效池金额", "必达项权重", "质量加分权重", "激励券最高金额"],
            "current_month_preview": {
                "record_required_weight": performance_rules["required_weight"],
                "quality_weight": performance_rules["quality_weight"],
                "record_pool_amount": performance_rules["base_amount"],
                "max_coupon_amount": 200,
                "can_lock_settlement": _as_dict(data_quality.get("settlement_guard")).get("can_lock_snapshot"),
            },
        },
    }


def _boss_daily_brief(
    *,
    role: str,
    summary: dict[str, Any],
    task_status: dict[str, Any],
    risk: dict[str, Any],
    payroll: dict[str, Any],
    data_quality: dict[str, Any],
    renewal_funnel: dict[str, Any],
    teacher_execution: list[dict[str, Any]],
    operations_focus: dict[str, Any],
) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    guard = _as_dict(data_quality.get("settlement_guard"))
    for reason in guard.get("blocking_reasons") or []:
        actions.append({"priority": "S", "title": "先处理数据问题", "detail": str(reason), "source": "数据守门"})
    open_s = int(_as_dict(task_status.get("open_by_level")).get("S") or 0)
    if open_s:
        actions.append({"priority": "S", "title": "闭环S级任务", "detail": f"当前还有{open_s}个S级任务未闭环。", "source": "任务闭环"})
    if role == "manager":
        open_a = int(_as_dict(task_status.get("open_by_level")).get("A") or 0)
        if open_a:
            actions.append({
                "priority": "A",
                "title": "督促团队闭环A级任务",
                "detail": f"当前校区还有{open_a}个A级任务未闭环，先确认负责人和处理结果。",
                "source": "团队管理",
            })
        for item in sorted(
            teacher_execution,
            key=lambda row: (
                -int(row.get("open_task_count") or 0),
                -len(row.get("stale_students") or []),
                -len(row.get("missing_growth_report_students") or []),
            ),
        ):
            teacher_name = str(item.get("teacher_name") or item.get("teacher_id") or "老师")
            open_count = int(item.get("open_task_count") or 0)
            stale = list(item.get("stale_students") or [])
            missing_reports = list(item.get("missing_growth_report_students") or [])
            if open_count:
                actions.append({
                    "priority": "A",
                    "title": f"督促{teacher_name}闭环任务",
                    "detail": f"当前还有{open_count}个未闭环任务，先看安全、投诉、续费风险。",
                    "source": "团队管理",
                })
            elif stale:
                actions.append({
                    "priority": "B",
                    "title": f"提醒{teacher_name}补记录",
                    "detail": f"{'、'.join(stale[:3])} 近7天缺成长证据。",
                    "source": "记录覆盖",
                })
            elif missing_reports:
                actions.append({
                    "priority": "C",
                    "title": f"检查{teacher_name}成长报告",
                    "detail": f"{'、'.join(missing_reports[:3])} 缺近期成长报告素材。",
                    "source": "家长信任",
                })
            if len(actions) >= 6:
                break
    due_7 = int(renewal_funnel.get("due_7_count") or 0)
    if due_7:
        actions.append({"priority": "A", "title": "续费前成长回顾", "detail": f"7天内到期学生{due_7}名，需要确认成长证据和沟通话术。", "source": "续费"})
    if operations_focus.get("active"):
        actions.append({
            "priority": "B",
            "title": "按经营重点巡检",
            "detail": str(operations_focus.get("summary") or "当前使用系统默认经营节奏"),
            "source": "经营重点",
        })
    question_count = int(_as_dict(payroll.get("summary")).get("teacher_question_count") or 0)
    if question_count:
        actions.append({"priority": "B", "title": "处理工资疑问", "detail": f"{question_count}位老师对工资有疑问，需要看证据链。", "source": "工资"})
    if not actions:
        actions.append({"priority": "C", "title": "保持日常巡检", "detail": "今天没有S级阻断项，重点看记录覆盖和续费沟通。", "source": "日常巡检"})
    return {
        "headline": "今日经营风险简报",
        "generated_at": str(summary.get("generated_at") or ""),
        "cards": [
            {"label": "今日记录", "value": summary.get("today_records", 0), "hint": "老师今天留下的记录数"},
            {"label": "7日覆盖", "value": f"{summary.get('coverage_rate_7d', 0)}%", "hint": "近7天有记录的学生占比"},
            {"label": "未闭环任务", "value": summary.get("open_task_count", 0), "hint": "所有未完成任务"},
            {"label": "续费7天内", "value": renewal_funnel.get("due_7_count", 0), "hint": "需要准备成长回顾"},
            {"label": "数据问题", "value": sum(int(v or 0) for v in _as_dict(data_quality.get("summary")).values()), "hint": "影响工资/报告可信度"},
        ],
        "top_actions": sorted(actions, key=_action_sort_key)[:6],
        "business_focus": operations_focus,
    }


def _boss_dashboard(
    *,
    role: str,
    user_id: str,
    display_name: str,
    campuses: set[str] | None,
    students: dict[str, dict[str, Any]],
    records: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    teacher_names: dict[str, str],
    teacher_dashboards: dict[str, dict[str, Any]],
    payroll_snapshot: dict[str, Any] | None,
    now: datetime,
    store: TuoguanStore,
    project_id: str = "",
) -> dict[str, Any]:
    week_start = now - timedelta(days=7)
    scoped_students = {
        name: profile
        for name, profile in students.items()
        if campuses is None or _student_campus(profile) in campuses
    }
    manager_team_ids: set[str] = set()
    if role == "manager":
        manager_team_ids = _manager_team_teacher_ids(store, user_id)
        if manager_team_ids:
            scoped_students = {
                name: profile
                for name, profile in scoped_students.items()
                if _student_teacher(profile) in manager_team_ids
            }
    scoped_names = set(scoped_students)
    scoped_records = [record for record in records if _record_student(record) in scoped_names]
    if manager_team_ids:
        scoped_tasks = [
            task
            for task in tasks
            if _task_open(task)
            and (_task_student(task) in scoped_names or _task_teacher(task, students) in manager_team_ids)
        ]
    else:
        scoped_tasks = [
            task
            for task in tasks
            if _task_open(task) and (_task_student(task) in scoped_names or _task_campus(task, students) in (campuses or {_task_campus(task, students)}))
        ]
    week_records = [
        record
        for record in scoped_records
        if _after(_timestamp(record), week_start)
    ]
    covered_names = {_record_student(record) for record in week_records if _record_student(record)}
    today_records = [
        record
        for record in scoped_records
        if _same_date(record, now)
    ]
    overview = build_business_overview(store, student_names=sorted(scoped_names), now=now)
    teacher_student_counts = Counter(_student_teacher(profile) for profile in scoped_students.values() if _student_teacher(profile))
    teacher_record_counts = Counter(_record_teacher(record, students) for record in week_records if _record_teacher(record, students))
    teacher_execution = []
    teacher_overview = {
        str(item.get("teacher_userid") or ""): item
        for item in overview.get("teacher_execution", [])
        if isinstance(item, dict)
    }
    for teacher_id, student_count in teacher_student_counts.items():
        overview_item = teacher_overview.get(teacher_id, {})
        performance = _as_dict(_as_dict(teacher_dashboards.get(teacher_id, {})).get("performance"))
        teacher_execution.append(
            {
                "teacher_id": teacher_id,
                "teacher_name": teacher_names.get(teacher_id, teacher_id),
                "student_count": student_count,
                "week_records": teacher_record_counts.get(teacher_id, 0),
                "coverage_rate_7d": round(teacher_record_counts.get(teacher_id, 0) / student_count * 100) if student_count else 0,
                "performance": {
                    "month_points": performance.get("month_points", 0),
                    "target_points": performance.get("target_points", 0),
                    "progress_rate": performance.get("progress_rate", 0),
                    "estimated_total_amount": performance.get("estimated_total_amount", 0),
                    "estimated_base_amount": performance.get("estimated_base_amount", 0),
                    "rank_bonus_amount": performance.get("rank_bonus_amount", 0),
                    "rank": performance.get("rank"),
                    "status_text": performance.get("status_text", ""),
                },
                "stale_students": list(overview_item.get("stale_students") or [])[:20],
                "missing_growth_report_students": list(overview_item.get("missing_growth_report_students") or [])[:20],
                "open_task_count": int(overview_item.get("open_task_count") or 0),
            }
        )
    relationship_risks = overview.get("risk_signals", {}).get("priority_students", [])
    long_unrecorded = sorted(
        {
            str(student)
            for item in overview.get("teacher_execution", [])
            if isinstance(item, dict)
            for student in item.get("stale_students", [])
        }
    )
    open_by_level = Counter(str(task.get("level") or "C") for task in scoped_tasks)
    open_by_status = Counter(str(task.get("status") or "") for task in scoped_tasks)
    safety_tasks = [
        _task_card(task, now=now)
        for task in scoped_tasks
        if str(task.get("level") or "") == "S" or str(task.get("type") or "") == "safety_incident"
    ][:12]
    closure_ledger_raw = store.read_json("task_closure_events.json", [])
    closure_ledger = [
        event
        for event in closure_ledger_raw
        if isinstance(event, dict)
    ] if isinstance(closure_ledger_raw, list) else []
    if manager_team_ids:
        scoped_closure_events = [
            event
            for event in closure_ledger
            if str(event.get("student_name") or "") in scoped_names
            or str(event.get("assignee_userid") or "") in manager_team_ids
        ]
    else:
        scoped_closure_events = [
            event
            for event in closure_ledger
            if campuses is None
            or str(event.get("student_name") or "") in scoped_names
        ]
    closure_gaps = [
        card
        for card in (_closure_gap_card(task) for task in scoped_tasks)
        if card is not None
    ]
    recent_progress = [
        _recent_record_card(record)
        for record in scoped_records
        if any(word in _record_content(record) for word in _POSITIVE_HINTS)
    ][-10:]
    recent_anomalies = [
        _recent_record_card(record)
        for record in scoped_records
        if any(word in _record_content(record) for word in _ISSUE_HINTS)
    ][-10:]
    student_count = len(scoped_students)
    high_risk_count = len(safety_tasks) + len(relationship_risks)
    performance_items = [
        item
        for item in teacher_execution
        if isinstance(item.get("performance"), dict)
    ]
    estimated_payout = round(
        sum(float(item["performance"].get("estimated_total_amount") or 0) for item in performance_items),
        2,
    )
    payroll_items = []
    is_manager = role == "manager"
    if isinstance(payroll_snapshot, dict):
        payroll_by_user = {
            str(item.get("user_id") or ""): item
            for item in payroll_snapshot.get("teachers", [])
            if isinstance(item, dict)
        }
        if role == "boss":
            payroll_items = list(payroll_by_user.values())
        else:
            visible_payroll_ids = set(teacher_student_counts) | {user_id}
            payroll_items = [
                payroll_by_user[teacher_id]
                for teacher_id in visible_payroll_ids
                if teacher_id in payroll_by_user
            ]
    payroll_total = round(
        sum(float(item.get("estimated_total") or 0) for item in payroll_items),
        2,
    )
    learning_candidates = list_pending_learning_candidates(store)
    project_opportunities = (
        query_project_opportunities(store, project_id=project_id, limit=3, now=now)
        if role == "boss"
        else {
            "ok": True,
            "data_state": "empty",
            "source_updated_at": "",
            "visible_count": 0,
            "items": [],
            "boundary": "只有老板可以查看新项目机会候选。",
        }
    )
    summary = {
        "student_count": student_count,
        "today_records": len(today_records),
        "today_valid_records": sum(1 for record in today_records if _is_valid_record(record)),
        "open_task_count": len(scoped_tasks),
        "high_risk_count": high_risk_count,
        "teacher_count": len(teacher_student_counts),
        "coverage_rate_7d": round(len(covered_names & scoped_names) / student_count * 100) if student_count else 0,
    }
    task_status = {
        "open_by_level": dict(open_by_level),
        "open_by_status": dict(open_by_status),
        "safety_risks": safety_tasks,
        "closure_evidence": {
            "recent_events": [
                _closure_event_card(event)
                for event in sorted(scoped_closure_events, key=lambda item: str(item.get("at") or ""), reverse=True)[:30]
            ],
            "missing_evidence_tasks": closure_gaps[:30],
            "recent_completed_count": sum(1 for event in scoped_closure_events if str(event.get("action") or "") == "completed"),
            "missing_evidence_count": len(closure_gaps),
        },
    }
    risk = {
        "renewal": overview.get("renewal", {}),
        "parent_relationship": overview.get("parent_relationship", {}),
        "priority_students": relationship_risks[:20],
        "long_unrecorded_students": long_unrecorded[:30],
    }
    payroll = {}
    if not is_manager:
        payroll = {
            "month": _as_dict(payroll_snapshot).get("month", now.strftime("%Y-%m")),
            "summary": {
                "teacher_count": len(payroll_items),
                "estimated_total": payroll_total,
                "pending_event_count": sum(
                    int(_as_dict(item.get("events")).get("pending_count") or 0)
                    for item in payroll_items
                ),
                "teacher_confirmed_count": sum(
                    1
                    for item in payroll_items
                    if _as_dict(item.get("review")).get("status") == "confirmed"
                ),
                "teacher_question_count": sum(
                    1
                    for item in payroll_items
                    if _as_dict(item.get("review")).get("status") == "question"
                ),
                "boss_final_confirmed": bool(
                    _as_dict(payroll_snapshot).get("final_confirmation")
                ),
            },
            "teachers": sorted(
                payroll_items,
                key=lambda item: float(item.get("estimated_total") or 0),
                reverse=True,
            ),
        }
    else:
        payroll = {"month": _as_dict(payroll_snapshot).get("month", now.strftime("%Y-%m"))}
    manager_execution = [
        _manager_execution_card(item)
        for item in payroll_items
        if isinstance(item, dict) and str(item.get("position") or "") == "manager"
    ]
    data_quality = _data_quality_report(
        students=scoped_students if campuses is not None else students,
        records=scoped_records if campuses is not None else records,
        tasks=scoped_tasks,
        teacher_names=teacher_names,
        now=now,
    )
    renewal_funnel = _renewal_funnel(
        students=scoped_students,
        store=store,
        now=now,
    )
    summer_import = summer_import_dashboard(store)
    summer_lessons = summer_lesson_dashboard(store)
    summer_reports = summer_report_dashboard(store)
    summer_course_coverage = summer_course_coverage_dashboard(store, now)
    operations_focus = active_operations_focus(store, now=now)
    rule_center = _rule_center(load_payroll_rules(store), data_quality, now)
    hermes_employee = _hermes_employee_snapshot(
        store,
        now=now,
        role=role,
        user_id=user_id,
        summary=summary,
        task_status=task_status,
        teacher_execution=teacher_execution,
    )
    hermes_assistant = _manager_assistant_snapshot(
        summary=summary,
        teacher_execution=teacher_execution,
        hermes_employee=hermes_employee,
        task_status=task_status,
    ) if is_manager else {}
    return {
        "role": role,
        "user_id": user_id,
        "display_name": display_name or user_id,
        "generated_at": now.isoformat(timespec="seconds"),
        "scope": {"campuses": sorted(campuses) if campuses is not None else ["全机构"]},
        "summary": summary,
        "hermes_employee": hermes_employee,
        "hermes_assistant": hermes_assistant,
        "student_roster": [
            {
                "student_name": name,
                "teacher_userid": _student_teacher(profile),
                "campus_id": _student_campus(profile),
            }
            for name, profile in sorted(scoped_students.items())
        ],
        "daily_brief": _boss_daily_brief(
            role=role,
            summary=summary,
            task_status=task_status,
            risk=risk,
            payroll=payroll,
            data_quality=data_quality,
            renewal_funnel=renewal_funnel,
            teacher_execution=teacher_execution,
            operations_focus=operations_focus,
        ),
        "business_focus": operations_focus,
        "task_status": task_status,
        "risk": risk,
        "execution": {
            "managers": manager_execution,
            "teachers": sorted(teacher_execution, key=lambda item: (item["coverage_rate_7d"], item["week_records"])),
            "coverage_rate_7d": round(len(covered_names & scoped_names) / student_count * 100) if student_count else 0,
            "task_completion_hint": "未闭环任务按等级和状态展示",
        },
        "performance": {} if is_manager else {
            "rules": _performance_rules(),
            "estimated_payout": estimated_payout,
            "teachers": sorted(
                performance_items,
                key=lambda item: (
                    int(_as_dict(item.get("performance")).get("rank") or 9999),
                    -float(_as_dict(item.get("performance")).get("month_points") or 0),
                ),
            ),
        },
        "payroll": payroll,
        "summer_import": summer_import,
        "summer_lessons": summer_lessons,
        "summer_reports": summer_reports,
        "summer_course_coverage": summer_course_coverage,
        "data_quality": data_quality,
        "renewal_funnel": renewal_funnel,
        "rule_center": {} if is_manager else rule_center,
        "project_opportunities": project_opportunities,
        "opportunities": [],
        "learning": {
            "pending_count": len(learning_candidates),
            "recent_candidates": [
                {
                    "id": str(item.get("id") or ""),
                    "title": str(item.get("title") or ""),
                    "category": str(item.get("category") or "general"),
                    "candidate_type": str(item.get("candidate_type") or "knowledge"),
                    "content": str(item.get("content") or "")[:160],
                    "created_at": str(item.get("created_at") or ""),
                }
                for item in learning_candidates[:10]
                if isinstance(item, dict)
            ],
        },
        "recent_progress": recent_progress[-10:],
        "recent_anomalies": recent_anomalies[-10:],
    }


def _manager_execution_card(item: dict[str, Any]) -> dict[str, Any]:
    performance = _as_dict(item.get("performance"))
    return {
        "user_id": str(item.get("user_id") or ""),
        "name": str(item.get("name") or item.get("user_id") or "店长"),
        "position_label": str(item.get("position_label") or "店长"),
        "estimated_total": item.get("estimated_total", 0),
        "management_closure_rate": performance.get("management_closure_rate", 0),
        "team_management_rate": performance.get("team_management_rate", 0),
        "risk_service_rate": performance.get("manager_risk_service_rate", 0),
        "team_teacher_count": performance.get("team_teacher_count", 0),
        "team_task_closed": performance.get("team_task_closed", 0),
        "team_task_total": performance.get("team_task_total", 0),
        "team_risk_task_closed": performance.get("team_risk_task_closed", 0),
        "team_risk_task_total": performance.get("team_risk_task_total", 0),
        "team_record_gap_count": performance.get("team_record_gap_count", 0),
        "team_low_quality_count": performance.get("team_low_quality_count", 0),
    }


def build_dashboard_snapshot(
    store: TuoguanStore,
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = _business_naive(now or datetime.now())
    students = _load_students(store)
    records = _load_records(store)
    tasks = store.load_tasks()
    payroll_rules = load_payroll_rules(store)
    teacher_names = _teacher_names(store)
    teacher_ids = {
        _student_teacher(profile)
        for profile in students.values()
        if _student_teacher(profile)
    } | set(teacher_names)
    managers = _manager_campuses(store)
    teacher_dashboards = {}
    for teacher_id in sorted(teacher_ids):
        if not teacher_id:
            continue
        identity = _dashboard_identity(teacher_id, "teacher", teacher_names.get(teacher_id, teacher_id))
        program_scope = user_program_ids(store, identity)
        scoped_students = filter_program_students(students, program_scope)
        scoped_records = filter_program_items(records, program_scope)
        scoped_tasks = filter_program_items(tasks, program_scope)
        teacher_dashboards[teacher_id] = _teacher_dashboard(
            store=store,
            user_id=teacher_id,
            display_name=teacher_names.get(teacher_id, teacher_id),
            students=scoped_students,
            records=scoped_records,
            tasks=scoped_tasks,
            now=timestamp,
            payroll_rules=payroll_rules,
        )
        teacher_dashboards[teacher_id]["program_scope"] = sorted(program_scope or {"global"})
    _apply_performance_rankings(teacher_dashboards)
    payroll_snapshot = build_payroll_snapshot(
        store,
        now=timestamp,
        teacher_dashboards=teacher_dashboards,
    )
    payroll_by_user = {
        str(item.get("user_id") or ""): item
        for item in payroll_snapshot.get("teachers", [])
        if isinstance(item, dict)
    }
    for teacher_id, dashboard in teacher_dashboards.items():
        if teacher_id in payroll_by_user and dashboard.get("program_scope") != [SUMMER_PROGRAM_ID]:
            dashboard["payroll"] = payroll_by_user[teacher_id]
            companion, tree = _teacher_hermes_blocks(
                display_name=str(dashboard.get("display_name") or teacher_id),
                today_records=[
                    record for record in records
                    if (_record_teacher(record, students) == teacher_id or _record_student(record) in {
                        name for name, profile in students.items() if _student_teacher(profile) == teacher_id
                    })
                    and _same_date(record, timestamp)
                ],
                week_records=[
                    record for record in records
                    if (_record_teacher(record, students) == teacher_id or _record_student(record) in {
                        name for name, profile in students.items() if _student_teacher(profile) == teacher_id
                    })
                    and _after(_timestamp(record), timestamp - timedelta(days=7))
                ],
                month_records=[
                    record for record in records
                    if (_record_teacher(record, students) == teacher_id or _record_student(record) in {
                        name for name, profile in students.items() if _student_teacher(profile) == teacher_id
                    })
                    and _after(_timestamp(record), _month_start(timestamp))
                ],
                material_records=_as_list(_as_dict(dashboard.get("materials")).get("quality_records")),
                suggested_records=_as_list(dashboard.get("suggested_records")),
                performance=_as_dict(dashboard.get("performance")),
                payroll_rules=payroll_rules,
                payroll_item=payroll_by_user[teacher_id],
            )
            dashboard["hermes_companion"] = companion
            dashboard["hermes_performance_tree"] = tree
        if dashboard.get("program_scope") == [SUMMER_PROGRAM_ID]:
            dashboard["program_role"] = "summer_teacher"
            dashboard["default_program_id"] = SUMMER_PROGRAM_ID
            dashboard["visible_modules"] = ["课节记录", "重点孩子", "孩子覆盖", "待处理任务"]
            dashboard["denied_capabilities"] = [
                "owner_dashboard", "regular_data", "payroll", "performance", "coupon",
                "renewal", "project_config", "formal_student_archive", "report_publish",
            ]
            dashboard["performance"] = {}
            dashboard["payroll"] = {}
            dashboard["record_templates"] = {}
    boss_dashboard = _boss_dashboard(
        role="boss",
        user_id="boss",
        display_name="老板",
        campuses=None,
        students=students,
        records=records,
        tasks=tasks,
        teacher_names=teacher_names,
        teacher_dashboards=teacher_dashboards,
        payroll_snapshot=payroll_snapshot,
        now=timestamp,
        store=store,
    )
    boss_dashboard["program_views"] = {
        program_id: {
            "program_id": program_id,
            "program_name": str(item.get("name") or program_id),
            "status": str(item.get("status") or "configured"),
            "student_count": len(filter_program_students(students, {program_id})),
            "record_count": len(filter_program_items(records, {program_id})),
            "open_task_count": len([task for task in filter_program_items(tasks, {program_id}) if _task_open(task)]),
        }
        for program_id, item in load_programs(store).items()
    }
    boss_dashboard["program_role"] = "global_owner"
    boss_dashboard["default_program_id"] = "global"
    boss_dashboard["selected_program_id"] = "global"
    boss_dashboard["program_scope"] = ["global", REGULAR_PROGRAM_ID, SUMMER_PROGRAM_ID]
    boss_dashboard["visible_modules"] = ["项目总览", "经营", "工资", "券", "绩效", "风险", "任务", "学生家长"]
    boss_project_dashboards: dict[str, dict[str, Any]] = {}
    for program_id in (REGULAR_PROGRAM_ID, SUMMER_PROGRAM_ID):
        project_students = filter_program_students(students, {program_id})
        project_records = filter_program_items(records, {program_id})
        project_tasks = filter_program_items(tasks, {program_id})
        project_teacher_dashboards = {
            teacher_id: dashboard
            for teacher_id, dashboard in teacher_dashboards.items()
            if program_id in set(dashboard.get("program_scope") or [])
        }
        project_dashboard = _boss_dashboard(
            role="boss",
            user_id="boss",
            display_name="老板",
            campuses=None,
            students=project_students,
            records=project_records,
            tasks=project_tasks,
            teacher_names=teacher_names,
            teacher_dashboards=project_teacher_dashboards,
            payroll_snapshot=payroll_snapshot if program_id == REGULAR_PROGRAM_ID else {},
            now=timestamp,
            store=store,
            project_id=program_id,
        )
        project_dashboard["program_views"] = boss_dashboard["program_views"]
        project_dashboard["program_role"] = "global_owner"
        project_dashboard["default_program_id"] = "global"
        project_dashboard["selected_program_id"] = program_id
        project_dashboard["program_scope"] = [program_id]
        project_dashboard["visible_modules"] = boss_dashboard["visible_modules"]
        if program_id == REGULAR_PROGRAM_ID:
            project_dashboard["summer_import"] = {}
            project_dashboard["summer_lessons"] = {}
            project_dashboard["summer_reports"] = {}
            project_dashboard["summer_course_coverage"] = {}
        else:
            project_dashboard["payroll"] = {}
            project_dashboard["performance"] = {}
            project_dashboard["renewal_funnel"] = {}
        boss_project_dashboards[program_id] = project_dashboard
    manager_dashboards: dict[str, dict[str, Any]] = {}
    for manager_id, campuses in managers.items():
        identity = _dashboard_identity(manager_id, "manager", teacher_names.get(manager_id, manager_id))
        program_scope = user_program_ids(store, identity)
        scoped_students = filter_program_students(students, program_scope)
        scoped_records = filter_program_items(records, program_scope)
        scoped_tasks = filter_program_items(tasks, program_scope)
        dashboard = _boss_dashboard(
            role="manager",
            user_id=manager_id,
            display_name=teacher_names.get(manager_id, manager_id),
            campuses=campuses,
            students=scoped_students,
            records=scoped_records,
            tasks=scoped_tasks,
            teacher_names=teacher_names,
            teacher_dashboards=teacher_dashboards,
            payroll_snapshot=payroll_snapshot,
            now=timestamp,
            store=store,
        )
        manager_dashboards[manager_id] = _sanitize_manager_dashboard(dashboard, program_scope)
    return {
        "schema_version": 1,
        "generated_at": timestamp.isoformat(timespec="seconds"),
        "teacher_dashboards": teacher_dashboards,
        "payroll": payroll_snapshot,
        "boss_dashboard": boss_dashboard,
        "boss_project_dashboards": boss_project_dashboards,
        "manager_dashboards": manager_dashboards,
        "parent_portal_reserved": {
            "enabled": False,
            "source": "approved_growth_reports_only",
            "sensitive_fields_blocked": [
                "raw_records",
                "internal_negative_notes",
                "safety_incident_details",
                "renewal_risk_reasoning",
                "teacher_internal_comments",
            ],
        },
    }


def refresh_dashboard_cache(
    store: TuoguanStore | None = None,
    now: datetime | None = None,
    *,
    create_operation_tasks: bool = False,
) -> dict[str, Any]:
    actual_store = store or TuoguanStore()
    created_priority_tasks = []
    created_periodic_tasks = []
    if create_operation_tasks:
        created_priority_tasks = ensure_priority_followup_tasks(actual_store, now=now)
        created_periodic_tasks = ensure_periodic_operation_tasks(actual_store, now=now)
        ensure_friday_summer_weekly_feedbacks(actual_store, now=now)
    snapshot = build_dashboard_snapshot(actual_store, now=now)
    from .write_guard import authorized_system_write

    with authorized_system_write(
        actual_store.data_dir,
        job_name="dashboard_cache_refresh",
        allowed_files={CACHE_FILE},
    ):
        actual_store.write_json(CACHE_FILE, snapshot)
    return {
        "generated_at": snapshot["generated_at"],
        "teacher_dashboards": len(snapshot.get("teacher_dashboards") or {}),
        "manager_dashboards": len(snapshot.get("manager_dashboards") or {}),
        "has_boss_dashboard": bool(snapshot.get("boss_dashboard")),
        "created_priority_followup_tasks": len(created_priority_tasks),
        "created_periodic_operation_tasks": len(created_periodic_tasks),
    }


def load_dashboard_cache(
    store: TuoguanStore,
    *,
    build_if_missing: bool = True,
) -> dict[str, Any]:
    cache = store.read_json(CACHE_FILE, {})
    if isinstance(cache, dict) and cache.get("schema_version") == 1:
        return cache
    if not build_if_missing:
        return {}
    refresh_dashboard_cache(store)
    return store.read_json(CACHE_FILE, {})
