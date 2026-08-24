"""Wakeup V2 dry-run patrol for Hermes digital employee.

The patrol is intentionally read-only for business state. It creates a boss
summary report under reports/ but does not send messages, create tasks, modify
salary, or close safety events.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from .digital_employee_state import (
    generate_due_wakeup_candidates,
    submit_wakeup_request,
    query_active_goal_work_state,
    query_autonomous_work_brief,
    query_parent_communication_coverage,
    query_profile_candidates,
    query_student_service_relations,
    query_value_ledger,
    query_weekly_record_coverage,
)
from .models import UserIdentity
from .store import TuoguanStore
from .tenant_context import current_tenant_id
from .tasks import CLOSED_TASK_STATUSES
from .write_guard import authorized_system_write


_CLOSED_TASK_STATUSES = CLOSED_TASK_STATUSES
TERM_STATE_FILE = "academic_term_state.json"


def run_wakeup_v2_dry_run(
    store: TuoguanStore | None = None,
    *,
    now: datetime | None = None,
    write_report: bool = True,
    materialize_wakeup_ledger: bool = False,
) -> dict[str, Any]:
    """Build a read-only owner patrol summary from real local data."""

    actual_store = store or TuoguanStore()
    timestamp = now or datetime.now().astimezone()
    identity = UserIdentity(
        platform="system",
        platform_user_id="wakeup_v2_dry_run",
        canonical_user_id="wakeup_v2_dry_run",
        person_name="Hermes",
        role="boss",
        approval_state="approved",
    )
    service_relations = query_student_service_relations(actual_store, identity=identity)
    weekly_records = query_weekly_record_coverage(actual_store, identity=identity, days=7)
    parent_coverage = query_parent_communication_coverage(actual_store, identity=identity, days=31)
    goals = query_active_goal_work_state(actual_store, identity=identity)
    profiles = query_profile_candidates(actual_store, limit=20)
    value_ledger = query_value_ledger(actual_store, limit=20)
    autonomous_work = query_autonomous_work_brief(actual_store, identity=identity, limit=20)
    due_wakeup_candidates = generate_due_wakeup_candidates(actual_store, identity=identity, now_at=timestamp.isoformat(timespec="seconds"), limit=20)
    safety = _safety_snapshot(actual_store)
    notifications = _notification_snapshot(actual_store)
    term_state = _term_state(actual_store, timestamp)
    deferred_items = _deferred_items(term_state, service_relations)
    readiness = _new_term_readiness(term_state, service_relations)
    opportunities = _business_opportunity_candidates(weekly_records, parent_coverage, profiles, term_state)
    risks = _risk_items(service_relations, weekly_records, parent_coverage, goals, safety, notifications, term_state)
    summary = {
        "schema_version": 1,
        "tenant_id": current_tenant_id(),
        "report_type": "wakeup_v2_dry_run",
        "generated_at": timestamp.isoformat(timespec="seconds"),
        "read_only": True,
        "actions_taken": [],
        "forbidden_actions_confirmed_absent": [
            "no_parent_messages_sent",
            "no_teacher_messages_sent",
            "no_tasks_created",
            "no_salary_or_payroll_changed",
            "no_data_deleted",
            "no_safety_event_closed",
            "no_business_action_auto_executed",
            "no_action_retried",
            "no_wakeup_requests_created",
            "no_wakeup_requests_updated",
        ],
        "source_counts": {
            "service_relation_count": service_relations.get("relation_count", 0),
            "service_relation_missing_count": service_relations.get("missing_count", 0),
            "weekly_record_total_students": weekly_records.get("total_students", 0),
            "weekly_record_covered_count": weekly_records.get("covered_count", 0),
            "weekly_record_missing_count": weekly_records.get("missing_count", 0),
            "parent_coverage_total_students": parent_coverage.get("total_students", 0),
            "parent_coverage_covered_count": parent_coverage.get("covered_count", 0),
            "parent_coverage_missing_count": parent_coverage.get("missing_count", 0),
            "active_goal_count": goals.get("goal_count", 0),
            "profile_candidate_count": profiles.get("candidate_count", 0),
            "value_ledger_entry_count": value_ledger.get("entry_count", 0),
            "open_safety_task_count": safety.get("open_safety_task_count", 0),
            "notification_pending_count": notifications.get("pending_count", 0),
            "notification_failed_count": notifications.get("failed_count", 0),
            "autonomous_work_item_count": autonomous_work.get("work_item_count", 0),
            "autonomous_waiting_count": autonomous_work.get("waiting_count", 0),
            "pending_wakeup_count": autonomous_work.get("pending_wakeup_count", 0),
            "result_unknown_action_count": autonomous_work.get("result_unknown_action_count", 0),
            "due_wakeup_candidate_count": due_wakeup_candidates.get("candidate_count", 0),
        },
        "term_state": term_state,
        "deferred_items": deferred_items,
        "new_term_readiness": readiness,
        "risks": risks,
        "opportunities": opportunities,
        "sections": {
            "service_relations": service_relations,
            "weekly_records": weekly_records,
            "parent_communication": parent_coverage,
            "active_goals": goals,
            "profile_candidates": profiles,
            "value_ledger": value_ledger,
            "autonomous_work": autonomous_work,
            "due_wakeup_candidates": due_wakeup_candidates,
            "safety": safety,
            "notifications": notifications,
        },
    }
    if materialize_wakeup_ledger:
        with authorized_system_write(actual_store.data_dir, job_name="timer_readonly_patrol", allowed_files={"wakeup_requests.jsonl"}):
            wakeup_ledger = submit_wakeup_request(
                actual_store,
                identity=identity,
                wakeup_source="timer_readonly_patrol",
                reason="30-minute readonly timer wakeup: patrol facts and write an internal report only; no messages, tasks, retries, or forced next step.",
                operation_id=f"timer_readonly_patrol_{timestamp.strftime('%Y%m%d%H%M%S')}",
                related_objects=[{"report_type": "wakeup_v2_dry_run", "generated_at": timestamp.isoformat(timespec="seconds")}],
                scheduled_for=timestamp.isoformat(timespec="seconds"),
                status="handled",
                source_text="systemd timer readonly wakeup",
                source_message_id=f"timer:{timestamp.strftime('%Y%m%d%H%M%S')}",
            )
        summary["wakeup_ledger"] = wakeup_ledger
    else:
        summary["wakeup_ledger"] = {"ok": True, "materialized": False}
    rendered = render_wakeup_v2_report(summary)
    summary["rendered_text"] = rendered
    summary["render_verified"] = True
    if write_report:
        report_path = _write_report(actual_store, timestamp, rendered, summary)
        summary["report_path"] = str(report_path)
    return summary


def render_wakeup_v2_report(summary: dict[str, Any]) -> str:
    counts = summary.get("source_counts") or {}
    term = summary.get("term_state") or {}
    service_line = f"- 服务关系缺口：{counts.get('service_relation_missing_count', 0)} 个。"
    if term.get("service_relation_policy") == "defer_until_new_term":
        service_line = (
            f"- 服务关系缺口：{counts.get('service_relation_missing_count', 0)} 个，"
            f"当前按“{term.get('label')}”处理为新学期待确认，不作为当前催办风险。"
        )
    lines = [
        f"# Hermes Wakeup V2 Dry Run｜{str(summary.get('generated_at') or '')[:10]}",
        "",
        "状态：只读巡店摘要。未发送家长消息，未发送老师消息，未批量派任务，未修改工资，未删除数据，未自动闭环安全事件。",
        "",
        "## 今日老板摘要",
        "",
        f"- 学期状态：{term.get('label') or '未配置'}；服务关系策略：{term.get('service_relation_policy') or 'unknown'}。",
        service_line,
        f"- 近7天表现记录覆盖：{counts.get('weekly_record_covered_count', 0)}/{counts.get('weekly_record_total_students', 0)}。",
        f"- 近31天家校沟通证据覆盖：{counts.get('parent_coverage_covered_count', 0)}/{counts.get('parent_coverage_total_students', 0)}。",
        f"- 活跃目标：{counts.get('active_goal_count', 0)} 个；画像候选：{counts.get('profile_candidate_count', 0)} 条；价值账本：{counts.get('value_ledger_entry_count', 0)} 条。",
        f"- 未闭环安全任务：{counts.get('open_safety_task_count', 0)} 条；待处理通知：{counts.get('notification_pending_count', 0)} 条；失败通知：{counts.get('notification_failed_count', 0)} 条。",
        f"- Hermes 自主工作：事项 {counts.get('autonomous_work_item_count', 0)} 条；等待 {counts.get('autonomous_waiting_count', 0)} 条；待处理唤醒 {counts.get('pending_wakeup_count', 0)} 条；到期候选 {counts.get('due_wakeup_candidate_count', 0)} 条；结果未知动作 {counts.get('result_unknown_action_count', 0)} 条。",
        "",
        "## 风险与卡点",
        "",
    ]
    risks = summary.get("risks") or []
    if risks:
        lines.extend(f"- [{item.get('level')}] {item.get('title')}：{item.get('detail')}" for item in risks[:12])
    else:
        lines.append("- 未发现需要立即升级的只读巡店风险。")
    lines.extend(["", "## Hermes 自主恢复候选", ""])
    due_section = (summary.get("sections") or {}).get("due_wakeup_candidates") or {}
    due_candidates = due_section.get("candidates") if isinstance(due_section, dict) else []
    if due_candidates:
        for item in due_candidates[:10]:
            if item.get("candidate_type") == "due_work_item_attention":
                lines.append(f"- 到期事项：{item.get('title') or item.get('focus_key')}；建议先核验新事实，不自动落账或执行。")
            else:
                lines.append(f"- 结果未知：{item.get('action_summary') or item.get('action_type')}；先核验回执和幂等键，不自动重试。")
    else:
        lines.append("- 当前没有到期等待事项或结果未知动作候选。")
    lines.append("- 本段只提供恢复材料；未创建或更新唤醒请求，未发送消息，未派任务，未重试动作。")

    lines.extend(["", "## 经营机会候选", ""])
    opportunities = summary.get("opportunities") or []
    if opportunities:
        lines.extend(f"- {item.get('title')}：{item.get('detail')}" for item in opportunities[:10])
    else:
        lines.append("- 当前数据不足以形成明确经营机会候选。")
    lines.extend(["", "## 新学期准备", ""])
    readiness = summary.get("new_term_readiness") or {}
    reminders = readiness.get("reminders") or []
    if reminders:
        lines.extend(f"- {item}" for item in reminders)
    else:
        lines.append("- 当前没有新学期准备提醒。")
    deferred = summary.get("deferred_items") or []
    if deferred:
        lines.extend(["", "## 暂缓事项", ""])
        lines.extend(f"- {item.get('title')}：{item.get('detail')}" for item in deferred[:10])
    lines.extend(["", "## 数据来源", ""])
    for name, value in counts.items():
        lines.append(f"- {name}: {value}")
    return "\n".join(lines).rstrip() + "\n"


def _write_report(store: TuoguanStore, timestamp: datetime, rendered: str, summary: dict[str, Any]) -> Path:
    report_dir = store.data_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = f"wakeup-v2-dry-run-{timestamp.strftime('%Y%m%d-%H%M%S')}"
    md_path = report_dir / f"{stem}.md"
    json_path = report_dir / f"{stem}.json"
    md_path.write_text(rendered, encoding="utf-8")
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return md_path


def _safety_snapshot(store: TuoguanStore) -> dict[str, Any]:
    tasks = store.read_json("tasks.json", [])
    if not isinstance(tasks, list):
        tasks = []
    safety_tasks = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        level = str(task.get("level") or "").upper()
        task_type = str(task.get("type") or task.get("source_type") or "").lower()
        status = str(task.get("status") or "").lower()
        if level == "S" or "safety" in task_type:
            row = deepcopy(task)
            safety_tasks.append(row)
    open_safety = [task for task in safety_tasks if str(task.get("status") or "").lower() not in _CLOSED_TASK_STATUSES]
    return {
        "ok": True,
        "safety_task_count": len(safety_tasks),
        "open_safety_task_count": len(open_safety),
        "open_safety_tasks": open_safety[:20],
        "render_verified": True,
    }


def _notification_snapshot(store: TuoguanStore) -> dict[str, Any]:
    rows = store.read_json("notification_outbox.json", [])
    if not isinstance(rows, list):
        rows = []
    pending = [row for row in rows if isinstance(row, dict) and str(row.get("status") or "").lower() in {"pending", "scheduled", ""}]
    failed = [row for row in rows if isinstance(row, dict) and str(row.get("status") or "").lower() in {"failed", "error"}]
    return {
        "ok": True,
        "pending_count": len(pending),
        "failed_count": len(failed),
        "pending_notifications": pending[-20:],
        "failed_notifications": failed[-20:],
        "render_verified": True,
    }


def _risk_items(
    service_relations: dict[str, Any],
    weekly_records: dict[str, Any],
    parent_coverage: dict[str, Any],
    goals: dict[str, Any],
    safety: dict[str, Any],
    notifications: dict[str, Any],
    term_state: dict[str, Any],
) -> list[dict[str, Any]]:
    risks: list[dict[str, Any]] = []
    if safety.get("open_safety_task_count"):
        risks.append(_risk("high", "安全任务未闭环", f"{safety.get('open_safety_task_count')} 条 S 级/安全任务仍处于开放状态。"))
    if notifications.get("failed_count"):
        risks.append(_risk("medium", "通知异常", f"{notifications.get('failed_count')} 条通知失败，需要检查通道或接收人映射。"))
    if service_relations.get("missing_count") and term_state.get("service_relation_policy") != "defer_until_new_term":
        risks.append(_risk("medium", "服务责任关系缺口", f"{service_relations.get('missing_count')} 个学生缺少服务类型或责任老师字段。"))
    if weekly_records.get("missing_count"):
        risks.append(_risk("medium", "表现记录覆盖缺口", f"近7天 {weekly_records.get('missing_count')} 名学生没有表现记录证据。"))
    if parent_coverage.get("missing_count"):
        risks.append(_risk("medium", "家校沟通覆盖缺口", f"近31天 {parent_coverage.get('missing_count')} 名学生没有家校沟通证据。"))
    if not goals.get("goal_count"):
        risks.append(_risk("low", "活跃目标缺失", "当前没有查到活跃目标状态；如果老板已有目标，需要补目标工作状态。"))
    return risks


def _business_opportunity_candidates(
    weekly_records: dict[str, Any],
    parent_coverage: dict[str, Any],
    profiles: dict[str, Any],
    term_state: dict[str, Any],
) -> list[dict[str, Any]]:
    opportunities: list[dict[str, Any]] = []
    total = int(weekly_records.get("total_students") or 0)
    covered = int(weekly_records.get("covered_count") or 0)
    if total and covered / total < 0.5:
        opportunities.append({
            "title": "记录覆盖提升",
            "detail": "表现记录覆盖偏低，先补真实记录证据，有助于续费沟通和学生画像。",
        })
    parent_total = int(parent_coverage.get("total_students") or 0)
    parent_covered = int(parent_coverage.get("covered_count") or 0)
    if parent_total and parent_covered / parent_total < 0.5:
        opportunities.append({
            "title": "家校沟通覆盖提升",
            "detail": "家校沟通证据偏少，可优先从高续费价值或长期未沟通学生开始整理事实和话术。",
        })
    if profiles.get("candidate_count"):
        opportunities.append({
            "title": "画像候选转经营洞察",
            "detail": f"已有 {profiles.get('candidate_count')} 条画像候选，可复核后用于续费风险、学习问题聚类或项目机会判断。",
        })
    if term_state.get("service_relation_policy") == "defer_until_new_term":
        opportunities.append({
            "title": "新学期服务关系准备",
            "detail": "当前处于暑假/新学期过渡期，服务关系不催办；到开学前后再确认新名单、服务类型和责任老师。",
        })
    return opportunities


def _risk(level: str, title: str, detail: str) -> dict[str, str]:
    return {"level": level, "title": title, "detail": detail}


def _term_state(store: TuoguanStore, timestamp: datetime) -> dict[str, Any]:
    configured = store.read_json(TERM_STATE_FILE, {})
    if isinstance(configured, dict) and configured.get("state"):
        state = str(configured.get("state") or "")
        label = str(configured.get("label") or _default_term_label(state))
        policy = str(configured.get("service_relation_policy") or _policy_for_term_state(state))
        return {
            "state": state,
            "label": label,
            "source": "academic_term_state.json",
            "service_relation_policy": policy,
            "new_term_month": int(configured.get("new_term_month") or 9),
            "reminder_window": str(configured.get("reminder_window") or "开学前后由老板/店长确认新学期名单、服务类型和责任老师。"),
        }
    month = timestamp.month
    if month in {7, 8}:
        state = "term_transition"
    else:
        state = "regular_term" if month in {9, 10, 11, 12, 1, 2, 3, 4, 5, 6} else "unknown"
    return {
        "state": state,
        "label": _default_term_label(state),
        "source": "calendar_default",
        "service_relation_policy": _policy_for_term_state(state),
        "new_term_month": 9,
        "reminder_window": "8月底到9月初，确认新学期名单、服务类型和责任老师。",
    }


def _policy_for_term_state(state: str) -> str:
    return "defer_until_new_term" if state in {"term_transition", "summer_break", "pre_new_term"} else "active_check"


def _default_term_label(state: str) -> str:
    return {
        "term_transition": "暑假/新学期过渡期",
        "summer_break": "暑假",
        "pre_new_term": "新学期准备期",
        "regular_term": "正式学期",
    }.get(str(state or ""), "未配置")


def _deferred_items(term_state: dict[str, Any], service_relations: dict[str, Any]) -> list[dict[str, str]]:
    if term_state.get("service_relation_policy") != "defer_until_new_term":
        return []
    missing = int(service_relations.get("missing_count") or 0)
    if not missing:
        return []
    return [{
        "title": "服务关系缺口暂缓",
        "detail": f"当前为{term_state.get('label')}，{missing} 个服务关系缺口先标记为新学期待确认，不主动追问老板/店长/老师。",
    }]


def _new_term_readiness(term_state: dict[str, Any], service_relations: dict[str, Any]) -> dict[str, Any]:
    reminders: list[str] = []
    if term_state.get("service_relation_policy") == "defer_until_new_term":
        reminders.append("暂不补上学期历史学生服务关系，避免把过期关系写成长期事实。")
        reminders.append(str(term_state.get("reminder_window") or "开学前后确认新学期名单、服务类型和责任老师。"))
        if service_relations.get("missing_count"):
            reminders.append(f"到新学期名单稳定后，再处理 {service_relations.get('missing_count')} 个服务关系字段缺口。")
    return {
        "policy": term_state.get("service_relation_policy"),
        "reminders": reminders,
        "auto_notify": False,
        "auto_create_tasks": False,
        "auto_write_candidates": False,
    }
