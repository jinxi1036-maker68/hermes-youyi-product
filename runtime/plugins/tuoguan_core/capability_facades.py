"""Twelve explicit domain facades over the legacy tool implementation set."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Callable


CAPABILITY_MANIFEST_VERSION = "xiaoyou-capabilities-v1.1-23"


DOMAIN_OPERATIONS: dict[str, tuple[str, ...]] = {
    "people": (
        "context", "query_staff_directory", "offboard_staff", "resolve_student_responsibility",
        "query_profile_candidates", "submit_profile_candidate", "submit_profile_candidate_correction",
    ),
    "students": (
        "query_students", "register_student", "register_summer_student", "create_trial_lead",
        "query_student_service_relations", "submit_service_relation_fact_candidate",
    ),
    "records": (
        "record_summer_lesson", "record_student", "change_summer_points", "query_summer_points",
        "query_summer_points_ranking", "report_safety_event", "parent_script_context",
        "query_parent_communication_coverage", "query_weekly_record_coverage",
        "submit_performance_evidence_candidate", "query_performance_evidence_candidates",
        "submit_performance_evidence_response",
    ),
    "tasks": (
        "query_tasks", "next_task", "current_task_guidance", "create_task", "cancel_task", "update_task",
    ),
    "goals": (
        "goal_workspace", "query_active_goal_work_state", "submit_goal_evidence",
        "query_goal_actions", "submit_goal_action",
    ),
    "proactive_work": (
        "submit_relationship_touch_candidate", "query_active_work_context",
        "query_proactive_authorizations", "execute_relationship_touch", "update_relationship_touch",
        "query_relationship_touch_candidates",
        "query_hermes_work_items", "submit_hermes_work_item", "update_hermes_work_item",
        "query_wakeup_requests", "submit_wakeup_request", "update_wakeup_request",
        "submit_due_wakeup_candidate", "query_business_events", "submit_business_event",
        "query_action_executions", "submit_action_execution", "query_autonomous_work_brief",
        "query_proactive_work_radar", "query_attention_threads", "update_attention_thread",
        "submit_proactive_authorization",
    ),
    "institution": (
        "query_institution_onboarding_gaps", "query_operational_facts", "submit_operational_fact",
        "confirm_operational_fact", "submit_information_request_record", "query_information_requests",
        "submit_information_request_update", "query_employee_work_map", "query_fact_gap_candidates",
        "submit_fact_gap_candidate",
    ),
    "workstyle": (
        "query_person_workstyle_profile", "submit_person_workstyle_preference",
        "query_workstyle_adaptation_health",
    ),
    "staff_voice": (
        "submit_staff_voice_signal", "query_staff_voice_radar", "query_staff_conversation_activity",
    ),
    "learning": (
        "query_self_evolution_ledger", "query_industry_learning_candidates",
        "query_external_research_runs", "query_market_research_candidates", "query_competitor_profiles",
        "query_external_learning_brief", "query_social_market_research",
        "submit_industry_learning_candidate", "submit_learning_candidate", "list_learning_candidates",
        "review_learning_candidate",
    ),
    "reports": (
        "query_operations_report", "verify_dashboard_visibility", "dashboard_link",
        "query_value_ledger", "submit_value_ledger_entry",
    ),
    "health": (
        "query_xiaoyou_health", "generate_autonomous_recovery_report", "generate_due_wakeup_candidates",
        "query_gray_observations", "submit_gray_observation", "query_gray_rollout_decisions",
        "generate_gray_review", "query_gray_scenario_cards", "generate_gray_trial_start_pack",
        "generate_autonomous_acceptance_pack", "generate_autonomous_log_review",
        "generate_gray_observation_candidates", "submit_gray_rollout_decision",
        "query_gray_optimization_decisions", "submit_gray_optimization_decision",
    ),
}

DOMAIN_ROLES = {
    "people": ("boss", "manager", "teacher"),
    "students": ("boss", "manager", "teacher"),
    "records": ("boss", "manager", "teacher"),
    "tasks": ("boss", "manager", "teacher"),
    "goals": ("boss", "manager", "teacher"),
    "proactive_work": ("boss", "manager", "teacher"),
    "institution": ("boss", "manager", "teacher"),
    "workstyle": ("boss", "manager", "teacher"),
    "staff_voice": ("boss", "manager", "teacher"),
    "learning": ("boss", "manager"),
    "reports": ("boss", "manager", "teacher"),
    "health": ("boss", "manager"),
}

OPERATION_ROLES = {
    "offboard_staff": ("boss",),
}


# These are the small, high-frequency abilities that a real employee needs to
# find immediately.  The domain facades remain available for the long tail,
# while these explicit tools avoid making the model rediscover an operation
# name for routine work on every turn.
FAST_PATH_TOOL_NAMES = (
    "tuoguan_submit_relationship_touch_candidate",
    "tuoguan_query_students",
    "tuoguan_query_tasks",
    "tuoguan_next_task",
    "tuoguan_current_task_guidance",
    "tuoguan_create_task",
    "tuoguan_cancel_task",
    "tuoguan_update_task",
    "tuoguan_dashboard_link",
    "tuoguan_query_staff_directory",
    "tuoguan_query_active_work_context",
)

_WRITE_PREFIXES = (
    "register_", "create_", "record_", "change_", "report_", "submit_", "update_",
    "cancel_", "confirm_", "execute_", "offboard_", "review_learning_",
)


def operation_manifest() -> dict[str, Any]:
    operations: dict[str, dict[str, Any]] = {}
    for domain, names in DOMAIN_OPERATIONS.items():
        for operation in names:
            write = operation.startswith(_WRITE_PREFIXES)
            operations[operation] = {
                "domain": domain,
                "roles": list(OPERATION_ROLES.get(operation, DOMAIN_ROLES[domain])),
                "risk": "write_guarded" if write else "read_only",
                "access": "write" if write else "read",
                "required_evidence": "execution_receipt" if write else "trusted_tool_result",
                "allowed_commitment": "verified_writeback_only" if write else "result_facts_only",
            }
    return {
        "manifest_version": CAPABILITY_MANIFEST_VERSION,
        "frozen": True,
        "surface": "11_fast_paths_plus_12_domain_facades",
        "model_visible_tool_count": len(FAST_PATH_TOOL_NAMES) + len(DOMAIN_OPERATIONS),
        "fast_paths": list(FAST_PATH_TOOL_NAMES),
        "domains": len(DOMAIN_OPERATIONS),
        "operations": operations,
    }


def _argument_contract(schema: dict[str, Any]) -> tuple[list[str], list[str]]:
    parameters = schema.get("parameters") if isinstance(schema, dict) else {}
    properties = parameters.get("properties") if isinstance(parameters, dict) else {}
    names = [
        str(name) for name in properties
        if str(name) not in {"platform", "user_id", "user_name", "chat_id", "session_key"}
    ]
    required = [
        str(name) for name in (parameters.get("required") or [])
        if str(name) not in {"platform", "user_id", "user_name", "chat_id", "session_key", "operation_id"}
    ]
    optional = [name for name in names if name not in required and name != "operation_id"]
    if "operation_id" in names:
        optional.append("operation_id(auto)")
    return required, optional


def _domain_schema(domain: str, legacy: dict[str, tuple[dict[str, Any], Callable[..., Any]]]) -> dict[str, Any]:
    contracts: list[str] = []
    for operation in DOMAIN_OPERATIONS[domain]:
        schema, _handler = legacy[operation]
        required, optional = _argument_contract(schema)
        text = operation
        if required:
            text += " required=" + ",".join(required)
        if optional:
            text += " optional=" + ",".join(optional)
        contracts.append(text)
    selection_hint = {
        "people": (
            "选择提示：查询人员、姓名、企业微信状态用 query_staff_directory；"
            "老板明确要求将一位已离职员工删除、移除或停用时用 offboard_staff。"
            "offboard_staff 是保留历史的离职停用，不会伪装成已删除企业微信通讯录成员。"
        ),
        "tasks": (
            "选择提示：老师汇报进展、结果或完成证据用 update_task；不会做或不知道怎么说用 current_task_guidance；"
            "开始当前最高优先级任务或继续唯一开放任务用 next_task；取消或停止提醒用 cancel_task；"
            "老板或店长明确分配一次性任务、低风险测试任务时直接用 create_task，不要绕到 goal_workspace。"
        ),
        "proactive_work": (
            "选择提示：用户明确要求现在主动找授权对象时，第一选择必须是 submit_relationship_touch_candidate，"
            "并显式设置 execute_if_authorized=true；查询目标、人员、工作事项或看板都不等于主动执行。"
            "只有确实需要消除对象歧义时才先查 query_active_work_context；不能把查询或候选说成已经发送。"
        ),
        "reports": (
            "选择提示：老板要早报、晚报或今日重点用 query_operations_report；要看板链接用 dashboard_link。"
        ),
        "learning": (
            "选择提示：公开行业学习先查询有来源的候选或研究记录；检查物流快递、资金托管等同词污染时优先用 "
            "query_industry_learning_candidates，并明确说明无关内容已隔离、不生成教培趋势。"
        ),
    }.get(domain, "")
    return {
        "description": (
            f"小优{domain}领域能力。模型必须显式选择 operation；系统不根据聊天文本替模型路由。"
            "arguments 只填该 operation 的业务参数，身份始终来自可信企业微信会话。"
            + selection_hint
            + "操作契约：" + "；".join(contracts)
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": list(DOMAIN_OPERATIONS[domain]),
                    "description": "明确选择本次业务操作。",
                },
                "arguments": {
                    "type": "object",
                    "description": "该 operation 的具名业务参数；未知参数会被结构化拒绝。",
                    "additionalProperties": True,
                },
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    }


def build_facade_tools(
    legacy_tools: tuple[tuple[str, dict[str, Any], Callable[..., Any]], ...],
    *,
    tool_result: Callable[[Any], str],
) -> tuple[tuple[str, dict[str, Any], Callable[..., Any]], ...]:
    legacy = {
        name.removeprefix("tuoguan_"): (schema, handler)
        for name, schema, handler in legacy_tools
    }
    expected = {operation for values in DOMAIN_OPERATIONS.values() for operation in values}
    missing = sorted(set(legacy) - expected)
    unknown = sorted(expected - set(legacy))
    duplicates = len(expected) != sum(len(values) for values in DOMAIN_OPERATIONS.values())
    if missing or unknown or duplicates:
        raise RuntimeError(
            f"capability_manifest_mismatch missing={missing} unknown={unknown} duplicates={duplicates}"
        )

    def handler_for(domain: str) -> Callable[..., Any]:
        def run(args: dict[str, Any], **kwargs: Any) -> str:
            payload = dict(args or {})
            nested = payload.get("arguments")
            nested_arguments = dict(nested) if isinstance(nested, dict) else {}
            # Accept one harmless layer of structural nesting produced by some
            # OpenAI-compatible model adapters.  This only normalizes a tool
            # call the model already selected; it never infers an operation
            # from the user's text.
            inner = nested_arguments.pop("arguments", None)
            if isinstance(inner, dict):
                nested_arguments = {**inner, **nested_arguments}
            operation = str(
                payload.get("operation")
                or nested_arguments.pop("operation", "")
                or ""
            ).strip()
            if operation not in DOMAIN_OPERATIONS[domain]:
                contracts = {}
                for allowed in DOMAIN_OPERATIONS[domain]:
                    allowed_schema, _handler = legacy[allowed]
                    required, optional = _argument_contract(allowed_schema)
                    contracts[allowed] = {"required": required, "optional": optional}
                return tool_result({
                    "ok": False,
                    "error": "unknown_facade_operation",
                    "message": "该领域没有这个 operation，本轮没有执行；请从 allowed_operations 选择准确名称。",
                    "data": {
                        "domain": domain,
                        "allowed_operations": list(DOMAIN_OPERATIONS[domain]),
                        "operation_contracts": contracts,
                    },
                })
            if nested is not None and not isinstance(nested, dict):
                return tool_result({
                    "ok": False, "error": "invalid_facade_arguments",
                    "message": "arguments 必须是对象，本轮没有执行。",
                })
            flat_arguments = {
                key: value for key, value in payload.items()
                if key not in {
                    "operation", "arguments", "platform", "user_id", "user_name",
                    "chat_id", "session_key",
                }
            }
            conflicts = sorted(
                key for key in flat_arguments
                if key in nested_arguments and flat_arguments[key] != nested_arguments[key]
            )
            if conflicts:
                return tool_result({
                    "ok": False,
                    "error": "ambiguous_facade_arguments",
                    "message": "同一个参数同时出现在外层和 arguments 且值不同，本轮没有执行。",
                    "data": {"conflicting_arguments": conflicts},
                })
            arguments = {**flat_arguments, **nested_arguments}
            identity_args = {
                key: payload[key] for key in ("platform", "user_id", "user_name", "chat_id", "session_key")
                if key in payload
            }
            _schema, legacy_handler = legacy[operation]
            raw_result = legacy_handler({**identity_args, **deepcopy(arguments)}, **kwargs)
            try:
                parsed = json.loads(raw_result) if isinstance(raw_result, str) else deepcopy(raw_result)
            except (TypeError, ValueError):
                parsed = {"ok": False, "error": "unparseable_legacy_tool_result"}
            if not isinstance(parsed, dict):
                parsed = {"ok": False, "error": "invalid_legacy_tool_result"}
            parsed["facade_domain"] = domain
            parsed["facade_operation"] = operation
            parsed["legacy_tool"] = f"tuoguan_{operation}"
            data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
            parsed["data"] = {
                **data,
                "facade_domain": domain,
                "facade_operation": operation,
                "legacy_tool": f"tuoguan_{operation}",
            }
            return tool_result(parsed)

        return run

    return tuple(
        (f"tuoguan_{domain}", _domain_schema(domain, legacy), handler_for(domain))
        for domain in DOMAIN_OPERATIONS
    )


def facade_schema_size(facade_tools: tuple[tuple[str, dict[str, Any], Callable[..., Any]], ...]) -> dict[str, int]:
    serialized = json.dumps(
        [{"name": name, "schema": schema} for name, schema, _handler in facade_tools],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {"characters": len(serialized), "estimated_tokens": (len(serialized) + 3) // 4}


def render_facade_instruction() -> str:
    domains = "、".join(f"tuoguan_{name}" for name in DOMAIN_OPERATIONS)
    fast_paths = "、".join(FAST_PATH_TOOL_NAMES)
    return (
        f"【小优能力面｜{CAPABILITY_MANIFEST_VERSION}｜封版】高频工作优先使用直连工具：" + fast_paths + "。"
        "查正式托管学生使用 tuoguan_query_students(query_scope=regular)；查暑假班才使用 query_scope=summer；"
        "用户要看板链接时直接使用 tuoguan_dashboard_link，不需要先查目标、任务或学生完整度。"
        "其余能力使用12个领域入口：" + domains + "。"
        "员工手册或历史材料中的 tuoguan_query_tasks、tuoguan_cancel_task 等旧名称，"
        "如果已作为高频直连工具出现就直接调用；否则表示领域入口里的 operation。"
        "例如低频目标查询使用 tuoguan_goals(operation=goal_workspace, arguments={action:query_progress})。"
        "不要编造 list、query_staff、query_goal_workspace 等不存在的 operation。"
        "必须由模型显式选择领域和 operation；系统不根据自然语言偷偷决定业务动作。"
    )
