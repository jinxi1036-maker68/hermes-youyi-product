"""Shared model-first observability and safety layer for Youyi gray traffic."""

from __future__ import annotations

import json
import re
import threading
import uuid
from copy import deepcopy
from datetime import datetime
from typing import Any

from .store import TuoguanStore
from .write_guard import changed_protected_hashes, protected_hashes
from .learning_loop import record_learning_from_ledger
from .runtime_ownership import allowed_claims_from_result, guard_business_claims


TENANT_ID = "youyi_tuoguan"
_LOCK = threading.RLock()
_PENDING_BY_USER: dict[str, dict[str, Any]] = {}
_TURN_BY_SESSION: dict[str, dict[str, Any]] = {}

# These cards are deterministic sub-steps of the confirmed task-completion
# workflow. They do not expand business capability scope or the runtime
# allowlist; they only preserve and explain the currently selected task.
INTERNAL_TASK_WORKFLOW_CARDS = {
    "老师任务反馈与完成",
    "老师继续下一个任务",
    "老师当前任务引导",
}


WRITE_TOOLS = {
    "tuoguan_record_student",
    "tuoguan_record_summer_lesson",
    "tuoguan_create_task",
    "tuoguan_update_task",
    "tuoguan_report_safety_event",
    "tuoguan_register_student",
    "tuoguan_register_summer_student",
    "tuoguan_create_trial_lead",
    "tuoguan_change_summer_points",
    "tuoguan_confirm_goal",
    "tuoguan_update_goal_progress",
    "tuoguan_submit_operational_fact",
    "tuoguan_confirm_operational_fact",
    "tuoguan_submit_person_workstyle_preference",
    "tuoguan_submit_service_relation_fact_candidate",
    "tuoguan_submit_profile_candidate",
    "tuoguan_submit_profile_candidate_correction",
    "tuoguan_submit_information_request_record",
    "tuoguan_submit_information_request_update",
    "tuoguan_submit_goal_evidence",
    "tuoguan_submit_performance_evidence_candidate",
    "tuoguan_submit_performance_evidence_response",
    "tuoguan_submit_value_ledger_entry",
    "tuoguan_submit_gray_observation",
    "tuoguan_submit_gray_rollout_decision",
    "tuoguan_submit_gray_optimization_decision",
    "tuoguan_submit_hermes_work_item",
    "tuoguan_update_hermes_work_item",
    "tuoguan_submit_wakeup_request",
    "tuoguan_update_wakeup_request",
    "tuoguan_submit_due_wakeup_candidate",
    "tuoguan_submit_business_event",
    "tuoguan_submit_action_execution",
    "tuoguan_submit_institution_fact_gap",
    "tuoguan_update_institution_understanding",
    "tuoguan_submit_employee_self_review",
    "tuoguan_submit_industry_learning_candidate",
    "tuoguan_submit_value_progress_entry",
    "tuoguan_submit_agent_delegation",
    "tuoguan_submit_agent_delegation_result",
    "tuoguan_update_agent_delegation_decision",
    "tuoguan_update_attention_thread",
}

MODEL_SELECTED_READ_TOOLS = {
    "tuoguan_context",
    "tuoguan_query_tasks",
    "tuoguan_query_students",
    "tuoguan_query_operations_report",
    "tuoguan_dashboard_link",
    "tuoguan_goal_workspace",
    "tuoguan_review_goal",
    "tuoguan_query_goal_progress",
    "tuoguan_resolve_student_responsibility",
    "tuoguan_query_institution_onboarding_gaps",
    "tuoguan_query_operational_facts",
    "tuoguan_query_staff_directory",
    "tuoguan_query_person_workstyle_profile",
    "tuoguan_query_student_service_relations",
    "tuoguan_query_parent_communication_coverage",
    "tuoguan_query_weekly_record_coverage",
    "tuoguan_query_active_goal_work_state",
    "tuoguan_query_profile_candidates",
    "tuoguan_query_value_ledger",
    "tuoguan_query_information_requests",
    "tuoguan_query_performance_evidence_candidates",
    "tuoguan_query_gray_observations",
    "tuoguan_query_gray_rollout_decisions",
    "tuoguan_generate_gray_review",
    "tuoguan_query_gray_scenario_cards",
    "tuoguan_generate_gray_trial_start_pack",
    "tuoguan_generate_gray_observation_candidates",
    "tuoguan_query_gray_optimization_decisions",
    "tuoguan_query_hermes_work_items",
    "tuoguan_query_wakeup_requests",
    "tuoguan_query_business_events",
    "tuoguan_query_action_executions",
    "tuoguan_query_autonomous_work_brief",
    "tuoguan_query_self_evolution_ledger",
    "tuoguan_query_employee_work_map",
    "tuoguan_query_fact_gap_candidates",
    "tuoguan_query_institution_understanding",
    "tuoguan_query_hermes_employee_scorecard",
    "tuoguan_query_industry_learning_candidates",
    "tuoguan_query_external_research_runs",
    "tuoguan_query_market_research_candidates",
    "tuoguan_query_competitor_profiles",
    "tuoguan_query_external_learning_brief",
    "tuoguan_query_value_progress_ledger",
    "tuoguan_query_agent_delegations",
    "tuoguan_query_agent_delegation_results",
    "tuoguan_query_multi_agent_brief",
    "tuoguan_query_attention_threads",
    "tuoguan_generate_autonomous_recovery_report",
    "tuoguan_generate_due_wakeup_candidates",
    "tuoguan_generate_autonomous_acceptance_pack",
    "tuoguan_generate_autonomous_log_review",
}

ALLOWED_TOOLS_BY_INTENT: dict[str, set[str]] = {
    "query_my_tasks": {"tuoguan_query_tasks"},
    "continue_next_task": {"tuoguan_next_task"},
    "task_guidance": {"tuoguan_current_task_guidance"},
    "query_all_tasks": {"tuoguan_query_tasks"},
    "query_operations_report": {"tuoguan_query_operations_report"},
    "query_dashboard_link": {"tuoguan_dashboard_link"},
    "query_student_performance": {"tuoguan_query_students"},
    "query_summer_points": {"tuoguan_query_summer_points"},
    "query_summer_points_ranking": {"tuoguan_query_summer_points_ranking"},
    "change_summer_points": {"tuoguan_change_summer_points"},
    "record_summer_lesson": {"tuoguan_record_summer_lesson"},
    "record_student_daily_behavior": {"tuoguan_record_student", "tuoguan_update_task"},
    "update_task": {"tuoguan_update_task"},
    "create_task": {"tuoguan_create_task"},
    "report_safety_event": {"tuoguan_report_safety_event"},
}


