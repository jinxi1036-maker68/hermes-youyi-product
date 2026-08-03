"""Escalation decisions and transport-neutral notification execution."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Callable

from .models import EscalationDecision, Notification
from .store import TuoguanStore
from .tasks import closure_missing_fields


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None


def escalation_decision(
    task: dict[str, Any],
    now: datetime | None = None,
) -> EscalationDecision:
    timestamp = now or datetime.now()
    if task.get("status") in {"completed", "cancelled", "closed", "done", "closed_by_admin", "completed_by_admin"}:
        return EscalationDecision("none", (), "任务已结束")
    level = str(task.get("level") or "C")
    next_remind = _parse_datetime(task.get("next_remind_at"))
    due = bool(next_remind and next_remind <= timestamp)
    deferred = int(task.get("defer_count") or 0)
    if level == "S":
        return EscalationDecision(
            "escalate_now",
            ("teacher", "manager", "boss"),
            "S级任务需要老师、店长和老板立即知道",
        )
    if level == "A" and (due or deferred >= 2):
        return EscalationDecision(
            "remind_and_manager_watch",
            ("teacher", "manager"),
            "A级任务已到提醒时间或多次延期，需要店长关注",
        )
    if level == "B" and due:
        return EscalationDecision(
            "remind_teacher",
            ("teacher",),
            "B级任务只提醒老师，暂不升级",
        )
    if level == "C":
        return EscalationDecision("none", (), "C级任务只沉淀，不主动打扰")
    return EscalationDecision("wait", (), f"{level}级任务尚未到提醒时间")


def _role_targets(store: TuoguanStore) -> dict[str, str]:
    data = store.read_json("wecom_whitelist.json", {})
    if not isinstance(data, dict):
        return {}
    roles = data.get("user_roles") or {}
    targets: dict[str, str] = {}
    if isinstance(roles, dict):
        for userid, role in roles.items():
            if role == "manager" and "manager" not in targets:
                targets["manager"] = str(userid)
            if role in {"boss", "admin", "super_admin"} and "boss" not in targets:
                targets["boss"] = str(userid)
    if "boss" not in targets:
        super_users = data.get("super_users") or []
        if super_users:
            targets["boss"] = str(super_users[0])
    return targets


def _should_emit(
    task: dict[str, Any],
    decision: EscalationDecision,
    now: datetime,
    cooldown_minutes: int = 30,
) -> bool:
    if decision.action in {"none", "wait"}:
        return False
    if task.get("last_escalation_action") != decision.action:
        return True
    last_at = _parse_datetime(task.get("last_escalated_at"))
    return not last_at or now - last_at >= timedelta(minutes=cooldown_minutes)


def _role_acknowledged(task: dict[str, Any], role: str) -> bool:
    if role == "teacher":
        return False
    acknowledgements = task.get("supervisor_ack") or {}
    return isinstance(acknowledgements, dict) and isinstance(
        acknowledgements.get(role),
        dict,
    )


def _message(
    task: dict[str, Any],
    role: str,
    decision: EscalationDecision,
) -> str:
    level = str(task.get("level") or "C")
    label = {
        "teacher": "任务提醒",
        "manager": "任务店长关注",
        "boss": "任务老板关注",
    }[role]
    lines = [
        f"【{level}级{label}】{task.get('title', '未命名任务')}",
        f"当前状态：{task.get('status', 'pending')}",
        f"升级原因：{decision.reason}",
    ]
    if task.get("source_text"):
        lines.append(f"原始记录：{task['source_text']}")
    assignee = str(task.get("assignee_userid") or "未指定")
    if role == "teacher":
        missing = closure_missing_fields(
            task,
            str(task.get("evidence_summary") or ""),
        )
        if task.get("status") == "waiting_confirmation" and missing:
            labels = {
                "parent_attitude": "家长当前态度",
                "next_step": "下一步跟进安排",
                "parent_informed": "家长是否已知情",
                "child_status": "孩子当前状态",
                "action_taken": "已做处理",
                "follow_up_needed": "后续观察/跟进安排",
                "result": "处理结果和时间",
            }
            lines.append(
                "还缺：" + "；".join(labels.get(item, item) for item in missing)
            )
            if missing == ["child_status"]:
                lines.append(
                    "请直接回复：孩子现在状态正常，没有红肿也没有疼；"
                    "或说明孩子还有什么异常。"
                )
            else:
                lines.append("请按缺口补充事实，信息齐后我会自动关闭任务。")
        else:
            lines.append("你可回复：开始 / 稍后 / 帮我写 / 完成了")
    elif role == "manager":
        lines.extend(
            [
                f"执行老师：{assignee}",
                "请关注老师是否及时处理，并在需要时协助补充现场信息或家长沟通。",
                "如已知晓，可回复“我知道了”，仅停止向你重复提醒，不会关闭老师任务。",
            ]
        )
    else:
        lines.extend(
            [
                f"执行老师：{assignee}",
                "请关注老师是否完成安全闭环，必要时督促店长/老师跟进。",
                "如已知晓，可回复“我知道了”，仅停止向你重复提醒，不会关闭老师任务。",
            ]
        )
    return "\n".join(lines)


def build_notification_plan(
    tasks: list[dict[str, Any]],
    store: TuoguanStore,
    now: datetime | None = None,
) -> list[Notification]:
    timestamp = now or datetime.now()
    configured = _role_targets(store)
    plan: list[Notification] = []
    for task in tasks:
        decision = escalation_decision(task, timestamp)
        if not _should_emit(task, decision, timestamp):
            continue
        for role in decision.notify_roles:
            if _role_acknowledged(task, role):
                continue
            touser = (
                str(task.get("assignee_userid") or "")
                if role == "teacher"
                else configured.get(role, "")
            )
            if not touser:
                continue
            plan.append(
                Notification(
                    task_id=str(task.get("id") or ""),
                    role=role,
                    touser=touser,
                    action=decision.action,
                    content=_message(task, role, decision),
                )
            )
    return plan


def run_notification_cycle(
    store: TuoguanStore,
    send: Callable[[str, str], Any],
    now: datetime | None = None,
) -> dict[str, int]:
    timestamp = now or datetime.now()
    tasks = store.load_tasks()
    plan = build_notification_plan(tasks, store, timestamp)
    expected = Counter(item.task_id for item in plan)
    succeeded: Counter[str] = Counter()
    action_by_task = {item.task_id: item.action for item in plan}
    deliveries = store.read_json("notification_deliveries.json", [])
    if not isinstance(deliveries, list):
        deliveries = []
    sent = failed = 0

    for item in plan:
        delivery = {
            "task_id": item.task_id,
            "role": item.role,
            "touser": item.touser,
            "action": item.action,
            "content": item.content,
            "attempted_at": timestamp.isoformat(timespec="seconds"),
        }
        try:
            response = send(item.touser, item.content)
            if isinstance(response, dict) and int(response.get("errcode") or 0) != 0:
                raise RuntimeError(str(response))
            delivery["status"] = "sent"
            delivery["response"] = response
            succeeded[item.task_id] += 1
            sent += 1
        except Exception as exc:
            delivery["status"] = "failed"
            delivery["error"] = str(exc)
            failed += 1
        deliveries.append(delivery)

    stamp = timestamp.isoformat(timespec="seconds")
    for task in tasks:
        task_id = str(task.get("id") or "")
        if expected[task_id] and succeeded[task_id] == expected[task_id]:
            task["last_escalation_action"] = action_by_task[task_id]
            task["last_escalated_at"] = stamp
            task["escalation_count"] = int(task.get("escalation_count") or 0) + 1

    if plan:
        from .write_guard import authorized_system_write

        with authorized_system_write(
            store.data_dir,
            job_name="task_escalation_delivery",
            allowed_files={"tasks.json"},
        ):
            store.save_tasks(tasks)
            store.write_json("notification_deliveries.json", deliveries[-2000:])
    return {"sent": sent, "failed": failed}
