"""Deterministic daily operations report built from the validated read model."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import UserIdentity
from .operations_query import query_operations
from .store import TuoguanStore


LOW_COVERAGE_PERCENT = 30
HIGH_OPEN_TASK_THRESHOLD = 20


def generate_operations_daily_report(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    expected_tenant_id: str = "",
) -> dict[str, Any]:
    source = query_operations(
        store,
        identity=identity,
        query_type="overview",
        expected_tenant_id=expected_tenant_id,
    )
    if not source.get("ok"):
        return source

    summary = source["summary"]
    unavailable = [name for name, status in source.get("source_status", {}).items() if status != "available"]
    record_count = summary.get("today_record_count")
    recorded_students = summary.get("today_recorded_student_count")
    summer_students = summary.get("summer_student_count")
    teacher_count = len(summary.get("teacher_record_counts") or {}) if record_count is not None else None
    open_tasks = summary.get("open_task_count")
    safety_tasks = summary.get("safety_task_count")
    open_safety_tasks = summary.get("open_safety_task_count")
    points = summary.get("points") or {}

    coverage_percent = None
    if isinstance(recorded_students, int) and isinstance(summer_students, int) and summer_students > 0:
        coverage_percent = round(recorded_students * 100 / summer_students, 1)

    notices: list[str] = []
    if record_count == 0:
        notices.append("今日暂无学生记录。")
    if coverage_percent is not None and coverage_percent < LOW_COVERAGE_PERCENT:
        notices.append(f"记录覆盖率为{coverage_percent:g}%，低于{LOW_COVERAGE_PERCENT}%，请关注记录完整性。")
    if isinstance(open_safety_tasks, int) and open_safety_tasks > 0:
        notices.append(f"存在{open_safety_tasks}条未闭环S级安全事项。")
    if isinstance(open_tasks, int) and open_tasks > HIGH_OPEN_TASK_THRESHOLD:
        notices.append(f"当前待办{open_tasks}条，超过{HIGH_OPEN_TASK_THRESHOLD}条阈值。")
    if unavailable:
        notices.append("数据不可用项：" + "、".join(unavailable) + "。")
    if not notices:
        notices.append("当前没有触发预设经营提醒。")

    value = lambda item, unit="": f"{item}{unit}" if item is not None else "unavailable"
    lines = [
        f"本机构托管经营日报｜{datetime.now().astimezone().date().isoformat()}",
        f"今日学生记录：{value(record_count, '条')}，有记录学生：{value(recorded_students, '人')}，记录老师：{value(teacher_count, '人')}",
        f"当前待办任务：{value(open_tasks, '条')}，S级安全任务：{value(safety_tasks, '条')}，其中未闭环：{value(open_safety_tasks, '条')}",
        f"有效暑假班学生：{value(summer_students, '人')}",
        f"积分概况：事件{value(points.get('event_count'), '条')}，累计加{value(points.get('total_added'), '分')}，累计扣{value(points.get('total_deducted'), '分')}，当前合计{value(points.get('current_total'), '分')}",
        "确定性提醒：",
    ]
    lines.extend(f"- {notice}" for notice in notices)
    return {
        "ok": True,
        "reason_code": "success",
        "report_type": "operations_daily_report",
        "report_date": datetime.now().astimezone().date().isoformat(),
        "summary": summary,
        "teacher_count": teacher_count,
        "coverage_percent": coverage_percent,
        "threshold_rules": {
            "low_coverage_percent": LOW_COVERAGE_PERCENT,
            "high_open_task_count": HIGH_OPEN_TASK_THRESHOLD,
        },
        "notices": notices,
        "unavailable_items": unavailable,
        "source_status": source.get("source_status", {}),
        "source_counts": source.get("source_counts", {}),
        "data_version": source.get("data_version"),
        "rendered_text": "\n".join(lines),
        "render_verified": True,
    }