def clear_runtime_state() -> None:
    """Clear in-memory runtime observation state for tests and controlled resets."""
    with _LOCK:
        _PENDING_BY_USER.clear()
        _TURN_BY_SESSION.clear()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _compact(text: str) -> str:
    return "".join(str(text or "").split()).rstrip("。！？!?；;")


def _looks_like_unverified_success(text: str) -> bool:
    compact = _compact(text)
    success_words = (
        "\u5df2\u5f55\u5165", "\u5df2\u767b\u8bb0", "\u5df2\u65b0\u589e", "\u5df2\u6dfb\u52a0", "\u5df2\u5408\u5e76", "\u5df2\u6539\u597d",
        "\u5df2\u5904\u7406", "\u5df2\u5b8c\u6210", "\u6210\u529f\u8bb0\u5f55", "\u5df2\u8bb0\u5f55", "\u641e\u5b9a\u4e86", "\u5df2\u7ecf\u5199\u5165",
        "\u5df2\u52a0", "\u5df2\u6263", "\u5f53\u524d\u79ef\u5206", "\u5df2\u521b\u5efa", "\u5df2\u751f\u6210",
    )
    return any(word in compact for word in success_words)


_INTERNAL_ERROR_MARKERS = (
    "api call failed", "rpm exhausted", "provider error", "custom stream drop",
    "traceback", "retrying api", "internal server error", "debug:",
    "writeback_verified", "record_id=", "task_id=", "event_id=", "ok=true",
    "operation interrupted", "waiting for model response",
)

_INTERNAL_MECHANISM_MARKERS = (
    "运行时分类器", "工具调用被", "工具被拦截", "系统路由层",
    "能力卡", "白名单", "灰度流量", "运行时契约",
    "tuoguan_", "分类器", "工具权限", "工具授权",
)


def _sanitize_external_reply(text: str, *, verified_state_change: bool = False, used_trusted_tool: bool = False) -> str:
    value = str(text or "")
    lowered = value.lower()
    if any(marker in lowered for marker in _INTERNAL_ERROR_MARKERS):
        return "我刚才连接中断，这次没有处理完整。请稍等一下再发一次，我会重新接着处理。"
    if any(marker in value for marker in _INTERNAL_MECHANISM_MARKERS):
        return "我刚才不该讲内部处理细节。你正常说要查谁、记录谁、处理哪件事就行，我会按你的身份权限去理解和处理。"
    state_commit_terms = (
        "已经确认并保存",
        "确认并保存",
        "已保存",
        "已经保存",
        "状态已保存",
        "已记下",
        "记下了",
        "记住了",
        "已经记住",
        "已记录",
        "已经记录",
        "以后就按这个来理解",
        "以后按这个来理解",
        "别忘了",
        "下次醒来",
        "明天上午我",
        "我醒来自己",
        "我会自己查",
    )
    if not verified_state_change and any(term in value for term in state_commit_terms):
        value = value.replace("已保存", "我已理解")
        value = value.replace("已经保存", "我已理解")
        value = value.replace("已经确认并保存了", "我先按这条原话理解")
        value = value.replace("已经确认并保存", "我先按这条原话理解")
        value = value.replace("确认并保存了", "我先按这条原话理解")
        value = value.replace("确认并保存", "我先按这条原话理解")
        value = value.replace("状态已保存", "状态我已理解")
        value = value.replace("已记下了", "我已理解")
        value = value.replace("已记下", "我已理解")
        value = value.replace("记下了", "我已理解")
        value = value.replace("记住了", "先按你这句话理解")
        value = value.replace("已经记住", "先按你这句话理解")
        value = value.replace("已记录", "我已理解")
        value = value.replace("已经记录", "我已理解")
        value = value.replace("以后就按这个来理解", "后续需要结合已确认事实继续核验")
        value = value.replace("以后按这个来理解", "后续需要结合已确认事实继续核验")
        value = value.replace("我醒来自己查", "我会优先核验")
        value = value.replace("我会自己查", "我会优先核验")
        value = value.replace("您放心。", "")
        if "下次醒来" in value or "明天上午" in value or "我会自己查" in value:
            value += "\n\n我会把这条作为当前目标的最新要求来处理；真正涉及工作状态、提醒或后续动作时，以后续醒来核验真实事实后的推进为准。"
    if not used_trusted_tool:
        read_claim_terms = (
            "小优查了",
            "我查了",
            "查了一下",
            "我看了一下",
            "系统里显示",
            "系统显示",
            "查到",
        )
        if any(term in value for term in read_claim_terms):
            value = value.replace("小优查了", "小优先按当前上下文看")
            value = value.replace("我查了", "我先按当前上下文看")
            value = value.replace("查了一下", "按当前上下文看")
            value = value.replace("我看了一下", "我先按当前上下文看")
            value = value.replace("系统里显示", "当前上下文里提到")
            value = value.replace("系统显示", "当前上下文里提到")
            value = value.replace("查到", "看到")
    return value


def _looks_like_unverified_business_fact(raw_text: str, reply_text: str) -> bool:
    raw = _compact(raw_text)
    reply = str(reply_text or "")
    if not raw or not reply:
        return False
    if re.search(r"1[3-9]\d{9}", reply):
        return True
    meta_dialogue = any(
        term in raw
        for term in (
            "你是谁", "在线", "只会说", "听不懂", "听懂", "模型", "系统",
            "权限", "权利", "限制", "能做什么", "能干什么", "为什么",
            "能不能", "可不可以", "可以查", "不能查", "不是说", "刚才说",
        )
    )
    sensitive_reply_terms = (
        "家长电话", "全机构学生总数", "暑假班在册", "开放任务", "高风险任务",
        "老师人数", "今日记录", "过去7天覆盖率", "系统里目前登记",
    )
    if not meta_dialogue and any(term in reply for term in sensitive_reply_terms):
        return True
    if meta_dialogue and not any(term in reply for term in sensitive_reply_terms):
        return False
    raw_asks_business = any(term in raw for term in ("查", "看", "了解", "多少", "几个", "名单", "怎么样", "今天", "最近", "任务", "老师", "学生", "孩子", "积分", "经营", "看板", "记录", "记忆", "知道"))
    if not raw_asks_business:
        return False
    if meta_dialogue and not any(term in raw for term in ("查", "看", "了解", "多少", "几个", "名单", "今天", "最近")):
        return False
    if any(term in reply for term in ("请问", "你想查", "你要查", "具体是哪", "哪位老师", "哪个学生", "再说具体")) and not any(term in reply for term in ("当前", "目前", "系统里", "在册", "登记", "共", "今天")):
        return False
    return any(term in reply for term in ("当前", "目前", "系统里", "在册", "登记", "共", "今天", "最近", "名单", "任务", "学生", "老师", "积分", "经营")) and (
        bool(re.search(r"\d", reply)) or "\n-" in reply or "|" in reply or "：" in reply
    )


