"""Payroll rules and event capture for the tutoring-center module."""

from __future__ import annotations

import calendar
import csv
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .models import UserIdentity
from .store import TuoguanStore
from .tenant_context import institution_display_names
from .tasks import task_is_closed, task_is_open


PAYROLL_EVENTS_FILE = "payroll_events.json"
PAYROLL_CONFIRMATIONS_FILE = "payroll_confirmations.json"
PAYROLL_SETTLEMENTS_FILE = "payroll_settlements.json"
PAYROLL_RULES_FILE = "payroll_rules.json"

_CN_NUMBERS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


@dataclass(frozen=True)
class PayrollEventDraft:
    event_type: str
    target_user_id: str
    target_name: str
    event_date: str
    quantity: float
    amount_delta: float | None
    reason: str
    source_text: str


def default_payroll_rules() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "currency": "CNY",
        "positions": {
            "part_time": {
                "label": "兼职老师",
                "per_shift_amount": 70,
                "shift_breakdown": {
                    "base_attendance": 45,
                    "record_performance": 8,
                    "execution_performance": 7,
                    "safety_responsibility": 5,
                    "admission_performance": 5,
                },
                "default_admission_target": 2,
            },
            "full_time_teacher": {
                "label": "全职老师",
                "base_salary": 2000,
                "attendance_bonus": 200,
                "performance_total": 600,
                "performance_breakdown": {
                    "record_performance": 180,
                    "execution_performance": 170,
                    "service_closure": 100,
                    "admission_performance": 150,
                },
                "saturday_duty_amount": 30,
                "default_admission_target": 5,
            },
            "manager": {
                "label": "店长",
                "base_salary": 2200,
                "attendance_bonus": 200,
                "performance_total": 800,
                "performance_breakdown": {
                    "personal_record_execution": 200,
                    "team_management": 200,
                    "risk_service_closure": 150,
                    "admission_performance": 250,
                },
                "saturday_duty_amount": 30,
                "default_admission_target": 8,
            },
        },
        "calendar": {
            "temporary_closed_affects_attendance_bonus": False,
            "leave_cancels_attendance_bonus": True,
            "absence_cancels_attendance_bonus": True,
        },
        "admission": {
            "monthly_targets_can_override_by_person": True,
            "zero_target_policy": "no_deduction",
        },
        "record_performance": {
            "mode": "required_first",
            "required_weight": 0.75,
            "quality_weight": 0.25,
            "parent_communication_required": "per_student_monthly",
            "growth_observation_required": "per_student_monthly",
            "parent_communication_monthly_required_count": 1,
            "growth_observation_monthly_required_count": 4,
            "low_quality_policy": "archive_no_payroll",
        },
    }


def load_payroll_rules(store: TuoguanStore) -> dict[str, Any]:
    rules = store.read_json(PAYROLL_RULES_FILE, {})
    if not isinstance(rules, dict) or not rules:
        return default_payroll_rules()
    merged = default_payroll_rules()
    _deep_update(merged, rules)
    return merged


def save_default_payroll_rules_if_missing(store: TuoguanStore) -> dict[str, Any]:
    existing = store.read_json(PAYROLL_RULES_FILE, {})
    if isinstance(existing, dict) and existing:
        return load_payroll_rules(store)
    rules = default_payroll_rules()
    store.write_json(PAYROLL_RULES_FILE, rules)
    return rules


