"""Operational runtime for the Hermes-native tutoring module."""

from __future__ import annotations

import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .escalation import run_notification_cycle
from .identity import IdentityService
from .reports import build_role_report
from .store import TuoguanStore
from .analytics import refresh_student_business_signals
from .analytics import build_business_overview
from .tasks import CLOSED_TASK_STATUSES


_CLOSED_STATUSES = CLOSED_TASK_STATUSES


def build_business_snapshot(
    store: TuoguanStore,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a current operating snapshot from authoritative Windows data."""
    timestamp = now or datetime.now().astimezone()
    students = store.read_json("students.json", {})
    records = store.read_json("records.json", [])
    tasks = store.load_tasks()
    if not isinstance(students, dict):
        students = {}
    if not isinstance(records, list):
        records = []

    profiles = [item for item in students.values() if isinstance(item, dict)]
    open_tasks = [
        task
        for task in tasks
        if str(task.get("status") or "") not in _CLOSED_STATUSES
    ]
    today = timestamp.date().isoformat()

    def missing(field: str) -> int:
        return sum(
            profile.get(field) in (None, "", [], {})
            for profile in profiles
        )

    return {
        "generated_at": timestamp.isoformat(timespec="seconds"),
        "authority": "windows_native_hermes",
        "students": {
            "count": len(profiles),
            "assigned_teacher_count": len(
                {
                    str(profile.get("teacher"))
                    for profile in profiles
                    if profile.get("teacher")
                }
            ),
            "by_campus": dict(
                Counter(
                    str(profile.get("campus_id") or "未分校区")
                    for profile in profiles
                )
            ),
        },
        "records": {
            "count": len(records),
            "today": sum(
                1
                for record in records
                if isinstance(record, dict)
                and str(
                    record.get("timestamp") or record.get("created_at") or ""
                ).startswith(today)
            ),
            "latest_timestamp": max(
                (
                    str(record.get("timestamp") or record.get("created_at") or "")
                    for record in records
                    if isinstance(record, dict)
                ),
                default="",
            ),
        },
        "tasks": {
            "count": len(tasks),
            "open": len(open_tasks),
            "open_by_level": dict(
                Counter(str(task.get("level") or "未分级") for task in open_tasks)
            ),
            "open_by_type": dict(
                Counter(str(task.get("type") or "未分类") for task in open_tasks)
            ),
        },
        "data_quality": {
            "missing_phone": missing("phone"),
            "missing_class": missing("class"),
            "missing_scores": missing("scores"),
        },
        "retired": {
            "wsl_production": True,
            "kitchen_menu": True,
            "parent_forwarding_entry": True,
        },
    }


def write_business_snapshot(
    store: TuoguanStore,
    now: datetime | None = None,
) -> dict[str, Any]:
    snapshot = build_business_snapshot(store, now)
    store.write_json("business_status_snapshot.json", snapshot)
    return snapshot


def build_status(
    store: TuoguanStore,
    *,
    gateway_running: bool,
    callback_connected: bool,
    model_name: str,
    active_jobs: int,
    active_sessions: int,
    now: datetime | None = None,
) -> str:
    timestamp = now or datetime.now()
    try:
        store.load_tasks()
        data_status = "正常"
    except Exception:
        data_status = "异常"
    module_status = "正常" if data_status == "正常" else "异常"
    return "\n".join(
        [
            f"Hermes 运行状态（{timestamp:%Y-%m-%d}）",
            "",
            f"Gateway：{'运行中' if gateway_running else '未运行'}",
            f"企微回调与转发：{'已连接' if callback_connected else '未连接'}",
            f"主模型：{model_name or '未配置'}",
            f"托管业务模块：{module_status}",
            f"本地数据：{data_status}",
            f"活跃定时任务：{active_jobs}",
            f"当前会话：{active_sessions}",
            "",
            "服务器仅负责企微回调与消息转发。",
        ]
    )


def run_backup(
    store: TuoguanStore,
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = now or datetime.now()
    date_key = timestamp.strftime("%Y%m%d")
    state = store.read_json("backup_state.json", {})
    if isinstance(state, dict) and state.get("last_backup_date") == date_key:
        return {"created": False, "date": date_key, "files": 0}

    destination = store.data_dir / "backup" / "daily" / date_key
    destination.mkdir(parents=True, exist_ok=True)
    copied = 0
    for source in store.data_dir.glob("*.json"):
        if source.is_file():
            shutil.copy2(source, destination / source.name)
            copied += 1
    store.write_json(
        "backup_state.json",
        {
            "last_backup_date": date_key,
            "created_at": timestamp.isoformat(timespec="seconds"),
            "files": copied,
            "path": str(destination),
        },
    )
    return {"created": True, "date": date_key, "files": copied}


def run_report_delivery(
    store: TuoguanStore,
    *,
    role: str,
    recipient: str,
    send: Callable[[str, str], Any],
) -> dict[str, int]:
    refresh_student_business_signals(store)
    write_business_snapshot(store)
    identity = IdentityService(store).resolve("wecom_callback", recipient)
    if role == "boss" and identity.role in ("boss", "super_admin", "admin"):
        pass
    elif identity.role != role:
        return {"sent": 0, "failed": 1}
    content = build_role_report(store.load_tasks(), identity, store)
    try:
        response = send(recipient, content)
        if isinstance(response, dict) and int(response.get("errcode") or 0) != 0:
            raise RuntimeError(str(response))
    except Exception:
        return {"sent": 0, "failed": 1}
    return {"sent": 1, "failed": 0}


def run_escalation_delivery(
    store: TuoguanStore,
    *,
    send: Callable[[str, str], Any],
    now: datetime | None = None,
) -> dict[str, int]:
    return run_notification_cycle(store, send, now)


def build_growth_review_digest(
    store: TuoguanStore,
    *,
    minimum_records: int = 3,
) -> str:
    overview = build_business_overview(store)
    records = store.read_json("records.json", [])
    counts: Counter[str] = Counter(
        str(item.get("student_name") or "")
        for item in records
        if isinstance(item, dict)
    ) if isinstance(records, list) else Counter()
    candidates = [
        name
        for name in overview["growth_reports"]["missing_students"]
        if counts.get(name, 0) >= minimum_records
    ]
    lines = [
        "【成长报告周度复核】",
        f"近30天无已确认成长报告：{overview['growth_reports']['missing_approved_within_30_days']}名",
        f"已有至少{minimum_records}条记录、可优先生成草稿：{len(candidates)}名",
    ]
    if candidates:
        lines.append("优先学生：" + "、".join(candidates[:20]))
        lines.append("请按实际沟通需要逐个生成，老师确认后再使用，不会自动发送家长。")
    else:
        lines.append("本周暂无记录证据充足的优先学生，不批量生成空报告。")
    return "\n".join(lines)


def desired_cron_jobs() -> list[dict[str, Any]]:
    return [
        {
            "key": "tuoguan-growth-review-weekly",
            "name": "成长报告周度复核",
            "schedule": "10 9 * * 1",
            "script": "tuoguan_growth_review.py",
            "deliver": "local",
            "no_agent": True,
        },
        {
            "key": "tuoguan-escalation-scan",
            "name": "托管任务升级扫描",
            "schedule": "every 5m",
            "script": "tuoguan_escalation.py",
            "deliver": "local",
            "no_agent": True,
        },
        {
            "key": "tuoguan-dashboard-refresh",
            "name": "托管看板缓存刷新",
            "schedule": "every 10m",
            "script": "tuoguan_dashboard_refresh.py",
            "deliver": "local",
            "no_agent": True,
        },
        {
            "key": "tuoguan-manager-daily-report",
            "name": "托管店长日报",
            "schedule": "40 8 * * *",
            "script": "tuoguan_manager_report.py",
            "deliver": "local",
            "no_agent": True,
        },
        {
            "key": "tuoguan-boss-daily-report",
            "name": "托管老板日报",
            "schedule": "0 21 * * *",
            "script": "tuoguan_boss_report.py",
            "deliver": "local",
            "no_agent": True,
        },
        {
            "key": "tuoguan-daily-backup",
            "name": "托管数据每日备份",
            "schedule": "0 2 * * *",
            "script": "tuoguan_backup.py",
            "deliver": "local",
            "no_agent": True,
        },
        {
            "key": "summer-opening-diagnostic-reminder",
            "name": "暑假班开班诊断卷提醒",
            "schedule": "2026-07-01T07:00:00+08:00",
            "script": "tuoguan_summer_opening.py",
            "deliver": "local",
            "no_agent": True,
        },
        {
            "key": "summer-closing-progress-reminder",
            "name": "暑假班结业进步单提醒",
            "schedule": "2026-07-30T07:00:00+08:00",
            "script": "tuoguan_summer_closing.py",
            "deliver": "local",
            "no_agent": True,
        },
    ]


REMINDERS = {
    "summer-opening": (
        "owner",
        "【暑假班开班提醒】\n今天是暑假班开班日，可以开始生成诊断卷了。\n"
        "请回复：生成诊断卷，一年级X个、三年级X个、四年级X个。"
    ),
    "summer-closing": (
        "owner",
        "【暑假班结业提醒】\n今天是暑假班最后一天，可以生成结业进步单了。\n"
        "请回复“结业了”，Hermes将根据本地学生记录逐一处理。"
    ),
}