def _record_learning_safely(store: TuoguanStore, item: dict[str, Any]) -> None:
    try:
        record_learning_from_ledger(store, item)
    except Exception:
        # Learning must never block model replies, clarification, or write guards.
        return


def _non_business_dialogue_plan(
    *, actor_user_id: str, actor_role: str, message_id: str, raw_text: str, trace_id: str = ""
) -> dict[str, Any]:
    plan_id = f"plan_{uuid.uuid5(uuid.NAMESPACE_URL, f'{TENANT_ID}:{message_id}:non_business_dialogue').hex[:24]}"
    return {
        "plan_id": plan_id,
        "tenant_id": TENANT_ID,
        "actor_user_id": str(actor_user_id or ""),
        "actor_role": str(actor_role or "unbound"),
        "source_message_id": str(message_id or ""),
        "operation_id": str(message_id or plan_id),
        "trace_id": str(trace_id or f"trace_{plan_id.removeprefix('plan_')}"),
        "intent": "non_business_dialogue",
        "business_object_type": "",
        "business_object_candidates": [],
        "confirmed_user_facts": {"user_statement": str(raw_text or "")},
        "confidence": 1.0 if raw_text else 0.0,
        "clarification_needed": False,
        "clarification_question": "",
        "proposed_tool_calls": [],
        "response_goal": "model_reply_without_business_tool",
        "retrieved_lesson_ids": [],
    }


_GENERIC_TASK_COMPLETION = {
    "这个任务已经完成",
    "这个任务已完成",
    "这个任务完成了",
    "刚才那个任务完成了",
    "这个任务已经处理了",
    "已经处理了",
    "完成了",
    "处理完了",
}


def _focused_task_for_user(store: TuoguanStore, user_id: str) -> dict[str, Any] | None:
    focuses = store.read_json("model_focus.json", {})
    if not isinstance(focuses, dict):
        return None
    tasks = {
        str(task.get("id") or ""): task
        for task in store.load_tasks()
        if isinstance(task, dict)
    }
    now = datetime.now().astimezone()
    suffix = f":{str(user_id or '').strip()}"
    candidates: list[tuple[str, dict[str, Any]]] = []
    for key, focus in focuses.items():
        if not isinstance(focus, dict) or not str(key).endswith(suffix):
            continue
        if str(focus.get("focus_source") or "") not in {"task_created", "explicit_task_interaction"}:
            continue
        task_id = str(focus.get("task_id") or "")
        expires_at = str(focus.get("focus_expires_at") or "")
        try:
            unexpired = bool(expires_at) and datetime.fromisoformat(expires_at) >= now
        except (TypeError, ValueError):
            unexpired = False
        if unexpired and task_id in tasks:
            candidates.append((str(focus.get("updated_at") or ""), tasks[task_id]))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _has_valid_task_focus(store: TuoguanStore, user_id: str) -> bool:
    return _focused_task_for_user(store, user_id) is not None


def should_clarify_without_tool(*, store: TuoguanStore, user_id: str, raw_text: str) -> bool:
    """Return True when a generic completion has no safe, scoped task focus."""

    return _compact(raw_text) in _GENERIC_TASK_COMPLETION and not _has_valid_task_focus(store, user_id)


def _append_jsonl(store: TuoguanStore, filename: str, payload: dict[str, Any]) -> None:
    path = store.data_dir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def foundation_enabled(store: TuoguanStore) -> bool:
    path = store.data_dir / "manual_context" / "hermes_model_context_injection_allowlist_v1.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    foundation = payload.get("runtime_foundation") if isinstance(payload, dict) else None
    return bool(isinstance(foundation, dict) and foundation.get("enabled") is True)


def _allowed_cards(store: TuoguanStore) -> set[str]:
    path = store.data_dir / "manual_context" / "hermes_model_context_injection_allowlist_v1.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return set()
    cards = payload.get("allowed_capability_cards") if isinstance(payload, dict) else []
    return {
        str(item.get("capability") or "")
        for item in cards or []
        if isinstance(item, dict)
        and item.get("status") == "confirmed"
        and int(item.get("pilot", 0) or 0) == 0
    }


def _allowed_card_entries(store: TuoguanStore, role: str) -> list[dict[str, Any]]:
    path = store.data_dir / "manual_context" / "hermes_model_context_injection_allowlist_v1.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    cards = payload.get("allowed_capability_cards") if isinstance(payload, dict) else []
    result: list[dict[str, Any]] = []
    for raw in cards or []:
        if not isinstance(raw, dict) or raw.get("status") != "confirmed" or int(raw.get("pilot", 0) or 0) != 0:
            continue
        roles = {str(value) for value in (raw.get("allowed_roles") or [])}
        if roles and str(role or "") not in roles:
            continue
        result.append({
            "capability": str(raw.get("capability") or ""),
            "intent": str(raw.get("intent") or ""),
            "allowed_tools": [str(value) for value in (raw.get("allowed_tools") or []) if str(value)],
            "constraints": [str(value) for value in (raw.get("constraints") or []) if str(value)],
            "report_type": str(raw.get("report_type") or ""),
        })
    return result


def _runtime_tool_cards(store: TuoguanStore, role: str) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for card in _allowed_card_entries(store, role):
        for tool in card["allowed_tools"]:
            result.setdefault(tool, []).append(card)
    return result


def begin_inbound(*, store: TuoguanStore, message_id: str, conversation_id: str, user_id: str, role: str, raw_text: str) -> dict[str, Any] | None:
    if not foundation_enabled(store):
        return None
    card, intent = "", "unclassified_message"
    runtime_tool_cards = _runtime_tool_cards(store, role)
    item = {
        "ledger_id": f"ledger_{uuid.uuid4().hex}",
        "message_id": str(message_id or ""),
        "source_message_id": str(message_id or ""),
        "conversation_id": str(conversation_id or user_id),
        "data_dir": str(store.data_dir.resolve()),
        "tenant_id": TENANT_ID,
        "channel": "wecom_callback",
        "user_id": user_id,
        "role": role,
        "raw_text": raw_text,
        "entered_model": False,
        "model_intent": intent,
        "model_confidence": None,
        "selected_capability_card": card,
        # Only confirmed runtime cards are injected. Partial cards remain observable.
        "used_manual_cards": [card] if card in _allowed_cards(store) else [],
        "used_tool_registry_entry": "",
        "requested_scope": "all" if intent == "query_all_tasks" else ("mine" if intent == "query_my_tasks" else None),
        "effective_scope": None,
        "tool_calls": [],
        "tool_results": [],
        "writeback_verified": None,
        "render_verified": False,
        "legacy_handler_intercepted": False,
        "route_decision": "model_first_pending",
        "guard_result": "pending",
        "audit_event_ids": [],
        "final_reply": "",
        "created_at": _now(),
        "protected_hashes_before": protected_hashes(store.data_dir),
        "runtime_tool_cards": runtime_tool_cards,
        "no_safe_task_focus": should_clarify_without_tool(store=store, user_id=user_id, raw_text=raw_text),
    }
    with _LOCK:
        _PENDING_BY_USER[user_id] = item
    return deepcopy(item)


