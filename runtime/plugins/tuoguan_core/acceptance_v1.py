"""Real-channel acceptance dry run for Hermes digital employee.

This module is intentionally read-only for business state. It checks whether
the boss/manager/teacher gray scenarios have enough real data and registered
tools to be tested in Enterprise WeChat, then writes an operator report under
reports/ only when requested.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any

from .digital_employee_state import (
    query_active_goal_work_state,
    query_information_requests,
    query_parent_communication_coverage,
    query_performance_evidence_candidates,
    query_student_service_relations,
    query_value_ledger,
    query_weekly_record_coverage,
)
from .models import UserIdentity
from .store import TuoguanStore
from .tools import TOOLS
from .wakeup_v2 import _term_state


DEFAULT_TARGET_ACCOUNTS = {
    "boss": "boss1",
    "manager": "manager1",
    "teacher": "teacher1",
}

ACCEPTANCE_SCENARIOS = (
    {
        "id": "boss_goal",
        "owner_role": "boss",
        "title": "老板设目标",
        "required_tools": ["tuoguan_query_active_goal_work_state", "tuoguan_submit_goal_evidence"],
        "description": "老板提出目标后，Hermes 应先理解目标、查事实、说明风险和计划，再在授权内保存证据或状态。",
    },
    {
        "id": "teacher_record",
        "owner_role": "teacher",
        "title": "老师记录学生表现",
        "required_tools": ["tuoguan_record_student", "tuoguan_query_weekly_record_coverage"],
        "description": "老师自然描述学生表现时，Hermes 可在权限内写入真实记录并写后反查。",
    },
    {
        "id": "manager_gap_query",
        "owner_role": "manager",
        "title": "店长查运营缺口",
        "required_tools": [
            "tuoguan_query_parent_communication_coverage",
            "tuoguan_query_weekly_record_coverage",
            "tuoguan_query_information_requests",
        ],
        "description": "店长能查记录覆盖、家校沟通覆盖和等待事项，但不自动催老师。",
    },
    {
        "id": "student_service_relation",
        "owner_role": "manager",
        "title": "新学生服务关系",
        "required_tools": ["tuoguan_query_student_service_relations", "tuoguan_submit_service_relation_fact_candidate"],
        "description": "新学期名单稳定后，再确认学生服务类型、责任老师和生效时间。",
    },
    {
        "id": "parent_communication_coverage",
        "owner_role": "boss",
        "title": "家校沟通覆盖",
        "required_tools": ["tuoguan_query_parent_communication_coverage"],
        "description": "Hermes 可整理真实沟通证据和缺口，家长消息默认只给建议稿，不自动发送。",
    },
    {
        "id": "wakeup_dry_run",
        "owner_role": "boss",
        "title": "夜间只读巡店摘要",
        "required_tools": [
            "tuoguan_query_active_goal_work_state",
            "tuoguan_query_parent_communication_coverage",
            "tuoguan_query_weekly_record_coverage",
            "tuoguan_query_value_ledger",
        ],
        "description": "夜间巡店只读产出老板摘要，不派任务、不通知家长、不改工资。",
    },
    {
        "id": "performance_evidence",
        "owner_role": "boss",
        "title": "绩效证据候选",
        "required_tools": [
            "tuoguan_query_performance_evidence_candidates",
            "tuoguan_submit_performance_evidence_candidate",
            "tuoguan_submit_performance_evidence_response",
        ],
        "description": "只形成证据候选、说明和异议，不自动评分、不改工资。",
    },
)


def run_acceptance_v1_dry_run(
    store: TuoguanStore | None = None,
    *,
    now: datetime | None = None,
    target_accounts: dict[str, str] | None = None,
    write_report: bool = True,
) -> dict[str, Any]:
    actual_store = store or TuoguanStore()
    timestamp = now or datetime.now().astimezone()
    targets = _target_accounts(actual_store, target_accounts)
    tool_names = {name for name, _schema, _handler in TOOLS}
    identity = UserIdentity(
        platform="system",
        platform_user_id="acceptance_v1_dry_run",
        canonical_user_id="acceptance_v1_dry_run",
        person_name="Hermes",
        role="boss",
        approval_state="approved",
    )
    term = _term_state(actual_store, timestamp)
    sources = _source_snapshot(actual_store, identity)
    account_checks = _account_checks(actual_store, targets)
    scenarios = [
        _scenario_readiness(scenario, tool_names, account_checks, sources, term)
        for scenario in ACCEPTANCE_SCENARIOS
    ]
    blocking = [item for item in scenarios if item["status"] == "blocked"]
    ready = [item for item in scenarios if item["status"] == "ready"]
    deferred = [item for item in scenarios if item["status"] == "deferred"]
    summary = {
        "schema_version": 1,
        "tenant_id": "youyi_tuoguan",
        "report_type": "acceptance_v1_dry_run",
        "generated_at": timestamp.isoformat(timespec="seconds"),
        "read_only": True,
        "actions_taken": [],
        "target_accounts": targets,
        "account_checks": account_checks,
        "tool_registry_count": len(tool_names),
        "term_state": term,
        "source_counts": _source_counts(sources),
        "scenarios": scenarios,
        "decision": {
            "recommended_scope": _recommended_scope(ready, blocking),
            "ready_count": len(ready),
            "blocked_count": len(blocking),
            "deferred_count": len(deferred),
            "auto_expand": False,
            "auto_notify": False,
            "auto_create_tasks": False,
            "auto_score_performance": False,
            "auto_change_salary": False,
        },
        "model_autonomy_boundary": {
            "handbook_is_reference": True,
            "no_router_added": True,
            "no_model_intent_injection": True,
            "execution_guards_only": [
                "identity",
                "permission",
                "operation_id",
                "ledger",
                "audit",
                "idempotency",
                "writeback_verification",
                "high_risk_boundary",
            ],
        },
        "sections": sources,
    }
    rendered = render_acceptance_v1_report(summary)
    summary["rendered_text"] = rendered
    summary["render_verified"] = True
    if write_report:
        summary["report_path"] = str(_write_report(actual_store, timestamp, rendered, summary))
    return summary


def render_acceptance_v1_report(summary: dict[str, Any]) -> str:
    decision = summary.get("decision") or {}
    lines = [
        f"# Hermes 真实渠道验收 V1 Dry Run｜{str(summary.get('generated_at') or '')[:10]}",
        "",
        "状态：只读验收准备报告。未发送老师消息，未发送家长消息，未创建任务，未评分，未修改工资，未删除数据。",
        "",
        "## 放量建议",
        "",
        f"- 建议范围：{decision.get('recommended_scope')}",
        f"- 准备就绪：{decision.get('ready_count', 0)} 个；暂缓：{decision.get('deferred_count', 0)} 个；阻塞：{decision.get('blocked_count', 0)} 个。",
        "- 自动放量：否；自动通知：否；自动派任务：否；自动评分/改工资：否。",
        "",
        "## 账号检查",
        "",
    ]
    for role, item in (summary.get("account_checks") or {}).items():
        lines.append(
            f"- {role}: {item.get('user_id') or '<missing>'}；"
            f"企业微信白名单={_yes_no(item.get('wecom_allowed'))}；"
            f"模型上下文白名单={_yes_no(item.get('runtime_allowed'))}；"
            f"身份角色={item.get('resolved_role') or 'unknown'}。"
        )
    lines.extend(["", "## 场景验收清单", ""])
    for item in summary.get("scenarios") or []:
        lines.append(f"- [{item.get('status')}] {item.get('title')}：{item.get('reason')}")
    lines.extend(["", "## 模型主导边界", ""])
    lines.append("- 手册和验收清单是材料，不是工作流；不注入 model_intent、next_tool、workflow_step、expected_reply。")
    lines.append("- 系统只在执行层守身份、权限、operation_id、ledger、audit、幂等、写后反查和高风险边界。")
    lines.extend(["", "## 数据概览", ""])
    for key, value in (summary.get("source_counts") or {}).items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines).rstrip() + "\n"


def _target_accounts(store: TuoguanStore, overrides: dict[str, str] | None) -> dict[str, str]:
    configured = store.read_json("acceptance_v1_config.json", {})
    result = dict(DEFAULT_TARGET_ACCOUNTS)
    wecom = store.read_json("wecom_whitelist.json", {})
    name_map = store.read_json("teacher_wecom_map.json", {})
    if isinstance(name_map, dict):
        result["boss"] = str(name_map.get("金总") or name_map.get("老板") or result["boss"])
        result["teacher"] = str(name_map.get("李老师") or result["teacher"])
    if isinstance(wecom, dict):
        managers = wecom.get("manager_ids")
        if isinstance(managers, list) and managers:
            result["manager"] = str(managers[0])
        elif wecom.get("manager_id"):
            result["manager"] = str(wecom.get("manager_id"))
        super_users = wecom.get("super_users")
        if not (isinstance(name_map, dict) and name_map.get("金总")) and isinstance(super_users, list) and super_users:
            result["boss"] = str(super_users[0])
    if isinstance(configured, dict) and isinstance(configured.get("target_accounts"), dict):
        result.update({str(k): str(v) for k, v in configured["target_accounts"].items() if str(v or "").strip()})
    if overrides:
        result.update({str(k): str(v) for k, v in overrides.items() if str(v or "").strip()})
    return result


def _source_snapshot(store: TuoguanStore, identity: UserIdentity) -> dict[str, Any]:
    return {
        "service_relations": query_student_service_relations(store, identity=identity),
        "weekly_records": query_weekly_record_coverage(store, identity=identity, days=7),
        "parent_communication": query_parent_communication_coverage(store, identity=identity, days=31),
        "active_goals": query_active_goal_work_state(store, identity=identity),
        "information_requests": query_information_requests(store, include_closed=True, limit=30),
        "performance_evidence": query_performance_evidence_candidates(store, identity=identity, limit=30),
        "value_ledger": query_value_ledger(store, limit=30),
    }


def _source_counts(sources: dict[str, Any]) -> dict[str, int]:
    weekly = sources.get("weekly_records") or {}
    parent = sources.get("parent_communication") or {}
    relations = sources.get("service_relations") or {}
    goals = sources.get("active_goals") or {}
    requests = sources.get("information_requests") or {}
    performance = sources.get("performance_evidence") or {}
    ledger = sources.get("value_ledger") or {}
    return {
        "service_relation_count": int(relations.get("relation_count") or 0),
        "service_relation_missing_count": int(relations.get("missing_count") or 0),
        "weekly_record_total_students": int(weekly.get("total_students") or 0),
        "weekly_record_covered_count": int(weekly.get("covered_count") or 0),
        "parent_coverage_total_students": int(parent.get("total_students") or 0),
        "parent_coverage_covered_count": int(parent.get("covered_count") or 0),
        "active_goal_count": int(goals.get("goal_count") or 0),
        "information_request_count": int(requests.get("request_count") or 0),
        "performance_evidence_candidate_count": int(performance.get("candidate_count") or 0),
        "value_ledger_entry_count": int(ledger.get("entry_count") or 0),
    }


def _account_checks(store: TuoguanStore, targets: dict[str, str]) -> dict[str, dict[str, Any]]:
    wecom = store.read_json("wecom_whitelist.json", {})
    runtime = _read_data_json(store, "manual_context/hermes_model_context_injection_allowlist_v1.json", {})
    runtime_ready = _runtime_capability_cards_ready(runtime)
    result: dict[str, dict[str, Any]] = {}
    for role, user_id in targets.items():
        result[role] = {
            "user_id": user_id,
            "wecom_allowed": _user_allowed(wecom, user_id),
            "runtime_allowed": runtime_ready,
            "resolved_role": _role_for(runtime, user_id) or _role_for(wecom, user_id),
        }
    return result


def _scenario_readiness(
    scenario: dict[str, Any],
    tool_names: set[str],
    account_checks: dict[str, dict[str, Any]],
    sources: dict[str, Any],
    term: dict[str, Any],
) -> dict[str, Any]:
    required = [str(item) for item in scenario.get("required_tools") or []]
    missing_tools = [name for name in required if name not in tool_names]
    account = account_checks.get(str(scenario.get("owner_role") or "")) or {}
    reasons: list[str] = []
    if missing_tools:
        reasons.append(f"缺少工具注册：{', '.join(missing_tools)}")
    if not account.get("wecom_allowed"):
        reasons.append("目标账号未在企业微信白名单。")
    if not account.get("runtime_allowed"):
        reasons.append("目标账号未在模型主链路上下文白名单。")
    status = "ready"
    if reasons:
        status = "blocked"
    scenario_id = str(scenario.get("id") or "")
    data_reason = _data_reason(scenario_id, sources, term)
    if data_reason:
        if data_reason["status"] == "deferred" and status != "blocked":
            status = "deferred"
        elif data_reason["status"] == "blocked":
            status = "blocked"
        reasons.append(str(data_reason["reason"]))
    if not reasons:
        reasons.append("账号、工具和基础数据均满足灰度验收。")
    return {
        "id": scenario_id,
        "title": scenario.get("title"),
        "owner_role": scenario.get("owner_role"),
        "status": status,
        "reason": " ".join(reasons),
        "required_tools": required,
        "description": scenario.get("description"),
    }


def _data_reason(scenario_id: str, sources: dict[str, Any], term: dict[str, Any]) -> dict[str, str] | None:
    weekly = sources.get("weekly_records") or {}
    parent = sources.get("parent_communication") or {}
    goals = sources.get("active_goals") or {}
    relations = sources.get("service_relations") or {}
    if scenario_id == "teacher_record" and int(weekly.get("total_students") or 0) <= 0:
        return {"status": "blocked", "reason": "没有可用于老师记录验收的在读学生数据。"}
    if scenario_id == "parent_communication_coverage" and int(parent.get("total_students") or 0) <= 0:
        return {"status": "blocked", "reason": "没有可用于家校沟通覆盖验收的在读学生数据。"}
    if scenario_id == "boss_goal" and int(goals.get("goal_count") or 0) <= 0:
        return {"status": "ready", "reason": "当前没有活跃目标也可验收老板新设目标，但应提醒 Hermes 先查事实再拆解。"}
    if scenario_id == "student_service_relation" and term.get("service_relation_policy") == "defer_until_new_term":
        return {
            "status": "deferred",
            "reason": f"当前为{term.get('label')}，学生服务关系按计划暂缓到新学期名单稳定后确认。",
        }
    if scenario_id == "student_service_relation" and int(relations.get("relation_count") or 0) <= 0:
        return {"status": "blocked", "reason": "正式学期下没有服务关系数据，需先由老板/店长确认样本。"}
    return None


def _recommended_scope(ready: list[dict[str, Any]], blocking: list[dict[str, Any]]) -> str:
    ready_ids = {str(item.get("id") or "") for item in ready}
    if blocking:
        return "先限老板账号和李老师账号做人工观察验收，暂不扩大到更多老师。"
    if {"boss_goal", "teacher_record", "manager_gap_query"} <= ready_ids:
        return "可进入老板、店长、李老师小范围真实渠道灰度，仍需人工观察。"
    return "先做老板账号只读/低风险写入验收，暂不扩大。"


def _user_allowed(config: Any, user_id: str) -> bool:
    if not isinstance(config, dict) or not user_id:
        return False
    for key in ("approved_user_ids", "allowed_user_ids", "allowed_users", "super_users", "model_context_allowed_user_ids"):
        values = config.get(key)
        if isinstance(values, list) and user_id in {str(item) for item in values}:
            return True
    roles = config.get("roles")
    if isinstance(roles, dict) and user_id in roles:
        return True
    user_roles = config.get("user_roles")
    if isinstance(user_roles, dict) and user_id in user_roles:
        return True
    users = config.get("users")
    if isinstance(users, dict) and user_id in users:
        return True
    if user_id in config and isinstance(config.get(user_id), (str, dict, list, bool)):
        return True
    return False


def _role_for(config: Any, user_id: str) -> str:
    if not isinstance(config, dict) or not user_id:
        return ""
    roles = config.get("roles")
    if isinstance(roles, dict) and user_id in roles:
        return str(roles.get(user_id) or "")
    user_roles = config.get("user_roles")
    if isinstance(user_roles, dict) and user_id in user_roles:
        role = str(user_roles.get(user_id) or "")
        return "boss" if role in {"super_admin", "owner"} else role
    super_users = config.get("super_users")
    if isinstance(super_users, list) and user_id in {str(item) for item in super_users}:
        return "boss"
    users = config.get("users")
    if isinstance(users, dict) and isinstance(users.get(user_id), dict):
        return str(users[user_id].get("role") or "")
    return ""


def _runtime_capability_cards_ready(config: Any) -> bool:
    if not isinstance(config, dict):
        return False
    if isinstance(config.get("allowed_capability_cards"), list):
        return bool(config.get("allowed_capability_cards"))
    runtime = config.get("runtime_foundation")
    if isinstance(runtime, dict) and runtime.get("enabled") is True:
        return True
    return _user_allowed(config, "boss1") or _user_allowed(config, "JinWenJie")


def _yes_no(value: Any) -> str:
    return "是" if bool(value) else "否"


def _read_data_json(store: TuoguanStore, relative_name: str, fallback: Any) -> Any:
    path = (store.data_dir / relative_name).resolve()
    root = store.data_dir.resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return fallback
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return fallback


def _write_report(store: TuoguanStore, timestamp: datetime, rendered: str, summary: dict[str, Any]) -> Path:
    report_dir = store.data_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = f"acceptance-v1-dry-run-{timestamp.strftime('%Y%m%d-%H%M%S')}"
    md_path = report_dir / f"{stem}.md"
    json_path = report_dir / f"{stem}.json"
    md_path.write_text(rendered, encoding="utf-8")
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return md_path
