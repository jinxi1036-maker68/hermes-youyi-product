"""Production-safe autonomous wakeup runner for Hermes.

This runner gives Hermes a real clock tick. It reads current autonomous work
state and patrol data, writes internal reports, and when the model-led employee
loop is enabled may queue bounded proactive messages through the audited outbox.
It never contacts parents, creates tasks, changes salary, closes events, or
stores a fixed next step.
"""

from __future__ import annotations

from datetime import datetime
import os
import json
from pathlib import Path
from typing import Any

from .digital_employee_state import (
    generate_autonomous_log_review,
    generate_autonomous_recovery_report,
    generate_due_wakeup_candidates,
)
from .models import UserIdentity
from .store import TuoguanStore
from .tenant_context import current_tenant_id
from .wakeup_v2 import run_wakeup_v2_dry_run
from .autonomous_employee_loop import run_autonomous_employee_loop


def run_autonomous_wakeup_once(
    store: TuoguanStore | None = None,
    *,
    now: datetime | None = None,
    write_report: bool = True,
) -> dict[str, Any]:
    """Run one autonomous wakeup pass with production boundaries."""

    actual_store = store or TuoguanStore()
    timestamp = now or datetime.now().astimezone()
    identity = UserIdentity(
        platform="system",
        platform_user_id="autonomous_wakeup_runner",
        canonical_user_id="autonomous_wakeup_runner",
        person_name="Hermes 自主唤醒",
        role="boss",
        approval_state="approved",
    )
    due = generate_due_wakeup_candidates(actual_store, identity=identity, now_at=timestamp.isoformat(timespec="seconds"), limit=50)
    recovery = generate_autonomous_recovery_report(
        actual_store,
        identity=identity,
        now_at=timestamp.isoformat(timespec="seconds"),
        limit=50,
    )
    materialize_ledger = str(os.getenv("HERMES_AUTONOMOUS_WAKEUP_MATERIALIZE_LEDGER") or "").strip() == "1"
    patrol = run_wakeup_v2_dry_run(actual_store, now=timestamp, write_report=False, materialize_wakeup_ledger=materialize_ledger)
    log_review = generate_autonomous_log_review(actual_store, identity=identity, limit=80, include_gray_observations=True)
    employee_loop_enabled = str(os.getenv("HERMES_AUTONOMOUS_EMPLOYEE_LOOP") or "").strip() == "1"
    employee_loop = None

    summary = {
        "ok": True,
        "status": "ok",
        "schema_version": 1,
        "tenant_id": current_tenant_id(),
        "report_type": "autonomous_wakeup_runner_v1",
        "generated_at": timestamp.isoformat(timespec="seconds"),
        "read_only": not employee_loop_enabled,
        "actions_taken": [],
        "boundary": {
            "clock_tick_only": not employee_loop_enabled,
            "limits_model": False,
            "routes_intent": False,
            "stores_model_next_step": False,
            "materializes_wakeup_requests": materialize_ledger,
            "sends_parent_messages": False,
            "sends_teacher_messages": False,
            "sends_owner_messages": False,
            "may_queue_owner_messages": employee_loop_enabled,
            "may_queue_manager_teacher_fact_requests": employee_loop_enabled,
            "creates_tasks": False,
            "changes_salary": False,
            "changes_permissions": False,
            "updates_handbook": False,
            "patches_tools": False,
            "deletes_data": False,
            "model_led_employee_loop": employee_loop_enabled,
        },
        "source_counts": {
            "due_wakeup_candidate_count": int(due.get("candidate_count") or 0),
            "visible_work_item_count": int((recovery.get("counts") or {}).get("visible_work_item_count") or 0),
            "waiting_count": int((recovery.get("counts") or {}).get("waiting_count") or 0),
            "blocked_count": int((recovery.get("counts") or {}).get("blocked_count") or 0),
            "pending_wakeup_count": int((recovery.get("counts") or {}).get("pending_wakeup_count") or 0),
            "result_unknown_action_count": int((recovery.get("counts") or {}).get("result_unknown_action_count") or 0),
            "patrol_risk_count": len(patrol.get("risks") or []),
            "patrol_opportunity_count": len(patrol.get("opportunities") or []),
            "log_review_candidate_count": int(log_review.get("issue_candidate_count") or 0),
            "employee_loop_ran": 0,
            "employee_loop_write_count": 0,
        },
        "sections": {
            "due_wakeup_candidates": due,
            "recovery": recovery,
            "patrol": patrol,
            "log_review": log_review,
        },
        "forbidden_actions_confirmed_absent": [
            "no_parent_messages_sent",
            "no_teacher_messages_sent",
            "no_owner_messages_sent",
            "no_tasks_created",
            "no_salary_or_payroll_changed",
            "no_data_deleted",
            "no_wakeup_requests_created" if not materialize_ledger else "only_handled_timer_wakeup_request_recorded",
            "no_wakeup_requests_updated",
            "no_action_retried",
            "no_work_items_closed",
            "no_router_changed",
            "no_model_next_step_stored",
        ],
    }

    if employee_loop_enabled:
        employee_loop = run_autonomous_employee_loop(actual_store, now=timestamp, wakeup_summary=summary, write_state=True)
        summary["sections"]["employee_loop"] = employee_loop
        summary["source_counts"]["employee_loop_ran"] = 1 if employee_loop.get("ok") else 0
        summary["source_counts"]["employee_loop_write_count"] = len(employee_loop.get("writes") or [])
        if not employee_loop.get("ok"):
            summary["ok"] = False
            summary["status"] = "degraded"
            summary["failure_stage"] = str(employee_loop.get("error") or "employee_loop_failed")
            summary["failure_message"] = str(employee_loop.get("message") or "")
    rendered = render_autonomous_wakeup_report(summary)
    summary["rendered_text"] = rendered
    summary["render_verified"] = True
    if write_report:
        path = _write_report(actual_store, timestamp, rendered, summary)
        summary["report_path"] = str(path)
    return summary