def current_raw_text(user_id: str) -> str:
    with _LOCK:
        item = _PENDING_BY_USER.get(str(user_id or ""), {})
        return str(item.get("raw_text") or "")


def current_ledger_id(user_id: str) -> str:
    with _LOCK:
        item = _PENDING_BY_USER.get(str(user_id or ""), {})
        return str(item.get("ledger_id") or "")


def current_message_id(user_id: str) -> str:
    with _LOCK:
        item = _PENDING_BY_USER.get(str(user_id or ""), {})
        return str(item.get("message_id") or "")


def write_authorization_for(user_id: str, operation: str) -> dict[str, str] | None:
    """Authorize writes only after the current message has entered the model.

    The runtime must not pre-classify ordinary language into an intent before
    the Agent thinks. A trusted tool write is allowed when there is an active
    model-led turn for the actor; the tool service then enforces identity,
    role permissions, parameters, idempotency and writeback verification.
    """

    supported_write_operations = {
        "record_student",
        "record_summer_lesson",
        "register_student",
        "register_summer_student",
        "create_trial_lead",
        "create_task",
        "update_task",
        "report_safety_event",
        "change_summer_points",
        "confirm_goal",
        "update_goal_progress",
        "withdraw_goal",
        "submit_operational_fact",
        "confirm_operational_fact",
        "submit_person_workstyle_preference",
        "submit_service_relation_fact_candidate",
        "submit_profile_candidate",
        "submit_profile_candidate_correction",
        "submit_information_request_record",
        "submit_information_request_update",
        "submit_goal_evidence",
        "submit_performance_evidence_candidate",
        "submit_performance_evidence_response",
        "submit_value_ledger_entry",
        "submit_gray_observation",
        "submit_gray_rollout_decision",
        "submit_gray_optimization_decision",
        "submit_hermes_work_item",
        "update_hermes_work_item",
        "submit_wakeup_request",
        "update_wakeup_request",
        "submit_due_wakeup_candidate",
        "submit_business_event",
        "submit_action_execution",
        "submit_institution_fact_gap",
        "update_institution_understanding",
        "submit_employee_self_review",
        "submit_industry_learning_candidate",
        "submit_value_progress_entry",
        "submit_agent_delegation",
        "submit_agent_delegation_result",
        "update_agent_delegation_decision",
        "update_attention_thread",
    }
    if str(operation or "") not in supported_write_operations:
        return None
    with _LOCK:
        item = _PENDING_BY_USER.get(str(user_id or ""))
        if not item or not item.get("entered_model"):
            return None
        return {
            "ledger_id": str(item.get("ledger_id") or ""),
            "intent": str(item.get("model_intent") or "model_selected_tool"),
            "data_dir": str(item.get("data_dir") or ""),
        }


def link_audit_to_ledger(store: TuoguanStore, ledger_id: str, audit_id: str) -> None:
    if not ledger_id or not audit_id:
        return
    path = store.data_dir / "reply_ledger.jsonl"
    if not path.exists():
        return
    with _LOCK:
        lines = path.read_text(encoding="utf-8").splitlines()
        changed = False
        output: list[str] = []
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                output.append(line)
                continue
            if item.get("ledger_id") == ledger_id:
                ids = list(item.get("audit_event_ids") or [])
                if audit_id not in ids:
                    ids.append(audit_id)
                    item["audit_event_ids"] = ids
                    changed = True
                output.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
            else:
                output.append(line)
        if changed:
            path.write_text("\n".join(output) + "\n", encoding="utf-8")



def _autonomous_work_context(sender_id: str, actor_role: str = "") -> str:
    """Return compact state material for autonomous work, never a route."""
    if not sender_id:
        return ""
    try:
        from .digital_employee_state import generate_due_wakeup_candidates, query_autonomous_work_brief
        from .models import UserIdentity

        identity = UserIdentity(
            platform="runtime_context",
            platform_user_id=str(sender_id or ""),
            canonical_user_id=str(sender_id or ""),
            person_name="",
            role=str(actor_role or "teacher"),
            approval_state="approved",
        )
        store = TuoguanStore()
        brief = query_autonomous_work_brief(store, identity=identity, limit=5)
        due = generate_due_wakeup_candidates(store, identity=identity, limit=5)
    except Exception:
        return ""
    if not brief.get("ok"):
        return ""
    total = int(brief.get("work_item_count") or 0)
    waiting_count = int(brief.get("waiting_count") or 0)
    pending_wakeup_count = int(brief.get("pending_wakeup_count") or 0)
    unknown_count = int(brief.get("result_unknown_action_count") or 0)
    due_count = int(due.get("candidate_count") or 0) if isinstance(due, dict) and due.get("ok") else 0
    if not any((total, waiting_count, pending_wakeup_count, unknown_count, due_count)):
        return ""
    lines = [
        "【Hermes 自主工作状态材料】以下只提供当前工作状态、等待、唤醒、到期关注和结果未知材料，不规定下一步动作，也不是流程或工具指令。",
        f"当前可见自主事项 {total} 条；等待 {waiting_count} 条；待处理唤醒 {pending_wakeup_count} 条；到期关注候选 {due_count} 条；结果未知动作 {unknown_count} 条。",
    ]
    for item in (brief.get("waiting_items") or [])[:3]:
        title = str(item.get("title") or item.get("focus_key") or "未命名事项")
        waiting = item.get("current_waiting") if isinstance(item.get("current_waiting"), dict) else {}
        target = str(waiting.get("target_person") or waiting.get("target_user_id") or "未指定对象")
        reason = str(waiting.get("reason") or item.get("focus_summary") or "")
        next_time = str(item.get("next_attention_at") or "")
        lines.append(f"等待事项：{title}；等待对象：{target}；原因：{reason[:120]}；下一关注：{next_time or '未设置'}。")
    if due_count:
        for candidate in (due.get("candidates") or [])[:3]:
            title = str(candidate.get("title") or candidate.get("action_summary") or candidate.get("candidate_type") or "未命名候选")
            lines.append(f"到期关注候选：{title[:120]}；恢复前先核验是否已有新事实或可靠回执。")
    lines.append("恢复自主事项时需要重新核验最新事实；不能把等待、旧状态或结果未知动作说成已完成，也不能盲目重试旧动作。")
    return "".join(lines)