def build_payroll_snapshot(
    store: TuoguanStore,
    *,
    now: datetime | None = None,
    teacher_dashboards: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    timestamp = now or datetime.now()
    month = timestamp.strftime("%Y-%m")
    rules = load_payroll_rules(store)
    events = _month_events(store, timestamp)
    tasks = _month_tasks(store, timestamp)
    confirmations = _month_confirmations(store, timestamp)
    people = _payroll_people(store, rules)
    teacher_dashboards = teacher_dashboards or {}
    manager_team_contexts = _manager_team_contexts(store, teacher_dashboards, tasks, now=timestamp)
    summaries = [
        _person_payroll_summary(
            user_id=user_id,
            person=person,
            rules=rules,
            events=events,
            tasks=tasks,
            month=month,
            now=timestamp,
            teacher_dashboard=teacher_dashboards.get(user_id, {}),
            team_context=manager_team_contexts.get(user_id, {}),
            confirmations=confirmations,
        )
        for user_id, person in sorted(people.items(), key=lambda item: item[1]["name"])
    ]
    total = round(sum(float(item.get("estimated_total") or 0) for item in summaries), 2)
    pending_events = sum(1 for event in events if not bool(event.get("confirmed")))
    confirmed_count = sum(1 for item in summaries if _as_dict(item.get("review")).get("status") == "confirmed")
    question_count = sum(1 for item in summaries if _as_dict(item.get("review")).get("status") == "question")
    boss_final = _boss_final_confirmation(confirmations)
    settlement = latest_payroll_settlement(store, month)
    return {
        "schema_version": 1,
        "generated_at": timestamp.isoformat(timespec="seconds"),
        "month": month,
        "rules": {
            "currency": rules.get("currency", "CNY"),
            "leave_cancels_attendance_bonus": _as_bool(
                _as_dict(rules.get("calendar")).get("leave_cancels_attendance_bonus"),
                True,
            ),
            "absence_cancels_attendance_bonus": _as_bool(
                _as_dict(rules.get("calendar")).get("absence_cancels_attendance_bonus"),
                True,
            ),
        },
        "summary": {
            "teacher_count": len(summaries),
            "estimated_total": total,
            "event_count": len(events),
            "pending_event_count": pending_events,
            "teacher_confirmed_count": confirmed_count,
            "teacher_question_count": question_count,
            "boss_final_confirmed": bool(boss_final),
            "settlement_locked": bool(settlement),
        },
        "final_confirmation": boss_final,
        "settlement": settlement,
        "teachers": summaries,
    }


def classify_payroll_event(
    text: str,
    identity: UserIdentity,
    store: TuoguanStore,
    *,
    now: datetime | None = None,
) -> PayrollEventDraft | None:
    if identity.role not in {"manager", "boss"}:
        return None
    raw = str(text or "").strip()
    if not raw:
        return None
    timestamp = now or datetime.now()
    event_date = _event_date(raw, timestamp)
    calendar = _classify_calendar_event(raw)
    if calendar:
        return PayrollEventDraft(
            event_type=calendar,
            target_user_id="",
            target_name="全校区",
            event_date=event_date,
            quantity=1,
            amount_delta=None,
            reason=_calendar_reason(calendar),
            source_text=raw,
        )
    teacher = _match_teacher(raw, store)
    if teacher is None:
        return None
    target_name, target_user_id = teacher
    attendance = _classify_teacher_event(raw)
    if attendance:
        return PayrollEventDraft(
            event_type=attendance,
            target_user_id=target_user_id,
            target_name=target_name,
            event_date=event_date,
            quantity=_quantity(raw),
            amount_delta=30 if attendance == "saturday_duty" else None,
            reason=_teacher_event_reason(attendance),
            source_text=raw,
        )
    if _is_admission_event(raw):
        return PayrollEventDraft(
            event_type="admission_confirmed",
            target_user_id=target_user_id,
            target_name=target_name,
            event_date=event_date,
            quantity=max(1, _quantity(raw)),
            amount_delta=None,
            reason="招生确认",
            source_text=raw,
        )
    return None


def append_payroll_event(
    draft: PayrollEventDraft,
    identity: UserIdentity,
    store: TuoguanStore,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = now or datetime.now()
    events = store.read_json(PAYROLL_EVENTS_FILE, [])
    if not isinstance(events, list):
        events = []
    event = {
        "id": f"payroll-{timestamp.strftime('%Y%m%d%H%M%S%f')}",
        "event_type": draft.event_type,
        "event_date": draft.event_date,
        "target_user_id": draft.target_user_id,
        "target_name": draft.target_name,
        "quantity": draft.quantity,
        "amount_delta": draft.amount_delta,
        "reason": draft.reason,
        "source_text": draft.source_text,
        "recorded_by": identity.canonical_user_id,
        "recorded_by_role": identity.role,
        "recorded_at": timestamp.isoformat(timespec="seconds"),
        "confirmed": identity.role == "boss",
    }
    events.append(event)
    store.write_json(PAYROLL_EVENTS_FILE, events[-5000:])
    save_default_payroll_rules_if_missing(store)
    return event


def payroll_event_reply(event: dict[str, Any]) -> str:
    labels = {
        "attendance_present": "出勤",
        "attendance_leave": "请假",
        "attendance_absent": "缺勤",
        "calendar_temporary_closed": "临时停课",
        "calendar_statutory_holiday": "法定节假日",
        "saturday_duty": "周六上午值班",
        "admission_confirmed": "招生确认",
    }
    label = labels.get(str(event.get("event_type") or ""), "工资事件")
    target = str(event.get("target_name") or "全校区")
    quantity = event.get("quantity")
    extra = ""
    if event.get("event_type") == "admission_confirmed":
        extra = f"，数量：{quantity:g}人"
    elif event.get("event_type") == "saturday_duty":
        extra = f"，补贴：{event.get('amount_delta') or 30}元"
    confirm = "已确认" if event.get("confirmed") else "待老板确认"
    return f"已记录工资事件：{target} {label}{extra}，日期：{event.get('event_date')}，状态：{confirm}。"


def classify_payroll_review(text: str, identity: UserIdentity) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    compact = raw.replace(" ", "")
    if not compact:
        return None
    if identity.role == "boss" and any(word in compact for word in ("确认本月工资结算", "本月工资结算确认", "工资最终确认", "确认工资结算")):
        return {"status": "boss_final_confirmed", "note": raw}
    if any(word in compact for word in ("工资有疑问", "工资不对", "工资有问题", "工资算错", "工资疑问")):
        return {"status": "question", "note": raw}
    if any(word in compact for word in ("工资确认无误", "工资没问题", "确认工资无误", "工资无误", "确认工资")):
        return {"status": "confirmed", "note": raw}
    return None


def append_payroll_review(
    review: dict[str, Any],
    identity: UserIdentity,
    store: TuoguanStore,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = now or datetime.now()
    month = timestamp.strftime("%Y-%m")
    data = store.read_json(PAYROLL_CONFIRMATIONS_FILE, [])
    if not isinstance(data, list):
        data = []
    item = {
        "id": f"payroll-review-{timestamp.strftime('%Y%m%d%H%M%S%f')}",
        "month": month,
        "user_id": identity.canonical_user_id,
        "role": identity.role,
        "status": str(review.get("status") or ""),
        "note": str(review.get("note") or ""),
        "created_at": timestamp.isoformat(timespec="seconds"),
    }
    data.append(item)
    store.write_json(PAYROLL_CONFIRMATIONS_FILE, data[-5000:])
    return item


def payroll_review_reply(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "")
    if status == "boss_final_confirmed":
        return f"已记录老板本月工资最终确认，月份：{item.get('month')}。后续可基于当前工资预估生成结算快照。"
    if status == "question":
        return "已记录你的工资疑问，老板端工资表会显示“有疑问”。请补充具体是哪一项有问题，方便核对。"
    if status == "confirmed":
        return f"已记录你的工资确认无误，月份：{item.get('month')}。月底仍以老板最终确认结算为准。"
    return "已记录工资确认状态。"


def classify_payroll_settlement(text: str, identity: UserIdentity) -> bool:
    if identity.role != "boss":
        return False
    compact = str(text or "").replace(" ", "")
    return any(word in compact for word in ("生成工资结算快照", "锁定工资结算", "生成本月工资结算", "工资结算快照"))


def classify_payroll_export(text: str, identity: UserIdentity) -> bool:
    if identity.role != "boss":
        return False
    compact = str(text or "").replace(" ", "")
    return any(word in compact for word in ("导出工资表", "工资表导出", "导出本月工资表", "导出工资结算表"))


def create_payroll_settlement(
    store: TuoguanStore,
    identity: UserIdentity,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    timestamp = now or datetime.now()
    month = timestamp.strftime("%Y-%m")
    existing = latest_payroll_settlement(store, month)
    if existing and not force:
        return {**existing, "already_exists": True}
    try:
        from .dashboard_builder import refresh_dashboard_cache

        refresh_dashboard_cache(store, now=timestamp)
    except Exception:
        pass
    validation = validate_payroll_settlement_readiness(store, now=timestamp)
    if not validation.get("ok"):
        return {"ok": False, "reason": "validation_failed", "month": month, "validation": validation}
    snapshot = build_payroll_snapshot(store, now=timestamp)
    settlement = {
        "id": f"payroll-settlement-{month}-{timestamp.strftime('%Y%m%d%H%M%S')}",
        "schema_version": 1,
        "month": month,
        "created_at": timestamp.isoformat(timespec="seconds"),
        "created_by": identity.canonical_user_id,
        "created_by_role": identity.role,
        "estimated_total": _as_dict(snapshot.get("summary")).get("estimated_total", 0),
        "teacher_count": _as_dict(snapshot.get("summary")).get("teacher_count", 0),
        "teacher_confirmed_count": _as_dict(snapshot.get("summary")).get("teacher_confirmed_count", 0),
        "teacher_question_count": _as_dict(snapshot.get("summary")).get("teacher_question_count", 0),
        "boss_final_confirmed": _as_dict(snapshot.get("summary")).get("boss_final_confirmed", False),
        "teachers": deepcopy(snapshot.get("teachers") or []),
    }
    settlements = store.read_json(PAYROLL_SETTLEMENTS_FILE, [])
    if not isinstance(settlements, list):
        settlements = []
    settlements.append(settlement)
    store.write_json(PAYROLL_SETTLEMENTS_FILE, settlements[-200:])
    return settlement


def latest_payroll_settlement(store: TuoguanStore, month: str) -> dict[str, Any] | None:
    latest = load_latest_payroll_settlement(store, month)
    if latest is None:
        return None
    return {
        "id": str(latest.get("id") or ""),
        "month": str(latest.get("month") or month),
        "created_at": str(latest.get("created_at") or ""),
        "created_by": str(latest.get("created_by") or ""),
        "estimated_total": latest.get("estimated_total", 0),
        "teacher_count": latest.get("teacher_count", 0),
        "teacher_confirmed_count": latest.get("teacher_confirmed_count", 0),
        "teacher_question_count": latest.get("teacher_question_count", 0),
        "boss_final_confirmed": bool(latest.get("boss_final_confirmed")),
    }


def load_latest_payroll_settlement(store: TuoguanStore, month: str) -> dict[str, Any] | None:
    settlements = store.read_json(PAYROLL_SETTLEMENTS_FILE, [])
    if not isinstance(settlements, list):
        return None
    candidates = [
        item
        for item in settlements
        if isinstance(item, dict) and str(item.get("month") or "") == month
    ]
    if not candidates:
        return None
    latest = sorted(candidates, key=lambda item: str(item.get("created_at") or ""))[-1]
    return deepcopy(latest)


def validate_payroll_settlement_readiness(
    store: TuoguanStore,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = now or datetime.now()
    month_start = timestamp.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    students = store.read_json("students.json", {})
    records = store.read_json("records.json", [])
    tasks = store.load_tasks()
    if not isinstance(students, dict):
        students = {}
    if not isinstance(records, list):
        records = []
    student_names = {str(name) for name in students}
    month_records = [
        record
        for record in records
        if isinstance(record, dict)
        and (_record_timestamp(record) is not None and _record_timestamp(record) >= month_start)
    ]
    missing_evaluation = [
        {
            "record_id": str(record.get("id") or ""),
            "student_name": str(record.get("student_name") or record.get("student") or ""),
            "timestamp": str(record.get("timestamp") or record.get("created_at") or ""),
        }
        for record in month_records
        if not isinstance(record.get("record_evaluation"), dict)
    ]
    orphan_records = [
        {
            "record_id": str(record.get("id") or ""),
            "student_name": str(record.get("student_name") or record.get("student") or ""),
            "timestamp": str(record.get("timestamp") or record.get("created_at") or ""),
        }
        for record in month_records
        if str(record.get("student_name") or record.get("student") or "")
        and str(record.get("student_name") or record.get("student") or "") not in student_names
    ]
    open_high_tasks = [
        {
            "task_id": str(task.get("id") or ""),
            "student_name": str(task.get("student_name") or ""),
            "level": str(task.get("level") or ""),
            "title": str(task.get("title") or task.get("summary") or "")[:80],
        }
        for task in tasks
        if isinstance(task, dict)
        and str(task.get("level") or "") in {"S", "A"}
        and task_is_open(task)
    ]
    blocking: list[str] = []
    if missing_evaluation:
        blocking.append(f"本月有 {len(missing_evaluation)} 条记录缺少工资级评估，请先补评估。")
    if orphan_records:
        blocking.append(f"本月有 {len(orphan_records)} 条记录的学生不在学生档案中，请先处理归属或归档。")
    warnings: list[str] = []
    if open_high_tasks:
        warnings.append(f"仍有 {len(open_high_tasks)} 个 S/A 级任务未闭环，会影响执行/服务闭环绩效。")
    return {
        "ok": not blocking,
        "month": timestamp.strftime("%Y-%m"),
        "blocking_issues": blocking,
        "warnings": warnings,
        "missing_evaluation_records": missing_evaluation[:30],
        "orphan_records": orphan_records[:30],
        "open_high_tasks": open_high_tasks[:30],
    }


def export_payroll_settlement(
    store: TuoguanStore,
    identity: UserIdentity,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = now or datetime.now()
    month = timestamp.strftime("%Y-%m")
    if identity.role != "boss":
        return {"ok": False, "reason": "forbidden"}
    settlement = load_latest_payroll_settlement(store, month)
    if settlement is None:
        return {"ok": False, "reason": "missing_settlement", "month": month}

    export_dir = store.data_dir / "exports" / "payroll"
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / f"payroll-{month}.csv"
    teachers = [item for item in settlement.get("teachers") or [] if isinstance(item, dict)]
    headers = [
        "月份",
        "姓名",
        "user_id",
        "岗位",
        "预计工资",
        "确认状态",
        "出勤次数",
        "请假天数",
        "缺勤天数",
        "临时停课天数",
        "招生完成",
        "招生目标",
        "招生完成率",
        "周六值班次数",
        "周六值班补贴",
        "明细",
        "疑问/备注",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for teacher in teachers:
            attendance = _as_dict(teacher.get("attendance"))
            performance = _as_dict(teacher.get("performance"))
            review = _as_dict(teacher.get("review"))
            writer.writerow(
                {
                    "月份": str(teacher.get("month") or month),
                    "姓名": str(teacher.get("name") or ""),
                    "user_id": str(teacher.get("user_id") or ""),
                    "岗位": str(teacher.get("position_label") or teacher.get("position") or ""),
                    "预计工资": _money(teacher.get("estimated_total")),
                    "确认状态": str(review.get("label") or review.get("status") or "待本人确认"),
                    "出勤次数": _number(attendance.get("present_count")),
                    "请假天数": _number(attendance.get("leave_days")),
                    "缺勤天数": _number(attendance.get("absent_days")),
                    "临时停课天数": _number(attendance.get("temporary_closed_days")),
                    "招生完成": _number(performance.get("admission_count")),
                    "招生目标": _number(performance.get("admission_target")),
                    "招生完成率": f"{_number(performance.get('admission_rate'))}%",
                    "周六值班次数": _number(performance.get("saturday_duty_count")),
                    "周六值班补贴": _money(performance.get("saturday_duty_amount")),
                    "明细": _export_breakdown_details(teacher),
                    "疑问/备注": str(review.get("note") or ""),
                }
            )
    return {
        "ok": True,
        "month": month,
        "path": str(path),
        "teacher_count": len(teachers),
        "estimated_total": settlement.get("estimated_total", 0),
        "created_at": timestamp.isoformat(timespec="seconds"),
    }


def payroll_settlement_reply(settlement: dict[str, Any]) -> str:
    if settlement.get("reason") == "validation_failed":
        validation = _as_dict(settlement.get("validation"))
        problems = validation.get("blocking_issues") or []
        lines = [
            "本月工资结算快照暂未生成：工资依据还不完整。",
            f"月份：{settlement.get('month')}",
        ]
        lines.extend(f"- {item}" for item in problems[:8])
        lines.append("请先处理以上数据问题后，再发送：生成工资结算快照")
        return "\n".join(lines)
    if settlement.get("already_exists"):
        return (
            "本月工资结算快照已经存在，不会重复覆盖。\n"
            f"月份：{settlement.get('month')}\n"
            f"总额：{settlement.get('estimated_total')} 元\n"
            f"生成时间：{settlement.get('created_at')}"
        )
    return (
        "已生成本月工资结算快照。\n"
        f"月份：{settlement.get('month')}\n"
        f"计薪对象：{settlement.get('teacher_count')} 人\n"
        f"总额：{settlement.get('estimated_total')} 元\n"
        f"已确认：{settlement.get('teacher_confirmed_count')} 人，有疑问：{settlement.get('teacher_question_count')} 人。"
    )


def payroll_export_reply(result: dict[str, Any]) -> str:
    reason = str(result.get("reason") or "")
    if reason == "forbidden":
        return "只有老板账号可以导出工资表。"
    if reason == "missing_settlement":
        return (
            "本月还没有工资结算快照，暂时不能导出固定工资表。\n"
            "请老板先发送：生成工资结算快照\n"
            "确认快照无误后，再发送：导出工资表"
        )
    if not result.get("ok"):
        return "工资表导出失败，请稍后再试或联系管理员查看日志。"
    return (
        "已导出本月工资表。\n"
        f"月份：{result.get('month')}\n"
        f"计薪对象：{result.get('teacher_count')} 人\n"
        f"总额：{result.get('estimated_total')} 元\n"
        f"文件：{result.get('path')}\n"
        "这个表格来自已锁定的工资结算快照，适合老板发工资前核对。"
    )


def _person_payroll_summary(
    *,
    user_id: str,
    person: dict[str, Any],
    rules: dict[str, Any],
    events: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    month: str,
    now: datetime,
    teacher_dashboard: dict[str, Any],
    team_context: dict[str, Any] | None,
    confirmations: list[dict[str, Any]],
) -> dict[str, Any]:
    position = str(person.get("position") or "part_time")
    position_rules = _as_dict(_as_dict(rules.get("positions")).get(position))
    person_events = [
        event
        for event in events
        if str(event.get("target_user_id") or "") == user_id
    ]
    calendar_events = [
        event
        for event in events
        if not str(event.get("target_user_id") or "")
    ]
    leave_days = _event_quantity(person_events, "attendance_leave")
    absent_days = _event_quantity(person_events, "attendance_absent")
    attendance_count = _event_quantity(person_events, "attendance_present")
    duty_count = _event_quantity(person_events, "saturday_duty")
    admission_count = _event_quantity(person_events, "admission_confirmed")
    temporary_closed_days = _event_quantity(calendar_events, "calendar_temporary_closed")
    record_rate = _record_completion_rate(teacher_dashboard)
    task_context = _task_performance_context(user_id, tasks, now=now)
    admission_target = _admission_target(rules, person, position_rules, month)
    admission_rate = 1.0 if admission_target <= 0 else min(admission_count / admission_target, 1.0)
    attendance_bonus = float(position_rules.get("attendance_bonus") or 0)
    attendance_bonus_actual = 0.0 if (leave_days > 0 or absent_days > 0) else attendance_bonus

    if position == "part_time":
        detail_items = _part_time_payroll(
            position_rules=position_rules,
            attendance_count=attendance_count,
            record_rate=record_rate,
            admission_rate=admission_rate,
            admission_count=admission_count,
            admission_target=admission_target,
            teacher_dashboard=teacher_dashboard,
            task_context=task_context,
        )
    else:
        detail_items = _monthly_payroll(
            position=position,
            position_rules=position_rules,
            now=now,
            temporary_closed_days=temporary_closed_days,
            leave_days=leave_days,
            absent_days=absent_days,
            attendance_bonus_actual=attendance_bonus_actual,
            record_rate=record_rate,
            admission_rate=admission_rate,
            admission_count=admission_count,
            admission_target=admission_target,
            teacher_dashboard=teacher_dashboard,
            task_context=task_context,
            team_context=team_context or {},
        )

    duty_amount = float(position_rules.get("saturday_duty_amount") or 0)
    saturday_duty = round(duty_count * duty_amount, 2)
    if saturday_duty:
        detail_items.append(
            _detail(
                "saturday_duty",
                "周六值班",
                saturday_duty,
                rule=f"周六上午值班 {duty_amount:g} 元/次",
                formula=f"{duty_count:g} 次 × {duty_amount:g} = {saturday_duty:g}",
                evidence=[f"本月已记录周六值班 {duty_count:g} 次"],
            )
        )
    estimated_total = round(sum(float(item["amount"]) for item in detail_items), 2)
    breakdown = {item["key"]: round(float(item["amount"]), 2) for item in detail_items}
    pending_events = sum(1 for event in person_events if not bool(event.get("confirmed")))
    latest_review = _latest_user_confirmation(confirmations, user_id)
    performance = {
        "record_rate": round(record_rate * 100),
        "record_points": _as_dict(teacher_dashboard.get("performance")).get("month_points", 0),
        "record_required_rate": _as_dict(_as_dict(teacher_dashboard.get("performance")).get("monthly_requirements")).get("required_rate", 0),
        "record_quality_rate": _as_dict(_as_dict(teacher_dashboard.get("performance")).get("monthly_requirements")).get("quality_rate", 0),
        "record_missing_required": list(_as_dict(_as_dict(teacher_dashboard.get("performance")).get("monthly_requirements")).get("missing_required") or [])[:20],
        "record_excluded_records": list(_as_dict(_as_dict(teacher_dashboard.get("performance")).get("monthly_requirements")).get("excluded_records") or [])[:20],
        "execution_rate": task_context["execution_percent"],
        "service_closure_rate": task_context["service_percent"],
        "task_total": task_context["total"],
        "task_closed": task_context["closed"],
        "risk_task_total": task_context["service_total"],
        "risk_task_closed": task_context["service_closed"],
        "task_evidence_records": list(task_context.get("task_evidence_records") or [])[:20],
        "admission_target": admission_target,
        "admission_count": admission_count,
        "admission_rate": round(admission_rate * 100),
        "saturday_duty_count": duty_count,
        "saturday_duty_amount": saturday_duty,
    }
    if position == "manager":
        team_context = team_context or {}
        performance.update({
            "management_closure_rate": round(float(team_context.get("management_closure_rate") or 0) * 100),
            "team_management_rate": round(float(team_context.get("team_management_rate") or 0) * 100),
            "manager_risk_service_rate": round(float(team_context.get("risk_service_rate") or 0) * 100),
            "team_teacher_count": int(team_context.get("team_teacher_count") or 0),
            "team_task_total": int(team_context.get("team_task_total") or 0),
            "team_task_closed": int(team_context.get("team_task_closed") or 0),
            "team_risk_task_total": int(team_context.get("risk_task_total") or 0),
            "team_risk_task_closed": int(team_context.get("risk_task_closed") or 0),
            "task_evidence_records": list(team_context.get("task_evidence_records") or [])[:20],
            "team_record_gap_count": int(team_context.get("record_gap_count") or 0),
            "team_low_quality_count": int(team_context.get("low_quality_count") or 0),
        })
    return {
        "user_id": user_id,
        "name": str(person.get("name") or user_id),
        "position": position,
        "position_label": str(position_rules.get("label") or position),
        "month": month,
        "estimated_total": estimated_total,
        "breakdown": breakdown,
        "breakdown_details": detail_items,
        "attendance": {
            "present_count": attendance_count,
            "leave_days": leave_days,
            "absent_days": absent_days,
            "temporary_closed_days": temporary_closed_days,
            "attendance_bonus_actual": attendance_bonus_actual,
        },
        "performance": performance,
        "events": {
            "count": len(person_events),
            "pending_count": pending_events,
            "recent": sorted(
                person_events,
                key=lambda item: str(item.get("recorded_at") or item.get("event_date") or ""),
                reverse=True,
            )[:6],
        },
        "review": latest_review or {
            "status": "pending",
            "label": "待本人确认",
            "note": "",
            "created_at": "",
        },
    }


def _part_time_payroll(
    *,
    position_rules: dict[str, Any],
    attendance_count: float,
    record_rate: float,
    admission_rate: float,
    admission_count: float,
    admission_target: float,
    teacher_dashboard: dict[str, Any],
    task_context: dict[str, Any],
) -> list[dict[str, Any]]:
    breakdown = _as_dict(position_rules.get("shift_breakdown"))
    base = attendance_count * float(breakdown.get("base_attendance") or 0)
    record_pool = attendance_count * float(breakdown.get("record_performance") or 0)
    execution_pool = attendance_count * float(breakdown.get("execution_performance") or 0)
    safety_pool = attendance_count * float(breakdown.get("safety_responsibility") or 0)
    admission_pool = attendance_count * float(breakdown.get("admission_performance") or 0)
    record_context = _record_performance_context(teacher_dashboard)
    execution_rate = float(task_context.get("execution_rate") or 0)
    service_rate = float(task_context.get("service_rate") or 0)
    return [
        _detail(
            "base_attendance",
            "基础出勤",
            base,
            rule=f"兼职单次 70 元中，基础出勤为 {float(breakdown.get('base_attendance') or 0):g} 元/次",
            formula=f"{attendance_count:g} 次 × {float(breakdown.get('base_attendance') or 0):g} = {base:g}",
            evidence=[f"本月已记录出勤 {attendance_count:g} 次"],
        ),
        _detail(
            "record_performance",
            "记录绩效",
            record_pool * record_rate,
            rule=f"兼职单次记录绩效池 {float(breakdown.get('record_performance') or 0):g} 元/次，按本月必达记录完成度为主、优质记录为辅发放",
            formula=f"{record_pool:g} × {round(record_rate * 100):g}% = {record_pool * record_rate:g}",
            evidence=record_context["evidence"],
        ),
        _detail(
            "execution_performance",
            "执行绩效",
            execution_pool * execution_rate,
            rule=f"兼职单次执行绩效池 {float(breakdown.get('execution_performance') or 0):g} 元/次，按本月任务闭环率发放",
            formula=f"{execution_pool:g} × {round(execution_rate * 100):g}% = {execution_pool * execution_rate:g}",
            evidence=task_context["execution_evidence"],
        ),
        _detail(
            "safety_responsibility",
            "安全责任",
            safety_pool * service_rate,
            rule=f"兼职单次安全责任池 {float(breakdown.get('safety_responsibility') or 0):g} 元/次",
            formula=f"{safety_pool:g} × {round(service_rate * 100):g}% = {safety_pool * service_rate:g}",
            evidence=task_context["service_evidence"],
        ),
        _detail(
            "admission_performance",
            "招生绩效",
            admission_pool * admission_rate,
            rule=f"兼职单次招生绩效池 {float(breakdown.get('admission_performance') or 0):g} 元/次，按个人招生目标完成率发放",
            formula=f"{admission_pool:g} × {round(admission_rate * 100):g}% = {admission_pool * admission_rate:g}",
            evidence=[f"本月确认招生 {admission_count:g}/{admission_target:g} 人"],
        ),
    ]


def _monthly_payroll(
    *,
    position: str,
    position_rules: dict[str, Any],
    now: datetime,
    temporary_closed_days: float,
    leave_days: float,
    absent_days: float,
    attendance_bonus_actual: float,
    record_rate: float,
    admission_rate: float,
    admission_count: float,
    admission_target: float,
    teacher_dashboard: dict[str, Any],
    task_context: dict[str, Any],
    team_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    payable_days = max(0.0, days_in_month - temporary_closed_days - leave_days - absent_days)
    base_salary = float(position_rules.get("base_salary") or 0)
    base_actual = base_salary * payable_days / days_in_month if days_in_month else 0.0
    perf = _as_dict(position_rules.get("performance_breakdown"))
    record_context = _record_performance_context(teacher_dashboard)
    execution_rate = float(task_context.get("execution_rate") or 0)
    service_rate = float(task_context.get("service_rate") or 0)
    base_items = [
        _detail(
            "base_salary",
            "底薪",
            base_actual,
            rule=f"{position_rules.get('label') or '岗位'}月度底薪 {base_salary:g} 元",
            formula=f"{base_salary:g} × {payable_days:g}/{days_in_month:g} = {base_actual:g}",
            evidence=[
                f"本月自然日 {days_in_month:g} 天",
                f"临时停课 {temporary_closed_days:g} 天",
                f"请假 {leave_days:g} 天",
                f"缺勤 {absent_days:g} 天",
            ],
        ),
        _detail(
            "attendance_bonus",
            "全勤奖",
            attendance_bonus_actual,
            rule=f"全勤奖 {float(position_rules.get('attendance_bonus') or 0):g} 元；只要本月有请假或缺勤，全勤为 0",
            formula=(
                f"无请假/缺勤，发放 {attendance_bonus_actual:g}"
                if attendance_bonus_actual
                else "本月存在请假或缺勤，全勤奖 = 0"
            ),
            evidence=[f"请假 {leave_days:g} 天", f"缺勤 {absent_days:g} 天"],
        ),
    ]
    if position == "manager":
        team_context = team_context or {}
        if team_context:
            management_rate = float(team_context.get("management_closure_rate") or 0)
            team_rate = float(team_context.get("team_management_rate") or 0)
            team_service_rate = float(team_context.get("risk_service_rate") or 0)
            management_evidence = list(team_context.get("management_evidence") or [])
            team_evidence = list(team_context.get("team_evidence") or [])
            risk_evidence = list(team_context.get("risk_evidence") or [])
        else:
            management_rate = 0.0
            team_rate = 0.0
            team_service_rate = 0.0
            management_evidence = ["未配置店长负责老师名单，暂不计管理闭环绩效"]
            team_evidence = ["未配置店长负责老师名单，暂不计团队管理绩效"]
            risk_evidence = ["未配置店长负责老师名单，暂不计风险服务闭环绩效"]
        personal_pool = float(perf.get("personal_record_execution") or 0)
        admission_pool = float(perf.get("admission_performance") or 0)
        return base_items + [
            _detail(
                "personal_record_execution",
                "管理闭环",
                personal_pool * management_rate,
                rule=f"店长管理闭环绩效池 {personal_pool:g} 元，按团队问题是否被推动闭环计算",
                formula=f"{personal_pool:g} × {round(management_rate * 100):g}% = {personal_pool * management_rate:g}",
                evidence=management_evidence,
            ),
            _detail(
                "team_management",
                "团队管理",
                float(perf.get("team_management") or 0) * team_rate,
                rule=f"店长团队管理绩效池 {float(perf.get('team_management') or 0):g} 元，按所管老师记录完成和任务闭环计算",
                formula=f"{float(perf.get('team_management') or 0):g} × {round(team_rate * 100):g}% = {float(perf.get('team_management') or 0) * team_rate:g}",
                evidence=team_evidence,
            ),
            _detail(
                "risk_service_closure",
                "风险/服务闭环",
                float(perf.get("risk_service_closure") or 0) * team_service_rate,
                rule=f"店长风险/服务闭环绩效池 {float(perf.get('risk_service_closure') or 0):g} 元，按所管校区安全/投诉/续费风险闭环计算",
                formula=f"{float(perf.get('risk_service_closure') or 0):g} × {round(team_service_rate * 100):g}% = {float(perf.get('risk_service_closure') or 0) * team_service_rate:g}",
                evidence=risk_evidence,
            ),
            _detail(
                "admission_performance",
                "招生绩效",
                admission_pool * admission_rate,
                rule=f"店长招生绩效池 {admission_pool:g} 元，按个人招生目标完成率发放",
                formula=f"{admission_pool:g} × {round(admission_rate * 100):g}% = {admission_pool * admission_rate:g}",
                evidence=[f"本月确认招生 {admission_count:g}/{admission_target:g} 人"],
            ),
        ]
    record_pool = float(perf.get("record_performance") or 0)
    execution_pool = float(perf.get("execution_performance") or 0)
    service_pool = float(perf.get("service_closure") or 0)
    admission_pool = float(perf.get("admission_performance") or 0)
    return base_items + [
        _detail(
            "record_performance",
            "记录绩效",
            record_pool * record_rate,
            rule=f"全职老师记录绩效池 {record_pool:g} 元，按本月必达记录完成度为主、优质记录为辅发放",
            formula=f"{record_pool:g} × {round(record_rate * 100):g}% = {record_pool * record_rate:g}",
            evidence=record_context["evidence"],
        ),
        _detail(
            "execution_performance",
            "执行绩效",
            execution_pool * execution_rate,
            rule=f"全职老师执行绩效池 {float(perf.get('execution_performance') or 0):g} 元",
            formula=f"{execution_pool:g} × {round(execution_rate * 100):g}% = {execution_pool * execution_rate:g}",
            evidence=task_context["execution_evidence"],
        ),
        _detail(
            "service_closure",
            "服务闭环",
            service_pool * service_rate,
            rule=f"全职老师服务闭环绩效池 {float(perf.get('service_closure') or 0):g} 元",
            formula=f"{service_pool:g} × {round(service_rate * 100):g}% = {service_pool * service_rate:g}",
            evidence=task_context["service_evidence"],
        ),
        _detail(
            "admission_performance",
            "招生绩效",
            admission_pool * admission_rate,
            rule=f"全职老师招生绩效池 {admission_pool:g} 元，按个人招生目标完成率发放",
            formula=f"{admission_pool:g} × {round(admission_rate * 100):g}% = {admission_pool * admission_rate:g}",
            evidence=[f"本月确认招生 {admission_count:g}/{admission_target:g} 人"],
        ),
    ]


def _payroll_people(store: TuoguanStore, rules: dict[str, Any]) -> dict[str, dict[str, Any]]:
    mapping = store.read_json("teacher_wecom_map.json", {})
    if not isinstance(mapping, dict):
        mapping = {}
    staff = store.read_json("staff.json", {})
    if not isinstance(staff, dict):
        staff = {}
    people_config = _as_dict(rules.get("people"))
    if people_config:
        reverse_names = _best_names_by_user(mapping, blocked_names=set(institution_display_names(store)))
        people: dict[str, dict[str, Any]] = {}
        for uid, configured_raw in people_config.items():
            configured = _as_dict(configured_raw)
            user_id = str(uid).strip()
            if not user_id:
                continue
            position = str(configured.get("position") or "").strip() or "part_time"
            people[user_id] = {
                "user_id": user_id,
                "name": str(configured.get("name") or reverse_names.get(user_id) or user_id),
                "position": position,
                "admission_target": configured.get("admission_target"),
            }
        return people
    blocked_names = set(institution_display_names(store))
    best_names = _best_names_by_user(mapping, blocked_names=blocked_names)
    best_scores = {uid: _name_score(name, blocked_names=blocked_names) for uid, name in best_names.items()}
    people: dict[str, dict[str, Any]] = {}
    for uid, label in best_names.items():
        if best_scores.get(uid, -999) < 0:
            continue
        if not uid or label == "老板":
            continue
        staff_item = _as_dict(staff.get(uid))
        position = ""
        if str(staff_item.get("role") or "") == "manager":
            position = "manager"
        elif str(staff_item.get("employment_type") or "") == "full_time":
            position = "full_time_teacher"
        else:
            position = "part_time"
        people[uid] = {
            "user_id": uid,
            "name": label or uid,
            "position": position,
            "admission_target": None,
        }
    return people


def _detail(
    key: str,
    label: str,
    amount: float,
    *,
    rule: str,
    formula: str,
    evidence: list[str],
    status: str = "待本人确认",
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "amount": round(float(amount), 2),
        "rule": rule,
        "formula": formula,
        "evidence": evidence,
        "status": status,
    }


def _best_names_by_user(mapping: dict[str, Any], *, blocked_names: set[str] | None = None) -> dict[str, str]:
    best_names: dict[str, str] = {}
    best_scores: dict[str, int] = {}
    for name, user_id in mapping.items():
        uid = str(user_id).strip()
        label = str(name).strip()
        item_score = _name_score(label, blocked_names=blocked_names)
        if uid and item_score > best_scores.get(uid, -999):
            best_names[uid] = label
            best_scores[uid] = item_score
    return best_names


def _name_score(name: str, *, blocked_names: set[str] | None = None) -> int:
    blocked = {"未分配", "执行校长", "老板", *(blocked_names or set())}
    if not name or name in blocked:
        return -100
    if "?" in name or "\ufffd" in name:
        return -80
    value = 0
    if any("\u4e00" <= char <= "\u9fff" for char in name):
        value += 50
    if name.endswith("老师"):
        value += 20
    if 2 <= len(name) <= 6:
        value += 10
    return value


def _month_events(store: TuoguanStore, now: datetime) -> list[dict[str, Any]]:
    events = store.read_json(PAYROLL_EVENTS_FILE, [])
    if not isinstance(events, list):
        return []
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now.month == 12:
        end = now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        end = now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
    result = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_dt = _parse_date(str(event.get("event_date") or ""))
        if event_dt is not None and start <= event_dt < end:
            result.append(event)
    return result


def _month_tasks(store: TuoguanStore, now: datetime) -> list[dict[str, Any]]:
    tasks = store.load_tasks()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now.month == 12:
        end = now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        end = now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
    result: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        timestamps = [
            _parse_date(str(task.get(key) or ""))
            for key in ("created_at", "updated_at", "completed_at", "due_at")
        ]
        if any(item is not None and start <= item < end for item in timestamps):
            result.append(task)
    return result


def _month_confirmations(store: TuoguanStore, now: datetime) -> list[dict[str, Any]]:
    items = store.read_json(PAYROLL_CONFIRMATIONS_FILE, [])
    if not isinstance(items, list):
        return []
    month = now.strftime("%Y-%m")
    return [
        item
        for item in items
        if isinstance(item, dict) and str(item.get("month") or "") == month
    ]


def _latest_user_confirmation(confirmations: list[dict[str, Any]], user_id: str) -> dict[str, Any] | None:
    candidates = [
        item
        for item in confirmations
        if str(item.get("user_id") or "") == user_id
        and str(item.get("status") or "") in {"confirmed", "question"}
    ]
    if not candidates:
        return None
    latest = sorted(candidates, key=lambda item: str(item.get("created_at") or ""))[-1]
    status = str(latest.get("status") or "")
    label = "已确认无误" if status == "confirmed" else "有疑问"
    return {
        "status": status,
        "label": label,
        "note": str(latest.get("note") or ""),
        "created_at": str(latest.get("created_at") or ""),
    }


def _boss_final_confirmation(confirmations: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [
        item
        for item in confirmations
        if str(item.get("role") or "") == "boss"
        and str(item.get("status") or "") == "boss_final_confirmed"
    ]
    if not candidates:
        return None
    latest = sorted(candidates, key=lambda item: str(item.get("created_at") or ""))[-1]
    return {
        "status": "boss_final_confirmed",
        "label": "老板已最终确认",
        "note": str(latest.get("note") or ""),
        "created_at": str(latest.get("created_at") or ""),
    }


def _parse_date(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.strip() + "T00:00:00")
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _event_quantity(events: list[dict[str, Any]], event_type: str) -> float:
    return sum(float(event.get("quantity") or 0) for event in events if event.get("event_type") == event_type)


def _task_performance_context(user_id: str, tasks: list[dict[str, Any]], *, now: datetime) -> dict[str, Any]:
    own_tasks = [
        task
        for task in tasks
        if str(task.get("assignee_userid") or task.get("assignee") or task.get("teacher") or "") == user_id
    ]
    service_types = {"safety_incident", "parent_complaint", "parent_anxiety", "renewal_risk"}
    service_tasks = [
        task
        for task in own_tasks
        if str(task.get("type") or "") in service_types or str(task.get("level") or "") == "S"
    ]
    execution_rate, execution_evidence = _task_rate_evidence(own_tasks, label="任务")
    service_rate, service_evidence = _task_rate_evidence(service_tasks, label="安全/服务闭环")
    if not service_tasks:
        service_rate = 1.0
        service_evidence = ["本月没有需扣减的安全/服务闭环任务"]
    task_evidence_records = _task_evidence_records(own_tasks)
    execution_evidence = [*execution_evidence, *_task_evidence_lines(task_evidence_records)]
    service_evidence = [*service_evidence, *_task_evidence_lines(_task_evidence_records(service_tasks))]
    return {
        "total": len(own_tasks),
        "closed": sum(1 for task in own_tasks if _task_closed(task)),
        "service_total": len(service_tasks),
        "service_closed": sum(1 for task in service_tasks if _task_closed(task)),
        "execution_rate": execution_rate,
        "execution_percent": round(execution_rate * 100),
        "service_rate": service_rate,
        "service_percent": round(service_rate * 100),
        "execution_evidence": execution_evidence,
        "service_evidence": service_evidence,
        "task_evidence_records": task_evidence_records,
    }


def _manager_team_contexts(
    store: TuoguanStore,
    teacher_dashboards: dict[str, dict[str, Any]],
    tasks: list[dict[str, Any]],
    *,
    now: datetime,
) -> dict[str, dict[str, Any]]:
    staff = store.read_json("staff.json", {})
    students = store.read_json("students.json", {})
    rules = load_payroll_rules(store)
    people_config = _as_dict(rules.get("people"))
    if not isinstance(staff, dict) or not isinstance(students, dict):
        return {}
    student_profiles = {str(name): item for name, item in students.items() if isinstance(item, dict)}
    manager_ids = {
        str(user_id)
        for user_id, item in people_config.items()
        if str(_as_dict(item).get("position") or "") == "manager"
    } | {
        str(user_id)
        for user_id, item in staff.items()
        if isinstance(item, dict) and str(item.get("role") or "") == "manager"
    }
    result: dict[str, dict[str, Any]] = {}
    for manager_id in manager_ids:
        profile = _as_dict(staff.get(manager_id))
        configured = _as_dict(people_config.get(manager_id))
        team_teacher_ids = {
            str(item).strip()
            for item in (
                configured.get("team_teacher_ids")
                or configured.get("team_member_ids")
                or configured.get("managed_teacher_ids")
                or []
            )
            if str(item).strip()
        }
        if not team_teacher_ids:
            campuses = {str(item) for item in profile.get("campus_ids") or [] if str(item)}
            team_teacher_ids = {
                str(item.get("teacher") or item.get("teacher_id") or "").strip()
                for item in student_profiles.values()
                if str(item.get("campus_id") or item.get("campus") or "未分校区") in campuses
                and str(item.get("teacher") or item.get("teacher_id") or "").strip()
            }
        if not team_teacher_ids:
            result[str(manager_id)] = {
                "management_closure_rate": 0.0,
                "team_management_rate": 0.0,
                "risk_service_rate": 0.0,
                "team_teacher_count": 0,
                "team_task_total": 0,
            "team_task_closed": 0,
            "risk_task_total": 0,
            "risk_task_closed": 0,
            "task_evidence_records": [],
            "record_gap_count": 0,
                "low_quality_count": 0,
                "management_evidence": ["未配置店长负责老师名单，暂不计管理闭环绩效"],
                "team_evidence": ["未配置店长负责老师名单，暂不计团队管理绩效"],
                "risk_evidence": ["未配置店长负责老师名单，暂不计风险服务闭环绩效"],
            }
            continue
        teacher_rates: list[float] = []
        for teacher_id in team_teacher_ids:
            perf = _as_dict(_as_dict(teacher_dashboards.get(teacher_id, {})).get("performance"))
            monthly = _as_dict(perf.get("monthly_requirements"))
            if monthly:
                teacher_rates.append(float(monthly.get("completion_rate") or 0))
        team_tasks = [
            task
            for task in tasks
            if str(task.get("assignee_userid") or task.get("assignee") or task.get("teacher") or "") in team_teacher_ids
            or str(student_profiles.get(str(task.get("student_name") or task.get("student") or "").strip(), {}).get("teacher") or "") in team_teacher_ids
        ]
        service_types = {"safety_incident", "parent_complaint", "parent_anxiety", "renewal_risk"}
        risk_tasks = [
            task
            for task in team_tasks
            if str(task.get("type") or "") in service_types or str(task.get("level") or "") in {"S", "A"}
        ]
        task_rate, task_evidence = _task_rate_evidence(team_tasks, label="团队任务")
        risk_rate, risk_evidence = _task_rate_evidence(risk_tasks, label="风险/服务闭环")
        record_rate = sum(teacher_rates) / len(teacher_rates) if teacher_rates else 0.0
        record_gap_count = 0
        low_quality_count = 0
        for teacher_id in team_teacher_ids:
            perf = _as_dict(_as_dict(teacher_dashboards.get(teacher_id, {})).get("performance"))
            monthly = _as_dict(perf.get("monthly_requirements"))
            record_gap_count += len(monthly.get("missing_required") or [])
            low_quality_count += len(monthly.get("excluded_records") or [])
        management_rate, management_evidence = _manager_management_rate_evidence(
            record_gap_count=record_gap_count,
            low_quality_count=low_quality_count,
            team_tasks=team_tasks,
        )
        team_rate = (record_rate * 0.7 + task_rate * 0.3) if team_teacher_ids else 0.0
        result[str(manager_id)] = {
            "management_closure_rate": min(max(management_rate, 0.0), 1.0),
            "team_management_rate": min(max(team_rate, 0.0), 1.0),
            "risk_service_rate": min(max(risk_rate, 0.0), 1.0),
            "team_teacher_count": len(team_teacher_ids),
            "team_task_total": len(team_tasks),
            "team_task_closed": sum(1 for task in team_tasks if _task_closed(task)),
            "risk_task_total": len(risk_tasks),
            "risk_task_closed": sum(1 for task in risk_tasks if _task_closed(task)),
            "task_evidence_records": _task_evidence_records(team_tasks),
            "record_gap_count": record_gap_count,
            "low_quality_count": low_quality_count,
            "management_evidence": management_evidence,
            "team_evidence": [
                f"团队老师 {len(team_teacher_ids)} 人",
                f"老师记录完成均值 {round(record_rate * 100):g}%",
                *task_evidence,
            ],
            "risk_evidence": [
                f"风险/服务任务 {sum(1 for task in risk_tasks if _task_closed(task))}/{len(risk_tasks)} 个闭环",
                *([] if risk_tasks else ["本月无安全/投诉/续费风险任务，默认不扣"]),
                *([] if not risk_tasks else risk_evidence[1:]),
            ],
        }
    return result


def _task_campus_for_payroll(task: dict[str, Any], students: dict[str, dict[str, Any]]) -> str:
    direct = str(task.get("campus_id") or "").strip()
    if direct:
        return direct
    student_name = str(task.get("student_name") or task.get("student") or "").strip()
    profile = students.get(student_name, {})
    return str(profile.get("campus_id") or profile.get("campus") or "未分校区")


def _manager_management_rate_evidence(
    *,
    record_gap_count: int,
    low_quality_count: int,
    team_tasks: list[dict[str, Any]],
) -> tuple[float, list[str]]:
    task_weights = {"S": 5.0, "A": 4.0, "B": 2.0, "C": 1.0}
    total = 0.0
    closed = 0.0
    for task in team_tasks:
        weight = task_weights.get(str(task.get("level") or "C"), 1.0)
        total += weight
        if _task_closed(task):
            closed += weight
    if record_gap_count:
        total += record_gap_count * 2.0
    if low_quality_count:
        total += min(low_quality_count, 20) * 0.5
    if total <= 0:
        return 1.0, ["团队当前无记录缺项、低质记录或未闭环任务，管理闭环默认不扣"]
    rate = closed / total
    return min(max(rate, 0.0), 1.0), [
        f"团队任务闭环权重 {closed:g}/{total:g}",
        f"记录缺项 {record_gap_count} 个",
        f"低质/不计绩效记录 {low_quality_count} 条",
    ]


def _task_rate_evidence(tasks: list[dict[str, Any]], *, label: str) -> tuple[float, list[str]]:
    if not tasks:
        return 1.0, [f"本月没有{label}，默认不扣减"]
    weights = {"S": 4.0, "A": 3.0, "B": 2.0, "C": 1.0}
    total = 0.0
    closed = 0.0
    overdue = 0
    for task in tasks:
        weight = weights.get(str(task.get("level") or "C"), 1.0)
        total += weight
        if _task_closed(task):
            closed += weight
        elif _task_overdue(task):
            overdue += 1
    rate = closed / total if total else 1.0
    if overdue:
        rate *= max(0.0, 1 - min(overdue, 5) * 0.08)
    return min(max(rate, 0.0), 1.0), [
        f"本月{label} {sum(1 for task in tasks if _task_closed(task))}/{len(tasks)} 个已闭环",
        f"按 S/A/B/C 权重计算，逾期未闭环 {overdue} 个",
    ]


def _task_evidence_records(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for task in sorted(tasks, key=lambda item: str(item.get("updated_at") or item.get("completed_at") or ""), reverse=True):
        events = task.get("closure_events")
        latest = events[-1] if isinstance(events, list) and events and isinstance(events[-1], dict) else {}
        source_type = str(task.get("source_type") or "")
        source_label = str(task.get("source_label") or "")
        if not source_label:
            source_label = {
                "manual_assignment": "手动安排",
                "record_triggered": "记录触发",
                "system_risk": "系统风险",
            }.get(source_type, "系统生成" if source_type else "")
        records.append({
            "task_id": str(task.get("id") or ""),
            "task_title": str(task.get("title") or "未命名任务"),
            "student_name": str(task.get("student_name") or ""),
            "level": str(task.get("level") or "C"),
            "status": str(task.get("status") or ""),
            "source_type": source_type,
            "source_label": source_label,
            "assigned_by": str(task.get("assigned_by") or ""),
            "assigned_by_name": str(task.get("assigned_by_name") or ""),
            "action": str(latest.get("action") or ("completed" if _task_closed(task) else "pending")),
            "at": str(latest.get("at") or task.get("completed_at") or task.get("updated_at") or ""),
            "by": str(latest.get("by") or task.get("assignee_userid") or ""),
            "missing_fields": list(latest.get("missing_fields") or [])[:8],
        })
    return records[:20]


def _task_evidence_lines(records: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in records[:5]:
        title = str(item.get("task_title") or "任务")
        source = str(item.get("source_label") or "").strip()
        prefix = f"{source}：" if source else ""
        action = str(item.get("action") or "")
        status = str(item.get("status") or "")
        at = str(item.get("at") or "")
        if action in {"completed", "completed_by_admin", "closed_by_admin", "superseded", "expired"} or task_is_closed(status):
            lines.append(f"{prefix}{title}：已闭环 {at}".strip())
        elif item.get("missing_fields"):
            lines.append(f"{prefix}{title}：缺闭环证据 {len(item.get('missing_fields') or [])} 项")
        else:
            lines.append(f"{prefix}{title}：{status or '待处理'}")
    return lines


def _task_closed(task: dict[str, Any]) -> bool:
    return task_is_closed(task)


def _task_overdue(task: dict[str, Any]) -> bool:
    due = _parse_date(str(task.get("due_at") or ""))
    return due is not None and due < datetime.now() and not _task_closed(task)


def _record_timestamp(record: dict[str, Any]) -> datetime | None:
    return _parse_date(str(record.get("timestamp") or record.get("created_at") or ""))


def _record_completion_rate(teacher_dashboard: dict[str, Any]) -> float:
    performance = _as_dict(teacher_dashboard.get("performance"))
    monthly = _as_dict(performance.get("monthly_requirements"))
    if monthly.get("completion_rate") not in (None, ""):
        return min(max(float(monthly.get("completion_rate") or 0), 0.0), 1.0)
    target = float(performance.get("target_points") or 100)
    points = float(performance.get("month_points") or 0)
    return min(points / target, 1.0) if target > 0 else 0.0


def _record_performance_context(teacher_dashboard: dict[str, Any]) -> dict[str, Any]:
    performance = _as_dict(teacher_dashboard.get("performance"))
    monthly = _as_dict(performance.get("monthly_requirements"))
    required_total = float(monthly.get("required_total") or 0)
    required_done = float(monthly.get("required_done") or 0)
    quality_score = float(monthly.get("quality_score") or 0)
    quality_target = float(monthly.get("quality_score_target") or 0)
    required_rate = float(monthly.get("required_rate") or 0)
    quality_rate = float(monthly.get("quality_rate") or 0)
    evidence = [
        f"必达项完成 {required_done:g}/{required_total:g}，完成率 {required_rate:g}%",
        f"优质记录质量分 {quality_score:g}/{quality_target:g}，完成率 {quality_rate:g}%",
        "低质量真实记录保留入档，但不计入记录绩效",
    ]
    missing = monthly.get("missing_required") or []
    if isinstance(missing, list) and missing:
        labels = [
            f"{item.get('student_name')}：{item.get('reason')}"
            for item in missing[:5]
            if isinstance(item, dict)
        ]
        if labels:
            evidence.append("未完成：" + "；".join(labels))
    excluded = monthly.get("excluded_records") or []
    if isinstance(excluded, list) and excluded:
        labels = [
            f"{item.get('student_name')}：{'/'.join(str(text) for text in item.get('reason_texts') or [])}"
            for item in excluded[:5]
            if isinstance(item, dict)
        ]
        if labels:
            evidence.append("不计绩效记录：" + "；".join(labels))
    if not monthly:
        points = float(performance.get("month_points") or 0)
        target = float(performance.get("target_points") or 100)
        evidence = [f"本月记录积分 {points:g}/{target:g}"]
    return {"evidence": evidence}


def _admission_target(
    rules: dict[str, Any],
    person: dict[str, Any],
    position_rules: dict[str, Any],
    month: str,
) -> float:
    if person.get("admission_target") not in (None, ""):
        return float(person.get("admission_target") or 0)
    monthly = _as_dict(_as_dict(rules.get("monthly_admission_targets")).get(month))
    user_target = monthly.get(str(person.get("user_id") or ""))
    if user_target not in (None, ""):
        return float(user_target or 0)
    return float(position_rules.get("default_admission_target") or 0)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_bool(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _money(value: Any) -> str:
    amount = round(float(value or 0), 2)
    return f"{amount:g}"


def _number(value: Any) -> str:
    amount = round(float(value or 0), 2)
    return f"{amount:g}"


def _export_breakdown_details(teacher: dict[str, Any]) -> str:
    details = teacher.get("breakdown_details") or []
    if not isinstance(details, list):
        return ""
    parts: list[str] = []
    for item in details:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("key") or "明细")
        amount = _money(item.get("amount"))
        status = str(item.get("status") or "已计算")
        parts.append(f"{label}:{amount}元({status})")
    return "；".join(parts)


def _deep_update(base: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = deepcopy(value)


def _event_date(text: str, now: datetime) -> str:
    if "明天" in text:
        from datetime import timedelta

        return (now + timedelta(days=1)).date().isoformat()
    if "昨天" in text:
        from datetime import timedelta

        return (now - timedelta(days=1)).date().isoformat()
    match = re.search(r"(\d{1,2})月(\d{1,2})[日号]?", text)
    if match:
        month = int(match.group(1))
        day = int(match.group(2))
        return now.replace(month=month, day=day).date().isoformat()
    return now.date().isoformat()


def _classify_calendar_event(text: str) -> str | None:
    compact = text.replace(" ", "")
    if any(word in compact for word in ("临时停课", "临时放假", "中招", "学校原因休息", "学校不上课")):
        return "calendar_temporary_closed"
    if any(word in compact for word in ("法定节假日", "国定节假日", "五一", "十一", "春节")):
        return "calendar_statutory_holiday"
    return None


def _calendar_reason(event_type: str) -> str:
    if event_type == "calendar_temporary_closed":
        return "非法定临时停课/学校原因休息"
    if event_type == "calendar_statutory_holiday":
        return "国家法定节假日"
    return ""


def _classify_teacher_event(text: str) -> str | None:
    compact = text.replace(" ", "")
    if "值班" in compact and ("周六" in compact or "上午" in compact):
        return "saturday_duty"
    if "请假" in compact:
        return "attendance_leave"
    if any(word in compact for word in ("未到", "没来", "缺勤", "旷工")):
        return "attendance_absent"
    if any(word in compact for word in ("出勤", "到岗", "正常到", "正常来")):
        return "attendance_present"
    return None


def _teacher_event_reason(event_type: str) -> str:
    return {
        "attendance_present": "出勤记录",
        "attendance_leave": "个人请假，全勤奖取消",
        "attendance_absent": "缺勤记录，全勤奖取消",
        "saturday_duty": "周六上午值班补贴",
    }.get(event_type, "")


def _is_admission_event(text: str) -> bool:
    compact = text.replace(" ", "")
    return any(word in compact for word in ("招生", "新生报名", "新生确认", "确认招生", "带来一个新生", "转介绍"))


def _quantity(text: str) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:人|次|天|个)?", text)
    if match:
        return float(match.group(1))
    for char, value in _CN_NUMBERS.items():
        if f"{char}人" in text or f"{char}个" in text or f"{char}次" in text or f"{char}天" in text:
            return float(value)
    if "半天" in text or "半次" in text:
        return 0.5
    return 1.0


def _match_teacher(text: str, store: TuoguanStore) -> tuple[str, str] | None:
    mapping = store.read_json("teacher_wecom_map.json", {})
    if not isinstance(mapping, dict):
        return None
    candidates: list[tuple[int, str, str]] = []
    compact_text = text.replace(" ", "")
    for name, user_id in mapping.items():
        label = str(name).strip()
        uid = str(user_id).strip()
        if not label or not uid:
            continue
        aliases = {label}
        if label.endswith("老师"):
            aliases.add(label[:-2])
        for alias in aliases:
            if alias and alias in compact_text:
                candidates.append((len(alias), label, uid))
    if not candidates:
        return None
    _, label, uid = sorted(candidates, reverse=True)[0]
    return label, uid
