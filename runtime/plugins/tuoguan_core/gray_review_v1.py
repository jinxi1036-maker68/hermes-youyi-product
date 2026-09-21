"""Gray rollout review report for Hermes digital employee.

The review summarizes acceptance readiness, real gray observations and wakeup
patrol data. It is intentionally read-only for business state and does not
promote observations into handbook rules, learning candidates, performance
evidence, salary changes or automatic rollout decisions.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any

from .acceptance_v1 import run_acceptance_v1_dry_run
from .digital_employee_state import query_gray_observations, query_gray_rollout_decisions
from .models import UserIdentity
from .store import TuoguanStore
from .tenant_context import current_tenant_id
from .wakeup_v2 import run_wakeup_v2_dry_run


def run_gray_review_v1(
    store: TuoguanStore | None = None,
    *,
    now: datetime | None = None,
    write_report: bool = True,
) -> dict[str, Any]:
    actual_store = store or TuoguanStore()
    timestamp = now or datetime.now().astimezone()
    identity = UserIdentity(
        platform="system",
        platform_user_id="gray_review_v1",
        canonical_user_id="gray_review_v1",
        person_name="Hermes",
        role="boss",
        approval_state="approved",
    )
    acceptance = run_acceptance_v1_dry_run(actual_store, now=timestamp, write_report=False)
    wakeup = run_wakeup_v2_dry_run(actual_store, now=timestamp, write_report=False)
    observations = query_gray_observations(actual_store, identity=identity, limit=100)
    rollout_decisions = query_gray_rollout_decisions(actual_store, identity=identity, limit=20)
    latest_rollout_decision = _latest_rollout_decision(rollout_decisions)
    suggestions = _review_suggestions(acceptance, observations, wakeup, latest_rollout_decision)
    summary = {
        "schema_version": 1,
        "tenant_id": current_tenant_id(),
        "report_type": "gray_review_v1",
        "generated_at": timestamp.isoformat(timespec="seconds"),
        "read_only": True,
        "actions_taken": [],
        "review_boundary": {
            "limits_model": False,
            "changes_router": False,
            "updates_handbook": False,
            "creates_learning_candidate": False,
            "creates_performance_evidence": False,
            "changes_salary": False,
            "sends_notifications": False,
            "auto_expands_rollout": False,
        },
        "source_counts": {
            "acceptance_ready_count": int((acceptance.get("decision") or {}).get("ready_count") or 0),
            "acceptance_deferred_count": int((acceptance.get("decision") or {}).get("deferred_count") or 0),
            "acceptance_blocked_count": int((acceptance.get("decision") or {}).get("blocked_count") or 0),
            "gray_observation_count": int(observations.get("observation_count") or 0),
            "gray_issue_count": int((observations.get("outcome_counts") or {}).get("issue") or 0),
            "gray_success_count": int((observations.get("outcome_counts") or {}).get("success") or 0),
            "rollout_decision_count": int(rollout_decisions.get("decision_count") or 0),
            "wakeup_risk_count": len(wakeup.get("risks") or []),
            "wakeup_opportunity_count": len(wakeup.get("opportunities") or []),
        },
        "decision": {
            "recommended_next_step": _recommended_next_step(acceptance, observations, wakeup, latest_rollout_decision),
            "latest_owner_decision": latest_rollout_decision,
            "auto_apply": False,
            "requires_owner_review": True,
        },
        "suggestions": suggestions,
        "sections": {
            "acceptance": acceptance,
            "gray_observations": observations,
            "gray_rollout_decisions": rollout_decisions,
            "wakeup": wakeup,
        },
    }
    rendered = render_gray_review_v1_report(summary)
    summary["rendered_text"] = rendered
    summary["render_verified"] = True
    if write_report:
        summary["report_path"] = str(_write_report(actual_store, timestamp, rendered, summary))
    return summary


def render_gray_review_v1_report(summary: dict[str, Any]) -> str:
    counts = summary.get("source_counts") or {}
    decision = summary.get("decision") or {}
    lines = [
        f"# Hermes 灰度复盘 V1｜{str(summary.get('generated_at') or '')[:10]}",
        "",
        "状态：只读复盘报告。未修改手册，未生成学习候选，未生成绩效证据，未改工资，未发通知，未自动放量。",
        "",
        "## 老板摘要",
        "",
        f"- 建议下一步：{decision.get('recommended_next_step')}",
        f"- 验收准备：ready {counts.get('acceptance_ready_count', 0)}，deferred {counts.get('acceptance_deferred_count', 0)}，blocked {counts.get('acceptance_blocked_count', 0)}。",
        f"- 灰度观察：共 {counts.get('gray_observation_count', 0)} 条，成功 {counts.get('gray_success_count', 0)} 条，问题 {counts.get('gray_issue_count', 0)} 条。",
        f"- 老板决策记录：{counts.get('rollout_decision_count', 0)} 条；最新决策：{_decision_label(decision.get('latest_owner_decision'))}。",
        f"- 巡店风险：{counts.get('wakeup_risk_count', 0)} 项；经营机会候选：{counts.get('wakeup_opportunity_count', 0)} 项。",
        "",
        "## 复盘建议",
        "",
    ]
    suggestions = summary.get("suggestions") or []
    if suggestions:
        lines.extend(f"- [{item.get('level')}] {item.get('title')}：{item.get('detail')}" for item in suggestions)
    else:
        lines.append("- 暂无需要处理的复盘建议。")
    lines.extend(["", "## 边界确认", ""])
    lines.append("- 复盘报告只是材料，不是规则；不会限制 Hermes 思考、追问、建议或选择工具。")
    lines.append("- 任何后续手册修改、学习候选、绩效证据、放量动作，都需要另行由老板或实现者明确确认。")
    return "\n".join(lines).rstrip() + "\n"


def _review_suggestions(
    acceptance: dict[str, Any],
    observations: dict[str, Any],
    wakeup: dict[str, Any],
    latest_rollout_decision: dict[str, Any] | None,
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    if latest_rollout_decision:
        result.append(_suggestion(
            "low",
            "参考老板最新灰度决策",
            f"{latest_rollout_decision.get('decision_text')} 这只是决策记录，系统不会自动执行放量或改权限。",
        ))
    decision = acceptance.get("decision") or {}
    if int(decision.get("blocked_count") or 0):
        result.append(_suggestion("high", "先处理验收阻塞", f"仍有 {decision.get('blocked_count')} 个验收场景 blocked，暂不扩大灰度。"))
    if int(decision.get("deferred_count") or 0):
        result.append(_suggestion("medium", "保留暂缓事项", f"{decision.get('deferred_count')} 个场景 deferred，应继续按学期状态或老板确认节奏处理。"))
    outcome_counts = observations.get("outcome_counts") or {}
    if int(outcome_counts.get("issue") or 0):
        result.append(_suggestion("medium", "复核灰度问题", f"已有 {outcome_counts.get('issue')} 条问题观察，建议人工复盘原对话后再决定是否优化手册或工具。"))
    if not int(observations.get("observation_count") or 0):
        result.append(_suggestion("low", "开始积累真实观察", "当前还没有灰度观察记录；真实渠道小范围试用时，先记录体验样本，不急着改规则。"))
    risks = wakeup.get("risks") or []
    if risks:
        high_count = len([item for item in risks if str(item.get("level") or "") == "high"])
        level = "high" if high_count else "medium"
        result.append(_suggestion(level, "巡店风险需人工查看", f"只读巡店发现 {len(risks)} 项风险，先由老板/店长看摘要，不自动派任务。"))
    return result


def _recommended_next_step(
    acceptance: dict[str, Any],
    observations: dict[str, Any],
    wakeup: dict[str, Any],
    latest_rollout_decision: dict[str, Any] | None = None,
) -> str:
    if latest_rollout_decision:
        decision_type = str(latest_rollout_decision.get("decision_type") or "")
        text = str(latest_rollout_decision.get("decision_text") or "")
        if decision_type == "pause":
            return f"按老板最新决策暂停灰度：{text}"
        if decision_type == "continue_small_gray":
            return f"按老板最新决策继续小范围灰度：{text}"
        if decision_type == "defer_item":
            return f"按老板最新决策暂缓指定事项：{text}"
        if decision_type == "expand_candidate":
            return f"老板已有扩大候选决策记录，但仍需另行人工执行和权限确认：{text}"
        if decision_type == "rollback_candidate":
            return f"老板已有回滚候选决策记录，但仍需另行人工执行和验证：{text}"
        return f"参考老板最新灰度决策记录：{text}"
    decision = acceptance.get("decision") or {}
    outcome_counts = observations.get("outcome_counts") or {}
    if int(decision.get("blocked_count") or 0):
        return "先修复 blocked 验收项，再继续真实渠道灰度。"
    if int(outcome_counts.get("issue") or 0):
        return "先人工复盘问题观察，再决定是否优化手册或工具。"
    if not int(observations.get("observation_count") or 0):
        return "继续老板、店长、示例老师小范围试用，并开始记录真实观察。"
    if wakeup.get("risks"):
        return "保持小范围灰度，同时由老板/店长查看巡店风险。"
    return "可继续小范围灰度，暂不自动扩大到更多老师。"


def _latest_rollout_decision(decisions: dict[str, Any]) -> dict[str, Any] | None:
    rows = decisions.get("decisions") if isinstance(decisions, dict) else []
    if not isinstance(rows, list) or not rows:
        return None
    rows = [row for row in rows if isinstance(row, dict)]
    if not rows:
        return None
    return max(rows, key=lambda item: str(item.get("created_at") or ""))


def _decision_label(decision: Any) -> str:
    if not isinstance(decision, dict):
        return "暂无"
    text = str(decision.get("decision_text") or "").strip()
    return text or str(decision.get("decision_type") or "已记录")


def _suggestion(level: str, title: str, detail: str) -> dict[str, str]:
    return {"level": level, "title": title, "detail": detail}


def _write_report(store: TuoguanStore, timestamp: datetime, rendered: str, summary: dict[str, Any]) -> Path:
    report_dir = store.data_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = f"gray-review-v1-{timestamp.strftime('%Y%m%d-%H%M%S')}"
    md_path = report_dir / f"{stem}.md"
    json_path = report_dir / f"{stem}.json"
    md_path.write_text(rendered, encoding="utf-8")
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return md_path