def inject_model_context(*, session_id: str, sender_id: str, user_message: str) -> dict[str, str] | None:
    with _LOCK:
        item = _PENDING_BY_USER.get(str(sender_id or ""))
        if not item or _compact(item.get("raw_text", "")) != _compact(user_message):
            return None
        item["entered_model"] = True
        item["route_decision"] = "model_first"
        item["guard_result"] = "allowed"
        item["session_id"] = str(session_id or "")
        _TURN_BY_SESSION[str(session_id or "")] = item
    task_query_rule = ""
    if item.get("model_intent") in {"query_my_tasks", "query_all_tasks"}:
        task_query_rule = (
            "本轮是任务查询，只能调用 tuoguan_query_tasks；"
            "query_my_tasks 必须 scope=mine；query_all_tasks 必须由工具校验老板权限；"
            "最终回复必须逐字使用工具 rendered_text，不得自行增删任务或重算数量。"
        )
    elif item.get("model_intent") == "continue_next_task":
        task_query_rule = (
            "本轮是继续下一个任务，只能调用 tuoguan_next_task；"
            "不要展示整张任务列表，不要让老师再次选择；最终逐字使用工具 rendered_text。"
        )
    elif item.get("model_intent") == "task_guidance":
        task_query_rule = (
            "本轮是当前任务状态或补充项查询，只能调用 tuoguan_current_task_guidance；"
            "不得写任务，不得把用户问题追加为证据，最终逐字使用工具 rendered_text。"
        )
    task_rule = ""
    if item.get("model_intent") == "update_task":
        task_rule = (
            "本轮已经识别为任务反馈或任务完成，只能调用 tuoguan_update_task；"
            "禁止调用 tuoguan_query_students，禁止新建任务。"
            "task_id 不确定时留空，由可信工具按点名学生和最近普通任务解析；"
            "reply 必须逐字使用用户本轮原话。"
        )
    daily_record_rule = ""
    if item.get("model_intent") == "record_student_daily_behavior":
        daily_record_rule = (
            "\u672c\u8f6e\u662f\u5b66\u751f\u65e5\u5e38\u8bb0\u5f55\u5199\u5165\uff0c\u53ea\u80fd\u8c03\u7528 tuoguan_record_student\uff1b"
            "\u5982\u679c\u8001\u5e08\u662f\u5728\u56de\u590d\u7cfb\u7edf\u521a\u521a\u8be2\u95ee\u7684\u4efb\u52a1\u5185\u5bb9\uff0c\u53ef\u5148\u8c03\u7528 tuoguan_record_student \u5199\u5b66\u751f\u8bb0\u5f55\uff0c\u518d\u8c03\u7528 tuoguan_update_task \u540c\u6b65\u5230\u8be5\u4efb\u52a1\uff1b"
            "\u5982\u679c\u6ca1\u6709\u660e\u786e task_id \u6216\u4efb\u52a1\u4e0a\u4e0b\u6587\uff0c\u4e0d\u5f97\u66f4\u65b0\u4efb\u52a1\u72b6\u6001\uff1b"
            "content \u5fc5\u987b\u57fa\u4e8e\u7528\u6237\u539f\u8bdd\uff0c\u4e0d\u5f97\u8865\u5145\u8001\u5e08\u672a\u8bf4\u7684\u4e8b\u5b9e\u3002"
        )
    student_performance_rule = ""
    if item.get("model_intent") == "query_student_performance":
        student_performance_rule = (
            "\u672c\u8f6e\u662f\u5b66\u751f\u8868\u73b0\u67e5\u8be2\uff0c\u53ea\u80fd\u8c03\u7528 tuoguan_query_students\uff1b"
            "\u7981\u6b62\u8c03\u7528 tuoguan_query_tasks\uff1b"
            "\u5b66\u751f\u8bb0\u5f55\u3001\u8fd1\u671f\u8868\u73b0\u548c\u7edf\u8ba1\u53ea\u80fd\u4f7f\u7528\u5de5\u5177 rendered_text\uff0c\u4e0d\u5f97\u81ea\u7531\u6269\u5199\u6216\u6539\u6210\u4efb\u52a1\u5217\u8868\u3002"
        )
    summer_lesson_rule = ""
    if item.get("model_intent") == "record_summer_lesson":
        summer_lesson_rule = (
            "本轮是暑假班课程整体记录，只能调用 tuoguan_record_summer_lesson；"
            "raw_text 必须逐字使用老师本轮原话，不得把‘还可以’改成‘良好’，不得增加辅导结论；"
            "工具会校验班级、课程表和出勤。范围不明确时直接使用工具返回的追问，不得改用个人记录工具。"
        )
    unclassified_rule = ""
    if item.get("model_intent") == "unclassified_message":
        confirmed_menu = "、".join(dict.fromkeys(
            str(card.get("capability") or "")
            for cards in (item.get("runtime_tool_cards") or {}).values()
            for card in cards
            if card.get("capability")
        ))
        unclassified_rule = (
            "本轮由模型主导理解和回复。它可能是普通对话、解释、讨论，也可能是尚未说完整的业务需求。"
            "正常对话不受业务执行范围限制：自然回答用户真正问的内容，保持上下文连续；不确定时只问一个最有帮助的问题。"
            "不要主动念固定功能菜单，不要因为用户提到‘模型’或‘系统’就调用业务工具，也不要把工具未调用说成系统限制。"
            "对外回复不要解释内部实现细节，也不要说工具或系统在限制你；"
            "如果某件事不能可靠执行，只用人的话说明需要先确认对象、范围或查系统。"
            "用户问‘我是谁’、自己的负责范围、当前上下文或可见对象时，可以调用 tuoguan_context 读取可信身份和上下文。"
            "涉及企业微信通讯录、老师/店长/老板名单、人员昵称、表情名、乱码、user_id、现任/离职或人员变更时，先调用 tuoguan_query_staff_directory；"
            "如果工具给出修复候选，只能说是候选，必须由老板确认后才能用 tuoguan_submit_operational_fact 保存为人员运营事实。"
            "当回复要给出老师、学生、任务、经营、积分、安全、看板等机构实时事实时，需要先调用对应 tuoguan_ 可信工具或自然追问查询范围；"
            "可以自由分析用户问题，但不能凭聊天记忆、上下文印象或系统提示直接把老师名单、学生资料、电话、数量、排名、任务状态、经营结论说成真实数据。"
            "只有用户明确要求查询实时业务事实或修改业务数据时，才选择对应可信工具；没有可信结果时不得声称数据已改变。"
            "需要执行时，模型可以从可用的 tuoguan_ 可信工具中自主选择合适功能，系统只校验身份、权限和执行结果。"
            "说做不了、查不到或需要技术前，先完成自救顺序：当前上下文、可信业务读工具、人员目录、历史事实/候选、必要时只读联网；仍失败时只问一个最关键问题。"
            "用户明确询问‘能做什么’时，可以自然说明你能理解、分析、提醒和协助推进机构工作；"
            "但如果要声明真实数据、写入、通知或状态变更已经发生，需要先取得可信工具结果并通过权限和反查。"
            f"当前已封版强执行能力包括：{confirmed_menu or '暂无'}；"
            "未列入这里不等于不能理解或不能建议，只表示不能在没有可信工具结果时宣布已执行成功。"
        )
    points_rule = ""
    if item.get("model_intent") == "change_summer_points":
        points_rule = (
            "本轮是暑假班积分变动，只能调用 tuoguan_change_summer_points；"
            "operation_id 必须使用当前消息 id；分值不明确时也调用该工具取得受控追问；"
            "积分计算、负分校验和学生匹配全部由工具完成，模型不得自行计算。"
        )
    elif item.get("model_intent") == "query_summer_points":
        points_rule = "本轮只能调用 tuoguan_query_summer_points，最终逐字使用工具 rendered_text。"
    elif item.get("model_intent") == "query_summer_points_ranking":
        points_rule = "本轮只能调用 tuoguan_query_summer_points_ranking，最终逐字使用工具 rendered_text，不得改写排名。"
    autonomous_work_rule = _autonomous_work_context(str(sender_id or ""), str(item.get("actor_role") or item.get("role") or ""))
    foundation_rule = (
        "【优益模型主导原则】由模型理解用户、结合上下文、决定是否追问或使用功能并负责最终回复；"
        "系统只在实际执行时校验身份、权限和真实结果，不限制模型正常对话、分析和建议。"
        if item.get("model_intent") == "unclassified_message"
        else
        "【优益执行边界】模型仍负责理解、分析、追问和选择下一步；系统不因业务流程偏好限制模型思考。"
        "当模型要声明真实业务数据或执行成功时，需要有可信工具结果；不得仅凭聊天历史声称成功。"
        "写操作只有工具返回 ok=true 且 writeback_verified=true 才能回复成功；"
        "任务反馈优先关联最近任务上下文；传给任务工具的 reply 必须保持用户原话，不得补充用户未说的事实；"
        "实时列表和报表最终回复必须逐字使用工具 rendered_text，不得扩写数字。"
    )
    return {"context": (
        foundation_rule
        + task_query_rule
        + task_rule
        + daily_record_rule
        + student_performance_rule
        + summer_lesson_rule
        + unclassified_rule
        + points_rule
        + autonomous_work_rule
    )}