def render_autonomous_wakeup_report(summary: dict[str, Any]) -> str:
    counts = summary.get("source_counts") or {}
    recovery = ((summary.get("sections") or {}).get("recovery") or {}) if isinstance(summary.get("sections"), dict) else {}
    work_items = (((recovery.get("sections") or {}).get("work_items") or {}).get("items") or []) if isinstance(recovery, dict) else []
    phase_lines: list[str] = []
    for item in work_items[:5]:
        phase = item.get("current_phase") if isinstance(item.get("current_phase"), dict) else {}
        if not phase:
            continue
        title = str(item.get("title") or item.get("focus_key") or "未命名事项")
        phase_name = str(phase.get("phase_name") or phase.get("phase_key") or "未命名阶段")
        phase_goal = str(phase.get("phase_goal") or "")
        phase_lines.append(f"- {title}：{phase_name}；{phase_goal}")
    lines = [
        f"# Hermes 自主唤醒心跳｜{str(summary.get('generated_at') or '')[:19]}",
        "",
        "状态：生产定时唤醒。Hermes 已醒来查看事实；如模型判断需要，可在边界内排队老板/店长/老师消息，但未联系家长，未派任务，未修改业务数据，未规定下一步。",
        "",
        "## 摘要",
        "",
        f"- 自主事项：{counts.get('visible_work_item_count', 0)} 条；等待 {counts.get('waiting_count', 0)} 条；阻塞 {counts.get('blocked_count', 0)} 条。",
        f"- 到期候选：{counts.get('due_wakeup_candidate_count', 0)} 条；待处理唤醒：{counts.get('pending_wakeup_count', 0)} 条；结果未知动作：{counts.get('result_unknown_action_count', 0)} 条。",
        f"- 巡店风险：{counts.get('patrol_risk_count', 0)} 条；经营机会候选：{counts.get('patrol_opportunity_count', 0)} 条；日志复盘候选：{counts.get('log_review_candidate_count', 0)} 条。",
        "",
        "## 当前推进阶段",
        "",
    ]
    if phase_lines:
        lines.extend(phase_lines)
    else:
        lines.append("- 当前没有记录了阶段计划的工作事项。")
    employee_loop = ((summary.get("sections") or {}).get("employee_loop") or {}) if isinstance(summary.get("sections"), dict) else {}
    lines.extend(["", "## Model-led employee loop", ""])
    if employee_loop:
        if employee_loop.get("ok"):
            decision = employee_loop.get("decision") or {}
            lines.append(f"- Ran: yes; internal writes: {len(employee_loop.get('writes') or [])}.")
            lines.append(f"- Employee summary: {decision.get('employee_summary') or 'No summary.'}")
            lines.append(f"- Institution understanding: {decision.get('institution_understanding') or 'No institution view.'}")
            lines.append(f"- Goal view: {decision.get('goal_progress_view') or 'No goal view.'}")
            owner_candidates = decision.get("boss_attention_candidates") or []
            if owner_candidates:
                lines.append(f"- Owner attention candidates: {len(owner_candidates)}; only boss-directed low-frequency notes may be queued in daytime.")
        else:
            lines.append(f"- Ran: attempted but failed safely; {employee_loop.get('message') or employee_loop.get('error')}")
    else:
        lines.append("- Not enabled for this wakeup; this run remained read-only patrol only.")
    lines.extend([
        "",
        "## 边界",
        "",
        "- 这只是把 Hermes 按时间叫醒看材料，不是 Router，也不是固定流程。",
        "- 是否继续、追问、等待、写入状态、停止或请求人工确认，仍应由模型结合真实事实自主判断。",
        "- 当前允许白天给老板低频提醒，也允许向白名单店长/老师低频询问明确工作事实；不自动发家长、不批量派老师任务、不改工资、不改权限。",
    ])
    return "\n".join(lines).rstrip() + "\n"


def _write_report(store: TuoguanStore, timestamp: datetime, rendered: str, summary: dict[str, Any]) -> Path:
    reports_dir = store.data_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stem = f"autonomous-wakeup-v1-{timestamp.strftime('%Y%m%d-%H%M%S')}"
    md_path = reports_dir / f"{stem}.md"
    json_path = reports_dir / f"{stem}.json"
    md_path.write_text(rendered, encoding="utf-8")
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path


def main() -> int:
    result = run_autonomous_wakeup_once(write_report=True)
    print(result.get("rendered_text") or "")
    if result.get("report_path"):
        print(f"REPORT:{result['report_path']}")
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
