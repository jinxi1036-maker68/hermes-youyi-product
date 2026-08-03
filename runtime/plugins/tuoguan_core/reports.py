"""Role-scoped task reports."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .models import UserIdentity
from .store import TuoguanStore


def _open(task: dict[str, Any]) -> bool:
    return task.get("status") not in {"completed", "cancelled", "closed", "done", "closed_by_admin", "completed_by_admin"}


def _display_names(store: TuoguanStore) -> dict[str, str]:
    mapping = store.read_json("teacher_wecom_map.json", {})
    if not isinstance(mapping, dict):
        return {}
    return {str(user_id): str(name) for name, user_id in mapping.items()}


def _latest_note(task: dict[str, Any]) -> str:
    notes = task.get("manager_notes")
    if not isinstance(notes, list) or not notes:
        return ""
    latest = notes[-1]
    return str(latest.get("content") or "") if isinstance(latest, dict) else ""


def _teacher_report(
    tasks: list[dict[str, Any]],
    identity: UserIdentity,
) -> str:
    visible = [
        task
        for task in tasks
        if _open(task) and task.get("assignee_userid") == identity.canonical_user_id
    ]
    lines = ["老师今日待办"]
    if not visible:
        return "\n".join(lines + ["今天暂无待处理任务。"])
    lines.append(f"你当前有{len(visible)}个待处理任务：")
    for task in visible[:5]:
        lines.append(
            f"- [{task.get('level', 'C')}级] {task.get('title', '未命名任务')}"
        )
    return "\n".join(lines)


def _manager_report(
    tasks: list[dict[str, Any]],
    identity: UserIdentity,
    store: TuoguanStore,
) -> str:
    staff = store.read_json("staff.json", {})
    profile = (
        staff.get(identity.canonical_user_id, {})
        if isinstance(staff, dict)
        else {}
    )
    campuses = set(profile.get("campus_ids") or []) if isinstance(profile, dict) else set()
    visible = [
        task
        for task in tasks
        if _open(task) and str(task.get("campus_id") or "") in campuses
    ]
    names = _display_names(store)
    counts = Counter(
        names.get(str(task.get("assignee_userid") or ""), str(task.get("assignee_userid") or "未分配"))
        for task in visible
    )
    s_tasks = [task for task in visible if task.get("level") == "S"]
    watched = [task for task in visible if task.get("manager_status") == "watching"]
    lines = [
        "店长执行日报",
        f"未完成任务：{len(visible)}",
        f"S级风险：{len(s_tasks)}",
        f"管理关注：{len(watched)}",
    ]
    if counts:
        lines.append("老师未完成：")
        for name, count in counts.most_common(8):
            lines.append(f"- {name}：{count}个")
    if visible:
        lines.append("当前任务：")
        for task in visible[:8]:
            lines.append(f"- {task.get('title', '未命名任务')}")
    if watched:
        lines.append("关注中任务：")
        for task in watched[:5]:
            line = f"- {task.get('title', '未命名任务')}"
            note = _latest_note(task)
            if note:
                line += f"｜{note}"
            lines.append(line)
    return "\n".join(lines)


def _boss_report(tasks: list[dict[str, Any]]) -> str:
    open_tasks = [task for task in tasks if _open(task)]
    key_types = {
        "parent_anxiety",
        "renewal_risk",
        "new_trial",
        "parent_complaint",
        "safety_incident",
    }
    key_tasks = [
        task
        for task in open_tasks
        if task.get("level") == "S" or task.get("type") in key_types
    ]
    execution_risks = [
        task
        for task in open_tasks
        if task.get("manager_status") == "watching"
        and int(task.get("nudge_count") or 0) >= 2
    ]
    lines = ["老板经营日报", "今日重点"]
    if not key_tasks:
        lines.append("今日暂无需要老板关注的关键风险。")
    for index, task in enumerate(key_tasks[:3], 1):
        lines.append(
            f"{index}. [{task.get('level', 'C')}级] "
            f"{task.get('title', '未命名任务')}"
        )
    if execution_risks:
        lines.append("执行风险")
        for task in execution_risks[:3]:
            line = (
                f"- {task.get('title', '未命名任务')}｜"
                f"已督促{int(task.get('nudge_count') or 0)}次"
            )
            note = _latest_note(task)
            if note:
                line += f"｜{note}"
            lines.append(line)
    return "\n".join(lines)


def build_role_report(
    tasks: list[dict[str, Any]],
    identity: UserIdentity,
    store: TuoguanStore,
) -> str:
    if identity.approval_state != "approved":
        return "暂未识别你的日报权限。"
    if identity.role == "teacher":
        return _teacher_report(tasks, identity)
    if identity.role == "manager":
        return _manager_report(tasks, identity, store)
    if identity.role == "boss":
        return _boss_report(tasks)
    return "暂未识别你的日报权限。"