def observe_tool_result(*, session_id: str, tool_name: str, args: Any, result: Any) -> None:
    if not str(tool_name or "").startswith("tuoguan_"):
        return
    with _LOCK:
        item = _TURN_BY_SESSION.get(str(session_id or ""))
        if not item:
            return
        _reconcile_model_tool_selection(item, tool_name, args)
        # The trusted tool has already resolved this turn. Model retries may
        # still be attempted by the agent loop, but they must not contaminate
        # the outward ledger or replace the terminal result.
        if item.get("terminal_tool_result"):
            blocked = item.setdefault("blocked_tool_calls", [])
            blocked.append({"tool": tool_name, "reason_code": "terminal_result_already_recorded"})
            return
        try:
            parsed = json.loads(result) if isinstance(result, str) else deepcopy(result)
        except (TypeError, ValueError):
            parsed = {"ok": False, "error": "unparseable_tool_result", "data": {}}
        item["tool_calls"].append({"tool": tool_name, "args": deepcopy(args or {})})
        item["tool_results"].append(parsed)
        item["used_tool_registry_entry"] = tool_name
        data = parsed.get("data", {}) if isinstance(parsed, dict) else {}
        if isinstance(data, dict):
            item["effective_scope"] = data.get("effective_scope", item.get("effective_scope"))
            item["result_count"] = data.get("result_count", data.get("count", item.get("result_count")))
            item["data_version"] = data.get("data_version", item.get("data_version"))
            if tool_name == "tuoguan_query_students":
                item["terminal_tool_result"] = True
            non_terminal_redirect = isinstance(parsed, dict) and str(parsed.get("error") or "") in {"goal_workspace_required"}
            if not non_terminal_redirect and (tool_name in WRITE_TOOLS or tool_name in {
                "tuoguan_next_task", "tuoguan_current_task_guidance",
                "tuoguan_goal_workspace", "tuoguan_query_tasks", "tuoguan_query_operations_report",
                "tuoguan_dashboard_link", "tuoguan_query_summer_points", "tuoguan_query_summer_points_ranking",
            }):
                # A capability turn has one authoritative tool result. Composite
                # actions belong inside a trusted tool, never in a model-driven
                # sequence of independent writes.
                item["terminal_tool_result"] = True
                if tool_name == "tuoguan_update_task":
                    item["idempotency_verified"] = bool(data.get("idempotency_verified"))



def _turn_for_tool(session_id: str, args: Any = None) -> dict[str, Any] | None:
    item = _TURN_BY_SESSION.get(str(session_id or ""))
    if item:
        return item
    user_id = str((args or {}).get("user_id") or "") if isinstance(args, dict) else ""
    return _PENDING_BY_USER.get(user_id) if user_id else None


