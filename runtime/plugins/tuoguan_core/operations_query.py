"""Read-only deterministic operations queries for the Youyi CommandBus."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
from typing import Any

from .models import UserIdentity
from .store import TuoguanStore, TuoguanStoreError
from .student_resolver import active_summer_students
from .tenant_context import current_tenant_id


COMPLETED_STATUSES = {"completed", "done", "cancelled", "closed", "closed_by_admin", "completed_by_admin", "superseded", "expired"}


def _today() -> str:
    return datetime.now().astimezone().date().isoformat()


def _rows(store: TuoguanStore, name: str) -> tuple[list[dict[str, Any]] | None, str]:
    try:
        payload = store.read_json(name, [])
    except TuoguanStoreError:
        return None, "unavailable"
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)], "available"
    if isinstance(payload, dict) and isinstance(payload.get("tasks"), list):
        return [row for row in payload["tasks"] if isinstance(row, dict)], "available"
    if isinstance(payload, dict):
        return [row for row in payload.values() if isinstance(row, dict)], "available"
    return None, "unsupported"


def _stamp(row: dict[str, Any]) -> str:
    return str(row.get("created_at") or row.get("timestamp") or row.get("date") or "")[:10]


def _task_due_date(row: dict[str, Any]) -> str:
    raw = str(row.get("due_at") or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(raw.replace(" ", "T"))
        except ValueError:
            return ""
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.date().isoformat()


def _is_safety_task(row: dict[str, Any]) -> bool:
    return str(row.get("level") or row.get("priority") or "").upper() == "S" or any(
        word in str(row.get(key) or "") for key in ("title", "task_type", "source", "category")
        for word in ("安全", "受伤", "S级")
    )


def _data_version(store: TuoguanStore, names: tuple[str, ...]) -> str:
    parts = []
    for name in names:
        path = store.data_dir / name
        if path.exists():
            stat = path.stat()
            parts.append(f"{name}:{stat.st_size}:{stat.st_mtime_ns}")
        else:
            parts.append(f"{name}:missing")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def query_operations(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    query_type: str = "overview",
    teacher_name: str = "",
    expected_tenant_id: str = "",
    allowed_roles: tuple[str, ...] = ("boss",),
) -> dict[str, Any]:
    if identity.approval_state != "approved":
        return {"ok": False, "reason_code": "permission_denied", "message": "当前账号尚未授权，不能查看机构经营数据。"}
    if identity.role not in set(allowed_roles):
        return {"ok": False, "reason_code": "permission_denied", "message": "经营数据仅限老板查看。"}
    if str(expected_tenant_id or current_tenant_id()) != current_tenant_id():
        return {"ok": False, "reason_code": "cross_tenant_denied", "message": "不能跨机构查询经营数据。"}

    today = _today()
    records, record_status = _rows(store, "records.json")
    tasks, task_status = _rows(store, "tasks.json")
    events, points_status = _rows(store, "point_events.json")
    balances, balance_status = _rows(store, "summer_points.json")
    statuses = {
        "records": record_status,
        "tasks": task_status,
        "summer_students": "available",
        "points": "available" if points_status == balance_status == "available" else "unavailable",
    }

    contacts = store.read_json("wecom_whitelist.json", {})
    contact_names = contacts.get("wecom_contacts", {}) if isinstance(contacts, dict) else {}
    user_roles = contacts.get("user_roles", {}) if isinstance(contacts, dict) else {}
    approved_users = {
        str(user_id) for key in ("allowed_users", "super_users")
        for user_id in (contacts.get(key, []) if isinstance(contacts.get(key), list) else [])
    } if isinstance(contacts, dict) else set()
    staff_file = store.read_json("staff.json", {})
    staff_by_role: dict[str, list[str]] = {"teacher": [], "manager": [], "boss": []}
    staff_role_counts = Counter()
    known_staff_names: dict[str, str] = {}
    for user_id, role in user_roles.items() if isinstance(user_roles, dict) else ():
        normalized_role = "boss" if str(role) in {"boss", "super_admin"} else str(role)
        if normalized_role not in staff_by_role or (approved_users and str(user_id) not in approved_users):
            continue
        staff_role_counts[normalized_role] += 1
        display_name = str(contact_names.get(str(user_id)) or "").strip()
        if display_name:
            staff_by_role[normalized_role].append(display_name)
            if normalized_role in {"teacher", "manager"}:
                known_staff_names[str(user_id)] = display_name
    if isinstance(staff_file, dict):
        for user_id, profile in staff_file.items():
            if not isinstance(profile, dict):
                continue
            role = str(profile.get("role") or "")
            name = str(profile.get("name") or "").strip()
            if role in {"teacher", "manager"} and name:
                known_staff_names[str(user_id)] = name
    if not user_roles and isinstance(contact_names, dict):
        # Compatibility for old fixtures/data that predate explicit role mapping.
        known_staff_names.update({str(user_id): str(name).strip() for user_id, name in contact_names.items() if str(name).strip()})
    for names in staff_by_role.values():
        names.sort()
    def _matches_staff_name(candidate: str, display: str) -> bool:
        candidate = str(candidate or "").strip()
        display = str(display or "").strip()
        return bool(candidate and display and (candidate in display or display in candidate))
    teacher_ids = {
        str(user_id) for user_id, name in known_staff_names.items()
        if _matches_staff_name(teacher_name, name)
    }
    if query_type == "teacher_activity" and teacher_name and not teacher_ids:
        return {
            "ok": True,
            "reason_code": "teacher_not_found",
            "query_type": query_type,
            "summary": {"staff": {"teacher_count": 0, "teacher_names": []}},
            "source_status": statuses,
            "source_counts": {},
            "data_version": _data_version(store, ("wecom_whitelist.json", "staff.json")),
            "rendered_text": f"没有查到名为{teacher_name}的老师。请确认老师姓名，或改查老师名单。",
            "render_verified": True,
        }
    def belongs_to_teacher(row: dict[str, Any]) -> bool:
        values = {
            str(row.get("operator_user_id") or ""), str(row.get("teacher_user_id") or ""),
            str(row.get("teacher") or ""), str(row.get("assignee_userid") or ""),
            str(row.get("assignee_user_id") or ""), str(row.get("operator_name") or ""),
        }
        return not teacher_name or teacher_name in values or bool(values & teacher_ids)

    today_records = [row for row in (records or []) if _stamp(row) == today and belongs_to_teacher(row)]
    recorded_students = {str(row.get("student_name") or row.get("student") or "") for row in today_records}
    recorded_students.discard("")
    teacher_counts = Counter(str(row.get("operator_user_id") or row.get("teacher_user_id") or row.get("teacher") or "unknown") for row in today_records)
    scoped_tasks = [row for row in (tasks or []) if belongs_to_teacher(row)]
    open_tasks = [row for row in scoped_tasks if str(row.get("status") or "pending").lower() not in COMPLETED_STATUSES]
    today_open_tasks = [row for row in open_tasks if _task_due_date(row) == today]
    safety_tasks = [row for row in scoped_tasks if _is_safety_task(row)]
    open_safety_tasks = [row for row in safety_tasks if str(row.get("status") or "pending").lower() not in COMPLETED_STATUSES]
    summer_students = active_summer_students(store)
    point_events = events or []
    total_added = sum(max(0, int(row.get("delta_points") or row.get("delta") or 0)) for row in point_events)
    total_deducted = sum(abs(min(0, int(row.get("delta_points") or row.get("delta") or 0))) for row in point_events)
    current_total = sum(int(row.get("current_points") or row.get("points") or 0) for row in (balances or []))

    summary = {
        "today_record_count": len(today_records) if records is not None else None,
        "today_recorded_student_count": len(recorded_students) if records is not None else None,
        "teacher_record_counts": dict(sorted(teacher_counts.items())),
        "open_task_count": len(open_tasks) if tasks is not None else None,
        "today_open_task_count": len(today_open_tasks) if tasks is not None else None,
        "safety_task_count": len(safety_tasks) if tasks is not None else None,
        "open_safety_task_count": len(open_safety_tasks) if tasks is not None else None,
        "summer_student_count": len(summer_students),
        "points": {
            "event_count": len(point_events) if statuses["points"] == "available" else None,
            "total_added": total_added if statuses["points"] == "available" else None,
            "total_deducted": total_deducted if statuses["points"] == "available" else None,
            "current_total": current_total if statuses["points"] == "available" else None,
        },
        "staff": {
            "teacher_count": staff_role_counts["teacher"],
            "teacher_names": staff_by_role["teacher"],
            "teacher_unnamed_count": staff_role_counts["teacher"] - len(staff_by_role["teacher"]),
            "manager_count": staff_role_counts["manager"],
            "boss_count": staff_role_counts["boss"],
        },
    }

    unavailable = "数据源不可用"
    teacher_text = "、".join(f"{teacher} {count}条" for teacher, count in teacher_counts.items()) or "今天暂无老师记录"
    renderers = {
        "teacher_records": [
            "今天老师记录情况",
            f"今日记录：{summary['today_record_count'] if summary['today_record_count'] is not None else unavailable}条",
            f"有记录学生：{summary['today_recorded_student_count'] if summary['today_recorded_student_count'] is not None else unavailable}人",
            f"老师明细：{teacher_text if records is not None else unavailable}",
        ],
        "open_tasks": ["今天未完成任务", f"今日未完成：{summary['today_open_task_count'] if summary['today_open_task_count'] is not None else unavailable}条"],
        "safety_tasks": ["安全任务情况", f"S级安全任务：{summary['safety_task_count'] if summary['safety_task_count'] is not None else unavailable}条"],
        "summer_students": ["暑假班学生情况", f"当前有效暑假班学生：{summary['summer_student_count']}人"],
        "points": [
            "暑假班积分概况",
            f"积分事件：{summary['points']['event_count'] if summary['points']['event_count'] is not None else unavailable}条",
            f"累计加分：{summary['points']['total_added'] if summary['points']['total_added'] is not None else unavailable}分",
            f"累计扣分：{summary['points']['total_deducted'] if summary['points']['total_deducted'] is not None else unavailable}分",
            f"当前积分合计：{summary['points']['current_total'] if summary['points']['current_total'] is not None else unavailable}分",
        ],
        "staff": [
            "当前老师情况",
            f"已授权老师：{summary['staff']['teacher_count']}位",
            "老师名单：" + ("、".join(summary["staff"]["teacher_names"]) or "暂无已配置姓名的老师"),
            *( [f"另有{summary['staff']['teacher_unnamed_count']}位老师未配置显示名"] if summary["staff"]["teacher_unnamed_count"] else [] ),
            f"店长：{summary['staff']['manager_count']}位；老板：{summary['staff']['boss_count']}位",
        ],
    }
    if query_type == "teacher_activity" and teacher_name:
        completed_tasks = [row for row in scoped_tasks if str(row.get("status") or "").lower() in COMPLETED_STATUSES]
        recent_records = sorted([row for row in (records or []) if belongs_to_teacher(row)], key=_stamp, reverse=True)[:5]
        recent_tasks = sorted(completed_tasks, key=lambda row: str(row.get("completed_at") or row.get("updated_at") or ""), reverse=True)[:5]
        lines = [f"{teacher_name}最近处理情况", f"今日学生记录：{len(today_records)}条", f"已完成任务：{len(completed_tasks)}项"]
        if recent_records:
            lines.append("最近记录：" + "；".join(str(row.get("student_name") or row.get("student") or "学生") + " - " + str(row.get("normalized_summary") or row.get("content") or row.get("record") or "已记录")[:60] for row in recent_records))
        if recent_tasks:
            lines.append("最近完成任务：" + "；".join(str(row.get("title") or "任务") for row in recent_tasks))
        if not recent_records and not recent_tasks:
            lines.append("当前没有查到可核验的近期处理记录。")
        renderers["teacher_activity"] = lines
    overview = [
        "今天经营情况",
        f"今日记录：{summary['today_record_count'] if summary['today_record_count'] is not None else unavailable}条，涉及学生{summary['today_recorded_student_count'] if summary['today_recorded_student_count'] is not None else unavailable}人",
        f"当前待办任务：{summary['open_task_count'] if summary['open_task_count'] is not None else unavailable}条",
        f"S级安全任务：{summary['safety_task_count'] if summary['safety_task_count'] is not None else unavailable}条",
        f"有效暑假班学生：{summary['summer_student_count']}人",
        f"当前积分合计：{summary['points']['current_total'] if summary['points']['current_total'] is not None else unavailable}分",
    ]
    lines = renderers.get(query_type, overview)
    return {
        "ok": True,
        "reason_code": "success",
        "query_type": query_type,
        "summary": summary,
        "source_status": statuses,
        "source_counts": {
            "records.json": len(records or []),
            "tasks.json": len(tasks or []),
            "point_events.json": len(events or []),
            "summer_points.json": len(balances or []),
            "active_summer_students": len(summer_students),
        },
        "data_version": _data_version(store, ("records.json", "tasks.json", "point_events.json", "summer_points.json", "students.json", "summer_enrollments.json", "wecom_whitelist.json")),
        "rendered_text": "\n".join(lines),
        "render_verified": True,
    }
