"""Runtime constitution and ownership manifests for Xiaoyou.

This module is deliberately declarative. It describes the stable boundaries
around the model without deciding business intent or selecting tools for it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


RUNTIME_CONSTITUTION_VERSION = "xiaoyou-runtime-constitution-v1"

MANDATORY_TURN_DUTIES = (
    "resolve_identity_from_trusted_gateway",
    "ground_current_time_and_business_state",
    "provide_evidence_with_source_and_freshness",
    "let_model_choose_business_judgment_and_action",
    "verify_writes_and_delivery_before_claiming_success",
    "preserve_role_specific_service_posture",
)

ABSOLUTE_PROHIBITIONS = (
    "infer_or_change_identity_from_conversation_text",
    "claim_read_without_trusted_read_result",
    "claim_write_without_verified_writeback",
    "claim_delivery_without_delivery_receipt",
    "cross_tenant_or_cross_person_memory",
    "revive_cancelled_completed_expired_or_superseded_work",
    "contact_parents_without_separate_explicit_authorization",
    "auto_change_payroll_performance_policy_permissions_or_delete_data",
    "let_subagents_write_business_state_or_send_messages",
)

ROLE_POSTURES = {
    "boss": "institution_management_employee_and_decision_support",
    "manager": "onsite_operations_partner_and_workload_reducer",
    "teacher": "education_friend_task_companion_and_growth_coach",
    "parent": "no_proactive_contact_in_current_release",
}


@dataclass(frozen=True)
class ModuleOwnership:
    category: str
    modules: tuple[str, ...]
    boundary: str


MODULE_OWNERSHIP = (
    ModuleOwnership(
        category="maintenance_and_repair",
        modules=(
            "acceptance_v1.py", "cleanup.py", "cli.py", "data_upgrade.py", "migrate.py",
            "repair_employee_closure_v1.py", "repair_latest_work_truth.py",
            "repair_task_companion_state.py", "repair_task_context_v1.py", "repair_task_unified_state_v1.py", "storage_maintenance.py",
        ),
        boundary="offline checks, repair candidates, reversible maintenance; never own live business judgment",
    ),
    ModuleOwnership(
        category="tenant_extensions",
        modules=(
            "p4_8_account_admin.py", "summer_course_coverage.py", "summer_enrollment.py",
            "summer_points.py", "summer_records.py", "summer_reports.py", "youyi_batch_capabilities.py",
        ),
        boundary="tenant-specific or seasonal capability behind tenant and permission boundaries",
    ),
    ModuleOwnership(
        category="compatibility_runtime",
        modules=(
            "capability_contracts_v1.py", "context_arbitration.py", "conversation_state.py",
            "core_understanding.py", "message_history.py", "router.py", "runtime_ownership.py",
            "semantic_router.py", "shadow_command_bus.py", "task_query_gray.py", "wakeup_v2.py",
            "workflow_semantics.py",
        ),
        boundary="compatibility projection or observation only; no new independent source of truth",
    ),
    ModuleOwnership(
        category="common_production",
        modules=(
            "__init__.py", "active_work_context.py", "analytics.py", "autonomous_employee_loop.py",
            "autonomous_wakeup_runner.py", "config_changes.py", "daily_push.py", "daily_reporter.py",
            "capability_facades.py",
            "dashboard_auth.py", "dashboard_builder.py", "dashboard_http.py", "dashboard_refresh_runner.py", "dashboard_workbench_v1.py", "digital_employee_state.py",
            "employee_identity.py", "escalation.py", "execution_receipts.py", "external_learning_runner.py", "goal_operator.py",
            "gray_observation_review.py", "gray_review_v1.py", "gray_scenario_cards.py",
            "growth_plan_exporter.py", "growth_reports.py", "identity.py", "knowledge.py",
            "learning_loop.py", "models.py", "notification_outbox_runner.py", "operational_facts.py",
            "operations_daily_report.py", "operations_focus.py", "operations_query.py", "payroll.py",
            "permission_guard.py", "permissions.py", "proactive_work.py", "programs.py", "project_opportunities.py", "queries.py",
            "provider_resilience.py", "model_context_budget.py",
            "record_evaluation.py", "record_reply_composer.py", "records.py", "reports.py", "research.py",
            "responsibility_resolver.py", "runtime.py", "runtime_foundation.py", "runtime_governance.py",
            "runtime_performance.py",
            "self_evolution.py", "social_market_research.py", "staff_administration.py", "staff_config.py",
            "staff_conversation_activity.py", "staff_directory.py", "store.py", "student_daily_records.py",
            "student_record_guidance.py", "student_resolver.py", "system_self_knowledge.py", "tasks.py",
            "supervision.py", "supervision_runner.py",
            "teacher_coaching.py",
            "temporal_grounding.py", "tenant_context.py", "tool_service.py", "tools.py", "turn_fence.py", "turn_trace.py",
            "work_context_snapshot.py",
            "workstyle_profiles.py", "write_guard.py",
        ),
        boundary="shared production behavior governed by the runtime constitution and verified stores",
    ),
)


STATE_RESOURCE_OWNERS: dict[str, str] = {
    "tasks.json": "task_domain",
    "task_closure_events.json": "task_domain",
    "active_task_context.json": "work_context_projection",
    "pending_next_task_context.json": "work_context_projection",
    "model_focus.json": "work_context_projection",
    "goal_operator_goals.json": "goal_domain",
    "goal_operator_events.jsonl": "goal_domain",
    "goal_actions.jsonl": "goal_domain",
    "notification_outbox.json": "outbound_delivery_domain",
    "notification_deliveries.json": "outbound_delivery_domain",
    "notification_failures.jsonl": "outbound_delivery_domain",
    "relationship_touch_candidates.jsonl": "proactive_work_domain",
    "relationship_touch_policy.json": "proactive_work_domain",
    "proactive_authorizations.jsonl": "proactive_work_domain",
    "attention_threads.jsonl": "proactive_work_domain",
    "students.json": "student_domain",
    "records.json": "student_record_domain",
    "service_relations.json": "student_service_relation_domain",
    "service_relation_candidates.jsonl": "student_service_relation_domain",
    "staff.json": "staff_identity_domain",
    "wecom_whitelist.json": "staff_identity_domain",
    "staff_offboarding_events.jsonl": "staff_identity_domain",
    "teacher_wecom_map.json": "staff_identity_domain",
    "teacher_feishu_map.json": "staff_identity_domain",
    "staff_voice_signals.jsonl": "staff_voice_domain",
    "person_workstyle_events.jsonl": "workstyle_domain",
    "operational_facts.json": "institution_fact_domain",
    "operational_fact_candidates.jsonl": "institution_fact_domain",
    "institution_operating_model.json": "institution_fact_domain",
    "youyi_operating_model.json": "institution_fact_domain",
    "institution_onboarding_state.json": "institution_fact_domain",
    "institution_understanding_state.json": "institution_fact_domain",
    "institution_fact_gap_events.jsonl": "institution_fact_domain",
    "information_requests.jsonl": "institution_fact_domain",
    "business_events.jsonl": "employee_work_domain",
    "action_executions.jsonl": "employee_work_domain",
    "hermes_work_items.jsonl": "employee_work_domain",
    "wakeup_requests.jsonl": "employee_work_domain",
    "value_ledger.jsonl": "employee_work_domain",
    "value_progress_ledger.jsonl": "employee_work_domain",
    "self_evolution_events.jsonl": "self_evolution_domain",
    "industry_learning_candidates.jsonl": "learning_domain",
    "external_research_runs.jsonl": "learning_domain",
    "external_research_corrections.jsonl": "learning_domain",
    "market_research_candidates.jsonl": "learning_domain",
    "competitor_profiles.jsonl": "learning_domain",
    "social_market_research_config.json": "learning_domain",
    "social_market_research_runs.jsonl": "learning_domain",
    "social_market_research_candidates.jsonl": "learning_domain",
    "project_opportunity_events.jsonl": "learning_domain",
    "weekly_market_report_runs.jsonl": "learning_domain",
    "agent_delegations.jsonl": "advisory_agent_domain",
    "agent_delegation_results.jsonl": "advisory_agent_domain",
    "daily_report_runs.jsonl": "report_domain",
    "dashboard_cache.json": "report_projection",
    "summer_enrollments.json": "summer_program_domain",
    "trial_leads.json": "enrollment_domain",
    "point_events.json": "summer_points_domain",
    "summer_points.json": "summer_points_domain",
    "core_workflow_contexts.json": "work_context_projection",
    "safety_test_events.json": "safety_domain",
    "profile_candidates.jsonl": "profile_domain",
    "profile_candidate_corrections.jsonl": "profile_domain",
    "goal_evidence.jsonl": "goal_domain",
    "performance_evidence_candidates.jsonl": "evidence_domain",
    "performance_evidence_responses.jsonl": "evidence_domain",
    "gray_observations.jsonl": "release_governance_domain",
    "gray_rollout_decisions.jsonl": "release_governance_domain",
    "gray_optimization_decisions.jsonl": "release_governance_domain",
    "hermes_employee_scorecard.jsonl": "health_domain",
    "reply_ledger.jsonl": "runtime_observability_domain",
    "turn_traces.jsonl": "runtime_observability_domain",
    "runtime_status_events.jsonl": "runtime_observability_domain",
    "tool_operations.json": "execution_receipt_domain",
    "teacher_coaching_events.jsonl": "teacher_coaching_domain",
    "task_unified_state_repair_events.jsonl": "task_domain",
    "supervision_runs.jsonl": "supervision_domain",
    "supervision_findings.jsonl": "supervision_domain",
    "supervision_repairs.jsonl": "supervision_domain",
}

COMPATIBILITY_PROJECTIONS = {
    "active_task_context.json", "pending_next_task_context.json", "model_focus.json",
    "core_workflow_contexts.json", "dashboard_cache.json",
}


def module_inventory(root: str | Path) -> dict[str, Any]:
    plugin_root = Path(root)
    classified: dict[str, str] = {}
    duplicates: list[str] = []
    for group in MODULE_OWNERSHIP:
        for name in group.modules:
            if name in classified:
                duplicates.append(name)
            classified[name] = group.category
    actual = {path.name for path in plugin_root.glob("*.py")}
    return {
        "constitution_version": RUNTIME_CONSTITUTION_VERSION,
        "module_count": len(actual),
        "classified_count": len(actual & set(classified)),
        "unclassified": sorted(actual - set(classified)),
        "missing_from_disk": sorted(set(classified) - actual),
        "duplicates": sorted(set(duplicates)),
        "categories": {group.category: asdict(group) for group in MODULE_OWNERSHIP},
    }


def state_ownership(resource_names: set[str] | list[str] | tuple[str, ...]) -> dict[str, Any]:
    names = {str(name) for name in resource_names}
    return {
        "resource_count": len(names),
        "owned_count": len(names & set(STATE_RESOURCE_OWNERS)),
        "unowned": sorted(names - set(STATE_RESOURCE_OWNERS)),
        "owners": {name: STATE_RESOURCE_OWNERS[name] for name in sorted(names & set(STATE_RESOURCE_OWNERS))},
        "compatibility_projections": sorted(names & COMPATIBILITY_PROJECTIONS),
    }