def _semantic_tool_match(item: dict[str, Any], tool_name: str, args: Any = None) -> bool:
    raw = _compact(item.get("raw_text", ""))
    payload = args if isinstance(args, dict) else {}
    if tool_name == "tuoguan_context":
        return any(term in raw for term in ("我是谁", "我的", "自己", "名下", "负责", "当前", "上下文", "身份", "角色", "能看", "可以看", "可见"))
    if tool_name == "tuoguan_record_student":
        student = _compact(payload.get("student_name", ""))
        return bool(student and student.replace("测试", "") in raw.replace("测试", "") and len(raw) >= len(student) + 4)
    if tool_name == "tuoguan_record_summer_lesson":
        return "暑假班" in raw and any(term in raw for term in ("整体", "全班", "所有小朋友", "所有孩子", "课程", "课"))
    if tool_name == "tuoguan_update_task":
        return bool(any(term in raw for term in ("任务", "回访", "沟通", "家长", "完成", "处理", "跟进")))
    if tool_name == "tuoguan_next_task":
        return "任务" in raw and any(term in raw for term in ("开始", "继续", "下一个", "先做", "A级", "A级"))
    if tool_name == "tuoguan_current_task_guidance":
        return "任务" in raw and any(term in raw for term in ("怎么", "缺", "完成", "处理", "下一步", "当前"))
    if tool_name == "tuoguan_query_tasks":
        return "任务" in raw or ("老师" in raw and any(term in raw for term in ("做了什么", "执行", "工作情况", "今天做", "有什么")))
    if tool_name == "tuoguan_query_students":
        student = _compact(payload.get("student_name", ""))
        if not student:
            return any(term in raw for term in ("我班", "我们班", "我负责", "名下", "我的学生", "我的孩子", "自己班", "暑假班", "学生名单", "孩子名单", "哪些学生", "几个孩子", "多少孩子"))
        if not student:
            return False
        return bool(
            student in raw
            and any(term in raw for term in ("查", "看", "了解", "信息", "资料", "记录", "最近", "怎么样", "表现"))
        )
    if tool_name == "tuoguan_query_operations_report":
        teacher = _compact(payload.get("teacher_name", ""))
        query_type = str(payload.get("query_type") or payload.get("report_type") or "")
        if query_type == "summer_students" and "暑假班" in raw and any(term in raw for term in ("学生", "孩子", "人数", "多少", "几个", "名单")):
            return True
        if query_type == "staff" and "老师" in raw and any(term in raw for term in ("查", "看", "多少", "几位", "几个", "名单")):
            return True
        if query_type == "teacher_activity" and teacher and teacher in raw and any(term in raw for term in ("查", "看", "了解", "最近", "怎么样")):
            return True
        if teacher and teacher in raw and any(term in raw for term in ("查", "看", "了解", "最近", "怎么样")):
            return True
        return any(term in raw for term in ("经营", "日报", "周报", "老师记录", "试听线索", "跟进统计"))
    if tool_name == "tuoguan_query_staff_directory":
        return any(
            term in raw
            for term in (
                "企业微信", "通讯录", "人员", "员工", "老师名单", "店长", "老师都有谁",
                "都有谁", "谁还在", "谁不在", "乱码", "名字", "昵称", "user_id", "userid",
                "踢出", "踢了", "离职", "现任", "配置人员", "白名单",
            )
        )
    if tool_name == "tuoguan_create_task":
        return any(term in raw for term in ("安排", "任务", "提醒", "回访", "跟进"))
    if tool_name == "tuoguan_dashboard_link":
        return "看板" in raw and any(term in raw for term in ("链接", "地址", "打开", "发", "给", "H5", "h5"))
    return False


def _reconcile_model_tool_selection(item: dict[str, Any], tool_name: str, args: Any = None) -> bool:
    """Promote a model-selected tool only through a confirmed runtime card.

    Pre-routing classification is a hint, not an execution authority. This
    function lets the model resolve natural language that the hint classifier
    missed, while the runtime allowlist and role profile remain the hard gate.
    """
    selected = str(item.get("selected_capability_card") or "")
    injected = {str(value) for value in (item.get("used_manual_cards") or [])}
    if selected in INTERNAL_TASK_WORKFLOW_CARDS or selected in injected:
        return True
    candidates = list((item.get("runtime_tool_cards") or {}).get(str(tool_name or ""), []))
    if not candidates:
        return False
    if str(item.get("model_intent") or "") == "unclassified_message" and not _semantic_tool_match(item, tool_name, args):
        return False
    if selected:
        exact = [card for card in candidates if card.get("capability") == selected]
        if exact:
            candidates = exact
    compact = _compact(item.get("raw_text", ""))
    if len(candidates) > 1:
        if "日报" in compact:
            preferred = [card for card in candidates if card.get("report_type") == "daily" or "日报" in card.get("capability", "")]
        else:
            preferred = [card for card in candidates if card.get("report_type") != "daily" and "日报" not in card.get("capability", "")]
        if preferred:
            candidates = preferred
    if len(candidates) != 1:
        return False
    card = candidates[0]
    item["selected_capability_card"] = str(card.get("capability") or "")
    item["used_manual_cards"] = [item["selected_capability_card"]]
    item["model_intent"] = str(card.get("intent") or item.get("model_intent") or "unclassified_message")
    item["capability_resolved_by"] = "model_tool_selection_validated_by_runtime_allowlist"
    return True


def _allow_model_selected_read_tool(item: dict[str, Any], tool_name: str, args: Any = None) -> bool:
    if tool_name not in MODEL_SELECTED_READ_TOOLS:
        return False
    if not _semantic_tool_match(item, tool_name, args):
        return False
    intent_by_tool = {
        "tuoguan_query_tasks": "query_my_tasks",
        "tuoguan_query_students": "query_student_performance",
        "tuoguan_dashboard_link": "query_dashboard_link",
        "tuoguan_query_staff_directory": "query_staff_directory",
    }
    item["model_intent"] = intent_by_tool.get(tool_name, item.get("model_intent") or "unclassified_message")
    item["capability_resolved_by"] = "model_selected_safe_read_tool"
    return True


def _complete_model_selected_read_args(item: dict[str, Any], tool_name: str, args: Any = None) -> None:
    if tool_name != "tuoguan_query_students" or not isinstance(args, dict):
        return
    raw = str(item.get("raw_text") or "")
    if "暑假班" in raw and not any(str(args.get(key) or "").strip() for key in ("student_name", "teacher_name")):
        args["query_scope"] = "summer"


def tool_contract_denial_for_user(user_id: str, tool_name: str, args: Any = None) -> dict[str, str] | None:
    return None

def validate_tool_call(*, session_id: str, tool_name: str, args: Any = None, **_: Any) -> dict[str, str] | None:
    return None

def block_tool_after_terminal_result(*, session_id: str, tool_name: str, args: Any = None, **_: Any) -> dict[str, str] | None:
    """Prevent a model retry from escaping an already-resolved task context."""
    if not str(tool_name or "").startswith("tuoguan_"):
        return None
    with _LOCK:
        item = _turn_for_tool(session_id, args)
        if not item or not item.get("terminal_tool_result"):
            return None
        idempotency_verified = bool(item.get("idempotency_verified"))
    return {
        "action": "block",
        "message": (
            "该任务已确认完成，本轮已经幂等结束。请直接回复用户，不要再调用其他工具。"
            if idempotency_verified
            else "本轮可信工具已经返回确定结果。请直接回复用户，不要再调用其他工具。"
        ),
    }


def _audit(store: TuoguanStore, item: dict[str, Any], event: str, result: str, **extra: Any) -> str:
    audit_id = f"audit_{uuid.uuid4().hex}"
    _append_jsonl(store, "business_action_audit.jsonl", {
        "audit_event_id": audit_id,
        "ledger_id": item["ledger_id"],
        "tenant_id": TENANT_ID,
        "channel": "wecom_callback",
        "actor_user_id": item["user_id"],
        "actor_role": item["role"],
        "event": event,
        "action": item["model_intent"],
        "result": result,
        "created_at": _now(),
        **extra,
    })
    return audit_id


