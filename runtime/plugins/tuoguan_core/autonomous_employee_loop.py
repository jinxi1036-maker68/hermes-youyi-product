"""Model-led autonomous employee loop for Hermes wakeups.

This module lets the timer wakeup hand real operating material to the model and
persist low-risk autonomous work state. It may queue bounded boss/manager/teacher
messages through the audited outbox, but it does not contact parents, create
teacher tasks, change salary, delete data, or store a fixed route/tool step.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable

import httpx
import yaml

from .digital_employee_state import (
    ACTION_EXECUTIONS_FILE,
    ATTENTION_THREADS_FILE,
    BUSINESS_EVENTS_FILE,
    HERMES_WORK_ITEMS_FILE,
    HERMES_EMPLOYEE_SCORECARD_FILE,
    INDUSTRY_LEARNING_CANDIDATES_FILE,
    INSTITUTION_FACT_GAP_EVENTS_FILE,
    INSTITUTION_UNDERSTANDING_FILE,
    RELATIONSHIP_TOUCH_CANDIDATES_FILE,
    VALUE_PROGRESS_LEDGER_FILE,
    WAKEUP_REQUESTS_FILE,
    query_action_executions,
    query_attention_threads,
    query_active_goal_work_state,
    query_autonomous_work_brief,
    query_business_events,
    query_hermes_employee_scorecard,
    query_hermes_work_items,
    query_external_learning_brief,
    query_industry_learning_candidates,
    query_institution_understanding,
    query_multi_agent_brief,
    query_proactive_work_radar,
    query_relationship_touch_candidates,
    relationship_touch_policy,
    query_value_progress_ledger,
    query_wakeup_requests,
    submit_action_execution,
    submit_attention_thread,
    submit_employee_self_review,
    submit_business_event,
    submit_fact_gap_candidate,
    submit_hermes_work_item,
    submit_relationship_touch_candidate,
    submit_value_progress_entry,
    update_agent_delegation_decision,
    update_hermes_work_item,
)
from .models import UserIdentity
from .self_evolution import (
    SELF_EVOLUTION_EVENTS_FILE,
    build_self_evolution_brief,
    normalize_evolution_candidate,
    submit_self_evolution_event,
)
from .store import JSON_NO_CHANGE, TuoguanStore
from .tool_service import TuoguanToolService
from .write_guard import authorized_system_write

DecisionProvider = Callable[[dict[str, Any]], dict[str, Any]]

_FORBIDDEN_EFFECT_KEYS = {
    "send_parent_message", "send_teacher_message", "create_teacher_task",
    "change_salary", "delete_data", "change_permission", "close_safety_event",
}
_FORBIDDEN_ROUTE_KEYS = {"model_intent", "next_tool", "workflow_step", "expected_reply"}
_ALLOWED_WORK_STATUSES = {"active", "waiting", "blocked", "closed", "superseded"}
_NON_MATERIAL_OBSERVATION_TYPES = {
    "no_new_input",
    "daytime_patrol",
    "night_patrol",
    "wakeup_heartbeat",
    "autonomous_heartbeat",
    "state_migration_applied",
    "state_migration_confirmed",
    "owner_attention_outdated",
    "no_new_human_input",
    "timer_patrol",
    "reminder_outdated",
    # Only the gateway may record raw human-message facts. A model summary with
    # this type would duplicate or paraphrase the actual inbound message.
    "owner_inbound_message",
}
_NOTIFICATION_OUTBOX_FILE = "notification_outbox.json"
_ALLOWED_FILES = {
    HERMES_WORK_ITEMS_FILE,
    WAKEUP_REQUESTS_FILE,
    BUSINESS_EVENTS_FILE,
    ACTION_EXECUTIONS_FILE,
    INSTITUTION_UNDERSTANDING_FILE,
    HERMES_EMPLOYEE_SCORECARD_FILE,
    INDUSTRY_LEARNING_CANDIDATES_FILE,
    INSTITUTION_FACT_GAP_EVENTS_FILE,
    VALUE_PROGRESS_LEDGER_FILE,
    _NOTIFICATION_OUTBOX_FILE,
    ATTENTION_THREADS_FILE,
    RELATIONSHIP_TOUCH_CANDIDATES_FILE,
    SELF_EVOLUTION_EVENTS_FILE,
}


def run_autonomous_employee_loop(
    store: TuoguanStore | None = None,
    *,
    now: datetime | None = None,
    wakeup_summary: dict[str, Any] | None = None,
    decision_provider: DecisionProvider | None = None,
    write_state: bool = True,
) -> dict[str, Any]:
    actual_store = store or TuoguanStore()
    timestamp = now or datetime.now().astimezone()
    identity = _system_identity()
    materials = build_employee_loop_materials(actual_store, identity=identity, timestamp=timestamp, wakeup_summary=wakeup_summary)
    result: dict[str, Any] = {
        "ok": True,
        "schema_version": 1,
        "report_type": "autonomous_employee_loop_v1",
        "generated_at": timestamp.isoformat(timespec="seconds"),
        "model_led": True,
        "limits_model": False,
        "external_actions_taken": [],
        "owner_attention_queued": [],
        "writes": [],
        "materials_summary": materials.get("materials_summary") or {},
        "work_cadence": materials.get("work_cadence") or {},
        "boundary": _boundary(),
    }
    try:
        decision = (decision_provider or _call_model_for_decision)(materials)
        decision = validate_employee_decision(decision)
        decision = normalize_employee_decision_for_materials(decision, materials)
        result["decision"] = decision
    except Exception as exc:
        result.update({
            "ok": False,
            "error": "employee_loop_model_decision_failed",
            "message": _safe_error(exc),
            "rendered_text": "Hermes woke and read material, but model-led employee review did not complete. No internal work state changed.",
            "render_verified": True,
        })
        return result
    if write_state:
        try:
            result["writes"] = materialize_employee_decision(
                actual_store,
                identity=identity,
                decision=decision,
                timestamp=timestamp,
                materials=materials,
            )
        except Exception as exc:
            result["ok"] = False
            result["error"] = "employee_loop_materialize_failed"
            result["message"] = _safe_error(exc)
            result["writes"] = []
    result["rendered_text"] = render_employee_loop_report(result)
    result["render_verified"] = True
    return result


def build_employee_loop_materials(store: TuoguanStore, *, identity: UserIdentity, timestamp: datetime, wakeup_summary: dict[str, Any] | None = None) -> dict[str, Any]:
    onboarding = _query_onboarding(store)
    active_goals = query_active_goal_work_state(store, identity=identity)
    work_items = query_hermes_work_items(store, identity=identity, include_closed=False, limit=20)
    work_brief = query_autonomous_work_brief(store, identity=identity, limit=20)
    wakeups = query_wakeup_requests(store, identity=identity, limit=20)
    events = query_business_events(store, identity=identity, limit=20)
    executions = query_action_executions(store, identity=identity, limit=20)
    institution_understanding = query_institution_understanding(store, identity=identity)
    proactive_radar = query_proactive_work_radar(store, identity=identity, limit=12)
    employee_scorecard = query_hermes_employee_scorecard(store, identity=identity, limit=10)
    industry_learning = query_industry_learning_candidates(store, identity=identity, limit=10)
    external_learning = query_external_learning_brief(store, identity=identity, limit=5)
    value_progress = query_value_progress_ledger(store, identity=identity, limit=10)
    attention_threads = query_attention_threads(store, identity=identity, include_closed=False, limit=10)
    multi_agent = query_multi_agent_brief(store, identity=identity, limit=10)
    relationship_policy = relationship_touch_policy(store)
    relationship_touches = query_relationship_touch_candidates(store, identity=identity, include_closed=False, limit=10)
    self_evolution = build_self_evolution_brief(store, identity=identity, limit=12)
    owner_messages = query_business_events(store, identity=identity, event_type="owner_inbound_message", limit=10)
    if isinstance(owner_messages.get("events"), list):
        owner_messages["events"] = [
            event
            for event in owner_messages["events"]
            if str((event.get("source") or {}).get("actor_user_id") or "")
            not in {"autonomous_employee_loop", "autonomous_wakeup_runner"}
        ][-5:]
        owner_messages["event_count"] = len(owner_messages["events"])
    operating_evidence = _query_operating_evidence(store)
    patrol_counts = (wakeup_summary or {}).get("source_counts") or {}
    term_state = (wakeup_summary or {}).get("term_state") or _fallback_term_state(store, timestamp)
    deferred_items = (wakeup_summary or {}).get("deferred_items") or []
    new_term_readiness = (wakeup_summary or {}).get("new_term_readiness") or {}
    cadence = _work_cadence(timestamp)
    public_identity = _public_identity_material(store)
    materials = {
        "timestamp": timestamp.isoformat(timespec="seconds"),
        "identity": public_identity.get("identity") or "Hermes internal product, public employee name Xiaoyou",
        "public_identity": public_identity,
        "mission": "understand the institution, protect reality and permissions, help the owner improve renewal, service quality, risk control, execution, and revenue",
        "principles": [
            "Use the public-facing employee name 小优 when speaking to the owner, managers, or teachers. Hermes is the internal product/architecture name.",
            "Handbook is guidance and business knowledge, not a fixed workflow.",
            "The model decides whether to continue, wait, ask, update state, or stop.",
            "This wakeup can only persist low-risk internal state.",
            "No teacher reply does not mean failure or completion.",
            "New-term student service relations remain deferred unless the owner asks earlier.",
            "Deferred new-term service relations are future confirmation material, not a current blocker for historical renewal analysis or preparation.",
            "During daytime, if work is blocked by missing owner facts, the model may ask for one low-frequency owner attention note.",
            "When proactive staff questions are allowed by policy, ask the right whitelisted manager or teacher for one concrete work fact instead of routing every missing fact through the owner. Never contact parents.",
            "Do not say Hermes cannot proactively ask for missing information. Distinguish safe owner attention, in-chat clarification, and blocked external outreach.",
            "Hermes has its own employee goals: institutional understanding, owner goal progress, teacher support, student service evidence, risk detection, business opportunity, and learning growth.",
            "At every wakeup, inspect proactive_work_radar as the handbook-based employee map: institution, organization, student service relations, operating rules, teacher work habits, goals, service evidence, risk, and reflection. It is material, not a Router.",
            "Public industry learning is advice material with sources; never treat it as confirmed institution fact before owner review.",
            "External learning and market research are evidence candidates. Use them to improve advice, but do not copy them into institution facts or long-term memory until the owner reviews them.",
            "Self-evolution is Xiaoyou's employee growth loop: daytime work, evening review, night learning, next-day application. It is internal candidate material, not a Router.",
            "Low-risk personal service preferences and self-corrections may inform future context only after writeback evidence. Medium/high-risk policy, salary, permissions, parent outreach, handbook, or institution-rule changes remain pending review.",
            "If this wakeup is evening or night, inspect self_evolution_brief, employee_scorecard, business_events, action_executions, workstyle preferences, proactive_work_radar, and multi_agent_brief. Save concise evolution_candidates for what Xiaoyou learned, what it must not repeat, and what should guide tomorrow.",
            "When a daytime due attention arrives and historical evidence is available, derive a concrete internal finding for the active goal instead of only restating deferred gaps.",
            "If a daytime active goal is blocked by a fact only the owner can confirm, or if you write that owner confirmation is needed before the next stage, put one concrete owner question into boss_attention_candidates. Do not hide the question only in employee_summary, goal_progress_view, or questions_to_humans.",
            "If multi_agent_brief contains pending completed sub-agent results, decide on at most one result per wakeup: adopted, partially_adopted, rejected, needs_more_evidence, or deferred. The result is advisory material only and never executes business action by itself.",
            "Hermes should build trust like a real colleague. Relationship touches may be care, encouragement, thanks, light chat, relief, material support, manager assistance, owner business insight, progress update, or presence report.",
            "Teacher and manager relationship touches may be sent only when policy allows, the target is whitelisted, the message asks for a concrete work fact, and the daily frequency limit is not exceeded. Private emotional support remains candidate-only.",
        ],
        "work_cadence": cadence,
        "owner_attention_policy": {
            "enabled": True,
            "allowed_target": "owner/boss for decisions; whitelisted managers/teachers for concrete work facts",
            "allowed_now": bool(cadence.get("owner_attention_allowed")),
            "daytime_window": "08:00-19:00 local time",
            "quiet_window": "19:00-08:00 no proactive owner interruption",
            "max_per_focus": "one pending/daily reminder per focus",
            "content_rule": "Say what is blocked, what fact is needed, and what Hermes will do after confirmation. Do not claim completion.",
            "not_a_blanket_ban": "This policy does not forbid Hermes from asking missing facts; it requires the model to choose the fact owner and the system to enforce permissions, frequency, and no-parent-contact boundaries.",
        },
        "onboarding_gaps": _compact_for_model(onboarding.get("data") or onboarding),
        "active_goal_state": _compact_for_model(active_goals),
        "autonomous_work_items": _compact_for_model(work_items),
        "autonomous_work_brief": _compact_for_model(work_brief),
        "wakeup_requests": _compact_for_model(wakeups),
        "business_events": _compact_for_model(events),
        "action_executions": _compact_for_model(executions),
        "institution_understanding_state": _compact_for_model(institution_understanding),
        "proactive_work_radar": _compact_for_model(proactive_radar, max_chars=9000),
        "employee_scorecard": _compact_for_model(employee_scorecard),
        "industry_learning_candidates": _compact_for_model(industry_learning),
        "external_learning_brief": _compact_for_model(external_learning),
        "value_progress_ledger": _compact_for_model(value_progress),
        "attention_threads": _compact_for_model(attention_threads),
        "multi_agent_brief": _compact_for_model(multi_agent),
        "relationship_touch_policy": _compact_for_model(relationship_policy),
        "relationship_touch_candidates": _compact_for_model(relationship_touches),
        "self_evolution_brief": _compact_for_model(self_evolution, max_chars=5000),
        "recent_owner_messages": _compact_for_model(owner_messages),
        "operating_evidence": _compact_for_model(operating_evidence, max_chars=8000),
        "term_state": _compact_for_model(term_state),
        "deferred_items": _compact_for_model(deferred_items),
        "new_term_readiness": _compact_for_model(new_term_readiness),
        "patrol_counts": _compact_for_model(patrol_counts),
        "allowed_internal_outputs": ["observations", "work_item_updates", "questions_to_humans", "boss_attention_candidates", "relationship_touch_candidates", "institution_fact_gaps", "value_progress_entries", "agent_delegation_decisions", "evolution_candidates", "self_review", "stop_or_wait_reason"],
        "forbidden_external_outputs": sorted(_FORBIDDEN_EFFECT_KEYS),
    }
    base_onboarding = onboarding.get("data") or onboarding if isinstance(onboarding, dict) else {}
    materials["materials_summary"] = {
        "onboarding_gap_count": int(base_onboarding.get("gap_count") or 0) if isinstance(base_onboarding, dict) else 0,
        "active_goal_count": int(active_goals.get("goal_count") or 0),
        "work_item_count": int(work_items.get("work_item_count") or 0),
        "waiting_count": int(work_brief.get("waiting_count") or 0),
        "pending_wakeup_count": int(wakeups.get("pending_count") or 0),
        "business_event_count": int(events.get("event_count") or 0),
        "result_unknown_action_count": int(executions.get("result_unknown_count") or 0),
        "institution_gap_count": int(((institution_understanding.get("audit") or {}).get("gap_count") or 0)) if isinstance(institution_understanding, dict) else 0,
        "proactive_radar_gap_count": int(proactive_radar.get("priority_gaps") and len(proactive_radar.get("priority_gaps") or []) or 0) if isinstance(proactive_radar, dict) else 0,
        "proactive_radar_question_candidate_count": int(proactive_radar.get("question_candidates") and len(proactive_radar.get("question_candidates") or []) or 0) if isinstance(proactive_radar, dict) else 0,
        "employee_self_review_count": int(employee_scorecard.get("review_count") or 0) if isinstance(employee_scorecard, dict) else 0,
        "industry_learning_candidate_count": int(industry_learning.get("candidate_count") or 0) if isinstance(industry_learning, dict) else 0,
        "external_research_run_count": int(((external_learning.get("external_research_runs") or {}).get("run_count") or 0)) if isinstance(external_learning, dict) else 0,
        "value_progress_entry_count": int(value_progress.get("entry_count") or 0) if isinstance(value_progress, dict) else 0,
        "work_mode": str(cadence.get("mode") or ""),
        "owner_attention_allowed": bool(cadence.get("owner_attention_allowed")),
        "open_attention_count": int(attention_threads.get("attention_count") or 0),
        "pending_agent_decision_count": int(multi_agent.get("pending_decision_count") or 0) if isinstance(multi_agent, dict) else 0,
        "relationship_touch_candidate_count": int(relationship_touches.get("candidate_count") or 0) if isinstance(relationship_touches, dict) else 0,
        "self_evolution_event_count": int(self_evolution.get("event_count") or 0) if isinstance(self_evolution, dict) else 0,
        "self_evolution_review_queue_count": int(self_evolution.get("review_queue_count") or 0) if isinstance(self_evolution, dict) else 0,
        "recent_owner_message_count": int(owner_messages.get("event_count") or 0),
        "operating_evidence_available": bool(operating_evidence.get("ok")),
        "service_relation_policy": str(term_state.get("service_relation_policy") or ""),
    }
    return materials


def _public_identity_material(store: TuoguanStore) -> dict[str, Any]:
    facts = store.read_json("operational_facts.json", {})
    if not isinstance(facts, dict):
        return {
            "identity": "Hermes as Youyi digital employee",
            "public_name": "Hermes",
            "internal_name": "Hermes",
            "source": "default",
        }
    for item in reversed(facts.get("facts") or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "") != "active":
            continue
        if str(item.get("fact_type") or "") == "owner_rule" and str(item.get("subject") or "") == "数字员工称呼":
            value = str(item.get("value") or "")
            if "小优" in value:
                return {
                    "identity": "Xiaoyou, Youyi digital employee; Hermes is the internal product name",
                    "public_name": "小优",
                    "internal_name": "Hermes",
                    "usage_rule": "When generating owner/manager/teacher-facing messages, self-identify as 小优. Use Hermes only for internal architecture or technical discussion.",
                    "source_text": str(item.get("source_text") or ""),
                    "confirmed_at": str(item.get("confirmed_at") or item.get("updated_at") or ""),
                }
    return {
        "identity": "Hermes as Youyi digital employee",
        "public_name": "Hermes",
        "internal_name": "Hermes",
        "source": "default",
    }


def validate_employee_decision(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("decision_must_be_object")
    cleaned = _strip_forbidden(deepcopy(raw))
    decision = {
        "employee_summary": _truthful_internal_text(cleaned.get("employee_summary"), 800),
        "institution_understanding": _limit(cleaned.get("institution_understanding"), 800),
        "goal_progress_view": _limit(cleaned.get("goal_progress_view"), 800),
        "observations": _list_of_dicts(cleaned.get("observations"), 8),
        "work_item_updates": _list_of_dicts(cleaned.get("work_item_updates"), 8),
        "questions_to_humans": _list_of_dicts(cleaned.get("questions_to_humans"), 8),
        "boss_attention_candidates": _list_of_dicts(cleaned.get("boss_attention_candidates"), 4),
        "relationship_touch_candidates": _list_of_dicts(cleaned.get("relationship_touch_candidates"), 6),
        "institution_fact_gaps": _list_of_dicts(cleaned.get("institution_fact_gaps"), 8),
        "value_progress_entries": _list_of_dicts(cleaned.get("value_progress_entries"), 6),
        "agent_delegation_decisions": _list_of_dicts(cleaned.get("agent_delegation_decisions"), 2),
        "evolution_candidates": _list_of_dicts(cleaned.get("evolution_candidates"), 8),
        "self_review": _dict(cleaned.get("self_review")),
        "external_actions": [],
    }
    for item in decision["work_item_updates"]:
        status = str(item.get("status") or "active").strip()
        item["status"] = status if status in _ALLOWED_WORK_STATUSES else "active"
        item["focus_key"] = _limit(item.get("focus_key"), 160)
        item["title"] = _limit(item.get("title"), 160)
        item["focus_summary"] = _limit(item.get("focus_summary"), 1000)
        item["update_text"] = _truthful_internal_text(item.get("update_text"), 1000)
        if "current_phase" in item and isinstance(item.get("current_phase"), str):
            item["current_phase"] = {"phase_key": _limit(item.get("current_phase"), 160)}
        if "blocked_by" in item:
            item["blocked_by"] = _list_any(item.get("blocked_by"), 8)
        if "ask_candidates" in item:
            item["ask_candidates"] = _list_any(item.get("ask_candidates"), 8)
        if "last_human_contact_at" in item:
            item["last_human_contact_at"] = _limit(item.get("last_human_contact_at"), 80)
        if "next_contact_after" in item:
            item["next_contact_after"] = _limit(item.get("next_contact_after"), 80)
        if "owner_escalation_reason" in item:
            item["owner_escalation_reason"] = _limit(item.get("owner_escalation_reason"), 500)
        if "value_progress_note" in item:
            item["value_progress_note"] = _truthful_internal_text(item.get("value_progress_note"), 500)
    for item in decision["institution_fact_gaps"]:
        item["gap_key"] = _limit(item.get("gap_key"), 120)
        item["gap_text"] = _limit(item.get("gap_text") or item.get("text") or item.get("reason"), 1000)
        item["ask_role"] = _limit(item.get("ask_role") or "boss", 80)
        item["target_time"] = _limit(item.get("target_time"), 80)
        item["urgency"] = _limit(item.get("urgency") or "normal", 40)
    for item in decision["value_progress_entries"]:
        item["subject"] = _limit(item.get("subject"), 160)
        item["discovered"] = _limit(item.get("discovered"), 1000)
        item["hermes_action"] = _limit(item.get("hermes_action"), 1000)
        item["human_action"] = _limit(item.get("human_action"), 1000)
        item["outcome"] = _limit(item.get("outcome"), 1000)
        item["attribution"] = _limit(item.get("attribution") or "participated", 40)
    decision["agent_delegation_decisions"] = _normalize_agent_delegation_decisions(decision.get("agent_delegation_decisions") or [])
    cleaned_candidates = []
    for item in decision["boss_attention_candidates"]:
        candidate = {
            "focus_key": _limit(item.get("focus_key"), 160),
            "reason": _limit(item.get("reason"), 500),
            "message": _limit(item.get("message") or item.get("content") or item.get("question"), 900),
            "urgency": _limit(item.get("urgency") or "normal", 40),
        }
        if candidate["focus_key"] and candidate["reason"] and candidate["message"]:
            cleaned_candidates.append(candidate)
    decision["boss_attention_candidates"] = cleaned_candidates
    decision["relationship_touch_candidates"] = _normalize_relationship_touch_candidates(decision.get("relationship_touch_candidates") or [])
    decision["evolution_candidates"] = _normalize_evolution_candidates(decision.get("evolution_candidates") or [])
    forbidden_text = json.dumps(cleaned, ensure_ascii=False).lower()
    if any(key.lower() in forbidden_text for key in _FORBIDDEN_EFFECT_KEYS):
        decision["rejected_external_action_attempt"] = True
    return decision


def _normalize_relationship_touch_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed_roles = {"boss", "manager", "teacher"}
    allowed_types = {
        "care", "encouragement", "thanks", "light_chat", "record_relief",
        "material_support", "manager_assist", "owner_business", "owner_progress", "presence_report",
    }
    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = _limit(item.get("target_role") or item.get("role"), 40)
        if role not in allowed_roles:
            continue
        touch_type = _limit(item.get("touch_type") or item.get("type"), 40)
        if touch_type not in allowed_types:
            touch_type = "care" if role == "teacher" else ("manager_assist" if role == "manager" else "presence_report")
        message = _limit(item.get("message") or item.get("content") or item.get("suggested_message"), 700)
        reason = _limit(item.get("reason") or item.get("why"), 500)
        if not message or not reason or _relationship_touch_text_unsafe(message, role=role):
            continue
        normalized.append({
            "target_role": role,
            "target_user_id": _limit(item.get("target_user_id") or item.get("user_id"), 120),
            "target_name": _limit(item.get("target_name") or item.get("name"), 80),
            "touch_type": touch_type,
            "message": message,
            "reason": reason,
            "value": _limit(item.get("value") or item.get("expected_value"), 500),
            "work_related": bool(item.get("work_related")),
            "private_emotional_support": bool(item.get("private_emotional_support")),
            "requires_authorization": item.get("requires_authorization", role != "boss") is not False,
            "external_send_allowed": bool(item.get("external_send_allowed")),
            "suggested_send_at": _limit(item.get("suggested_send_at"), 80),
        })
    return normalized[:6]


def _relationship_touch_text_unsafe(message: str, *, role: str) -> bool:
    text = str(message or "")
    forbidden = (
        "家长已发送", "已通知家长", "发给家长", "批量派", "扣工资", "改工资",
        "绩效结论", "删除", "修改权限", "责任绑定", "必须回复", "马上回复",
        "不回复就", "监控", "老板让我盯着你", "汇报给老板",
    )
    if any(term in text for term in forbidden):
        return True
    if role in {"teacher", "manager"} and any(term in text for term in ("工资处罚", "扣绩效", "通报老板")):
        return True
    return False


def _normalize_agent_delegation_decisions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed = {"adopted", "partially_adopted", "rejected", "needs_more_evidence", "deferred"}
    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        delegation_id = _limit(item.get("delegation_id"), 120)
        decision = _limit(item.get("main_hermes_decision") or item.get("decision"), 40)
        note = _limit(item.get("decision_note") or item.get("reason") or item.get("summary"), 1000)
        if not delegation_id or decision not in allowed or not note:
            continue
        normalized.append({
            "delegation_id": delegation_id,
            "main_hermes_decision": decision,
            "decision_note": note,
            "adopted_points": _list_any(item.get("adopted_points"), 8),
            "rejected_points": _list_any(item.get("rejected_points"), 8),
        })
        break
    return normalized


def _normalize_evolution_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        candidate = normalize_evolution_candidate(item)
        if not candidate.get("summary"):
            continue
        normalized.append(candidate)
        if len(normalized) >= 6:
            break
    return normalized


def _fallback_term_state(store: TuoguanStore, timestamp: datetime) -> dict[str, Any]:
    try:
        from .wakeup_v2 import _term_state

        return _term_state(store, timestamp)
    except Exception:
        return {}


def normalize_employee_decision_for_materials(decision: dict[str, Any], materials: dict[str, Any]) -> dict[str, Any]:
    term_state = materials.get("term_state") if isinstance(materials, dict) else {}
    service_relations_deferred = (
        isinstance(term_state, dict)
        and str(term_state.get("service_relation_policy") or "") == "defer_until_new_term"
    )
    normalized = deepcopy(decision)
    latest_owner_contact_at = _latest_owner_contact_at(materials)
    if service_relations_deferred:
        for field in ("employee_summary", "institution_understanding", "goal_progress_view"):
            if isinstance(normalized.get(field), str):
                normalized[field] = _rewrite_deferred_relation_text(normalized[field])
    for update in normalized.get("work_item_updates") or []:
        if not isinstance(update, dict):
            continue
        if service_relations_deferred:
            _apply_deferred_service_relation_boundary(update)
        if latest_owner_contact_at and _work_update_mentions_owner(update):
            update["last_human_contact_at"] = latest_owner_contact_at
    raw_owner_attention_candidates = normalized.get("boss_attention_candidates") or []
    normalized["boss_attention_candidates"] = _filter_owner_attention_candidates(
        raw_owner_attention_candidates,
        service_relations_deferred=service_relations_deferred,
    )
    normalized["questions_to_humans"] = _filter_human_questions(
        normalized.get("questions_to_humans") or [],
        service_relations_deferred=service_relations_deferred,
    )
    if not raw_owner_attention_candidates:
        normalized["boss_attention_candidates"] = _bridge_owner_questions_to_attention_candidates(
            normalized,
            materials,
            service_relations_deferred=service_relations_deferred,
        )
        normalized["boss_attention_candidates"] = _bridge_owner_confirmation_text_to_attention_candidates(
            normalized,
            materials,
            service_relations_deferred=service_relations_deferred,
        )
    if not normalized.get("boss_attention_candidates"):
        for field in ("employee_summary", "institution_understanding", "goal_progress_view"):
            if isinstance(normalized.get(field), str):
                normalized[field] = _rewrite_unmaterialized_owner_reply(normalized[field])
        for update in normalized.get("work_item_updates") or []:
            if not isinstance(update, dict):
                continue
            for field in ("focus_summary", "update_text", "owner_escalation_reason", "value_progress_note"):
                if isinstance(update.get(field), str):
                    update[field] = _rewrite_unmaterialized_owner_reply(update[field])
            if isinstance(update.get("next_actions"), list):
                update["next_actions"] = [
                    _rewrite_unmaterialized_owner_reply(str(item)) if isinstance(item, str) else item
                    for item in update.get("next_actions") or []
                ][:8]
        for entry in normalized.get("value_progress_entries") or []:
            if not isinstance(entry, dict):
                continue
            for field in ("discovered", "hermes_action", "human_action", "outcome"):
                if isinstance(entry.get(field), str):
                    entry[field] = _rewrite_unmaterialized_owner_reply(entry[field])
    return normalized


def _filter_owner_attention_candidates(candidates: Any, *, service_relations_deferred: bool) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for item in candidates if isinstance(candidates, list) else []:
        if not isinstance(item, dict):
            continue
        focus_key = str(item.get("focus_key") or "")
        reason = str(item.get("reason") or "")
        message = str(item.get("message") or item.get("content") or item.get("question") or "")
        if service_relations_deferred and _is_deferred_service_relation_attention(focus_key, reason, message):
            continue
        if not _owner_attention_candidate_is_safe(focus_key, reason, message):
            continue
        filtered.append(item)
    return filtered[:4]


def _filter_human_questions(questions: Any, *, service_relations_deferred: bool) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for item in questions if isinstance(questions, list) else []:
        if not isinstance(item, dict):
            continue
        ask_role = str(item.get("ask_role") or item.get("target_role") or "")
        reason = str(item.get("reason") or "")
        question = str(item.get("question") or item.get("summary") or "")
        if service_relations_deferred and _is_deferred_service_relation_attention(ask_role, reason, question):
            continue
        filtered.append(item)
    return filtered[:8]


def _bridge_owner_questions_to_attention_candidates(
    decision: dict[str, Any],
    materials: dict[str, Any],
    *,
    service_relations_deferred: bool,
) -> list[dict[str, Any]]:
    existing = list(decision.get("boss_attention_candidates") or [])
    if existing:
        return existing[:4]
    bridged: list[dict[str, Any]] = []
    for item in decision.get("questions_to_humans") or []:
        if not isinstance(item, dict) or not _question_targets_owner(item):
            continue
        question = _limit(item.get("question") or item.get("summary"), 400)
        reason = _limit(item.get("reason") or item.get("why") or "Hermes 明确提出需要老板确认的事实。", 400)
        focus_key = _attention_focus_key_for_question(item, decision, materials)
        next_action = _attention_next_action_for_question(decision)
        if not focus_key or not question:
            continue
        if service_relations_deferred and _is_deferred_service_relation_attention(focus_key, reason, question):
            continue
        message = (
            f"金总，我推进当前事项时有一个需要你确认的点。\n"
            f"卡点：{reason}\n"
            f"需要你确认：{question}\n"
            f"确认后：{next_action}"
        )
        if not _owner_attention_candidate_is_safe(focus_key, reason, message):
            continue
        bridged.append({
            "focus_key": focus_key,
            "reason": reason,
            "message": _limit(message, 900),
            "urgency": _limit(item.get("urgency") or "normal", 40),
            "bridged_from": "questions_to_humans",
            "needed_facts": [question],
        })
        break
    return bridged


def _bridge_owner_confirmation_text_to_attention_candidates(
    decision: dict[str, Any],
    materials: dict[str, Any],
    *,
    service_relations_deferred: bool,
) -> list[dict[str, Any]]:
    existing = list(decision.get("boss_attention_candidates") or [])
    if existing:
        return existing[:4]
    text = _owner_confirmation_source_text(decision)
    if not text:
        return []
    focus_key = _attention_focus_key_for_question({}, decision, materials)
    if not focus_key:
        return []
    if service_relations_deferred and _is_deferred_service_relation_attention(focus_key, text, text):
        return []
    reason = "当前阶段需要老板确认是否进入下一步；否则我只能继续内部准备，不能把准备结果当成已获授权。"
    next_action = "我会整理下一步候选材料和审核要点，作为老板审核材料继续推进。"
    question = "是否同意我按当前分析进入下一步准备，并把需要你审核的候选材料整理出来？"
    message = (
        f"金总，我推进当前事项时需要你确认是否进入下一步。\n"
        f"卡点：{reason}\n"
        f"需要你确认：{question}\n"
        f"确认后：{next_action}"
    )
    if not _owner_attention_candidate_is_safe(focus_key, reason, message):
        return []
    return [{
        "focus_key": focus_key,
        "reason": reason,
        "message": _limit(message, 900),
        "urgency": "normal",
        "bridged_from": "owner_confirmation_text",
        "needed_facts": [question],
    }]


def _owner_confirmation_source_text(decision: dict[str, Any]) -> str:
    fields = [
        _limit(decision.get("goal_progress_view"), 700),
        _limit(decision.get("employee_summary"), 700),
    ]
    for update in decision.get("work_item_updates") or []:
        if not isinstance(update, dict):
            continue
        fields.append(_limit(update.get("update_text"), 700))
        fields.append(_limit(update.get("focus_summary"), 700))
        actions = update.get("next_actions")
        if isinstance(actions, list):
            fields.extend(_limit(action, 240) for action in actions[:4])
    for text in fields:
        if not text:
            continue
        if _has_explicit_owner_confirmation_need(text):
            return text
    return ""


def _has_explicit_owner_confirmation_need(text: str) -> bool:
    return any(term in text for term in (
        "待老板确认",
        "等待老板确认",
        "请老板确认",
        "请求老板确认",
        "待金总确认",
        "等待金总确认",
        "请金总确认",
        "待您确认",
        "需要您确认",
        "请求下一步指示",
        "请求下一步确认",
        "待老板确认后",
        "老板确认后",
        "金总确认后",
    ))


def _question_targets_owner(item: dict[str, Any]) -> bool:
    target = " ".join(str(item.get(key) or "") for key in ("ask_role", "target_role", "target", "ask_who"))
    text = f"{target} {item.get('reason') or ''} {item.get('question') or item.get('summary') or ''}"
    return any(term in text for term in ("boss", "owner", "老板", "金总", "金文杰"))


def _attention_focus_key_for_question(item: dict[str, Any], decision: dict[str, Any], materials: dict[str, Any]) -> str:
    direct = _safe_focus(item.get("focus_key") or item.get("related_focus_key"))
    if direct:
        return direct
    update_keys = [
        _safe_focus(update.get("focus_key"))
        for update in (decision.get("work_item_updates") or [])
        if isinstance(update, dict) and _safe_focus(update.get("focus_key"))
    ]
    if len(set(update_keys)) == 1:
        return update_keys[0]
    work_items = ((materials.get("autonomous_work_items") or {}).get("items") or []) if isinstance(materials.get("autonomous_work_items"), dict) else []
    material_keys = [
        _safe_focus(item.get("focus_key"))
        for item in work_items
        if isinstance(item, dict) and str(item.get("status") or "") in {"active", "waiting", "blocked"} and _safe_focus(item.get("focus_key"))
    ]
    if len(set(material_keys)) == 1:
        return material_keys[0]
    return ""


def _attention_next_action_for_question(decision: dict[str, Any]) -> str:
    for update in decision.get("work_item_updates") or []:
        if not isinstance(update, dict):
            continue
        actions = update.get("next_actions")
        if isinstance(actions, list):
            for action in actions:
                text = _limit(action, 180)
                if text:
                    return text
        note = _limit(update.get("value_progress_note") or update.get("focus_summary"), 180)
        if note:
            return note
    return "我会把你的确认写回当前工作项，并继续推进下一步分析或准备材料。"


def materialize_employee_decision(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    decision: dict[str, Any],
    timestamp: datetime,
    materials: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    writes: list[dict[str, Any]] = []
    with authorized_system_write(store.data_dir, job_name="autonomous_employee_loop_v1", allowed_files=_ALLOWED_FILES):
        op_prefix = "system:autonomous_employee_loop:" + timestamp.strftime("%Y%m%d%H%M%S")
        term_state = (materials or {}).get("term_state") if isinstance(materials, dict) else {}
        service_relations_deferred = (
            isinstance(term_state, dict)
            and str(term_state.get("service_relation_policy") or "") == "defer_until_new_term"
        )
        latest_owner_contact_at = _latest_owner_contact_at(materials)
        cadence_mode = str(((materials or {}).get("work_cadence") or {}).get("mode") or "")
        for idx, candidate in enumerate(decision.get("evolution_candidates") or []):
            if not isinstance(candidate, dict):
                continue
            res = submit_self_evolution_event(
                store,
                identity=identity,
                operation_id=f"{op_prefix}:evolution:{idx}",
                candidate_type=_limit(candidate.get("candidate_type"), 80),
                summary=_limit(candidate.get("summary"), 700),
                evidence=_list_any(candidate.get("evidence"), 8),
                risk_level=_limit(candidate.get("risk_level"), 40),
                status=_limit(candidate.get("status"), 40),
                target_store=_limit(candidate.get("target_store"), 120),
                proposed_effect=_limit(candidate.get("proposed_effect"), 700),
                next_effect=_limit(candidate.get("next_effect"), 700),
                applies_to_user_id=_limit(candidate.get("applies_to_user_id"), 120),
                applies_to_role=_limit(candidate.get("applies_to_role"), 40),
                review_required_by=_limit(candidate.get("review_required_by"), 80),
                source_text=_limit(candidate.get("source_text") or decision.get("employee_summary") or "", 1000),
                source_message_id=f"autonomous_employee_loop:{timestamp.strftime('%Y%m%d%H%M%S')}",
                cadence_mode=cadence_mode,
            )
            writes.append(_write_result("self_evolution_event", res))
        for idx, obs in enumerate(decision.get("observations") or []):
            event_type = _limit(obs.get("event_type") or "autonomous_employee_observation", 80)
            if event_type in _NON_MATERIAL_OBSERVATION_TYPES or event_type.startswith("owner_"):
                continue
            event_text = _limit(obs.get("event_text") or obs.get("summary") or obs.get("text"), 1000)
            if any(label in event_text for label in ("老板", "金总")):
                continue
            if not event_text:
                continue
            res = submit_business_event(
                store, identity=identity, event_type=event_type,
                event_text=event_text, operation_id=f"{op_prefix}:event:{idx}", related_objects=_list_any(obs.get("related_objects"), 8),
                occurred_at=timestamp.isoformat(timespec="seconds"), source_text=decision.get("employee_summary") or "model-led observation",
                source_message_id=f"autonomous_employee_loop:{timestamp.strftime('%Y%m%d%H%M%S')}",
            )
            writes.append(_write_result("business_event", res))
        for idx, gap in enumerate(decision.get("institution_fact_gaps") or []):
            gap_text = _limit(gap.get("gap_text") or gap.get("text") or gap.get("reason"), 1000)
            gap_key = _limit(gap.get("gap_key") or "institution_fact_gap", 120)
            if not gap_text or not gap_key:
                continue
            if service_relations_deferred and _is_deferred_service_relation_attention(gap_key, gap_text, gap_text):
                continue
            res = submit_fact_gap_candidate(
                store,
                identity=identity,
                gap_key=gap_key,
                gap_text=gap_text,
                fact_owner_role=_limit(gap.get("ask_role") or "boss", 80),
                operation_id=f"{op_prefix}:institution_gap:{idx}",
                suggested_question=_limit(gap.get("suggested_question") or gap.get("question") or gap.get("ask_candidate"), 300),
                target_user_id=_limit(gap.get("target_user_id"), 120),
                target_name=_limit(gap.get("target_name"), 80),
                impact=_limit(gap.get("impact"), 400),
                target_time=_limit(gap.get("target_time"), 80),
                urgency=_limit(gap.get("urgency") or "normal", 40),
                related_objects=_list_any(gap.get("related_objects"), 8),
                source_text=decision.get("institution_understanding") or decision.get("employee_summary") or "",
                source_message_id=f"autonomous_employee_loop:{timestamp.strftime('%Y%m%d%H%M%S')}",
            )
            writes.append(_write_result("fact_gap_candidate", res))
        for idx, update in enumerate(decision.get("work_item_updates") or []):
            if service_relations_deferred:
                _apply_deferred_service_relation_boundary(update)
            focus_key = _limit(update.get("focus_key"), 160)
            title = _limit(update.get("title"), 160) or focus_key
            focus_summary = _limit(update.get("focus_summary"), 1000) or _limit(update.get("update_text"), 1000)
            if not focus_key or not title or not focus_summary:
                continue
            common = dict(
                identity=identity,
                operation_id=f"{op_prefix}:work:{idx}",
                focus_key=focus_key,
                status=str(update.get("status") or "active"),
                focus_summary=focus_summary,
                source_text=decision.get("employee_summary") or "model-led work update",
                source_message_id=f"autonomous_employee_loop:{timestamp.strftime('%Y%m%d%H%M%S')}",
            )
            optional_list_fields = (
                "execution_plan",
                "next_actions",
                "progress_evidence",
                "confirmed_facts",
                "pending_judgements",
                "completed_actions",
                "blocked_by",
                "ask_candidates",
            )
            for field in optional_list_fields:
                if field in update:
                    common[field] = _list_any(update.get(field), 8)
            if "current_phase" in update:
                common["current_phase"] = _dict(update.get("current_phase"))
            if "current_waiting" in update:
                common["current_waiting"] = _dict(update.get("current_waiting"))
            for field, length in (
                ("owner_escalation_reason", 500),
                ("value_progress_note", 500),
                ("next_attention_at", 80),
            ):
                if field in update:
                    common[field] = _limit(update.get(field), length)
            if "last_human_contact_at" in update:
                proposed_contact = _limit(update.get("last_human_contact_at"), 80)
                raw_owner_events = (
                    ((materials or {}).get("recent_owner_messages") or {}).get("events") or []
                    if isinstance((materials or {}).get("recent_owner_messages"), dict)
                    else []
                )
                verified_contact_times = {
                    str(event.get("occurred_at") or "")
                    for event in raw_owner_events
                    if isinstance(event, dict)
                }
                if proposed_contact in verified_contact_times:
                    common["last_human_contact_at"] = proposed_contact
            if latest_owner_contact_at and _work_update_mentions_owner(update):
                common["last_human_contact_at"] = latest_owner_contact_at
            res = update_hermes_work_item(store, update_text=_limit(update.get("update_text"), 1000) or focus_summary, **common)
            if not res.get("ok") and res.get("error") == "work_item_not_found":
                res = submit_hermes_work_item(store, title=title, **common)
            writes.append(_write_result("work_item", res))
        review_text = _render_self_review_text(decision)
        if review_text and 19 <= timestamp.astimezone().hour < 23:
            review = decision.get("self_review") if isinstance(decision.get("self_review"), dict) else {}
            score_res = submit_employee_self_review(
                store,
                identity=identity,
                operation_id=f"{op_prefix}:employee_scorecard",
                review_date=timestamp.date().isoformat(),
                institution_understanding=_limit(decision.get("institution_understanding") or review.get("institution_understanding"), 800),
                goal_progress=_limit(decision.get("goal_progress_view") or review.get("goal_progress"), 800),
                teacher_support=_limit(review.get("teacher_support"), 800),
                student_service_evidence=_limit(review.get("student_service_evidence"), 800),
                risk_detection=_limit(review.get("risk_detection"), 800),
                business_opportunity=_limit(review.get("business_opportunity"), 800),
                learning_growth=_limit(review.get("what_i_learned") or review.get("learning_growth"), 800),
                tomorrow_focus=_limit(review.get("tomorrow_focus"), 800),
                blocked_by=_list_any(review.get("blocked_by") or review.get("what_is_missing"), 8),
                quality_score=_score_value(review.get("quality_score")),
                source_text=review_text,
                source_message_id=f"autonomous_employee_loop:{timestamp.strftime('%Y%m%d%H%M%S')}",
            )
            writes.append(_write_result("employee_scorecard", score_res))
            if score_res.get("state_changed", True):
                res = submit_action_execution(
                    store, identity=identity, action_type="autonomous_self_review", action_summary="Hermes completed one model-led internal employee self-review.",
                    status="success", operation_id=f"{op_prefix}:self_review", idempotency_key=f"autonomous_employee_loop:{timestamp.strftime('%Y%m%d')}",
                    receipt={"external_actions_taken": []}, result_text=review_text, source_text=decision.get("employee_summary") or review_text,
                    source_message_id=f"autonomous_employee_loop:{timestamp.strftime('%Y%m%d%H%M%S')}",
                )
                writes.append(_write_result("self_review", res))
        for idx, entry in enumerate(decision.get("value_progress_entries") or []):
            if not entry.get("discovered") or not entry.get("hermes_action") or not _is_real_value_progress(entry):
                continue
            res = submit_value_progress_entry(
                store,
                identity=identity,
                subject=_limit(entry.get("subject"), 160),
                discovered=_limit(entry.get("discovered"), 1000),
                hermes_action=_limit(entry.get("hermes_action"), 1000),
                operation_id=f"{op_prefix}:value_progress:{idx}",
                human_action=_limit(entry.get("human_action"), 1000),
                outcome=_limit(entry.get("outcome"), 1000),
                evidence=_list_any(entry.get("evidence"), 8),
                attribution=_limit(entry.get("attribution") or "participated", 40),
                source_text=decision.get("employee_summary") or "",
                source_message_id=f"autonomous_employee_loop:{timestamp.strftime('%Y%m%d%H%M%S')}",
            )
            writes.append(_write_result("value_progress", res))
        for idx, item in enumerate(decision.get("agent_delegation_decisions") or []):
            delegation_id = _limit(item.get("delegation_id"), 120)
            main_decision = _limit(item.get("main_hermes_decision"), 40)
            note = _limit(item.get("decision_note"), 1000)
            if not delegation_id or not main_decision or not note:
                continue
            res = update_agent_delegation_decision(
                store,
                identity=identity,
                delegation_id=delegation_id,
                main_hermes_decision=main_decision,
                decision_note=note,
                operation_id=f"{op_prefix}:agent_decision:{idx}",
                adopted_points=_list_any(item.get("adopted_points"), 8),
                rejected_points=_list_any(item.get("rejected_points"), 8),
                source_text=decision.get("employee_summary") or note,
                source_message_id=f"autonomous_employee_loop:{timestamp.strftime('%Y%m%d%H%M%S')}",
            )
            writes.append(_write_result("agent_delegation_decision", res))
        relationship_writes = _materialize_relationship_touch_candidates(
            store,
            identity=identity,
            decision=decision,
            timestamp=timestamp,
            op_prefix=op_prefix,
        )
        writes.extend(relationship_writes)
        reminder_writes = _materialize_owner_attention_candidates(
            store,
            identity=identity,
            decision=decision,
            timestamp=timestamp,
            op_prefix=op_prefix,
            term_state=term_state,
        )
        writes.extend(reminder_writes)
    return [row for row in writes if row.get("state_changed", True)]


def render_employee_loop_report(result: dict[str, Any]) -> str:
    decision = result.get("decision") or {}
    lines = [
        f"# Hermes autonomous employee review | {str(result.get('generated_at') or '')[:19]}", "",
        "Status: model participated in wakeup review; only internal state/evidence was saved. No messages, tasks, salary changes, or deletes.", "",
        "## Employee judgment", "",
        f"- {decision.get('employee_summary') or 'No summary.'}",
        f"- Institution understanding: {decision.get('institution_understanding') or 'No new judgment.'}",
        f"- Goal progress: {decision.get('goal_progress_view') or 'No new judgment.'}", "", "## Internal writes", "",
    ]
    writes = result.get("writes") or []
    if writes:
        for row in writes[:12]:
            lines.append(f"- {row.get('kind')}: {row.get('ok')} {row.get('message') or row.get('error') or ''}")
    else:
        lines.append("- No internal state changed this time.")
    questions = decision.get("questions_to_humans") or []
    lines.extend(["", "## Human confirmation candidates", ""])
    if questions:
        for item in questions[:8]:
            lines.append(f"- Ask {item.get('ask_role') or item.get('target_role') or 'human'}: {item.get('question') or item.get('summary') or item}")
    else:
        lines.append("- No new human question proposed by the model.")
    owner_candidates = decision.get("boss_attention_candidates") or []
    lines.extend(["", "## Owner attention candidates", ""])
    if owner_candidates:
        for item in owner_candidates[:4]:
            lines.append(f"- {item.get('focus_key')}: {item.get('message')}")
    else:
        lines.append("- No owner reminder proposed by the model.")
    evolution = decision.get("evolution_candidates") or []
    lines.extend(["", "## Self-evolution candidates", ""])
    if evolution:
        for item in evolution[:6]:
            lines.append(f"- {item.get('candidate_type')}: {item.get('summary')} ({item.get('risk_level')}/{item.get('status')})")
    else:
        lines.append("- No new learning candidate proposed by the model.")
    lines.extend(["", "## Boundary", "", "- This is not a Router or fixed workflow; model judgment is saved as material.", "- External action still requires a real user channel, permission, audit, idempotency, and writeback verification."])
    return "\n".join(lines).rstrip() + "\n"


def _call_model_for_decision(materials: dict[str, Any]) -> dict[str, Any]:
    payload = _model_payload(materials)
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    errors: list[str] = []

    def request(cfg: dict[str, Any], active_messages: list[dict[str, str]], max_tokens: int) -> str:
        for attempt in range(2):
            response = httpx.post(
                f"{cfg['base_url'].rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
                json={
                    "model": cfg["model"],
                    "temperature": 0.2,
                    "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"},
                    "messages": active_messages,
                },
                timeout=float(cfg.get("timeout") or 60),
            )
            if response.status_code == 429 and attempt == 0:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = max(2.0, min(float(retry_after or 8), 15.0))
                except ValueError:
                    delay = 8.0
                time.sleep(delay)
                continue
            response.raise_for_status()
            return str(response.json()["choices"][0]["message"]["content"] or "")
        raise RuntimeError("model_request_exhausted")

    for cfg in _load_model_configs()[:2]:
        try:
            content = request(cfg, messages, 4000)
            try:
                return json.loads(_json_text(content))
            except json.JSONDecodeError as first_exc:
                retry_messages = [
                    messages[0],
                    messages[1],
                    {
                        "role": "user",
                        "content": (
                            "Your prior JSON was truncated. Return a fresh compact JSON object under 2200 Chinese characters. "
                            "Use empty arrays for unchanged sections. Include at most one work_item_update, two gaps, "
                            "one owner candidate, and no copied student lists or historical prose."
                        ),
                    },
                ]
                retry_content = request(cfg, retry_messages, 2500)
                try:
                    return json.loads(_json_text(retry_content))
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        "model_returned_invalid_json:"
                        + str(first_exc)[:60]
                        + ";retry:"
                        + str(exc)[:60]
                    ) from exc
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            errors.append(_safe_error(exc))
    raise RuntimeError("all_model_providers_failed:" + "|".join(errors[-2:]))


_SYSTEM_PROMPT = """You are Xiaoyou (小优), the public-facing digital employee for a tutoring institution; Hermes is only the internal product/architecture name. You were woken by a clock tick.
When writing owner/manager/teacher-facing messages, self-identify as 小优. Use Hermes only when discussing internal architecture, code, or technical implementation.
Think like an employee: understand the institution, inspect goals, notice missing facts, decide whether to continue, wait, ask a human, or stop.
The handbook is guidance, not a fixed workflow. Do not claim an external action happened.
You cannot contact parents, assign new teacher tasks, change salary, delete data, close safety events, or change permissions in this wakeup.
proactive_work_radar is the handbook-based employee map. Use it before deciding that there is nothing to do: it tells you what Xiaoyou should understand about the institution, organization, student service relations, operating rules, teacher work habits, goals, service evidence, risk, business opportunity, and reflection. It is read-only material, not a Router, and does not force you to ask every listed question.
If owner_attention_policy.allowed_now is true and a real goal is blocked by missing owner facts, propose one boss_attention_candidate. If you write that the next stage is waiting for owner confirmation, boss confirmation, or next-step instruction, you must also put one concrete boss_attention_candidate. This is only a candidate for the owner/boss, not teachers or parents. If you put an owner/boss question in questions_to_humans, also put the same concrete question in boss_attention_candidates unless it is unsafe or deferred by term policy.
Do not answer or record that Hermes "cannot proactively ask" as a general rule. Correct boundary: Hermes may ask the current conversation participant for clarification; the autonomous scheduler may queue daytime low-frequency owner attention; and it may queue manager/teacher questions when the missing fact belongs to that staff member, the person is whitelisted, and the message is work-related. Parents remain out of scope.
Hermes also has employee responsibilities: complete institution understanding, push owner goals, support teachers, protect student service evidence, detect risks, find business opportunities, and keep learning.
Public internet learning is only advice material with sources. Never turn it into confirmed institution fact without owner review.
term_state, deferred_items, roster confidence, recent_owner_messages, and attention_threads are current evidence materials. If service_relation_policy is defer_until_new_term, the historical roster is not a confirmed new-term roster. Treat service type, main teacher, and new-term roster as deferred future confirmation material, not as a current blocker for historical renewal analysis, communication coverage analysis, risk framing, or owner summary preparation. You may analyze and prepare questions, but do not propose another proactive roster/service-relation reminder before the confirmation window unless a recent owner message explicitly asks to handle it early.
Process the latest owner messages, not only the first few in the window. If the owner provides missing facts, says a safety item was only test data, asks Hermes to check tomorrow morning, or authorizes boss-only reminders while forbidding teacher/parent outreach, reflect that as current work-item material when state changes.
operating_evidence contains compact read-only business evidence available at this wakeup. Use it to do concrete analysis when sufficient. Do not write "started analysis" or "will analyze" as progress evidence unless you actually derived a finding from supplied evidence. Missing source data is a fact gap, not completed work.
An owner message is only a raw fact. Decide yourself whether it answers an attention thread, whether it is sufficient, or whether it is unrelated. Do not infer a reply merely because a message exists.
multi_agent_brief contains only internal advisory materials. If it has pending completed results, decide on at most one delegation and explain whether you adopt, partially adopt, reject, need more evidence, or defer it. Do not let a sub-agent result become a business fact unless you, the main Hermes, adopt it with evidence.
relationship_touch_policy controls proactive presence and staff fact requests. Boss messages may be sent only when policy allows and there is real evidence/value. Teacher and manager messages may be sent only when policy allows, target_user_id is known, work_related is true, private_emotional_support is false, and the message asks for one concrete task/record/operation fact. For teachers, warm presence can still be recorded as a candidate, but private emotional chat is not auto-sent and does not become boss performance material.
Use self_evolution_brief as Xiaoyou's experience memory for the next day. In evening_review and night_read_only_review, output concise evolution_candidates when the materials show a real lesson: a low-risk workstyle preference already verified by a tool, a self-correction to avoid repeating, a tool failure to review, a handbook method candidate, a fact gap owner, tomorrow focus, or a multi-agent suggestion you adopted/rejected. Do not create candidates just to prove the wakeup ran.
No material change is a valid outcome. Do not create observations, gaps, work updates, self-reviews, or value entries just to prove the wakeup ran.
If a waiting branch says not_a_goal_blocker=true and there is no other blocker, keep the parent work item active; do not mark the entire goal waiting.
When materials conflict, the latest folded work item and term_state are authoritative for current status. Older goal reviews, counts, plans, and rosters remain historical evidence only. Never describe responsibility_confirmation as the current phase after the latest work item has moved to historical_analysis_and_preparation.
Keep the JSON under 2200 Chinese characters. Use empty arrays for unchanged sections. Include at most one work_item_update, two gaps, one owner candidate, and never copy student lists or long historical prose into the output.
Return only one JSON object with keys: employee_summary, institution_understanding, goal_progress_view, observations, work_item_updates, questions_to_humans, boss_attention_candidates, relationship_touch_candidates, institution_fact_gaps, value_progress_entries, agent_delegation_decisions, evolution_candidates, self_review, external_actions.
observations: array of objects with event_type and event_text.
work_item_updates: array of objects with focus_key, title, focus_summary, status, update_text, current_phase, next_actions, confirmed_facts, pending_judgements, current_waiting, blocked_by, ask_candidates, last_human_contact_at, next_contact_after, owner_escalation_reason, value_progress_note, next_attention_at.
questions_to_humans: array of objects with ask_role, reason, question, urgency.
boss_attention_candidates: array of objects with focus_key, reason, message, urgency. The message must say what Hermes is blocked on, what the owner should confirm, and what Hermes will do after confirmation. It must be a question, not a status report.
relationship_touch_candidates: array of objects with target_role, target_user_id, target_name, touch_type, message, reason, value, work_related, private_emotional_support, requires_authorization, external_send_allowed, suggested_send_at. touch_type may be care, encouragement, thanks, light_chat, record_relief, material_support, manager_assist, owner_business, owner_progress, or presence_report. For manager/teacher proactive fact requests, set work_related=true and external_send_allowed=true only when you are asking that person for one concrete task/record/operation fact.
institution_fact_gaps: array of objects with gap_key, gap_text, ask_role, target_time, urgency, related_objects.
value_progress_entries: array of objects with subject, discovered, hermes_action, human_action, outcome, evidence, attribution.
agent_delegation_decisions: array with at most one object per wakeup. Include delegation_id, main_hermes_decision, decision_note, adopted_points, rejected_points. Only decide on completed sub-agent results that are present in multi_agent_brief; never let the sub-agent decide for you.
evolution_candidates: array of objects with candidate_type, summary, evidence, risk_level, status, target_store, proposed_effect, next_effect, applies_to_user_id, applies_to_role, review_required_by, source_text. candidate_type must be one of person_preference_candidate, institution_fact_gap, self_correction, tool_failure_or_bug, handbook_method_candidate, tomorrow_focus, multi_agent_adoption. Low-risk candidates may guide future context; medium/high-risk candidates remain review material and must not change policy, salary, permissions, parent outreach, handbook, or institution rules.
self_review: object with what_i_checked, what_i_learned, what_is_missing, tomorrow_focus, quality_score, teacher_support, student_service_evidence, risk_detection, business_opportunity.
external_actions must always be an empty array. Do not include routing or tool-step fields. Do not invent numbers. If facts are missing, say what is missing and who should confirm it."""


def _load_model_configs() -> list[dict[str, Any]]:
    config_path = os.getenv("HERMES_CONFIG_PATH")
    if config_path:
        path = Path(config_path)
    else:
        path = None
        current = Path(__file__).resolve()
        for parent in current.parents:
            candidate = parent / "home-proddata" / "config.yaml"
            if candidate.exists():
                path = candidate
                break
        if path is None:
            path = Path("/opt/hermes-youyi-upgrade-0.19.0/home-proddata/config.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    candidates: list[dict[str, Any]] = []
    model_cfg = data.get("model") or {}
    if isinstance(model_cfg, dict):
        candidates.append(model_cfg)
    fallback = data.get("fallback_providers") or []
    if isinstance(fallback, str):
        try:
            fallback = json.loads(fallback)
        except json.JSONDecodeError:
            fallback = []
    if isinstance(fallback, list):
        candidates.extend(item for item in fallback if isinstance(item, dict))
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for candidate in candidates:
        model = str(candidate.get("model") or candidate.get("name") or "").strip()
        base_url = str(candidate.get("base_url") or "").strip()
        api_key = str(candidate.get("api_key") or "").strip()
        key = (base_url.rstrip("/"), model, api_key)
        if not (base_url and api_key and model) or key in seen:
            continue
        seen.add(key)
        normalized.append({
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "timeout": float(candidate.get("request_timeout_seconds") or 60),
        })
    if not normalized:
        raise ValueError("model_config_incomplete")
    return normalized


def _query_onboarding(store: TuoguanStore) -> dict[str, Any]:
    try:
        service = TuoguanToolService(store=store, platform="system", user_id="JinWenJie", user_name="Jin", chat_id="system:autonomous", session_key="system:autonomous")
        return service.query_institution_onboarding_gaps(program_id="regular_tuoguan")
    except Exception as exc:
        return {"ok": False, "error": "onboarding_query_failed", "message": _safe_error(exc)}


def _query_operating_evidence(store: TuoguanStore) -> dict[str, Any]:
    """Collect a small general evidence pack without choosing an action."""

    try:
        service = TuoguanToolService(
            store=store,
            platform="system",
            user_id="JinWenJie",
            user_name="金总",
            chat_id="system:autonomous:evidence",
            session_key="system:autonomous:evidence",
        )
        return {
            "ok": True,
            "operations_overview": service.query_operations_report(query_type="overview"),
            "open_safety_tasks": service.query_tasks(
                status="open",
                level="S",
                scope="all",
                write_focus=False,
            ),
            "historical_parent_communication_coverage": service.query_parent_communication_coverage(
                days=90,
                program_id="regular_tuoguan",
            ),
            "recent_record_coverage": service.query_weekly_record_coverage(
                days=14,
                program_id="regular_tuoguan",
            ),
            "material_rule": (
                "Read-only evidence only. The model decides relevance; historical "
                "roster metrics are not confirmed new-term facts."
            ),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": "operating_evidence_query_failed",
            "message": _safe_error(exc),
        }


def _system_identity() -> UserIdentity:
    return UserIdentity(platform="system", platform_user_id="autonomous_employee_loop", canonical_user_id="autonomous_employee_loop", person_name="Hermes autonomous employee", role="boss", approval_state="approved")


def _boundary() -> dict[str, bool]:
    return {"sends_parent_messages": False, "sends_teacher_messages": False, "sends_owner_messages": False, "creates_teacher_tasks": False, "changes_salary": False, "changes_permissions": False, "deletes_data": False, "stores_fixed_route": False, "limits_model": False}


def _model_payload(materials: dict[str, Any]) -> dict[str, Any]:
    onboarding = materials.get("onboarding_gaps") if isinstance(materials.get("onboarding_gaps"), dict) else {}
    goals = materials.get("active_goal_state") if isinstance(materials.get("active_goal_state"), dict) else {}
    work = materials.get("autonomous_work_items") if isinstance(materials.get("autonomous_work_items"), dict) else {}
    brief = materials.get("autonomous_work_brief") if isinstance(materials.get("autonomous_work_brief"), dict) else {}
    return {
        "timestamp": materials.get("timestamp"),
        "identity": materials.get("identity"),
        "mission": materials.get("mission"),
        "principles": materials.get("principles"),
        "materials_summary": materials.get("materials_summary"),
        "work_cadence": materials.get("work_cadence"),
        "owner_attention_policy": materials.get("owner_attention_policy"),
        "onboarding": {
            "rendered_text": onboarding.get("rendered_text") or onboarding.get("message"),
            "gap_count": onboarding.get("gap_count"),
            "gaps": _compact_for_model(onboarding.get("gaps") or onboarding.get("priority_questions") or [], max_chars=3000),
        },
        "goals": {
            "rendered_text": goals.get("rendered_text"),
            "goal_count": goals.get("goal_count"),
            "goals": _compact_for_model(goals.get("goals") or [], max_chars=5000),
        },
        "work": {
            "rendered_text": work.get("rendered_text"),
            "items": _compact_for_model(work.get("items") or [], max_chars=5000),
            "brief": _compact_for_model(brief, max_chars=3000),
        },
        "institution_understanding_state": _compact_for_model(materials.get("institution_understanding_state"), max_chars=5000),
        "proactive_work_radar": _compact_for_model(materials.get("proactive_work_radar"), max_chars=9000),
        "employee_scorecard": _compact_for_model(materials.get("employee_scorecard"), max_chars=3000),
        "industry_learning_candidates": _compact_for_model(materials.get("industry_learning_candidates"), max_chars=3000),
        "external_learning_brief": _compact_for_model(materials.get("external_learning_brief"), max_chars=3000),
        "value_progress_ledger": _compact_for_model(materials.get("value_progress_ledger"), max_chars=3000),
        "attention_threads": _compact_for_model(materials.get("attention_threads"), max_chars=3500),
        "multi_agent_brief": _compact_for_model(materials.get("multi_agent_brief"), max_chars=3500),
        "relationship_touch_policy": _compact_for_model(materials.get("relationship_touch_policy"), max_chars=2500),
        "relationship_touch_candidates": _compact_for_model(materials.get("relationship_touch_candidates"), max_chars=3500),
        "self_evolution_brief": _compact_for_model(materials.get("self_evolution_brief"), max_chars=5000),
        "recent_owner_messages": _compact_for_model(materials.get("recent_owner_messages"), max_chars=3500),
        "operating_evidence": _compact_for_model(materials.get("operating_evidence"), max_chars=8000),
        "term_state": _compact_for_model(materials.get("term_state"), max_chars=2500),
        "deferred_items": _compact_for_model(materials.get("deferred_items"), max_chars=2500),
        "new_term_readiness": _compact_for_model(materials.get("new_term_readiness"), max_chars=2500),
        "wakeup_requests": _compact_for_model(materials.get("wakeup_requests"), max_chars=2500),
        "business_events": _compact_for_model(materials.get("business_events"), max_chars=2500),
        "action_executions": _compact_for_model(materials.get("action_executions"), max_chars=2500),
        "patrol_counts": materials.get("patrol_counts"),
        "allowed_internal_outputs": materials.get("allowed_internal_outputs"),
        "forbidden_external_outputs": materials.get("forbidden_external_outputs"),
    }


def _work_cadence(timestamp: datetime) -> dict[str, Any]:
    hour = int(timestamp.astimezone().hour)
    if 8 <= hour < 19:
        mode = "daytime_goal_progress"
        owner_allowed = True
        focus = "目标推进、等待恢复、缺事实判断、必要时低频提醒老板本人。"
    elif 19 <= hour < 23:
        mode = "evening_review"
        owner_allowed = False
        focus = "轻量复盘、自审、明日计划；不在安静时段外发提醒，但要记录明天该问谁。"
    else:
        mode = "night_read_only_review"
        owner_allowed = False
        focus = "只读夜间复查和风险观察；不在夜间外发提醒，但要保留缺失信息问题。"
    return {
        "mode": mode,
        "local_hour": hour,
        "owner_attention_allowed": owner_allowed,
        "cadence_focus": focus,
        "does_not_limit_model_reasoning": True,
    }


def _materialize_owner_attention_candidates(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    decision: dict[str, Any],
    timestamp: datetime,
    op_prefix: str,
    term_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    writes: list[dict[str, Any]] = []
    cadence = _work_cadence(timestamp)
    if not cadence.get("owner_attention_allowed"):
        return writes
    owner_id = _owner_user_id(store)
    if not owner_id:
        return writes
    outbox = store.read_json(_NOTIFICATION_OUTBOX_FILE, [])
    if not isinstance(outbox, list):
        outbox = []
    existing_ids = {str(item.get("id") or "") for item in outbox if isinstance(item, dict)}
    stamp = timestamp.isoformat(timespec="seconds")
    day = timestamp.strftime("%Y%m%d")
    queued_count = 0
    queued_rows: list[dict[str, Any]] = []
    for idx, candidate in enumerate(decision.get("boss_attention_candidates") or []):
        if queued_count >= 1:
            break
        focus_key = _safe_focus(candidate.get("focus_key"))
        message = _safe_owner_attention_text(candidate.get("message"))
        reason = _limit(candidate.get("reason"), 500)
        if not focus_key or not reason or not message:
            continue
        if not _owner_attention_candidate_is_safe(focus_key, reason, message):
            continue
        if (
            str((term_state or {}).get("service_relation_policy") or "") == "defer_until_new_term"
            and _is_deferred_service_relation_attention(focus_key, reason, message)
        ):
            continue
        notification_id = f"autonomous_owner_attention:{day}:{focus_key}"
        attention_id = f"attention:{day}:{focus_key}"
        duplicate_reason = _owner_attention_duplicate_reason(
            outbox,
            owner_id=owner_id,
            focus_key=focus_key,
            message=message,
            reason=reason,
            now=timestamp,
        )
        if notification_id in existing_ids or duplicate_reason:
            writes.append(_write_result("owner_attention_deduplicated", {
                "ok": True,
                "state_changed": True,
                "message": duplicate_reason or "same focus already queued today",
            }))
            continue
        row = {
            "id": notification_id,
            "status": "pending",
            "delivery_mode": "direct_wecom",
            "notification_type": "autonomous_owner_attention",
            "task_id": f"autonomous:{focus_key}",
            "role": "boss",
            "action": "owner_attention",
            "target_user_id": owner_id,
            "recipient_user_id": owner_id,
            "to_user_id": owner_id,
            "touser": owner_id,
            "content": message,
            "summary": reason,
            "focus_key": focus_key,
            "attention_id": attention_id,
            "created_at": stamp,
            "attempt_count": 0,
            "auto_effects": {
                "sends_parent_messages": False,
                "sends_teacher_messages": False,
                "creates_teacher_tasks": False,
                "changes_salary": False,
                "changes_permissions": False,
                "changes_router": False,
                "forces_next_action": False,
            },
        }
        outbox.append(row)
        queued_rows.append(deepcopy(row))
        existing_ids.add(notification_id)
        queued_count += 1
        attention_res = submit_attention_thread(
            store,
            identity=identity,
            focus_key=focus_key,
            question_text=message,
            operation_id=f"{op_prefix}:attention_thread:{idx}",
            target_user_id=owner_id,
            needed_facts=_list_any(candidate.get("needed_facts") or [reason], 8),
            status="queued",
            attention_id=attention_id,
            source_text=decision.get("employee_summary") or reason,
            source_decision_summary=decision.get("goal_progress_view") or decision.get("employee_summary") or reason,
            source_message_id=f"autonomous_employee_loop:{timestamp.strftime('%Y%m%d%H%M%S')}",
        )
        writes.append(_write_result("attention_thread_queued", attention_res))
        res = submit_action_execution(
            store,
            identity=identity,
            action_type="owner_attention_queued",
            action_summary="Hermes queued one low-frequency owner attention note for a blocked autonomous work focus.",
            status="success",
            operation_id=f"{op_prefix}:owner_attention:{idx}",
            related_work_item_id="",
            idempotency_key=notification_id,
            receipt={"target_user_id": owner_id, "queued_notification_id": notification_id, "external_actions_taken": ["owner_attention_queued"]},
            result_text=message,
            source_text=decision.get("employee_summary") or "",
            source_message_id=f"autonomous_employee_loop:{timestamp.strftime('%Y%m%d%H%M%S')}",
        )
        writes.append(_write_result("owner_attention_queued", res))
    if queued_rows:
        def append_queued(existing: Any) -> Any:
            existing = existing if isinstance(existing, list) else []
            ids = {str(item.get("id") or "") for item in existing if isinstance(item, dict)}
            changed = False
            for row in queued_rows:
                row_id = str(row.get("id") or "")
                if not row_id or row_id in ids:
                    continue
                existing.append(deepcopy(row))
                ids.add(row_id)
                changed = True
            return existing[-2000:] if changed else JSON_NO_CHANGE

        store.update_json(_NOTIFICATION_OUTBOX_FILE, [], append_queued)
    return writes


def _owner_attention_duplicate_reason(
    outbox: list[Any],
    *,
    owner_id: str,
    focus_key: str,
    message: str,
    reason: str,
    now: datetime,
) -> str:
    """Suppress repeated owner nudges while the prior unresolved nudge is live."""

    candidate_terms = _owner_attention_terms(" ".join([focus_key, reason, message]))
    for item in outbox:
        if not isinstance(item, dict):
            continue
        if str(item.get("notification_type") or "") != "autonomous_owner_attention":
            continue
        if str(item.get("touser") or item.get("target_user_id") or "") != owner_id:
            continue
        if str(item.get("status") or "") not in {"pending", "retry_pending", "sent"}:
            continue
        created_at = _parse_attention_time(item.get("created_at"))
        if created_at is not None:
            try:
                if (now - created_at).total_seconds() > 48 * 3600:
                    continue
            except TypeError:
                pass
        if str(item.get("focus_key") or "") == focus_key:
            return "same focus already has an unresolved owner attention"
        existing_terms = _owner_attention_terms(
            " ".join([
                str(item.get("focus_key") or ""),
                str(item.get("summary") or ""),
                str(item.get("content") or ""),
            ])
        )
        if len(candidate_terms & existing_terms) >= 2 and (
            "确认" in candidate_terms or "确认" in existing_terms
        ):
            return "similar unresolved owner attention already exists"
    return ""


def _parse_attention_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None


def _owner_attention_terms(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", str(text or ""))
    terms = {
        "一直发",
        "明天早上",
        "李老师",
        "沟通结果",
        "小金",
        "家长",
        "续费",
        "安全任务",
        "不能干",
        "删除",
        "确认",
        "授权",
        "日报",
        "汇报",
    }
    return {term for term in terms if term in compact}


def _materialize_relationship_touch_candidates(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    decision: dict[str, Any],
    timestamp: datetime,
    op_prefix: str,
) -> list[dict[str, Any]]:
    writes: list[dict[str, Any]] = []
    candidates = decision.get("relationship_touch_candidates") or []
    if not candidates:
        return writes
    policy = relationship_touch_policy(store)
    owner_id = _owner_user_id(store)
    outbox = store.read_json(_NOTIFICATION_OUTBOX_FILE, [])
    if not isinstance(outbox, list):
        outbox = []
    existing_ids = {str(item.get("id") or "") for item in outbox if isinstance(item, dict)}
    day = timestamp.strftime("%Y%m%d")
    stamp = timestamp.isoformat(timespec="seconds")
    owner_sent_today = _relationship_owner_sent_count(outbox, day)
    owner_queued = 0
    staff_queued_counts: dict[tuple[str, str], int] = {}
    queued_rows: list[dict[str, Any]] = []
    for idx, candidate in enumerate(candidates[:6]):
        role = _limit(candidate.get("target_role"), 40)
        role_policy = policy.get(role) if isinstance(policy.get(role), dict) else {}
        target_user_id = _limit(candidate.get("target_user_id"), 120)
        if role == "boss" and not target_user_id:
            target_user_id = owner_id
        if role in {"manager", "teacher"} and not target_user_id:
            target_user_id = _resolve_staff_user_id(store, role=role, target_name=_limit(candidate.get("target_name"), 80))
        queued_for_target = staff_queued_counts.get((role, target_user_id), 0)
        external_allowed = _relationship_touch_external_allowed(
            store,
            outbox,
            day=day,
            role=role,
            target_user_id=target_user_id,
            candidate=candidate,
            timestamp=timestamp,
            role_policy=role_policy,
            queued_count=owner_queued if role == "boss" else queued_for_target,
            owner_sent_today=owner_sent_today,
        )
        res = submit_relationship_touch_candidate(
            store,
            identity=identity,
            target_role=role,
            target_user_id=target_user_id,
            target_name=_limit(candidate.get("target_name"), 80),
            touch_type=_limit(candidate.get("touch_type"), 40),
            message=_limit(candidate.get("message"), 700),
            reason=_limit(candidate.get("reason"), 500),
            value=_limit(candidate.get("value"), 500),
            work_related=bool(candidate.get("work_related")),
            private_emotional_support=bool(candidate.get("private_emotional_support")),
            requires_authorization=not external_allowed,
            external_send_allowed=external_allowed,
            suggested_send_at=_limit(candidate.get("suggested_send_at") or stamp, 80),
            status="queued" if external_allowed else "candidate",
            operation_id=f"{op_prefix}:relationship_touch:{idx}",
            source_text=decision.get("employee_summary") or "",
            source_message_id=f"autonomous_employee_loop:{timestamp.strftime('%Y%m%d%H%M%S')}",
        )
        writes.append(_write_result("relationship_touch_candidate", res))
        if not external_allowed or not res.get("ok"):
            continue
        stored = res.get("candidate") if isinstance(res.get("candidate"), dict) else {}
        candidate_id = str(stored.get("candidate_id") or f"relationship_touch:{idx}")
        notification_id = f"relationship_touch:{day}:{candidate_id}"
        if notification_id in existing_ids:
            continue
        row = {
            "id": notification_id,
            "status": "pending",
            "delivery_mode": "direct_wecom",
            "notification_type": "relationship_touch",
            "task_id": f"relationship_touch:{candidate_id}",
            "role": role,
            "action": "relationship_touch",
            "target_user_id": target_user_id,
            "recipient_user_id": target_user_id,
            "to_user_id": target_user_id,
            "touser": target_user_id,
            "content": _limit(candidate.get("message"), 700),
            "summary": _limit(candidate.get("reason"), 240),
            "relationship_touch_candidate_id": candidate_id,
            "created_at": stamp,
            "attempt_count": 0,
            "auto_effects": {
                "sends_parent_messages": False,
                "sends_teacher_messages": role == "teacher",
                "sends_manager_messages": role == "manager",
                "creates_teacher_tasks": False,
                "changes_salary": False,
                "changes_permissions": False,
                "changes_router": False,
                "forces_next_action": False,
            },
        }
        outbox.append(row)
        queued_rows.append(deepcopy(row))
        existing_ids.add(notification_id)
        if role == "boss":
            owner_queued += 1
        else:
            staff_queued_counts[(role, target_user_id)] = queued_for_target + 1
        action_res = submit_action_execution(
            store,
            identity=identity,
            action_type="relationship_touch_queued",
            action_summary=f"Hermes queued one {role}-facing proactive message with permission, evidence, and frequency limits.",
            status="success",
            operation_id=f"{op_prefix}:relationship_touch_outbox:{idx}",
            related_work_item_id="",
            idempotency_key=notification_id,
            receipt={"target_user_id": target_user_id, "queued_notification_id": notification_id, "target_role": role},
            result_text=_limit(candidate.get("message"), 700),
            source_text=decision.get("employee_summary") or "",
            source_message_id=f"autonomous_employee_loop:{timestamp.strftime('%Y%m%d%H%M%S')}",
        )
        writes.append(_write_result("relationship_touch_queued", action_res))
    if queued_rows:
        def append_queued(existing: Any) -> Any:
            existing = existing if isinstance(existing, list) else []
            ids = {str(item.get("id") or "") for item in existing if isinstance(item, dict)}
            changed = False
            for row in queued_rows:
                row_id = str(row.get("id") or "")
                if not row_id or row_id in ids:
                    continue
                existing.append(deepcopy(row))
                ids.add(row_id)
                changed = True
            return existing[-2000:] if changed else JSON_NO_CHANGE

        store.update_json(_NOTIFICATION_OUTBOX_FILE, [], append_queued)
    return writes


def _relationship_touch_external_allowed(
    store: TuoguanStore,
    outbox: list[Any],
    *,
    day: str,
    role: str,
    target_user_id: str,
    candidate: dict[str, Any],
    timestamp: datetime,
    role_policy: dict[str, Any],
    queued_count: int = 0,
    owner_sent_today: int = 0,
) -> bool:
    if role not in {"boss", "manager", "teacher"}:
        return False
    if str(role_policy.get("mode") or "candidate") != "direct":
        return False
    if not target_user_id:
        return False
    if not _relationship_touch_time_allowed(timestamp, role_policy):
        return False
    allowed_types = {str(item) for item in role_policy.get("allowed_types") or []}
    touch_type = str(candidate.get("touch_type") or "")
    if allowed_types and touch_type and touch_type not in allowed_types:
        return False
    limit = int(role_policy.get("daily_limit") or (2 if role == "boss" else 1))
    if role == "boss":
        if owner_sent_today + queued_count >= limit:
            return False
        return _relationship_target_role_allowed(store, target_user_id, role) and _relationship_owner_message_is_sendable(candidate)
    if _relationship_role_sent_count(outbox, day, role, target_user_id) + queued_count >= limit:
        return False
    return _relationship_target_role_allowed(store, target_user_id, role) and _relationship_staff_message_is_sendable(candidate, role=role)


def _relationship_owner_sent_count(outbox: list[Any], day: str) -> int:
    return _relationship_role_sent_count(outbox, day, "boss", "")


def _relationship_role_sent_count(outbox: list[Any], day: str, role: str, target_user_id: str = "") -> int:
    count = 0
    for item in outbox:
        if not isinstance(item, dict):
            continue
        if str(item.get("notification_type") or "") != "relationship_touch":
            continue
        if role and str(item.get("role") or "") != role:
            continue
        if target_user_id and str(item.get("touser") or item.get("target_user_id") or "") != target_user_id:
            continue
        if not str(item.get("created_at") or "").replace("-", "").startswith(day):
            continue
        if str(item.get("status") or "") in {"pending", "retry_pending", "sent"}:
            count += 1
    return count


def _relationship_touch_time_allowed(timestamp: datetime, role_policy: dict[str, Any]) -> bool:
    start = str(role_policy.get("allowed_start") or "08:00")
    end = str(role_policy.get("allowed_end") or "19:00")
    current = timestamp.astimezone().strftime("%H:%M")
    return start <= current <= end


def _relationship_owner_message_is_sendable(candidate: dict[str, Any]) -> bool:
    message = str(candidate.get("message") or "")
    if len(message) < 20 or _relationship_touch_text_unsafe(message, role="boss"):
        return False
    has_evidence = any(term in message for term in ("我看到", "我发现", "今天", "当前", "记录", "目标", "风险", "机会", "日报", "续费"))
    has_value = any(term in message for term in ("建议", "我会", "你可以", "需要你", "不用回复", "回我", "下一步"))
    return has_evidence and has_value


def _relationship_staff_message_is_sendable(candidate: dict[str, Any], *, role: str) -> bool:
    message = str(candidate.get("message") or "")
    if len(message) < 8 or _relationship_touch_text_unsafe(message, role=role):
        return False
    if bool(candidate.get("private_emotional_support")):
        return False
    if not bool(candidate.get("work_related")):
        return False
    if _looks_like_parent_outreach_instruction(message):
        return False
    asks_for_fact = any(term in message for term in ("？", "?", "请", "麻烦", "帮我确认", "确认一下", "回我", "告诉我", "发我", "是否", "能不能"))
    work_fact = any(term in message for term in ("任务", "进展", "结果", "记录", "沟通", "学生", "孩子", "家长", "截止", "安排", "反馈", "执行", "完成", "缺"))
    return asks_for_fact and work_fact


def _looks_like_parent_outreach_instruction(message: str) -> bool:
    text = re.sub(r"\s+", "", str(message or ""))
    forbidden = (
        "联系家长", "通知家长", "给家长发", "发给家长", "发家长",
        "转发家长", "群发家长", "家长群", "把这段发给",
    )
    return any(term in text for term in forbidden)


def _relationship_target_role_allowed(store: TuoguanStore, user_id: str, role: str) -> bool:
    user = str(user_id or "").strip()
    if not user:
        return False
    whitelist = store.read_json("wecom_whitelist.json", {})
    if not isinstance(whitelist, dict):
        return False
    if user in {str(item) for item in whitelist.get("rejected_users") or []}:
        return False
    super_users = {str(item) for item in whitelist.get("super_users") or []}
    allowed_users = {str(item) for item in whitelist.get("allowed_users") or []}
    if user not in super_users and user not in allowed_users:
        return False
    roles = whitelist.get("user_roles") if isinstance(whitelist.get("user_roles"), dict) else {}
    actual = str(roles.get(user) or ("boss" if user in super_users else "teacher"))
    actual = {"super_admin": "boss", "owner": "boss"}.get(actual, actual)
    if user in {str(item) for item in whitelist.get("summer_manager_ids") or []} and actual != "boss":
        actual = "manager"
    return actual == role


def _resolve_staff_user_id(store: TuoguanStore, *, role: str, target_name: str) -> str:
    name = str(target_name or "").strip()
    if not name:
        return ""
    mapping = store.read_json("teacher_wecom_map.json", {})
    if isinstance(mapping, dict) and str(mapping.get(name) or "").strip():
        candidate = str(mapping.get(name) or "").strip()
        if _relationship_target_role_allowed(store, candidate, role):
            return candidate
    staff = store.read_json("staff.json", {})
    if isinstance(staff, dict):
        for user_id, item in staff.items():
            if not isinstance(item, dict):
                continue
            if str(item.get("role") or "") != role:
                continue
            staff_name = str(item.get("name") or "").strip()
            if staff_name and (staff_name == name or name in staff_name or staff_name in name):
                candidate = str(user_id or "").strip()
                if _relationship_target_role_allowed(store, candidate, role):
                    return candidate
    return ""


def _owner_user_id(store: TuoguanStore) -> str:
    whitelist = store.read_json("wecom_whitelist.json", {})
    if isinstance(whitelist, dict):
        super_users = whitelist.get("super_users")
        if isinstance(super_users, list):
            for item in super_users:
                if str(item or "").strip():
                    return str(item).strip()
    mapping = store.read_json("teacher_wecom_map.json", {})
    if isinstance(mapping, dict):
        for name in ("金总", "老板", "JinWenJie"):
            if str(mapping.get(name) or "").strip():
                return str(mapping[name]).strip()
    return ""


def _safe_focus(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_\-:.]+", "_", str(value or "").strip())[:120]
    return text.strip("_")


def _safe_owner_attention_text(value: Any) -> str:
    text = _limit(value, 900)
    if not text:
        return ""
    if not _owner_attention_candidate_is_safe("", "", text):
        return ""
    return text


def _owner_attention_candidate_is_safe(focus_key: str, reason: str, message: str) -> bool:
    text = f"{focus_key} {reason} {message}"
    boundary_text = text
    for allowed_boundary in (
        "不会直接安排老师",
        "不会安排老师",
        "不会通知老师",
        "不直接安排老师",
        "不安排老师",
        "不通知老师",
        "不会擅自联系老师",
        "不擅自联系老师",
    ):
        boundary_text = boundary_text.replace(allowed_boundary, "")
    lowered = boundary_text.lower()
    if len(str(message or "").strip()) < 20:
        return False
    forbidden = (
        "家长已发送", "已通知家长", "已发家长", "发给家长", "通知家长",
        "已派任务", "安排老师", "通知老师", "发给老师", "群发", "批量派",
        "已扣工资", "已修改工资", "改工资", "绩效结论", "修改权限", "permission changed",
        "已删除", "删除数据", "责任绑定", "主责老师改为",
        "已完成", "已汇报", "已回复老板", "已外发", "已经发送",
    )
    if any(item in lowered or item in boundary_text for item in forbidden):
        return False
    has_block = any(term in text for term in ("卡点", "卡在", "缺", "需要", "无法继续", "待确认", "不确定"))
    has_question = any(mark in text for mark in ("？", "?", "请确认", "确认一下", "你确认", "需要你确认", "能否"))
    has_next = any(term in text for term in ("确认后", "下一步", "我会", "然后", "接下来"))
    if not (has_block and has_question and has_next):
        return False
    return True


def _is_deferred_service_relation_attention(focus_key: str, reason: str, message: str) -> bool:
    text = f"{focus_key} {reason} {message}".lower()
    return any(term in text for term in (
        "service_relation",
        "student_roster",
        "服务关系",
        "主责老师",
        "服务类型",
        "新学期名单",
        "122",
    ))


def _latest_owner_contact_at(materials: dict[str, Any] | None) -> str:
    owner_messages = (materials or {}).get("recent_owner_messages") if isinstance(materials, dict) else {}
    events = owner_messages.get("events") if isinstance(owner_messages, dict) else []
    times = [
        str(event.get("occurred_at") or "")
        for event in events
        if isinstance(event, dict) and str(event.get("occurred_at") or "")
    ]
    return max(times) if times else ""


def _work_update_mentions_owner(update: dict[str, Any]) -> bool:
    payload = json.dumps(update, ensure_ascii=False)
    return any(term in payload for term in ("老板", "金总", "owner", "boss"))


def _drop_deferred_relation_items(values: Any) -> list[Any]:
    kept: list[Any] = []
    for item in values if isinstance(values, list) else []:
        text = json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item)
        if _is_deferred_service_relation_attention("", text, text):
            continue
        kept.append(item)
    return kept


def _apply_deferred_service_relation_boundary(update: dict[str, Any]) -> None:
    """Keep deferred new-term relation facts out of current blockers.

    This does not hide the topic from the model. It only preserves the owner's
    confirmed boundary: before the new-term window, service relation gaps are
    future confirmation material, not a blocker for historical analysis.
    """
    for key in ("blocked_by", "ask_candidates"):
        if key in update:
            update[key] = _drop_deferred_relation_items(update.get(key))
    if "pending_judgements" in update:
        update["pending_judgements"] = _drop_deferred_relation_items(update.get("pending_judgements"))
    if "next_actions" in update and isinstance(update.get("next_actions"), list):
        update["next_actions"] = [
            _rewrite_deferred_relation_text(str(item)) if isinstance(item, str) else item
            for item in update.get("next_actions") or []
        ][:8]
    confirmed = list(update.get("confirmed_facts") or []) if isinstance(update.get("confirmed_facts"), list) else []
    deferred_fact = "新学期名单、服务类型和责任老师确认已延期至2026-08-25至2026-09-10窗口；当前不阻塞历史续费分析、沟通覆盖分析和材料准备。"
    if deferred_fact not in [str(item) for item in confirmed]:
        confirmed.append(deferred_fact)
    update["confirmed_facts"] = confirmed[:8]
    replacements = {
        "但服务类型和主责老师仍缺失，无法制定具体沟通计划": "服务关系确认已延期到新学期窗口；当前可继续历史续费分析、沟通覆盖分析和材料准备",
        "但服务类型和主责老师仍缺失，需要老板决定是否提前处理或等待8月底窗口": "服务关系确认已延期到新学期窗口；当前不需要提前催问，可继续历史分析和准备工作",
        "服务类型和主责老师仍缺失，需要老板决定是否提前处理或等待8月底窗口": "服务关系确认已延期到新学期窗口；当前不需要提前催问，可继续历史分析和准备工作",
        "服务类型和主责老师仍缺失，无法制定具体沟通计划": "服务关系确认已延期到新学期窗口；当前可继续历史续费分析、沟通覆盖分析和材料准备",
    }
    for field in ("focus_summary", "update_text", "value_progress_note", "owner_escalation_reason"):
        if isinstance(update.get(field), str):
            update[field] = _rewrite_deferred_relation_text(update[field], replacements=replacements)


def _rewrite_deferred_relation_text(value: str, *, replacements: dict[str, str] | None = None) -> str:
    text = str(value or "")
    mapping = {
        "但仍有服务类型和主责老师缺口未解决": "服务类型和主责老师属于新学期确认项，当前不阻塞历史分析",
        "但仍缺服务类型和主责老师": "服务类型和主责老师属于新学期确认项，当前不阻塞历史分析",
        "但服务类型和主责老师缺口仍存": "服务类型和主责老师属于新学期确认项，当前不阻塞历史分析",
        "但服务类型和主责老师缺口仍然存在，影响具体学生沟通计划": "服务类型和主责老师属于新学期确认项，暂不做具体责任分配",
        "但服务类型和主责老师缺口仍待决定": "服务类型和主责老师已延期到新学期窗口确认",
        "服务类型和主责老师缺口仍为推进障碍": "服务类型和主责老师是新学期窗口的未来确认项",
        "服务关系缺口仍为具体沟通计划的障碍": "服务关系缺口是新学期窗口的未来确认项",
        "服务关系缺口仍为推进障碍": "服务关系缺口是新学期窗口的未来确认项",
        "服务关系缺口仍为具体沟通计划的未来确认项": "服务关系缺口是新学期窗口的未来确认项",
        "但服务类型和主责老师缺口仍缺失": "服务类型和主责老师属于新学期确认项",
        "服务关系缺口未解决，无法推进具体沟通名单": "服务关系确认已延期到新学期窗口；当前可继续历史分析和材料准备",
        "无法推进具体沟通名单": "暂不推进基于新学期责任人的具体名单",
        "无法推进下一步": "当前仍可推进历史分析和材料准备",
        "无法推进到具体学生沟通": "暂不推进具体学生责任分配，但可继续历史分析和材料准备",
        "无法制定具体沟通计划": "暂不制定基于新学期责任人的具体沟通计划",
        "暂时无法展开具体沟通": "暂不展开基于新学期责任人的具体沟通",
        "直接影响具体沟通覆盖计划": "暂不用于具体责任分配",
        "影响具体学生沟通计划": "暂不用于具体责任分配",
        "需要老板决定是否提前处理或等待8月底窗口": "按已确认边界等待8月底窗口",
        "需老板决定是否提前确认或等待8月25日窗口": "按已确认边界等待8月25日窗口",
        "需老板决定是否提前确认或等待8月底窗口": "按已确认边界等待8月底窗口",
        "建议提前处理或等待8月底窗口": "按已确认边界等待8月底窗口",
        "服务关系缺口需指示": "服务关系缺口已延期到新学期窗口",
        "仍需老板决定是否提前处理服务关系缺口": "服务关系缺口已延期到新学期窗口",
        "若要推进下一步，仍需要服务类型和主责老师信息": "推进下一步先做历史分析和材料准备，服务类型和主责老师到新学期窗口确认",
        "是否需提前确认或等待8月25日-9月10日窗口": "已延期至8月25日-9月10日窗口确认",
        "是否希望提前确认这些信息，还是按原计划等8月底窗口": "按原计划等8月底窗口确认这些信息",
    }
    if replacements:
        mapping.update(replacements)
    for old, new in mapping.items():
        text = text.replace(old, new)
    if _is_deferred_service_relation_attention("", text, text) and "当前不阻塞历史" not in text and "延期" not in text:
        text += " 服务关系确认已延期到2026-08-25至2026-09-10窗口，当前不阻塞历史分析和准备工作。"
    if _is_deferred_service_relation_attention("", text, text):
        sentence = r"[^。；\n]*(?:服务关系|服务类型|主责老师|责任老师|新学期名单)[^。；\n]*(?:障碍|无法|需老板|建议提前|未解决|仍缺失|仍缺|缺口需指示)[^。；\n]*[。；]?"
        text = re.sub(sentence, "服务关系确认已延期到2026-08-25至2026-09-10窗口，当前不阻塞历史分析和准备工作。", text)
        reverse_sentence = r"[^。；\n]*(?:需老板|老板决定|建议提前|仍需要|仍需)[^。；\n]*(?:服务关系|服务类型|主责老师|责任老师|新学期名单)[^。；\n]*[。；]?"
        text = re.sub(reverse_sentence, "服务关系确认已延期到2026-08-25至2026-09-10窗口，当前不阻塞历史分析和准备工作。", text)
        deferred_sentence = "服务关系确认已延期到2026-08-25至2026-09-10窗口，当前不阻塞历史分析和准备工作。"
        escaped = re.escape(deferred_sentence)
        text = re.sub(rf"(?:\s*{escaped}){{2,}}", " " + deferred_sentence, text).strip()
    return text


def _rewrite_unmaterialized_owner_reply(value: str) -> str:
    text = str(value or "")
    mapping = {
        "计划白天08:00回复老板": "白天核验是否需要形成老板提醒候选",
        "计划白天08:00回应": "白天核验是否需要形成老板提醒候选",
        "计划明天08:00第一时间回应": "白天核验是否需要形成老板提醒候选",
        "计划明天白天窗口（08:00后）向老板回应": "白天核验是否需要形成老板提醒候选",
        "在白天窗口向老板回应": "在白天窗口核验是否需要形成老板提醒候选",
        "白天窗口向老板回应": "白天窗口核验是否需要形成老板提醒候选",
        "08:00白天窗口回应老板": "08:00白天窗口核验老板问题并更新内部判断",
        "08:30回应老板": "08:30核验老板问题并更新内部判断",
        "准备回应老板": "准备核验老板问题并形成内部判断",
        "已在白天窗口回应": "已在白天窗口形成内部判断",
        "老板主动询问推进情况和回复是否足够，已在白天窗口回应。": "老板主动询问推进情况和回复是否足够；本轮已形成内部判断，未生成外发候选。",
        "我将直接回应老板，并展示具体分析进展。": "本轮先形成内部分析进展；如需要外发，必须生成老板提醒候选并取得真实发送回执。",
        "直接回应老板": "形成内部判断",
        "回应老板": "形成内部判断",
        "向老板汇报": "形成老板提醒候选后再汇报",
        "汇报分析成果": "形成分析成果候选",
        "请示是否同意": "形成待老板确认候选",
        "当前无外部输出权限": "当前没有生成外发候选",
    }
    for old, new in mapping.items():
        text = text.replace(old, new)
    return text


def _is_real_value_progress(entry: dict[str, Any]) -> bool:
    evidence = entry.get("evidence") if isinstance(entry.get("evidence"), list) else []
    combined = " ".join(
        str(entry.get(key) or "")
        for key in ("hermes_action", "human_action", "outcome")
    ).lower()
    waiting_only = any(term in combined for term in ("等待", "待回复", "待确认", "still waiting", "no reply"))
    completed_signal = bool(evidence) or bool(str(entry.get("outcome") or "").strip()) or bool(str(entry.get("human_action") or "").strip())
    return completed_signal and not (waiting_only and not evidence)


def _compact_for_model(value: Any, *, max_chars: int = 20000, depth: int = 0) -> Any:
    if depth > 5:
        return _limit(value, 500)
    if isinstance(value, str):
        return _limit(value, 1200)
    if isinstance(value, list):
        items = [_compact_for_model(item, max_chars=max(500, max_chars // 2), depth=depth + 1) for item in value[:20]]
        if len(value) > 20:
            items = [{"items_preview": items, "total_count": len(value), "omitted_count": len(value) - 20}]
        return _fit_model_value(items, max_chars)
    if isinstance(value, dict):
        out = {
            str(k): _compact_for_model(v, max_chars=max(500, max_chars // 2), depth=depth + 1)
            for k, v in value.items()
            if str(k) not in _FORBIDDEN_ROUTE_KEYS
        }
        return _fit_model_value(out, max_chars)
    return deepcopy(value)


def _fit_model_value(value: Any, max_chars: int) -> Any:
    if len(json.dumps(value, ensure_ascii=False)) <= max_chars:
        return value
    if isinstance(value, str):
        return value[:max(0, max_chars - 20)]
    if isinstance(value, list):
        fitted: list[Any] = []
        for item in value:
            candidate = fitted + [_fit_model_value(item, max(200, max_chars // 2))]
            if len(json.dumps(candidate, ensure_ascii=False)) > max_chars:
                break
            fitted = candidate
        return {
            "items_preview": fitted,
            "total_count": len(value),
            "omitted_count": max(0, len(value) - len(fitted)),
            "_compacted_note": "material compacted for model context",
        }
    if isinstance(value, dict):
        fitted_dict: dict[str, Any] = {}
        omitted: list[str] = []
        for key, item in value.items():
            compact_item = _fit_model_value(item, max(200, max_chars // 2))
            candidate = {**fitted_dict, key: compact_item}
            if len(json.dumps(candidate, ensure_ascii=False)) > max_chars:
                omitted.append(key)
                continue
            fitted_dict[key] = compact_item
        if omitted:
            fitted_dict["_omitted_keys"] = omitted[:20]
            fitted_dict["_compacted_note"] = "material compacted for model context"
        return fitted_dict
    return _limit(value, max(0, max_chars - 20))


def _strip_forbidden(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _strip_forbidden(v) for k, v in value.items() if str(k) not in _FORBIDDEN_ROUTE_KEYS}
    if isinstance(value, list):
        return [_strip_forbidden(item) for item in value]
    return value


def _dict(value: Any) -> dict[str, Any]:
    return _strip_forbidden(value) if isinstance(value, dict) else {}


def _list_of_dicts(value: Any, limit: int) -> list[dict[str, Any]]:
    return [_dict(item) for item in value[:limit] if isinstance(item, dict)] if isinstance(value, list) else []


def _list_any(value: Any, limit: int) -> list[Any]:
    return [_strip_forbidden(item) for item in value[:limit]] if isinstance(value, list) else []


def _limit(value: Any, length: int = 1200) -> str:
    return str(value or "").strip()[:length]


def _truthful_internal_text(value: Any, length: int = 1200) -> str:
    text = _limit(value, length)
    false_delivery_claims = (
        "已向老板汇报",
        "已经向老板汇报",
        "已回复老板",
        "本次直接回复老板",
        "已向老板发送",
        "已经发给老板",
    )
    if any(claim in text for claim in false_delivery_claims):
        for claim in false_delivery_claims:
            text = text.replace(claim, "形成老板沟通候选（未发送）")
        return _limit("本轮没有新的企业微信发送回执。" + text, length)
    return text


def _score_value(value: Any) -> int:
    match = re.search(r"\d+", str(value or ""))
    score = int(match.group(0)) if match else 0
    return max(0, min(score, 100))


def _json_text(value: str) -> str:
    value = str(value or "").strip()
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.I) if value.startswith("```") else value


def _write_result(kind: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": kind,
        "ok": bool(result.get("ok")),
        "state_changed": bool(result.get("state_changed", True)),
        "message": result.get("rendered_text") or result.get("message") or "",
        "error": result.get("error") or "",
    }


def _render_self_review_text(decision: dict[str, Any]) -> str:
    review = decision.get("self_review") if isinstance(decision.get("self_review"), dict) else {}
    parts = [decision.get("employee_summary") or "", decision.get("institution_understanding") or "", decision.get("goal_progress_view") or "", json.dumps(review, ensure_ascii=False) if review else ""]
    return _limit("\n".join(part for part in parts if part), 1500)


def _safe_error(exc: Exception) -> str:
    return re.sub(r"[^A-Za-z0-9_:\-.,\[\] ']+", "?", f"{type(exc).__name__}:{exc}")[:300]
