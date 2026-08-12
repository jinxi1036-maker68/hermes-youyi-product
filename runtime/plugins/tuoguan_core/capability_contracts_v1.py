"""Machine-readable contracts for Core Capability Sprint V1."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .tenant_context import current_tenant_id


CONTRACTS: dict[str, dict[str, Any]] = {
    "teacher_task_guidance": {
        "capability_id": "teacher_task_guidance",
        "allowed_roles": ["teacher"],
        "tenant_scope": "current_tenant",
        "required_context": "task_context_or_unique_explicit_task",
        "input_schema": ["raw_text", "source_message_id"],
        "business_object_type": "ordinary_task",
        "command_type": "guide_or_update_task",
        "write_scope": ["tasks.json", "core_workflow_contexts.json"],
        "required_evidence": "task_type_specific",
        "state_transition": ["selected", "collecting_evidence", "ready_for_completion", "completed"],
        "idempotency_policy": "source_message_id",
        "renderer_policy": "verified_result_plus_short_guidance",
        "ai_guidance_policy": "one_next_best_action_no_new_facts",
    },
    "safety_workflow_coach": {
        "capability_id": "safety_workflow_coach",
        "allowed_roles": ["teacher", "manager", "boss"],
        "tenant_scope": "current_tenant",
        "required_context": "explicit_safety_test_or_active_safety_context",
        "input_schema": ["raw_text", "source_message_id", "safety_test"],
        "business_object_type": "safety_event",
        "command_type": "create_or_advance_safety_workflow",
        "write_scope": ["safety_test_events.json", "tasks.json", "core_workflow_contexts.json"],
        "required_evidence": "workflow_stage_specific",
        "state_transition": ["reported", "immediate_assessment", "immediate_handling", "observation", "parent_communication", "followup", "manager_review", "closed"],
        "idempotency_policy": "source_message_id",
        "renderer_policy": "verified_state_plus_safety_guidance",
        "ai_guidance_policy": "one_current_safety_action_emergency_boundary",
    },
    "management_boss_advisor": {
        "capability_id": "management_boss_advisor",
        "allowed_roles": ["manager", "boss"],
        "tenant_scope": "current_tenant",
        "required_context": "none",
        "input_schema": ["raw_text", "source_message_id"],
        "business_object_type": "management_snapshot",
        "command_type": "query_management_attention",
        "write_scope": [],
        "required_evidence": "repository_snapshot",
        "state_transition": [],
        "idempotency_policy": "source_message_id",
        "renderer_policy": "deterministic_facts_plus_validated_advice",
        "ai_guidance_policy": "maximum_three_prioritized_actions_no_number_changes",
    },
}

FUTURE_GOAL_INTERFACES = {
    name: {
        "goal_type": name,
        "output_schema": ["attention_item", "opportunity", "risk", "recommendation", "next_best_action"],
        "implementation_status": "interface_only",
        "production_enabled": False,
    }
    for name in ("OpportunityGoal", "EnrollmentGoal", "RenewalGoal", "RetentionGoal", "ReferralGoal")
}


def contract(capability_id: str) -> dict[str, Any] | None:
    row = CONTRACTS.get(str(capability_id or ""))
    return deepcopy(row) if row else None


def validate_contract(capability_id: str, *, role: str, tenant_id: str, expected_tenant_id: str = "") -> tuple[bool, str]:
    row = CONTRACTS.get(str(capability_id or ""))
    if not row:
        return False, "capability_contract_missing"
    if str(role or "") not in row["allowed_roles"]:
        return False, "permission_denied"
    if str(tenant_id or "") != str(expected_tenant_id or current_tenant_id()):
        return False, "cross_tenant_denied"
    return True, "allowed"