def finalize_no_focus_clarification(*, store: TuoguanStore, user_id: str) -> str | None:
    """Close a generic no-focus turn without entering the model or a write tool."""

    with _LOCK:
        item = _PENDING_BY_USER.get(str(user_id or ""))
        if not item:
            return None
        final = "你说的是哪个任务？请说一下学生姓名或任务内容，比如“测试小林回访任务完成了”。"
        item.update({
            "entered_model": False,
            "reason_code": "no_focus_task",
            "tool_calls": [],
            "tool_results": [],
            "used_tool_registry_entry": "",
            "writeback_verified": None,
            "render_verified": True,
            "guard_result": "clarification_needed",
            "route_decision": "no_focus_clarification",
            "legacy_handler_intercepted": False,
            "final_reply": final,
            "completed_at": _now(),
        })
        changed = changed_protected_hashes(
            item.get("protected_hashes_before") or {}, protected_hashes(store.data_dir)
        )
        item["protected_hashes_changed"] = changed
        if changed:
            item["guard_result"] = "critical_write_violation"
            audit_ids = [_audit(store, item, "unauthorized_write_detected", "critical", changed_files=changed)]
        else:
            audit_ids = []
        audit_ids.extend([
            _audit(store, item, "clarification_needed", "no_focus_task"),
            _audit(store, item, "no_write", "no_state_change"),
            _audit(store, item, "no_tool_call", "no_tool_call"),
            _audit(store, item, "reply_completed", "clarification"),
        ])
        item["audit_event_ids"] = audit_ids
        _append_jsonl(store, "reply_ledger.jsonl", deepcopy(item))
        _record_learning_safely(store, item)
        _PENDING_BY_USER.pop(str(user_id or ""), None)
        return final


def ensure_outbound_reply_recorded(
    *,
    store: TuoguanStore,
    message_id: str,
    conversation_id: str,
    user_id: str,
    role: str,
    raw_text: str,
    final_reply: str,
    entered_model: bool,
    session_id: str = "",
    route_decision: str = "",
    message_owner_id: str = "",
    ownership_type: str = "",
    owner_capability: str = "",
    reply_owner: str = "",
    reply_sender_count: int = 1,
) -> None:
    """Guarantee that every user-facing WeCom reply has a ledger and audit row."""

    message_id = str(message_id or "")
    path = store.data_dir / "reply_ledger.jsonl"
    if path.exists():
        with _LOCK:
            for line in reversed(path.read_text(encoding="utf-8").splitlines()[-200:]):
                try:
                    if str(json.loads(line).get("message_id") or "") == message_id:
                        return
                except ValueError:
                    continue
    if session_id:
        transformed = transform_final_response(
            store=store,
            session_id=session_id,
            response_text=final_reply,
        )
        if transformed is not None:
            final_reply = transformed
    owner = str(reply_owner or ("model" if entered_model else "system_technical"))
    item = {
        "ledger_id": f"ledger_{uuid.uuid4().hex}",
        "message_id": message_id,
        "source_message_id": message_id,
        "conversation_id": str(conversation_id or user_id),
        "tenant_id": TENANT_ID,
        "channel": "wecom_callback",
        "user_id": str(user_id or ""),
        "role": str(role or "unbound"),
        "raw_text": str(raw_text or ""),
        "entered_model": bool(entered_model),
        "model_intent": "unclassified_message",
        "model_confidence": None,
        "selected_capability_card": "",
        "used_manual_cards": [],
        "used_tool_registry_entry": "",
        "tool_calls": [],
        "tool_results": [],
        "writeback_verified": None,
        "render_verified": True,
        "route_decision": route_decision or ("model_first" if entered_model else "deterministic_system_reply"),
        "legacy_handler_intercepted": (route_decision or ("model_first" if entered_model else "deterministic_system_reply")) == "legacy_router",
        "guard_result": "allowed",
        "final_reply": str(final_reply or ""),
        "created_at": _now(),
        "completed_at": _now(),
        "audit_event_ids": [],
        "message_owner_id": str(message_owner_id or ""),
        "ownership_type": str(ownership_type or ""),
        "owner_capability": str(owner_capability or ""),
        "reply_owner": owner,
        "model_reply_applied": owner == "model",
        "reply_sender_count": int(reply_sender_count),
    }
    if entered_model:
        item["business_plan"] = _non_business_dialogue_plan(
            actor_user_id=user_id,
            actor_role=role,
            message_id=message_id,
            raw_text=raw_text,
        )
    audit_id = _audit(store, item, "reply_completed", "success")
    item["audit_event_ids"] = [audit_id]
    _append_jsonl(store, "reply_ledger.jsonl", item)
    _record_learning_safely(store, item)


def mark_outbound_reply_delivered(
    *, store: TuoguanStore, source_message_id: str, platform_message_id: str
) -> bool:
    """Attach the actual WeCom send acknowledgement to its reply ledger."""

    source_message_id = str(source_message_id or "")
    if not source_message_id:
        return False
    path = store.data_dir / "reply_ledger.jsonl"
    if not path.exists():
        return False
    with _LOCK:
        lines = path.read_text(encoding="utf-8").splitlines()
        for index in range(len(lines) - 1, -1, -1):
            try:
                item = json.loads(lines[index])
            except ValueError:
                continue
            if str(item.get("message_id") or "") != source_message_id:
                continue
            if item.get("delivery_status") == "delivered":
                return True
            item["delivery_status"] = "delivered"
            item["delivery_confirmed_at"] = _now()
            item["platform_message_id"] = str(platform_message_id or "")
            audit_id = _audit(
                store,
                item,
                "reply_delivered",
                "success",
                platform_message_id=str(platform_message_id or ""),
            )
            item["audit_event_ids"] = list(item.get("audit_event_ids") or []) + [audit_id]
            lines[index] = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True
    return False


def transform_final_response(*, store: TuoguanStore, session_id: str, response_text: str) -> str | None:
    verified_state_change = False
    used_trusted_tool = False
    with _LOCK:
        item = _TURN_BY_SESSION.get(str(session_id or "")) or {}
        for tool_name in (call.get("tool") for call in (item.get("tool_calls") or []) if isinstance(call, dict)):
            if str(tool_name or "").startswith("tuoguan_"):
                used_trusted_tool = True
            if str(tool_name or "") in WRITE_TOOLS:
                verified_state_change = True
                break
    return _sanitize_external_reply(
        response_text,
        verified_state_change=verified_state_change,
        used_trusted_tool=used_trusted_tool,
    )
