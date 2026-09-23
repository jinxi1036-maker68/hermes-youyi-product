"""Permission-scoped business operations exposed to the Hermes model."""

from __future__ import annotations

from copy import deepcopy
from contextlib import closing
from datetime import datetime, timedelta
import hashlib
import json
import os
import re
import sqlite3
import uuid
from typing import Any, Callable
from urllib.parse import quote

from .dashboard_auth import DashboardAuthError, sign_dashboard_token, token_expiry_datetime
from .dashboard_builder import refresh_dashboard_cache
from .dashboard_http import verify_public_dashboard_route
from .execution_receipts import build_execution_receipt
from .escalation import build_notification_plan
from .identity import IdentityService
from .knowledge import (
    answer_script_request,
    list_pending_learning_candidates,
    review_learning_candidate,
    submit_learning_candidate,
)
from .permissions import PermissionService
from .models import UserIdentity
from .tenant_context import current_tenant_id
from .programs import resolve_record_program
from .programs import is_summer_operator
from .records import analyze_teacher_record, save_analysis
from .student_resolver import resolve_student_for_record
from .student_directory import filter_candidates_by_classroom, find_student_candidates, safe_candidate_labels
from .business_object_context import (
    remember_candidate_set,
    remember_resolved_object,
    resolve_confirmed_object,
    resolve_pending_candidate_confirmation,
)
from .summer_records import save_summer_lesson_record
from .store import JSON_NO_CHANGE, TuoguanStore, TuoguanStoreError
from .summer_points import change_points, query_points_ranking, query_student_points
from .tasks import (
    CLOSED_TASK_STATUSES,
    apply_task_reply,
    cancel_task as cancel_task_state,
    closure_missing_fields,
    current_task_for_user,
    task_is_closed,
    task_is_open,
)
from .temporal_grounding import parse_business_due_at
from .youyi_batch_capabilities import (
    create_assigned_task,
    create_trial_lead,
    operations_report,
    register_official_student,
    register_summer_student,
    verify_dashboard_visibility,
)


def _legacy_institution_rollout_target_allowed(store: TuoguanStore, user_id: str) -> bool:
    """Read the historical rollout sandbox from a server-owned policy.

    This compatibility-only path is not part of the current Work Runtime.  It
    deliberately fails closed until a deployment explicitly supplies a trusted
    policy file, rather than embedding real people or anonymous stand-ins in
    product source.
    """

    policy = store.read_json("institution_rollout_policy.json", {})
    values = policy.get("allowed_user_ids") if isinstance(policy, dict) else []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        values = []
    allowed = {str(value or "").strip() for value in values if str(value or "").strip()}
    return bool(str(user_id or "").strip() and str(user_id).strip() in allowed)
from .operations_query import query_operations
from .responsibility_resolver import resolve_student_responsibility
from .goal_operator import (
    confirm_goal,
    operate_goal_workspace,
    query_goal_progress,
    review_parent_communication_goal,
    update_goal_progress,
    withdraw_goal,
)
from .gray_observation_review import generate_gray_observation_candidates
from .gray_scenario_cards import generate_autonomous_acceptance_pack, generate_gray_trial_start_pack, query_gray_scenario_cards
from .operational_facts import (
    confirm_operational_fact,
    onboarding_gap_audit,
    query_operational_facts,
    submit_operational_fact_candidate,
)
from .staff_directory import query_staff_directory as build_staff_directory_report
from .staff_administration import offboard_staff as offboard_staff_member, staff_is_offboarded
from .active_work_context import query_active_work_context as build_active_work_context
from .staff_conversation_activity import query_staff_conversation_activity as build_staff_conversation_activity
from .self_evolution import query_self_evolution_ledger as build_self_evolution_ledger
from .social_market_research import query_social_market_research as build_social_market_research
from .project_opportunities import (
    query_project_opportunities as build_project_opportunities,
    review_project_opportunity as review_project_opportunity_state,
)
from .supervision import query_supervision_status
from .proactive_work import (
    execute_relationship_touch as execute_relationship_touch_state,
    query_goal_actions as build_goal_actions,
    query_proactive_authorizations as build_proactive_authorizations,
    submit_goal_action as save_goal_action,
    submit_proactive_authorization as save_proactive_authorization,
    update_relationship_touch as update_relationship_touch_state,
)
from .workstyle_profiles import (
    query_person_workstyle_profile as build_person_workstyle_profile,
    query_workstyle_adaptation_health as build_workstyle_adaptation_health,
    submit_person_workstyle_preference as save_person_workstyle_preference,
)
from .digital_employee_state import (
    update_wakeup_request,
    submit_due_wakeup_candidate,
    generate_autonomous_log_review,
    generate_due_wakeup_candidates,
    generate_autonomous_recovery_report,
    query_action_executions,
    query_autonomous_work_brief,
    query_institution_work,
    query_business_events,
    query_hermes_work_items,
    query_work_commitments,
    query_wakeup_requests,
    query_active_goal_work_state,
    query_attention_threads,
    query_gray_optimization_decisions,
    query_gray_observations,
    query_gray_rollout_decisions,
    query_xiaoyou_health,
    query_parent_communication_coverage,
    query_performance_evidence_candidates,
    query_profile_candidates,
    query_information_requests,
    query_competitor_profiles,
    query_external_learning_brief,
    query_external_research_runs,
    query_employee_work_map,
    query_fact_gap_candidates,
    query_industry_learning_candidates,
    query_market_research_candidates,
    query_proactive_work_radar,
    query_relationship_touch_candidates,
    query_staff_voice_radar,
    query_student_service_relations,
    query_value_ledger,
    query_value_progress_ledger,
    query_weekly_record_coverage,
    relationship_touch_policy,
    submit_industry_learning_candidate,
    submit_action_execution,
    submit_business_event,
    submit_fact_gap_candidate,
    submit_staff_voice_signal,
    submit_goal_evidence,
    submit_hermes_work_item,
    submit_work_commitment,
    advance_institution_work,
    submit_gray_optimization_decision,
    submit_gray_observation,
    submit_gray_rollout_decision,
    submit_information_request_record,
    submit_information_request_update,
    submit_wakeup_request,
    submit_performance_evidence_candidate,
    submit_performance_evidence_response,
    submit_profile_candidate,
    submit_profile_candidate_correction,
    submit_relationship_touch_candidate,
    submit_service_relation_fact_candidate,
    submit_value_ledger_entry,
    update_attention_thread,
    update_hermes_work_item,
    update_work_commitment,
)


_CLOSED_STATUSES = CLOSED_TASK_STATUSES

_MISSING_FIELD_LABELS = {
    "parent_attitude": "家长的反馈或态度",
    "renewal_reason": "家长为什么暂缓、犹豫或不续费；没说原因也要如实说明",
    "teacher_response": "老师当时怎样回应家长",
    "next_step": "下一步跟进安排",
    "child_status": "孩子当前状态",
    "action_taken": "老师已经采取的处理措施",
    "parent_informed": "家长是否知情",
    "follow_up_needed": "后续是否需要继续观察",
    "result": "实际处理结果",
}


def _record_time(item: dict[str, Any]) -> str:
    return str(item.get("timestamp") or item.get("created_at") or item.get("time") or item.get("date") or "")


def _render_student_records(students: list[dict[str, Any]]) -> str:
    if not students:
        return "没有查到这个学生的档案，或者当前账号没有权限查看。"
    output: list[str] = []
    for student in students:
        name = str(student.get("name") or "该学生")
        records = student.get("recent_records") if isinstance(student.get("recent_records"), list) else []
        output.append(f"【{name}近期表现】")
        if not records:
            output.append("最近暂无老师记录。")
            continue
        output.append(f"共找到最近 {len(records)} 条老师记录：")
        for index, record in enumerate(records, 1):
            when = _record_time(record)
            date = when[:10] if when else "未标注日期"
            content = str(record.get("content") or record.get("source_text") or record.get("summary") or "").strip()
            if not content:
                content = "（记录内容为空）"
            output.append(f"{index}. {date}：{content}")
    return "\n".join(output)


def _normalize_grade(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    digits = {
        "一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6",
        "1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6",
    }
    match = re.search(r"[一二三四五六1-6]", text)
    return digits.get(match.group(0), "") if match else text


def _student_grades(profile: dict[str, Any]) -> set[str]:
    grades = {_normalize_grade(profile.get("grade")), _normalize_grade(profile.get("summer_grade"))}
    enrollments = profile.get("program_enrollments")
    if isinstance(enrollments, list):
        for row in enrollments:
            if isinstance(row, dict):
                grades.add(_normalize_grade(row.get("grade")))
    return {grade for grade in grades if grade}


def _parse_iso(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed.astimezone()


def _looks_like_task_cancel_intent(text: str) -> bool:
    compact = "".join(str(text or "").split())
    if not compact:
        return False
    status_question_terms = (
        "取消了吗",
        "关闭了吗",
        "关掉了吗",
        "删掉了吗",
        "删除了吗",
        "还会提醒吗",
        "为什么不能取消",
        "为什么不能关闭",
        "为什么不能删除",
    )
    if any(term in compact for term in status_question_terms):
        return False
    cancel_terms = (
        "取消",
        "撤销",
        "撤回",
        "作废",
        "终止",
        "关闭",
        "关掉",
        "关了",
        "闭关",
        "删掉",
        "删除",
        "不用做",
        "不用再做",
        "不做了",
        "不用处理",
        "不用管",
        "先不用管",
        "别提醒",
        "不要提醒",
        "不用再提醒",
        "不要再提醒",
        "停止提醒",
        "停掉提醒",
    )
    return any(term in compact for term in cancel_terms)


class TuoguanToolService:
    """Execute tutoring operations under a trusted gateway identity."""

    def __init__(
        self,
        store: TuoguanStore | None = None,
        *,
        platform: str,
        user_id: str,
        user_name: str = "",
        chat_id: str = "",
        session_key: str = "",
        identity: UserIdentity | None = None,
    ) -> None:
        self.store = store or TuoguanStore()
        self.platform = str(platform or "")
        self.user_id = str(user_id or "")
        self.user_name = str(user_name or "")
        self.chat_id = str(chat_id or "")
        self.session_key = str(session_key or "")
        # Runtime Tool handlers pass the immutable identity established by the
        # XiaoYou Runtime Contract.  Direct service construction remains for
        # offline/domain tests, where the caller still has to resolve through
        # the same repository-backed IdentityService.
        self.identity = identity or IdentityService(self.store).resolve(
            self.platform,
            self.user_id,
            user_name=self.user_name,
            chat_id=self.chat_id,
        )
        self.permissions = PermissionService(self.store)

    @staticmethod
    def _ok(
        action: str,
        *,
        data: dict[str, Any] | None = None,
        message: str = "",
        already_applied: bool = False,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "action": action,
            "already_applied": already_applied,
            "data": data or {},
            "message": message,
        }

    @staticmethod
    def _error(error: str, message: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error": error,
            "message": message,
            "data": {},
        }

    def _approved(self) -> dict[str, Any] | None:
        if self.identity.approval_state == "approved":
            return None
        return self._error(
            "account_not_approved",
            "当前企业微信账号尚未完成正式身份确认，暂无正式权限；如需启用，请由老板确认。",
        )

    def read_agenda_work_facts(self) -> dict[str, Any]:
        """Read only the server-attested facts for this exact Agenda turn.

        The method has no model-provided object selector.  It can therefore
        neither widen the service identity's visibility nor turn a fact from a
        different tenant/partition into business context.
        """

        denied = self._approved()
        if denied:
            return denied
        try:
            from .runtime_contract import current_trusted_turn

            turn = current_trusted_turn()
            database = str(os.getenv("XIAOYOU_AGENDA_SERVICE_INGRESS_DB") or "").strip()
            if turn is None or not database or str(getattr(turn, "platform", "")) != "agenda_service_work":
                return self._error("agenda_work_facts_untrusted", "当前回合没有可信 Agenda 工作事实，本轮未读取。")
            with closing(sqlite3.connect(database, timeout=5.0)) as connection:
                connection.row_factory = sqlite3.Row
                ticket = connection.execute(
                    "SELECT ticket_id,work_ids_json FROM agenda_service_tickets "
                    "WHERE tenant_id=? AND service_identity=? AND agent_session_id=? AND agent_turn_id=? AND state='agent_claimed'",
                    (str(getattr(turn, "tenant_id", "")), str(self.identity.canonical_user_id), str(getattr(turn, "session_id", "")), str(getattr(turn, "turn_id", ""))),
                ).fetchone()
                if ticket is None:
                    return self._error("agenda_work_facts_ticket_missing", "当前 Agenda 工单未通过可信关联，本轮未读取。")
                work_ids = [str(value) for value in json.loads(str(ticket["work_ids_json"]))]
                facts: list[dict[str, Any]] = []
                for work_id in work_ids:
                    fact = connection.execute(
                        "SELECT payload_ref FROM work_facts WHERE work_id=? AND tenant_id=?",
                        (work_id, str(getattr(turn, "tenant_id", ""))),
                    ).fetchone()
                    if fact is None:
                        continue
                    payload = connection.execute(
                        "SELECT payload_json FROM agenda_workspace_payloads WHERE payload_ref=? AND tenant_id=?",
                        (str(fact["payload_ref"]), str(getattr(turn, "tenant_id", ""))),
                    ).fetchone()
                    if payload is None:
                        continue
                    decoded = json.loads(str(payload["payload_json"]))
                    if isinstance(decoded, dict):
                        # The original payload is intentionally immutable, but
                        # a continuation turn must reason from the *current*
                        # authoritative lifecycle state rather than repeat a
                        # stale question. This uses no model-selected object:
                        # the task id came from the attested ticket fact.
                        if str(decoded.get("source_kind") or "") == "workspace_task":
                            task_id = str(decoded.get("task_id") or "")
                            current = next(
                                (
                                    row for row in self.store.load_tasks()
                                    if isinstance(row, dict) and str(row.get("id") or "") == task_id
                                ),
                                None,
                            )
                            if isinstance(current, dict) and str(current.get("tenant_id") or "") == str(getattr(turn, "tenant_id", "")):
                                decoded = {
                                    **decoded,
                                    "current_lifecycle": {
                                        "status": str(current.get("status") or "pending"),
                                        "updated_at": str(current.get("updated_at") or ""),
                                        "agenda_followup": (
                                            dict(current.get("agenda_followup"))
                                            if isinstance(current.get("agenda_followup"), dict)
                                            else {}
                                        ),
                                        "agenda_continuation": (
                                            dict(current.get("agenda_continuation"))
                                            if isinstance(current.get("agenda_continuation"), dict)
                                            else {}
                                        ),
                                        "agenda_contact_history": [
                                            dict(value)
                                            for value in (current.get("agenda_contact_history") or [])
                                            if isinstance(value, dict)
                                        ][-12:],
                                        # Durable outbox state is the only
                                        # delivery fact.  A staged contact is
                                        # not a delivered message, while a
                                        # WeCom-accepted delivery still is not
                                        # evidence of a human reply or task
                                        # completion.  Keep this structured
                                        # and read-only so Hermes can decide
                                        # whether actual progress is possible.
                                        "contact_delivery_observations": self._agenda_task_delivery_observations(
                                            tenant_id=str(getattr(turn, "tenant_id", "")),
                                            task=current,
                                            current_ticket_id=str(ticket["ticket_id"]),
                                        ),
                                    },
                                }
                        facts.append(decoded)
        except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError):
            return self._error("agenda_work_facts_unavailable", "可信 Agenda 工作事实暂时不可读取，本轮未据此执行。")
        return self._ok(
            "read_agenda_work_facts",
            data={"ticket_id": str(ticket["ticket_id"]), "facts": facts, "count": len(facts)},
            message="已读取本回合可信 Agenda 工作事实。",
        )

    def _agenda_task_delivery_observations(
        self,
        *,
        tenant_id: str,
        task: dict[str, Any],
        current_ticket_id: str,
    ) -> list[dict[str, Any]]:
        """Return delivery observations for one current task without IDs.

        Agenda uses this only to correct an earlier loss of truth in which a
        locally staged reply was treated as the entire contact history.  It
        does not query model-selected people or work, and it intentionally
        exposes no reply, operation, ticket or channel identifiers to Hermes.
        """

        contact_rows = [
            dict(item) for item in (task.get("agenda_contact_history") or [])
            if isinstance(item, dict)
        ]
        ticket_ids = {str(current_ticket_id or "").strip()}
        for item in contact_rows:
            ticket = str(item.get("ticket_id") or "").strip()
            if ticket:
                ticket_ids.add(ticket)
        operation_labels: dict[str, tuple[str, str]] = {
            "agenda-notice:" + ticket: ("agenda_status_notice", "")
            for ticket in ticket_ids if ticket
        }
        for item in contact_rows:
            ticket = str(item.get("ticket_id") or "").strip()
            if ticket:
                operation_labels["agenda-notice:" + ticket] = (
                    "agenda_status_notice",
                    str(item.get("recorded_at") or ""),
                )
        for item in contact_rows:
            candidate_id = str(item.get("candidate_id") or "").strip()
            if candidate_id:
                operation_labels[
                    "proactive-notice:relationship_touch:" + str(tenant_id) + ":relationship_touch:" + candidate_id
                ] = ("model_selected_task_party_followup", str(item.get("recorded_at") or ""))
        if not operation_labels:
            return []
        try:
            from .direct_reply_recovery import get_direct_reply_recovery_manager

            outbox = get_direct_reply_recovery_manager().outbox
            rows = []
            for operation_id, (kind, recorded_at) in operation_labels.items():
                job = outbox.get(operation_id)
                if job is None or job.tenant_id != str(tenant_id):
                    continue
                state = str(job.delivery_state or "not_ready")
                rows.append({
                    "kind": kind,
                    "delivery_observation": (
                        "wecom_accepted" if state == "delivered"
                        else "delivery_failed" if state in {"failed", "suppressed"}
                        else "delivery_pending" if state in {"pending", "leased", "sending"}
                        else "not_deliverable"
                    ),
                    "human_response_observation": "not_observed",
                    "recorded_at": recorded_at,
                })
            return rows[-20:]
        except Exception:
            # Failure to read the outbox must never become a false claim that
            # a contact failed or was delivered.
            return []

    def agenda_contact_current_task_party(
        self,
        *,
        message: str,
        reason: str,
        action_type: str = "ask_task_fact",
        evidence_requirement: str = "",
        operation_id: str,
    ) -> dict[str, Any]:
        """Let Hermes progress its one attested task by contacting its assignee.

        The model may choose this Tool or a later recheck and composes the
        message/reason.  It cannot select a tenant, task, recipient, channel,
        delivery identity or operation id: all of those are derived from the
        current signed Agenda ticket and checked again at delivery.
        """

        denied = self._approved()
        if denied:
            return denied
        normalized_action = str(action_type or "ask_task_fact").strip()
        if normalized_action not in {"ask_task_fact", "ask_task_result", "task_companion_followup"}:
            return self._error("agenda_task_contact_action_invalid", "当前任务只能围绕进度、结果或明确工作事实联系责任人。")

        def execute() -> dict[str, Any]:
            from .agenda_task_lifecycle import (
                AgendaTaskLifecycleError,
                current_task_contact_target,
                record_current_task_contact_attempt,
            )
            from .proactive_work import effective_proactive_permission

            try:
                target = current_task_contact_target(self)
            except AgendaTaskLifecycleError as exc:
                return self._error(str(exc), "当前任务的可信责任人已经变化，本轮没有联系任何人。")
            permission = effective_proactive_permission(
                self.store,
                target_role=target["target_role"],
                target_user_id=target["target_user_id"],
                action_type=normalized_action,
                related_task_id=target["task_id"],
            )
            if not permission.get("allowed"):
                return self._error(
                    str(permission.get("reason_code") or "agenda_task_contact_not_allowed"),
                    "当前责任人不满足主动联系的可信授权或可达条件，本轮没有创建外发。",
                )
            candidate = submit_relationship_touch_candidate(
                self.store,
                identity=self.identity,
                target_role=target["target_role"],
                target_user_id=target["target_user_id"],
                target_name=target["target_name"],
                touch_type="record_relief" if target["target_role"] == "teacher" else "manager_assist",
                message=str(message or "").strip(),
                reason=str(reason or "").strip(),
                value="当前逾期或待确认任务的模型自主推进",
                work_related=True,
                private_emotional_support=False,
                requires_authorization=False,
                external_send_allowed=True,
                suggested_send_at="",
                status="candidate",
                operation_id=operation_id + ":candidate",
                source_text=str(reason or "").strip(),
                source_message_id=target["message_id"],
                action_type=normalized_action,
                related_task_id=target["task_id"],
                evidence_requirement=str(evidence_requirement or "").strip(),
                agenda_ticket_id=target["ticket_id"],
            )
            if not candidate.get("ok"):
                return candidate
            candidate_row = candidate.get("candidate") if isinstance(candidate.get("candidate"), dict) else {}
            candidate_id = str(candidate_row.get("candidate_id") or "")
            if not candidate_id:
                return self._error("agenda_task_contact_candidate_missing", "当前主动联系候选没有形成可信编号，本轮未发送。")
            execution = execute_relationship_touch_state(
                self.store,
                identity=self.identity,
                candidate_id=candidate_id,
                operation_id=operation_id + ":deliver",
            )
            if not execution.get("ok"):
                # Candidate persistence is real, but delivery did not become
                # a fact.  Return that distinction instead of turning a
                # partially completed attempt into a fictitious send.
                return self._ok(
                    "agenda_contact_current_task_party",
                    data={
                        "candidate": candidate_row,
                        "contact_attempt": "not_delivered",
                        "delivery_error": str(execution.get("error") or "delivery_not_created"),
                        "writeback_verified": bool(candidate.get("writeback_verified")),
                    },
                    message="已记录本次需要联系责任人的工作事实，但当前没有取得可信发送结果。",
                )
            recorded = record_current_task_contact_attempt(
                service=self,
                target_user_id=target["target_user_id"],
                candidate_id=candidate_id,
                delivery_state=str(execution.get("delivery_state") or "not_ready"),
                delivery_id=str(execution.get("delivery_id") or ""),
                operation_id=operation_id,
            )
            verified = bool(execution.get("writeback_verified") and recorded.get("writeback_verified"))
            return self._ok(
                "agenda_contact_current_task_party",
                data={
                    "task_id": target["task_id"],
                    "contact_attempt": (
                        "wecom_accepted" if bool(execution.get("wecom_accepted"))
                        else "delivery_pending"
                    ),
                    "human_response_observation": "not_observed",
                    "writeback_verified": verified,
                },
                message=(
                    "已取得企业微信接受回执；仍需等待责任人的真实回复或任务结果。"
                    if bool(execution.get("wecom_accepted"))
                    else "已进入可信投递链路；尚未取得企业微信接受回执，不能声称对方已收到。"
                ),
            )

        return self._operation(operation_id, "agenda_contact_current_task_party", execute)

    def _visible_students(self) -> dict[str, dict[str, Any]]:
        return self._visible_students_for(self.identity)

    def _visible_students_for(self, identity: UserIdentity) -> dict[str, dict[str, Any]]:
        students = self.store.read_json("students.json", {})
        if not isinstance(students, dict):
            return {}
        return {
            str(name): deepcopy(profile)
            for name, profile in students.items()
            if isinstance(profile, dict)
            and self.permissions.can_query_student(identity, str(name))
        }

    def _filter_student_coverage(
        self,
        result: dict[str, Any],
        *,
        student_name: str,
        limit: int,
        label: str,
    ) -> dict[str, Any]:
        filtered = deepcopy(result if isinstance(result, dict) else {})
        safe_limit = max(1, min(int(limit or 30), 100))
        covered = [item for item in filtered.get("covered_students") or [] if isinstance(item, dict)]
        missing = [item for item in filtered.get("missing_students") or [] if isinstance(item, dict)]
        filtered["available_covered_count"] = int(filtered.get("covered_count") or len(covered))
        filtered["available_missing_count"] = int(filtered.get("missing_count") or len(missing))
        requested = str(student_name or "").strip()
        if not requested:
            filtered["covered_students"] = covered[:safe_limit]
            filtered["missing_students"] = missing[:safe_limit]
            filtered["rendered_count"] = len(filtered["covered_students"]) + len(filtered["missing_students"])
            return filtered

        students = self.store.read_json("students.json", {})
        if not isinstance(students, dict) or requested not in students:
            return self._error("student_not_found", f"没有找到学生“{requested}”。")
        if requested not in self._visible_students():
            return self._error("permission_denied", f"学生“{requested}”不在当前账号的负责范围内。")

        covered = [item for item in covered if str(item.get("student_name") or "") == requested]
        missing = [item for item in missing if str(item.get("student_name") or "") == requested]
        filtered.update({
            "student_name_filter": requested,
            "total_students": 1,
            "covered_count": len(covered),
            "missing_count": len(missing),
            "covered_students": covered,
            "missing_students": missing,
            "rendered_count": len(covered) + len(missing),
        })
        if covered:
            filtered["rendered_text"] = f"{requested}：近 {filtered.get('days')} 天有{label}。"
        elif missing:
            filtered["rendered_text"] = f"{requested}：近 {filtered.get('days')} 天未查到{label}。"
        else:
            filtered["coverage_status"] = "not_concluded_from_current_term_data"
            filtered["rendered_text"] = f"{requested}：{str(result.get('rendered_text') or '当前数据不足，不能下结论。')}"
        return filtered

    def _summer_student_names(self) -> set[str]:
        enrollments = self.store.read_json("summer_enrollments.json", [])
        rows = enrollments.values() if isinstance(enrollments, dict) else enrollments
        names: set[str] = set()
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            program = str(row.get("program_id") or row.get("program") or "")
            status = str(row.get("status") or "active").lower()
            if program not in {"summer_2026", "summer_class"} or status in {"inactive", "cancelled", "deleted"}:
                continue
            name = str(row.get("student_name") or row.get("name") or "").strip()
            if name:
                names.add(name)
        return names

    def _current_query_staff_entries(self) -> list[dict[str, Any]]:
        """Return staff usable for current query authorization.

        Once personnel identity authority is enforced, legacy staff metadata may
        still provide aliases and directory labels, but it cannot create an
        active person, role, or permission.  Before an institution has migrated
        to the authority aggregate, keep the historical read-only compatibility
        surface.
        """

        from .personnel_identity_authority import active_identity_snapshot
        from .staff_directory import _build_entries

        entries = [row for row in _build_entries(self.store) if isinstance(row, dict)]
        snapshot = active_identity_snapshot(self.store, tenant_id=current_tenant_id())
        if snapshot is None:
            return [row for row in entries if bool(row.get("is_active_staff"))]
        approved = {
            str(user_id)
            for user_id, resolved in snapshot.items()
            if str(getattr(resolved, "approval_state", "") or "") == "approved"
        }
        return [
            row
            for row in entries
            if str(row.get("user_id") or "") in approved
            and int(row.get("identity_authority_rank") or 0) >= 3
            and bool(row.get("is_active_staff"))
        ]

    def _query_teacher_identity_by_name(self, teacher_name: str) -> UserIdentity | None:
        requested = str(teacher_name or "").strip()
        if not requested:
            return None
        normalized = "".join(requested.casefold().split())
        matches: list[dict[str, Any]] = []
        for entry in self._current_query_staff_entries():
            if str(entry.get("role") or "") != "teacher":
                continue
            aliases = {
                "".join(str(value or "").casefold().split())
                for value in (
                    entry.get("business_name"),
                    entry.get("staff_name"),
                    entry.get("directory_name"),
                    entry.get("user_id"),
                    *(entry.get("known_aliases") or []),
                )
                if str(value or "").strip()
            }
            if normalized in aliases:
                matches.append(entry)
        user_ids = {str(row.get("user_id") or "") for row in matches if str(row.get("user_id") or "")}
        if len(user_ids) != 1:
            return None
        row = next(row for row in matches if str(row.get("user_id") or "") in user_ids)
        user_id = str(row.get("user_id") or "")
        display_name = str(row.get("business_name") or row.get("directory_name") or user_id)
        return UserIdentity(self.platform, user_id, user_id, display_name, "teacher", "approved")

    def _teacher_identity_by_name(self, teacher_name: str) -> UserIdentity | None:
        requested = str(teacher_name or "").strip()
        if not requested:
            return None
        staff = self.store.read_json("staff.json", {})
        if isinstance(staff, dict):
            for user_id, profile in staff.items():
                if not isinstance(profile, dict):
                    continue
                if str(profile.get("role") or "") != "teacher":
                    continue
                names = {str(profile.get("name") or ""), str(user_id)}
                if requested in names:
                    return UserIdentity(
                        self.platform,
                        str(user_id),
                        str(user_id),
                        str(profile.get("name") or requested),
                        "teacher",
                        "approved",
                    )
        mapping = self.store.read_json("teacher_wecom_map.json", {})
        user_id = str(mapping.get(requested) or "") if isinstance(mapping, dict) else ""
        if user_id:
            return UserIdentity(self.platform, user_id, user_id, requested, "teacher", "approved")
        return None

    def _resolve_task_assignee(
        self,
        *,
        assignee_user_id: str = "",
        teacher_name: str = "",
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Resolve a real, active WeCom person for a task assignment.

        Model turns should be able to use the human name the boss supplied.
        The directory remains the authority for the eventual user id and
        delivery eligibility; this helper does not infer an identity from a
        previous conversation.
        """

        from .staff_directory import _build_entries

        requested_id = str(assignee_user_id or "").strip()
        requested_name = str(teacher_name or "").strip()
        entries = [row for row in _build_entries(self.store) if isinstance(row, dict)]
        by_id = [row for row in entries if str(row.get("user_id") or "") == requested_id] if requested_id else []
        normalized_name = "".join(requested_name.casefold().split())
        by_name = [
            row
            for row in entries
            if normalized_name
            and normalized_name in {
                "".join(str(value or "").casefold().split())
                for value in (
                    row.get("business_name"), row.get("staff_name"), row.get("directory_name"), row.get("user_id"),
                    *(row.get("known_aliases") or []),
                )
                if str(value or "").strip()
            }
        ]
        if requested_id and requested_name and by_id and by_name and str(by_id[0].get("user_id") or "") != str(by_name[0].get("user_id") or ""):
            return None, self._error("ambiguous_target", "老师姓名和企业微信账号指向不同人员，本轮没有创建任务。")
        candidates = by_id or by_name
        if not candidates:
            return None, self._error("teacher_not_found", "没有在当前可信人员目录中找到这位执行人，本轮没有创建任务。")
        if len({str(row.get("user_id") or "") for row in candidates}) != 1:
            return None, self._error("ambiguous_target", "找到多位可能的执行人，请补充完整姓名或企业微信账号后再创建任务。")
        entry = candidates[0]
        if not bool(entry.get("is_active_staff")):
            return None, self._error("target_staff_inactive", "该员工已离职、停用或不在可用人员目录中，不能再分配任务。")
        if not bool(entry.get("in_wecom_directory") or entry.get("is_whitelisted")):
            return None, self._error("target_wecom_unreachable", "该员工没有可信企业微信可达记录，任务没有创建，避免出现只建任务却无法通知。")
        role = str(entry.get("role") or "")
        if role not in {"teacher", "manager", "boss"}:
            return None, self._error("target_role_invalid", "任务执行人角色不明确，本轮没有创建任务。")
        return entry, None

    def _teacher_name_from_current_raw_text(self) -> str:
        try:
            from .runtime_foundation import current_raw_text
            raw_text = current_raw_text(self.identity.canonical_user_id)
        except Exception:
            raw_text = ""
        raw_text = str(raw_text or "")
        if not raw_text:
            return ""
        candidates: list[str] = []
        for entry in self._current_query_staff_entries():
            if str(entry.get("role") or "") != "teacher":
                continue
            candidates.extend([
                str(entry.get("business_name") or ""),
                str(entry.get("directory_name") or ""),
                str(entry.get("staff_name") or ""),
                str(entry.get("user_id") or ""),
                *[str(value or "") for value in entry.get("known_aliases") or []],
            ])
        candidates = sorted({name.strip() for name in candidates if name and name.strip()}, key=len, reverse=True)
        for name in candidates:
            if name in raw_text:
                return name
        return ""

    def _query_manager_can_view_teacher(self, teacher_user_id: str) -> bool:
        if self.identity.role != "manager":
            return True
        target = str(teacher_user_id or "").strip()
        return any(
            str(entry.get("user_id") or "") == target
            and str(entry.get("role") or "") == "teacher"
            for entry in self._current_query_staff_entries()
        )

    def _manager_can_view_teacher(self, teacher_user_id: str) -> bool:
        if self.identity.role != "manager":
            return True
        staff = self.store.read_json("staff.json", {})
        if not isinstance(staff, dict):
            return False
        manager = staff.get(self.identity.canonical_user_id, {})
        teacher = staff.get(teacher_user_id, {})
        if not isinstance(manager, dict) or not isinstance(teacher, dict):
            return False
        manager_programs = set(manager.get("program_ids") or [])
        teacher_programs = set(teacher.get("program_ids") or [])
        if manager_programs and teacher_programs and manager_programs & teacher_programs:
            return True
        manager_campuses = set(manager.get("campus_ids") or [])
        teacher_campuses = set(teacher.get("campus_ids") or [])
        return bool(manager_campuses and teacher_campuses and manager_campuses & teacher_campuses)

    def _visible_tasks(self) -> list[dict[str, Any]]:
        tasks = [
            task
            for task in self.store.load_tasks()
            if not task.get("safety_test")
            and str(task.get("source_type") or "").lower() != "safety_test"
        ]
        if self.identity.role == "boss":
            return tasks
        if self.identity.role == "teacher":
            return [
                task
                for task in tasks
                if str(task.get("assignee_userid") or "")
                == self.identity.canonical_user_id
            ]
        if self.identity.role == "manager":
            staff = self.store.read_json("staff.json", {})
            profile = (
                staff.get(self.identity.canonical_user_id, {})
                if isinstance(staff, dict)
                else {}
            )
            campuses = (
                set(profile.get("campus_ids") or [])
                if isinstance(profile, dict)
                else set()
            )
            return [
                task
                for task in tasks
                if str(task.get("campus_id") or "") in campuses
            ]
        return []

    def _query_visible_tasks(self) -> list[dict[str, Any]]:
        """Read visibility for the current single-institution query surface."""

        trusted_tenant = str(current_tenant_id() or "").strip()
        tasks = [
            task
            for task in self.store.load_tasks()
            if not task.get("safety_test")
            and str(task.get("source_type") or "").lower() != "safety_test"
            and (
                not str(task.get("tenant_id") or "").strip()
                or str(task.get("tenant_id") or "").strip() == trusted_tenant
            )
        ]
        if self.identity.role in {"boss", "manager"}:
            return tasks
        if self.identity.role == "teacher":
            return [
                task
                for task in tasks
                if str(task.get("assignee_userid") or "")
                == self.identity.canonical_user_id
            ]
        return []

    def _focus_key(self) -> str:
        return self.session_key or f"{self.platform}:{self.user_id}"

    def _read_focus(self) -> dict[str, Any]:
        data = self.store.read_json("model_focus.json", {})
        if not isinstance(data, dict):
            return {}
        item = data.get(self._focus_key(), {})
        return deepcopy(item) if isinstance(item, dict) else {}

    def _active_open_task(self) -> dict[str, Any] | None:
        visible = [
            task
            for task in self._visible_tasks()
            if str(task.get("status") or "") not in _CLOSED_STATUSES
        ]
        by_id = {str(task.get("id") or ""): task for task in visible}
        active = self.store.read_json("active_task_context.json", {})
        active_item = active.get(self.identity.canonical_user_id, {}) if isinstance(active, dict) else {}
        preferred_ids = [
            str(active_item.get("task_id") or "") if isinstance(active_item, dict) else "",
            str(self._read_focus().get("task_id") or ""),
        ]
        for task_id in preferred_ids:
            if task_id and task_id in by_id:
                return deepcopy(by_id[task_id])
        current = current_task_for_user(visible, self.identity.canonical_user_id)
        return deepcopy(current) if isinstance(current, dict) else None

    def _active_context_open_task(self) -> dict[str, Any] | None:
        """Return only the durable, in-scope task anchor for this person."""

        active = self.store.read_json("active_task_context.json", {})
        item = active.get(self.identity.canonical_user_id, {}) if isinstance(active, dict) else {}
        if not isinstance(item, dict):
            return None
        expires_at = _parse_iso(item.get("expires_at"))
        if expires_at is not None and expires_at < datetime.now().astimezone():
            return None
        task_id = str(item.get("task_id") or "")
        if not task_id:
            return None
        for task in self._visible_tasks():
            if str(task.get("id") or "") == task_id and not task_is_closed(task):
                return deepcopy(task)
        return None

    def _write_focus(self, **updates: Any) -> None:
        focus_key = self._focus_key()

        def mutate(value: Any) -> dict[str, Any]:
            data = value if isinstance(value, dict) else {}
            item = data.get(focus_key, {})
            item = item if isinstance(item, dict) else {}
            item.update({key: value for key, value in updates.items() if value is not None})
            item["updated_at"] = datetime.now().isoformat(timespec="seconds")
            data[focus_key] = item
            return data

        self.store.update_json("model_focus.json", {}, mutate)

    def _write_user_focus(self, user_id: str, **updates: Any) -> None:
        key = f"{self.platform}:{str(user_id or '').strip()}"

        def mutate(value: Any) -> dict[str, Any]:
            data = value if isinstance(value, dict) else {}
            item = data.get(key, {})
            item = item if isinstance(item, dict) else {}
            item.update({name: value for name, value in updates.items() if value is not None})
            item["updated_at"] = datetime.now().isoformat(timespec="seconds")
            data[key] = item
            return data

        self.store.update_json("model_focus.json", {}, mutate)

    def _remember_user_task_context(self, user_id: str, task: dict[str, Any], *, ttl_hours: int = 36) -> None:
        user_id = str(user_id or "").strip()
        task_id = str(task.get("id") or "")
        if not user_id or not task_id:
            return
        now = datetime.now()
        started_at = now.isoformat(timespec="seconds")
        expires_at = (now + timedelta(hours=ttl_hours)).isoformat(timespec="seconds")

        def update_active(active: Any) -> dict[str, Any]:
            active = active if isinstance(active, dict) else {}
            active[user_id] = {
                "user_id": user_id,
                "task_id": task_id,
                "task_title": str(task.get("title") or ""),
                "student_id": str(task.get("student_id") or task.get("student_name") or ""),
                "student_name": str(task.get("student_name") or ""),
                "task_type": str(task.get("type") or "manual_assignment"),
                "status": "selected",
                "started_at": started_at,
                "expires_at": expires_at,
                "candidate_task_ids": [],
                "source": "assigned_task_created",
                "owner_user_id": str(task.get("created_by") or ""),
                "expected_report_at": str(task.get("due_at") or ""),
                "original_owner_text": str(task.get("source_text") or task.get("title") or ""),
            }
            return active

        def update_pending(pending: Any) -> dict[str, Any]:
            pending = pending if isinstance(pending, dict) else {}
            pending[user_id] = {
                "user_id": user_id,
                "task_id": task_id,
                "task_title": str(task.get("title") or ""),
                "task_level": str(task.get("level") or "C"),
                "task_type": str(task.get("type") or "manual_assignment"),
                "student_id": str(task.get("student_id") or task.get("student_name") or ""),
                "student_name": str(task.get("student_name") or ""),
                "source": "new_task_notification",
                "trigger_words": ["继续", "开始", "处理", "1", "开始下一个", "处理下一个"],
                "created_at": started_at,
                "expires_at": expires_at,
                "owner_user_id": str(task.get("created_by") or ""),
                "expected_report_at": str(task.get("due_at") or ""),
                "original_owner_text": str(task.get("source_text") or task.get("title") or ""),
            }
            return pending

        self.store.update_json("active_task_context.json", {}, update_active)
        self.store.update_json("pending_next_task_context.json", {}, update_pending)

    def _clear_task_context_for_task(self, task_id: str) -> dict[str, int]:
        normalized_id = str(task_id or "").strip()
        if not normalized_id:
            return {"active_removed": 0, "pending_removed": 0, "focus_removed": 0}
        counts = {"active_removed": 0, "pending_removed": 0, "focus_removed": 0}

        def clear_mapping(value: Any, counter_name: str) -> Any:
            data = value if isinstance(value, dict) else {}
            kept: dict[str, Any] = {}
            removed = 0
            for key, item in data.items():
                if isinstance(item, dict) and str(item.get("task_id") or "") == normalized_id:
                    removed += 1
                    continue
                kept[key] = item
            counts[counter_name] = removed
            return kept if removed else JSON_NO_CHANGE

        self.store.update_json("active_task_context.json", {}, lambda value: clear_mapping(value, "active_removed"))
        self.store.update_json("pending_next_task_context.json", {}, lambda value: clear_mapping(value, "pending_removed"))

        def clear_focus(value: Any) -> Any:
            data = value if isinstance(value, dict) else {}
            kept: dict[str, Any] = {}
            removed = 0
            for key, item in data.items():
                if isinstance(item, dict) and str(item.get("task_id") or "") == normalized_id:
                    removed += 1
                    continue
                kept[key] = item
            counts["focus_removed"] = removed
            return kept if removed else JSON_NO_CHANGE

        self.store.update_json("model_focus.json", {}, clear_focus)
        return counts

    def _suppress_pending_notifications_for_task(self, task_id: str, *, reason: str) -> int:
        normalized_id = str(task_id or "").strip()
        if not normalized_id:
            return 0
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        changed_count = {"value": 0}

        def suppress(outbox: Any) -> Any:
            rows = outbox if isinstance(outbox, list) else []
            changed = False
            for item in rows:
                if not isinstance(item, dict):
                    continue
                if str(item.get("task_id") or "") != normalized_id:
                    continue
                status = str(item.get("status") or "")
                if status not in {"pending", "retry_pending", "sending"}:
                    continue
                if status == "sending":
                    item["status"] = "result_unknown"
                    item["result_unknown_at"] = stamp
                    item["last_error"] = f"{reason}_while_send_in_flight"
                else:
                    item["status"] = "suppressed"
                    item["suppressed_at"] = stamp
                    item["suppressed_reason"] = reason
                changed = True
                changed_count["value"] += 1
            return rows if changed else JSON_NO_CHANGE

        self.store.update_json("notification_outbox.json", [], suppress)
        return int(changed_count["value"])

    def _retire_task_linked_work(self, task: dict[str, Any], *, operation_id: str, reason: str) -> dict[str, Any]:
        """Stop open task-linked proactive work without deleting its evidence."""

        from .digital_employee_state import _fold_relationship_touch_candidates, update_relationship_touch_candidate_status
        from .proactive_work import query_goal_actions, submit_goal_action

        task_id = str(task.get("id") or "")
        system_identity = UserIdentity(
            platform="system",
            platform_user_id="task_lifecycle",
            canonical_user_id="task_lifecycle",
            person_name="小优任务生命周期",
            role="boss",
            approval_state="approved",
        )
        retired_touch_ids: list[str] = []
        for candidate in _fold_relationship_touch_candidates(self.store).values():
            if str(candidate.get("related_task_id") or "") != task_id:
                continue
            if str(candidate.get("status") or "") in {"resolved", "expired", "superseded", "suppressed"}:
                continue
            candidate_id = str(candidate.get("candidate_id") or "")
            updated = update_relationship_touch_candidate_status(
                self.store,
                identity=system_identity,
                candidate_id=candidate_id,
                status="superseded",
                operation_id=f"{operation_id}:retire_touch:{candidate_id}",
                failure_reason=reason,
                source_text=f"task_id={task_id}; reason={reason}",
            )
            if updated.get("writeback_verified"):
                retired_touch_ids.append(candidate_id)

        retired_outbox = {"suppressed": 0, "result_unknown": 0}
        if retired_touch_ids:
            retired_notification_ids = {
                f"relationship_touch:{candidate_id}" for candidate_id in retired_touch_ids
            }
            stamp = datetime.now().astimezone().isoformat(timespec="seconds")

            def retire_outbox(rows: Any) -> Any:
                rows = rows if isinstance(rows, list) else []
                changed = False
                for item in rows:
                    if not isinstance(item, dict) or str(item.get("id") or "") not in retired_notification_ids:
                        continue
                    status = str(item.get("status") or "")
                    if status in {"pending", "retry_pending"}:
                        item["status"] = "suppressed"
                        item["suppressed_at"] = stamp
                        item["suppressed_reason"] = reason
                        retired_outbox["suppressed"] += 1
                        changed = True
                    elif status == "sending":
                        item["status"] = "result_unknown"
                        item["result_unknown_at"] = stamp
                        item["last_error"] = f"{reason}_while_send_in_flight"
                        retired_outbox["result_unknown"] += 1
                        changed = True
                return rows if changed else JSON_NO_CHANGE

            self.store.update_json("notification_outbox.json", [], retire_outbox)

        goal_action_id = str(task.get("goal_action_id") or "")
        goal_action_update: dict[str, Any] = {}
        if (
            goal_action_id
            and str(task.get("goal_id") or "")
            and str(task.get("status") or "") in {"cancelled", "expired", "superseded"}
        ):
            actions = query_goal_actions(
                self.store,
                identity=system_identity,
                goal_id=str(task.get("goal_id") or ""),
                include_closed=True,
                limit=100,
            )
            action = next(
                (
                    item for item in actions.get("goal_actions") or []
                    if str(item.get("goal_action_id") or "") == goal_action_id
                ),
                None,
            )
            if isinstance(action, dict) and str(action.get("status") or "") not in {"verified", "resolved", "cancelled", "superseded"}:
                goal_action_update = submit_goal_action(
                    self.store,
                    identity=system_identity,
                    goal_id=str(task.get("goal_id") or ""),
                    action_type=str(action.get("action_type") or "create_low_risk_task"),
                    summary=str(action.get("summary") or task.get("title") or "任务生命周期收口"),
                    operation_id=f"{operation_id}:retire_goal_action",
                    goal_action_id=goal_action_id,
                    status="cancelled" if str(task.get("status") or "") == "cancelled" else "superseded",
                    source_text=f"task_id={task_id}; task_status={task.get('status')}; reason={reason}",
                )
        return {
            "retired_relationship_touch_ids": retired_touch_ids,
            "retired_relationship_outbox": retired_outbox,
            "goal_action_update": goal_action_update,
            "writeback_verified": bool(
                not goal_action_update or goal_action_update.get("writeback_verified")
            ),
        }

    @staticmethod
    def _looks_like_goal_workspace_task_misuse(*, raw_text: str, title: str, operation_id: str, assignee_user_id: str, student_name: str) -> bool:
        """Protect single-task creation from long-running goal workspace misuse.

        This is a tool boundary, not pre-model routing. The model may choose the
        create-task tool, but this tool only creates one concrete internal task.
        Institution goals, monthly coverage plans and batch parent-communication
        objectives must first live in the goal workspace.
        """
        combined = "".join(str(value or "") for value in (raw_text, title, operation_id, student_name))
        compact = "".join(combined.split()).lower()
        has_goal_subject = any(term in compact for term in ("家长沟通", "正式托管", "覆盖目标", "目标", "每个孩子", "每名孩子", "每个正式托管孩子"))
        has_bulk_or_plan = any(term in compact for term in ("这个月", "本月", "每个", "每名", "全部", "全员", "覆盖", "推进", "方案", "计划", "按这个方案"))
        generated_goal_task = str(operation_id or "").startswith(("task_parent_comm", "task_goal_", "goal_task_"))
        missing_concrete_target = not str(student_name or "").strip() and (not str(assignee_user_id or "").strip() or has_bulk_or_plan)
        return bool(generated_goal_task or (has_goal_subject and (has_bulk_or_plan or missing_concrete_target)))

    @staticmethod
    def _relative_due_at(text: str) -> str:
        return parse_business_due_at(text, allow_default=False)

    def _trusted_runtime_raw_text(self, operation: str) -> str:
        """Return the live inbound text only when runtime ownership matches this store."""

        try:
            from .runtime_foundation import current_raw_text, write_authorization_for

            runtime_auth = write_authorization_for(self.identity.canonical_user_id, operation)
            if not runtime_auth:
                return ""
            authorized_dir = str(runtime_auth.get("data_dir") or "")
            if authorized_dir and authorized_dir != str(self.store.data_dir.resolve()):
                return ""
            return current_raw_text(self.identity.canonical_user_id)
        except Exception:
            return ""

    @staticmethod
    def _normalize_unverified_exception_truth(result: dict[str, Any]) -> dict[str, Any]:
        """Project a legacy failed exception as one truthful outcome.

        Older ledgers could contain ``system_error`` + failed/unverified
        Receipt but an ``applied`` idempotency label.  The original immutable
        audit entry is retained; every business read/replay now observes the
        only defensible interpretation: result unknown, never verified as
        applied.  This is an audit compatibility projection, not a retry and
        not a claim that no write occurred.
        """

        normalized = deepcopy(result if isinstance(result, dict) else {})
        receipt = normalized.get("execution_receipt")
        if not isinstance(receipt, dict):
            return normalized
        if not (
            str(normalized.get("error") or receipt.get("error_code") or "") == "system_error"
            and str(receipt.get("status") or "") == "failed"
            and not bool(receipt.get("writeback_verified"))
            and str(receipt.get("idempotency_result") or "") in {"applied", "replayed"}
        ):
            return normalized
        corrected = {**receipt, "idempotency_result": "result_unknown"}
        normalized["execution_receipt"] = corrected
        normalized["already_applied"] = False
        normalized["execution_truth_projection"] = "legacy_unverified_exception_result_unknown"
        if isinstance(normalized.get("data"), dict):
            normalized["data"] = {**normalized["data"], "execution_receipt": corrected}
        return normalized

    def _operation(
        self,
        operation_id: str,
        operation: str,
        execute: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        key = str(operation_id or "").strip()
        if not key:
            return build_execution_receipt(self._error(
                "operation_id_required",
                "写操作缺少 operation_id，未执行，避免产生重复数据。",
            ), operation_id="", operation=operation, idempotency_result="rejected")
        try:
            from .agenda_service_policy import enforce_agenda_service_write_operation
            service_policy_denial = enforce_agenda_service_write_operation(service=self, operation=operation)
        except Exception:
            service_policy_denial = {"ok": False, "error": "agenda_service_policy_unavailable", "message": "后台服务策略不可用，本轮未执行。", "data": {}}
        if service_policy_denial is not None:
            return build_execution_receipt(
                service_policy_denial,
                operation_id=key,
                operation=operation,
                idempotency_result="rejected",
            )
        from .runtime_foundation import write_authorization_for
        from .write_guard import authorized_business_write, guard_enabled, record_unauthorized_tool_attempt

        runtime_auth = write_authorization_for(self.identity.canonical_user_id, operation)
        if runtime_auth is not None:
            current_data_dir = str(self.store.data_dir.resolve())
            if str(runtime_auth.get("data_dir") or "") and str(runtime_auth.get("data_dir") or "") != current_data_dir:
                runtime_auth = None
        try:
            from .runtime_foundation import current_raw_text
            raw_text = current_raw_text(self.identity.canonical_user_id) if runtime_auth is not None else ""
        except Exception:
            raw_text = ""
        compact_raw = "".join(str(raw_text or "").split())
        write_operations = {
            "record_student",
            "record_summer_lesson",
            "register_student",
            "register_summer_student",
            "create_trial_lead",
            "create_task",
            "cancel_task",
            "offboard_staff",
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
            "submit_work_commitment",
            "update_work_commitment",
            "submit_wakeup_request",
            "update_wakeup_request",
            "submit_due_wakeup_candidate",
            "submit_business_event",
            "submit_action_execution",
            "submit_fact_gap_candidate",
            "submit_staff_voice_signal",
            "submit_relationship_touch_candidate",
            "submit_proactive_authorization",
            "execute_relationship_touch",
            "update_relationship_touch",
            "submit_goal_action",
            "update_attention_thread",
            "agenda_contact_current_task_party",
            "review_project_opportunity",
            "governance_claim_query_reference",
            "governance_claim_submit",
            "governance_claim_record",
            "governance_claim_confirm",
            "governance_claim_activate_pending_identity",
        }
        if compact_raw in {
            "你再试一下",
            "再试一下",
            "试一下",
            "继续",
            "继续吧",
            "好的",
            "好",
            "嗯",
            "对",
            "可以",
        } and operation in write_operations:
            return build_execution_receipt(self._error(
                "ambiguous_retry_write_not_executed",
                "当前消息没有明确说明要写入的对象、动作和内容，不能沿用上文执行真实写入。请把要记录或修改的内容重新说完整。",
            ), operation_id=key, operation=operation, idempotency_result="rejected")
        capability_anchors = ("能力", "写入", "功能", "工具")
        capability_check_terms = (
            "检查",
            "自检",
            "看一下",
            "测一下",
            "测试",
            "试一下",
            "正常",
            "能用",
            "可用",
            "恢复",
            "不能用",
            "能不能用",
            "可不可用",
            "是否",
            "都能",
            "都可以",
        )
        capability_health_check = (
            any(anchor in compact_raw for anchor in capability_anchors)
            and any(term in compact_raw for term in capability_check_terms)
        )
        if capability_health_check and operation in write_operations:
            return build_execution_receipt(self._error(
                "capability_self_check_write_not_executed",
                "这是能力检查问题，不能通过真实写入来测试。请改用只读查询、工具目录或测试环境检查。",
            ), operation_id=key, operation=operation, idempotency_result="rejected")

        if guard_enabled(self.store.data_dir) and runtime_auth is None:
            try:
                from .runtime_foundation import current_ledger_id
                ledger_probe = current_ledger_id(self.identity.canonical_user_id)
            except Exception:
                ledger_probe = ""
            try:
                from .runtime_contract import current_trusted_turn
                trusted_turn = current_trusted_turn()
                session_user_probe = str(getattr(trusted_turn, "actor_user_id", "") or "")
                session_id_probe = str(getattr(trusted_turn, "session_id", "") or "")
            except Exception:
                session_user_probe = ""
                session_id_probe = ""
            import logging
            logging.getLogger(__name__).warning(
                "YOUYI_WRITE_AUTH_MISS operation=%s actor_hash=%s platform_user_hash=%s role=%s message_hash=%s chars=%s ledger_probe=%s session_user_hash=%s session_id=%s module=%s",
                operation,
                hashlib.sha256(self.identity.canonical_user_id.encode("utf-8")).hexdigest()[:12],
                hashlib.sha256(self.user_id.encode("utf-8")).hexdigest()[:12],
                self.identity.role,
                hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:12],
                len(raw_text),
                ledger_probe,
                hashlib.sha256(str(session_user_probe).encode("utf-8")).hexdigest()[:12],
                session_id_probe,
                __name__,
            )
            audit_id = record_unauthorized_tool_attempt(
                self.store.data_dir,
                operation=operation,
                operation_id=key,
                user_id=self.identity.canonical_user_id,
                reason="missing_or_mismatched_active_runtime_context",
            )
            return build_execution_receipt(self._error(
                "unauthorized_write_blocked",
                f"当前请求没有通过业务写入校验，未修改任何业务数据。审计编号：{audit_id}",
            ), operation_id=key, operation=operation, idempotency_result="rejected")
        receipt_key = f"{self.identity.canonical_user_id}:{operation}:{key}"
        receipt_claim: dict[str, Any] = {"claimed": False, "existing_result": None, "in_progress": False}
        runtime_auth = runtime_auth or {"ledger_id": "test_or_offline_runtime"}
        preaudit_id = f"audit_write_authorized_{uuid.uuid4().hex}"

        def claim_receipt(receipts: Any) -> Any:
            receipts = receipts if isinstance(receipts, dict) else {}
            existing = receipts.get(receipt_key)
            if isinstance(existing, dict) and isinstance(existing.get("result"), dict):
                receipt_claim["existing_result"] = deepcopy(existing["result"])
                return JSON_NO_CHANGE
            if isinstance(existing, dict) and str(existing.get("status") or "") == "in_progress":
                claimed_at = _parse_iso(existing.get("claimed_at"))
                if claimed_at is not None and datetime.now().astimezone() - claimed_at < timedelta(minutes=10):
                    receipt_claim["in_progress"] = True
                    return JSON_NO_CHANGE
            receipts[receipt_key] = {
                "actor": self.identity.canonical_user_id,
                "operation": operation,
                "operation_id": key,
                "status": "in_progress",
                "claimed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            receipt_claim["claimed"] = True
            return receipts

        with authorized_business_write(
            source="trusted_tool",
            operation_id=key,
            ledger_id=runtime_auth["ledger_id"],
            audit_id=preaudit_id,
            allowed_files={"tool_operations.json"},
        ):
            self.store.update_json("tool_operations.json", {}, claim_receipt)
        if isinstance(receipt_claim.get("existing_result"), dict):
            result = self._normalize_unverified_exception_truth(receipt_claim["existing_result"])
            prior_receipt = result.get("execution_receipt") if isinstance(result.get("execution_receipt"), dict) else {}
            result["already_applied"] = bool(
                str(prior_receipt.get("status") or "") == "completed"
                and bool(prior_receipt.get("writeback_verified"))
            )
            return build_execution_receipt(
                result, operation_id=key, operation=operation, idempotency_result="replayed"
            )
        if receipt_claim.get("in_progress"):
            return build_execution_receipt(self._error(
                "operation_already_in_progress",
                "同一个 operation_id 正在执行或等待写后反查，本轮未重复执行，避免产生重复写入。",
            ), operation_id=key, operation=operation, idempotency_result="in_progress")
        try:
            with authorized_business_write(
                source="trusted_tool",
                operation_id=key,
                ledger_id=runtime_auth["ledger_id"],
                audit_id=preaudit_id,
            ):
                result = execute()
        except Exception as exc:
            # Capability domain failures are a truthful, stable outcome.  Do
            # not erase them behind ``system_error``: the Agent needs to know
            # whether the protected write was rejected before execution (for
            # example ``person_not_suspended``) rather than treating every
            # failure as an unknown infrastructure result.  Unexpected
            # exceptions remain result-unknown and keep their opaque internal
            # diagnostic type for audit only.
            domain_error = str(exc or "").strip()
            is_stable_domain_error = (
                type(exc).__name__ in {"GovernanceError", "ClaimError", "IdentityAuthorityError", "AgendaTaskLifecycleError"}
                and bool(re.fullmatch(r"[a-z0-9_:-]+", domain_error))
            )
            result = self._error(
                domain_error if is_stable_domain_error else "system_error",
                "本轮操作未执行成功；系统已保留失败事实，不会把它当作已完成。",
            )
            result["diagnostic_type"] = type(exc).__name__
        result_data = result.get("data") if isinstance(result.get("data"), dict) else {}
        no_write_performed = bool(result_data.get("no_write_performed"))
        explicit_writeback = result.get("writeback_verified")
        if explicit_writeback is None and isinstance(result.get("data"), dict):
            explicit_writeback = result["data"].get("writeback_verified")
        if result.get("ok") and not no_write_performed and explicit_writeback is not True:
            result["ok"] = False
            result["error"] = "writeback_consistency_failed"
            result["message"] = "已理解并执行该操作，但写入反查没有完全通过，暂时不能确认成功。"
        if result.get("ok"):
            result["already_applied"] = False
        if result.get("ok"):
            idempotency_outcome = "not_applied" if no_write_performed else "applied"
        elif str(result.get("error") or "") in {"system_error", "writeback_consistency_failed"}:
            # The protected operation raised or failed its post-write proof.
            # We cannot truthfully say that it either did or did not write.
            idempotency_outcome = "result_unknown"
        else:
            idempotency_outcome = "not_applied"
        result = build_execution_receipt(
            result,
            operation_id=key,
            operation=operation,
            idempotency_result=idempotency_outcome,
        )
        def finish_receipt(receipts: Any) -> dict[str, Any]:
            receipts = receipts if isinstance(receipts, dict) else {}
            receipts[receipt_key] = {
                "actor": self.identity.canonical_user_id,
                "operation": operation,
                "operation_id": key,
                "status": str(result.get("execution_receipt", {}).get("status") or ("completed" if result.get("ok") else "failed")),
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "result": deepcopy(result),
            }
            return receipts

        with authorized_business_write(
            source="trusted_tool",
            operation_id=key,
            ledger_id=runtime_auth["ledger_id"],
            audit_id=preaudit_id,
            allowed_files={"tool_operations.json"},
        ):
            self.store.update_json("tool_operations.json", {}, finish_receipt)
        return result

    def execute_capability_write(
        self,
        *,
        operation_id: str,
        operation: str,
        execute: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """Run a declared Capability write through XiaoYou's receipt fence.

        Capability packages may provide their own domain storage contracts, but
        they must not bypass the product's trusted-turn authorization,
        idempotency ledger, ExecutionReceipt or writeback gate.  This small
        public-to-the-package seam deliberately receives an already selected
        operation and a closure; it never sees user text and never selects a
        business action.
        """

        return self._operation(
            str(operation_id or ""),
            str(operation or ""),
            execute,
        )

    def _enqueue_notifications(self, notifications: list[dict[str, Any]]) -> dict[str, Any]:
        """Stage current-runtime delivery facts; never write the retired queue.

        A task receipt proves only that the task exists.  Its initial notice is
        a separate, server-attested Work Runtime delivery job.  Due reminders
        are intentionally left to Agenda, which later gives Hermes the real
        task fact and an opportunity to decide the appropriate follow-up.
        """

        from .direct_reply_recovery import get_direct_reply_recovery_manager
        from .proactive_delivery_authority import ProactiveDeliveryAuthority
        from .work_runtime import ReplyDestination

        tenant = str(current_tenant_id() or "").strip()
        authority = ProactiveDeliveryAuthority(self.store.data_dir)
        manager = get_direct_reply_recovery_manager()
        requested_ids: list[str] = []
        outcomes: list[dict[str, Any]] = []
        for item in notifications:
            task_id = str(item.get("task_id") or "").strip()
            action = str(item.get("action") or "").strip()
            recipient = str(item.get("touser") or "").strip()
            notice_id = ":".join((task_id, "teacher", action))
            if notice_id:
                requested_ids.append(notice_id)
            if action == "task_due":
                # The durable task fact is already in Workspace.  Agenda, not
                # a hidden timer/template queue, owns any future due-time turn.
                outcomes.append({
                    "notification_id": notice_id,
                    "action": action,
                    "delivery_state": "agenda_managed",
                    "wecom_accepted": False,
                })
                continue
            if action != "task_created" or not all((tenant, task_id, recipient, str(item.get("content") or "").strip())):
                outcomes.append({
                    "notification_id": notice_id,
                    "action": action,
                    "delivery_state": "not_deliverable",
                    "reason": "task_delivery_required_fields_missing",
                    "wecom_accepted": False,
                })
                continue
            destination = ReplyDestination(
                tenant_id=tenant,
                channel="wecom_callback",
                recipient_id=recipient,
                source_identity="task_delivery:" + tenant,
            )
            decision = authority.decide(destination)
            if not decision.allowed:
                outcomes.append({
                    "notification_id": notice_id,
                    "action": action,
                    "delivery_state": "not_deliverable",
                    "reason": decision.reason,
                    "wecom_accepted": False,
                })
                continue
            job = manager.stage_proactive_notice(
                tenant_id=tenant,
                recipient_id=recipient,
                source_kind="task_delivery",
                notice_id=notice_id,
                notice_text=str(item.get("content") or ""),
                trace_ref="verified-task-notice:" + task_id + ":" + action,
            )
            outcomes.append({
                "notification_id": notice_id,
                "action": action,
                "delivery_state": str(job.delivery_state),
                "wecom_accepted": bool(job.delivery_state == "delivered"),
                "reason": "institutional_proactive_authorization_active",
            })
        initial = next((row for row in outcomes if row.get("action") == "task_created"), {})
        return {
            # Compatibility names intentionally describe a current durable job,
            # never the retired ``notification_outbox.json`` queue.
            "requested_notification_ids": requested_ids,
            "queued_notification_ids": [str(row.get("notification_id") or "") for row in outcomes if row.get("delivery_state") == "pending"],
            "queued_actions": [str(row.get("action") or "") for row in outcomes if row.get("delivery_state") == "pending"],
            "outcomes": outcomes,
            "initial_delivery_state": str(initial.get("delivery_state") or "not_deliverable"),
            "initial_delivery_reason": str(initial.get("reason") or ""),
            "initial_wecom_accepted": bool(initial.get("wecom_accepted")),
            "writeback_verified": bool(initial),
        }

    def context(self) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        visible_students = sorted(self._visible_students())
        visible_tasks = self._query_visible_tasks()
        current = current_task_for_user(
            visible_tasks,
            self.identity.canonical_user_id,
        )
        focus = self._read_focus()
        return self._ok(
            "context",
            data={
                "identity": {
                    "platform": self.identity.platform,
                    "platform_user_id": self.identity.platform_user_id,
                    "canonical_user_id": self.identity.canonical_user_id,
                    "person_name": self.identity.person_name,
                    "role": self.identity.role,
                },
                "visible_students": visible_students,
                "open_task_count": sum(
                    task.get("status") not in _CLOSED_STATUSES
                    for task in visible_tasks
                ),
                "current_task": deepcopy(current) if current else None,
                "focus": focus,
            },
            message="已读取当前账号的托管业务上下文。",
        )

    def query_students(
        self,
        *,
        student_name: str = "",
        student_id: str = "",
        teacher_name: str = "",
        query_scope: str = "",
        grade: str = "",
        class_name: str = "",
        name: str = "",
        role: str = "",
        limit: int = 30,
        offset: int = 0,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if str(role or "").strip():
            return self._error(
                "wrong_tool_for_staff_query",
                "学生查询不能按员工角色筛选；请改用 tuoguan_query_staff_directory 查询老师、店长或老板。",
            )
        requested = str(student_name or "").strip()
        requested_student_id = str(student_id or "").strip()
        requested_class_name = str(class_name or "").strip()
        requested_grade = str(grade or "").strip()
        alias_name = str(name or "").strip()
        if requested and alias_name and requested != alias_name:
            return self._error("ambiguous_target", "student_name 与 name 指向不同学生，请只保留一个明确姓名。")
        requested = requested or alias_name
        try:
            from .runtime_foundation import current_raw_text
            raw_text = current_raw_text(self.identity.canonical_user_id) or current_raw_text(self.user_id)
        except Exception:
            raw_text = ""
        scope = str(query_scope or "").strip().lower()
        scope_reason = "explicit_student" if requested else ""
        active_task: dict[str, Any] | None = None
        if not requested and self.identity.role == "teacher":
            compact_raw = "".join(str(raw_text or "").split())
            visible_names = sorted(self._visible_students(), key=len, reverse=True)
            explicit_names = [candidate for candidate in visible_names if candidate and candidate in str(raw_text or "")]
            if len(explicit_names) == 1:
                requested = explicit_names[0]
                scope_reason = "student_named_in_current_message"
            elif any(
                term in compact_raw
                for term in (
                    "他家长", "她家长", "这个任务", "当前任务", "怎么说", "怎么沟通",
                    "不知道怎么", "不会说", "帮我写", "开始处理", "开始这个",
                )
            ):
                active_task = self._active_open_task()
                active_student = str((active_task or {}).get("student_name") or "").strip()
                if active_student:
                    requested = active_student
                    scope_reason = "active_task_student"
        session_object_evidence: dict[str, Any] | None = None
        if requested_student_id and self.session_key:
            # A model-provided ID is never itself a write authority.  It can
            # only continue a student the same trusted session had already
            # established for protected work.
            session_object_evidence = resolve_confirmed_object(
                self.store,
                self.identity,
                session_key=self.session_key,
                object_type="student",
                object_id=requested_student_id,
                require_write_authorization=True,
            )
        safe_limit = max(1, min(int(limit or 30), 100))
        safe_offset = max(0, int(offset or 0))
        target_identity = self.identity
        requested_teacher = str(teacher_name or "").strip()
        if not requested_teacher and self.identity.role in {"boss", "manager"}:
            requested_teacher = self._teacher_name_from_current_raw_text()
        if requested_teacher:
            if self.identity.role not in {"boss", "manager"}:
                return self._error("permission_denied", "当前账号不能代查其他老师负责的学生。")
            target_identity = self._query_teacher_identity_by_name(requested_teacher)
            if target_identity is None:
                return self._error("teacher_not_found", f"没有找到老师“{requested_teacher}”。")
            if not self._query_manager_can_view_teacher(target_identity.canonical_user_id):
                return self._error("permission_denied", f"老师“{requested_teacher}”不在当前店长管理范围内。")
        visible = self._visible_students_for(target_identity)
        if requested_student_id:
            # An ID can only originate from a prior authorised directory
            # result.  Resolve it through the repository again; never trust
            # a client/model-supplied profile or bypass PermissionService.
            requested = requested_student_id
        if requested:
            directory_candidates = find_student_candidates(self.store, requested)
            authorised_candidates = [
                entry for entry in directory_candidates
                if self.permissions.can_query_student(target_identity, str(entry.get("student_id") or ""))
            ]
            # Keep the pre-filter cardinality.  A model may offer a class
            # predicate after seeing a broad directory/context result, but a
            # hidden predicate must not turn an otherwise ambiguous name into
            # a writable object.  Only the separately verified pending-set
            # confirmation below can do that for an ambiguous source set.
            ambiguous_before_classroom_filter = len(directory_candidates) > 1
            if (requested_class_name or requested_grade) and directory_candidates:
                # A class/grade supplied by the model is not a selection
                # instruction.  It is a concrete repository predicate over
                # the candidates already authorised for this actor.  We still
                # reject zero or multiple results below rather than guessing.
                directory_candidates = filter_candidates_by_classroom(
                    directory_candidates,
                    class_name=requested_class_name,
                    grade=requested_grade,
                )
                authorised_candidates = [
                    entry for entry in directory_candidates
                    if self.permissions.can_query_student(target_identity, str(entry.get("student_id") or ""))
                ]
                if not directory_candidates:
                    return self._error(
                        "student_classroom_not_found",
                        "没有找到与本轮班级或年级确认一致的学生，请重新确认班级或年级。",
                    )
            if len(directory_candidates) > 1:
                if not authorised_candidates:
                    return self._error("permission_denied", "该学生不在当前账号的负责范围内。")
                result = self._error("student_name_ambiguous", "存在同名学生，请根据班级确认后使用 student_id 查询或记录。")
                candidates = safe_candidate_labels(authorised_candidates)
                result["data"] = {
                    "candidates": candidates,
                    "no_write_performed": True,
                }
                remember_candidate_set(
                    self.store,
                    self.identity,
                    session_key=self.session_key,
                    object_type="student",
                    display_name=requested,
                    candidates=candidates,
                    source_tool="tuoguan_query_students",
                    source_subject_explicit=bool(requested and requested in str(raw_text or "")),
                )
                return result
            if len(directory_candidates) == 1:
                entry = directory_candidates[0]
                if not authorised_candidates:
                    return self._error("permission_denied", "该学生不在当前账号的负责范围内。")
                profile = deepcopy(entry.get("profile") or {})
                canonical_name = str(entry.get("student_name") or "")
                canonical_id = str(entry.get("student_id") or "")
                records = self.store.read_json("records.json", [])
                if not isinstance(records, list):
                    records = []
                recent = [
                    deepcopy(item) for item in records
                    if isinstance(item, dict)
                    and (
                        (canonical_id and str(item.get("student_id") or "") == canonical_id)
                        or (not str(item.get("student_id") or "") and str(item.get("student_name") or item.get("student") or "") == canonical_name)
                    )
                ]
                recent = sorted(recent, key=_record_time, reverse=True)[:5]
                self._write_focus(student_name=canonical_name)
                # A query result is read evidence, not an automatic upgrade
                # to a writable subject.  The model may search a broad list
                # or infer a name, but it cannot turn that result into a
                # future write target.  Only a name actually present in the
                # trusted current turn, an already write-authorised session
                # object, or the trusted active-task context may issue the
                # short-lived reference consumed by ``record_student``.
                authority_basis = ""
                if requested_student_id and session_object_evidence and str(session_object_evidence.get("object_id") or "") == canonical_id:
                    authority_basis = "trusted_session_object"
                elif (
                    not ambiguous_before_classroom_filter
                    and requested
                    and requested in str(raw_text or "")
                ):
                    authority_basis = "explicit_subject_in_trusted_turn"
                elif self.session_key and resolve_pending_candidate_confirmation(
                    self.store,
                    self.identity,
                    session_key=self.session_key,
                    object_type="student",
                    display_name=canonical_name,
                    object_id=canonical_id,
                    trusted_confirmation_text=str(raw_text or ""),
                ):
                    authority_basis = "trusted_pending_candidate_confirmation"
                elif scope_reason == "active_task_student" and active_task:
                    authority_basis = "trusted_active_task_subject"
                object_evidence = None
                if authority_basis:
                    object_evidence = remember_resolved_object(
                        self.store,
                        self.identity,
                        session_key=self.session_key,
                        object_type="student",
                        object_id=canonical_id,
                        display_name=canonical_name,
                        attributes={
                            key: profile.get(key)
                            for key in ("class_name", "class", "grade", "campus_id")
                            if profile.get(key) not in {None, ""}
                        },
                        source_tool="tuoguan_query_students",
                        write_authorized=True,
                        authority_basis=authority_basis,
                    )
                payload = [{
                    "name": canonical_name,
                    "student_id": canonical_id,
                    "profile": profile,
                    "recent_records": recent,
                }]
                rendered_text = f"已查询到{canonical_name}的可见学生档案。"
                return self._ok(
                    "query_students",
                    data={
                        "count": 1, "result_count": 1, "result_scope": "explicit_student",
                        "total_count": 1, "returned_count": 1, "truncated": False,
                        "students": payload, "rendered_text": rendered_text,
                        "business_object_evidence": object_evidence or {},
                        # This is a read receipt, never evidence that a
                        # student record was persisted.
                        "render_verified": True, "writeback_verified": False,
                        "scope_user_id": target_identity.canonical_user_id,
                        "scope_person_name": target_identity.person_name,
                        "scope_reason": scope_reason or "explicit_student", "query_scope": "visible",
                        "active_task": deepcopy(active_task) if scope_reason == "active_task_student" and active_task else None,
                    },
                    message=rendered_text,
                )
            students = self.store.read_json("students.json", {})
            exists = isinstance(students, dict) and requested in students
            if not exists:
                return self._error("student_not_found", f"没有找到学生“{requested}”。")
            if requested not in visible:
                return self._error(
                    "permission_denied",
                    f"学生“{requested}”不在当前账号的负责范围内。",
                )
            names = [requested]
            self._write_focus(student_name=requested)
        else:
            compact_raw = "".join(str(raw_text or "").split())
            regular_terms = ("正式托管", "托管班", "常规托管", "不是暑假班", "不含暑假班", "排除暑假班")
            summer_negated = any(term in compact_raw for term in ("不是暑假班", "不含暑假班", "排除暑假班"))
            if any(term in compact_raw for term in regular_terms):
                scope = "regular"
            elif "暑假班" in compact_raw and not summer_negated:
                scope = "summer"
            if scope == "summer":
                summer_names = self._summer_student_names()
                names = sorted(name for name in visible if name in summer_names)
            elif scope == "regular":
                names = sorted(
                    name for name, profile in visible.items()
                    if str((profile or {}).get("campus_id") or "main") == "main"
                )
            else:
                names = sorted(visible)
            requested_grade = _normalize_grade(grade)
            if requested_grade:
                names = [name for name in names if requested_grade in _student_grades(visible.get(name, {}))]
            if requested_class_name:
                expected_class = re.sub(r"\s+", "", requested_class_name)
                names = [
                    student_name
                    for student_name in names
                    if expected_class in {
                        re.sub(r"\s+", "", str((visible.get(student_name) or {}).get(field) or ""))
                        for field in ("class_name", "class")
                    }
                ]
        # `business_signals.open_task_count` is a legacy display cache.  Never
        # return it as fact: derive the count from the authoritative tasks
        # ledger for this read instead.
        open_task_counts: dict[str, int] = {}
        for task in self.store.load_tasks():
            if isinstance(task, dict) and task_is_open(task):
                task_student = str(task.get("student_name") or "").strip()
                if task_student:
                    open_task_counts[task_student] = int(open_task_counts.get(task_student) or 0) + 1

        def student_payload(name: str, recent_records: list[dict[str, Any]]) -> dict[str, Any]:
            profile = deepcopy(visible[name])
            signals = profile.get("business_signals") if isinstance(profile.get("business_signals"), dict) else {}
            profile["business_signals"] = {
                **signals,
                "open_task_count": int(open_task_counts.get(name) or 0),
                "open_task_count_source": "tasks.json",
            }
            return {"name": name, "profile": profile, "recent_records": recent_records}

        payload = []
        if requested:
            records = self.store.read_json("records.json", [])
            if not isinstance(records, list):
                records = []
            for name in names:
                recent = [
                    deepcopy(item)
                    for item in records
                    if isinstance(item, dict)
                    and str(item.get("student_name") or item.get("student") or "")
                    == name
                ]
                recent = sorted(recent, key=_record_time, reverse=True)[:5]
                payload.append(student_payload(name, recent))
            rendered_text = _render_student_records(payload)
        else:
            page_names = names[safe_offset:safe_offset + safe_limit]
            payload = [student_payload(name, []) for name in page_names]
            if scope == "summer":
                title = "暑假班"
            elif scope == "regular":
                term_state = self.store.read_json("academic_term_state.json", {})
                historical = bool(
                    isinstance(term_state, dict)
                    and term_state.get("service_relation_policy") == "defer_until_new_term"
                )
                title = "正式托管历史名单" if historical else "正式托管班"
            elif requested_teacher:
                title = f"{target_identity.person_name}名下"
            else:
                title = "当前可见范围内"
            if _normalize_grade(grade):
                title += f"{grade}"
            shown = "、".join(page_names)
            has_more = safe_offset + len(page_names) < len(names)
            suffix = "等" if has_more else ""
            rendered_text = f"{title}共{len(names)}名学生" + (f"：{shown}{suffix}。" if shown else "。")
            if scope == "regular" and historical:
                rendered_text += " 当前处于新学期过渡期，这是上学期历史名单数量，不等于已确认的新学期在读人数。"
        data_version = ""
        as_of = ""
        try:
            records_path = self.store.data_dir / "records.json"
            stat = records_path.stat()
            data_version = str(int(stat.st_mtime_ns))
            as_of = datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds")
        except OSError:
            pass
        return self._ok(
            "query_students",
            data={
                "count": len(names),
                "result_count": len(payload),
                "result_scope": scope or "visible",
                "total_count": len(names),
                "returned_count": len(payload),
                "truncated": len(payload) < len(names),
                "offset": safe_offset,
                "next_offset": safe_offset + len(payload) if safe_offset + len(payload) < len(names) else None,
                "has_more": safe_offset + len(payload) < len(names),
                "as_of": as_of,
                "students": payload,
                "rendered_text": rendered_text,
                "render_verified": True,
                # Listing visible students is read-only.  A model may use the
                # returned canonical ID in a later write, but this response
                # itself must never authorize an "already recorded" claim.
                "writeback_verified": False,
                "data_version": data_version,
                "scope_user_id": target_identity.canonical_user_id,
                "scope_person_name": target_identity.person_name,
                "scope_reason": scope_reason or "visible_scope",
                "query_scope": scope or "visible",
                "active_task": deepcopy(active_task) if scope_reason == "active_task_student" and active_task else None,
            },
            message=f"查询到 {len(names)} 名有权限查看的学生，返回 {len(payload)} 名。",
        )

    def change_summer_points(
        self,
        *,
        student_name: str = "",
        delta_points: int | None = None,
        reason_text: str = "",
        reason_type: str = "",
        operation_id: str,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        try:
            from .runtime_foundation import current_raw_text, current_message_id
            raw_text = current_raw_text(self.identity.canonical_user_id)
            message_id = current_message_id(self.identity.canonical_user_id)
        except Exception:
            raw_text = reason_text
            message_id = operation_id
        def execute() -> dict[str, Any]:
            result = change_points(
                self.store,
                identity=self.identity,
                requested_student_name=student_name,
                delta_points=delta_points,
                raw_text=raw_text or reason_text,
                reason_text=reason_text,
                reason_type=reason_type,
                source_message_id=message_id,
                operation_id=operation_id,
            )
            if result.get("ok"):
                return self._ok(
                    "change_summer_points",
                    data=result,
                    message=str(result.get("rendered_text") or "积分已更新。"),
                )
            return {
                "ok": False,
                "error": str(result.get("reason_code") or "points_update_failed"),
                "message": str(result.get("message") or "积分未更新。"),
                "data": result,
                "writeback_verified": False,
            }

        return self._operation(operation_id, "change_summer_points", execute)

    def query_summer_points(self, *, student_name: str, detail_limit: int = 3) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_student_points(
            self.store,
            identity=self.identity,
            requested_student_name=student_name,
            detail_limit=detail_limit,
        )
        if not result.get("ok"):
            return self._error(str(result.get("reason_code") or "query_failed"), str(result.get("message") or "积分查询失败。"))
        return self._ok("query_summer_points", data=result, message=str(result.get("rendered_text") or ""))

    def query_summer_points_ranking(self, *, limit: int = 10) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_points_ranking(self.store, identity=self.identity, limit=limit)
        if not result.get("ok"):
            return self._error(str(result.get("reason_code") or "query_failed"), str(result.get("message") or "排行榜查询失败。"))
        return self._ok("query_summer_points_ranking", data=result, message=str(result.get("rendered_text") or ""))

    def register_student(
        self,
        *,
        student_name: str,
        parent_phone: str,
        operation_id: str,
        grade: str = "",
        campus_id: str = "main",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"teacher", "manager", "boss"}:
            return self._error("permission_denied", "当前账号不能登记学生。")
        return self._operation(
            operation_id,
            "register_student",
            lambda: register_official_student(
                self.store,
                student_name=student_name,
                parent_phone=parent_phone,
                teacher_user_id=self.identity.canonical_user_id if self.identity.role == "teacher" else self.identity.canonical_user_id,
                grade=grade,
                campus_id=campus_id,
                channel=self.platform,
            ),
        )

    def register_summer_student(
        self,
        *,
        student_name: str,
        parent_phone: str,
        grade: str,
        operation_id: str,
        attendance_mode: str = "full_day",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"manager", "boss"}:
            return self._error("permission_denied", "暑假班正式建档只允许老板或店长操作。")
        return self._operation(
            operation_id,
            "register_summer_student",
            lambda: register_summer_student(
                self.store,
                student_name=student_name,
                parent_phone=parent_phone,
                grade=grade,
                attendance_mode=attendance_mode,
                created_by=self.identity.canonical_user_id,
                channel=self.platform,
            ),
        )

    def create_trial_lead(
        self,
        *,
        student_name: str,
        parent_phone: str,
        operation_id: str,
        observation: str = "",
        age_or_grade: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        return self._operation(
            operation_id,
            "create_trial_lead",
            lambda: create_trial_lead(
                self.store,
                student_name=student_name,
                parent_phone=parent_phone,
                recorder_user_id=self.identity.canonical_user_id,
                observation=observation,
                age_or_grade=age_or_grade,
                channel=self.platform,
            ),
        )

    def create_task(
        self,
        *,
        title: str,
        assignee_user_id: str = "",
        teacher_name: str = "",
        operation_id: str = "",
        due_at: str = "",
        level: str = "A",
        student_name: str = "",
        goal_id: str = "",
        goal_action_id: str = "",
        parent_work_item_id: str = "",
        artifact_version_id: str = "",
        evidence_requirement: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if not self.permissions.can_manage_tasks(self.identity):
            return self._error("permission_denied", "只有老板或店长可以创建并分配任务。")
        assignee, assignee_error = self._resolve_task_assignee(
            assignee_user_id=assignee_user_id,
            teacher_name=teacher_name,
        )
        if assignee_error:
            return assignee_error
        assert isinstance(assignee, dict)
        assignee_user_id = str(assignee.get("user_id") or "")
        assignee_name = str(assignee.get("business_name") or assignee.get("staff_name") or teacher_name or assignee_user_id)
        responsibility: dict[str, Any] = {"evidence": []}
        institutional_item: dict[str, Any] = {}
        assignee_role = str(assignee.get("role") or "")
        if parent_work_item_id:
            institutional_item = query_institution_work(
                self.store,
                identity=self.identity,
                include_closed=True,
                limit=100,
            )
            institutional_item = next(
                (
                    row for row in institutional_item.get("items") or []
                    if str(row.get("work_item_id") or "") == str(parent_work_item_id)
                ),
                {},
            )
            if not institutional_item:
                return self._error("institution_work_not_found", "没有找到要关联的机构工作事项。")
            if str(institutional_item.get("institution_stage") or "") not in {"implementing", "effective", "verifying"}:
                return self._error("implementation_authorization_required", "机构草案必须经过内容确认和单独落实授权后，才能创建执行任务。")
            if artifact_version_id and str(institutional_item.get("current_artifact_version_id") or "") != str(artifact_version_id):
                return self._error("artifact_version_mismatch", "任务必须关联当前已授权的成果版本。")
            # This historical rollout is intentionally bound to an explicit,
            # server-owned compatibility policy.  Display names and test-account
            # placeholders are not delivery or permission authority.
            if not _legacy_institution_rollout_target_allowed(self.store, str(assignee_user_id)):
                return self._error("institution_rollout_target_not_authorized", "当前机构制度落实灰度目标未在已确认的兼容策略中。")
            if not assignee_role or assignee_role == "boss":
                return self._error("institution_task_responsible_role_invalid", "机构落实任务必须由可信目录中的实际执行人承担，不能把老板误写成老师或执行人。")
        if goal_id:
            from .goal_operator import find_active_goal
            from .proactive_work import GOAL_TASK_HIGH_RISK_TERMS, effective_proactive_permission, verify_goal_task_responsibility

            if not find_active_goal(self.store, goal_id=goal_id):
                return self._error("active_goal_required", "目标未确认、已撤销或不存在，不能创建目标子任务。")
            if any(term in f"{title}{evidence_requirement}" for term in GOAL_TASK_HIGH_RISK_TERMS):
                return self._error("high_risk_goal_task_requires_confirmation", "该任务涉及高风险制度或家长外发边界，不能由小优自主创建。")
            permission = effective_proactive_permission(
                self.store,
                target_role="teacher",
                target_user_id=assignee_user_id,
                action_type="assign_low_risk_goal_task",
                goal_id=goal_id,
            )
            if not permission.get("allowed"):
                return self._error(str(permission.get("reason_code") or "permission_denied"), "目标子任务没有通过当前主动授权、灰度或在职状态校验。")
            responsibility = verify_goal_task_responsibility(
                self.store,
                target_user_id=assignee_user_id,
                student_names=[student_name] if student_name else [],
            )
            if not responsibility.get("ok"):
                return self._error(str(responsibility.get("error") or "responsibility_evidence_required"), str(responsibility.get("message") or "目标子任务缺少可信责任关系。"))
            today = datetime.now().astimezone().date().isoformat()
            existing_goal_tasks = [
                task for task in self.store.load_tasks()
                if isinstance(task, dict)
                and str(task.get("goal_id") or "") == str(goal_id)
                and str(task.get("created_at") or "").startswith(today)
                and str(task.get("status") or "") not in _CLOSED_STATUSES
            ]
            if sum(str(task.get("assignee_userid") or "") == str(assignee_user_id) for task in existing_goal_tasks) >= 1:
                return self._error("goal_task_person_daily_limit", "这位老师今天已经有一个该目标的新子任务，本轮不再重复创建。")
            if len(existing_goal_tasks) >= 3:
                return self._error("goal_task_institution_daily_limit", "今天全机构已经创建三个目标子任务，本轮不再追加。")
        raw_text_for_boundary = self._trusted_runtime_raw_text("create_task")
        if self._looks_like_goal_workspace_task_misuse(
            raw_text=raw_text_for_boundary,
            title=title,
            operation_id=operation_id,
            assignee_user_id=assignee_user_id,
            student_name=student_name,
        ):
            return self._error(
                "goal_workspace_required",
                "这是长期经营目标或批量覆盖计划，不是单个内部任务。请改用 tuoguan_goal_workspace 保存目标、确认阶段或查询下一步；不要把本工具失败当作最终结果。",
            )
        if not due_at:
            due_at = self._relative_due_at(raw_text_for_boundary) if raw_text_for_boundary else ""
        trusted_task_source = str(title or "").strip()
        candidate_source = str(raw_text_for_boundary or "").strip()
        if candidate_source:
            trusted_task_source = candidate_source
        def execute() -> dict[str, Any]:
            result = create_assigned_task(
                self.store,
                title=title,
                assignee_user_id=assignee_user_id,
                created_by=self.identity.canonical_user_id,
                due_at=due_at,
                level=level,
                student_name=student_name,
                channel=self.platform,
                source_text=trusted_task_source,
                evidence_requirement=evidence_requirement,
                created_by_role=self.identity.role,
                created_by_name=self.identity.person_name,
                business_goal=str(title or "").strip(),
                assignee_role=assignee_role,
                assignee_name=assignee_name,
                operation_id=operation_id,
            )
            if result.get("ok") and not result.get("already_applied"):
                task = result.get("task") if isinstance(result.get("task"), dict) else {}
                task_id = str(result.get("task_id") or task.get("id") or "")
                if (goal_id or parent_work_item_id) and task_id:
                    persisted = self.store.update_task(
                        task_id,
                        lambda current: {
                            **current,
                            "goal_id": str(goal_id),
                            "goal_action_id": str(goal_action_id or ""),
                            "evidence_requirement": str(evidence_requirement or "").strip(),
                            "responsibility_evidence": responsibility.get("evidence") or [],
                            "created_autonomously_within_goal": bool(goal_id),
                            "parent_work_item_id": str(parent_work_item_id or ""),
                            "artifact_version_id": str(artifact_version_id or institutional_item.get("current_artifact_version_id") or ""),
                        },
                    )
                    if isinstance(persisted, dict):
                        task = persisted
                        result["task"] = deepcopy(persisted)
                        result["writeback_verified"] = (
                            (not goal_id or (str(persisted.get("goal_id") or "") == str(goal_id) and str(persisted.get("goal_action_id") or "") == str(goal_action_id or "")))
                            and (not parent_work_item_id or (str(persisted.get("parent_work_item_id") or "") == str(parent_work_item_id) and str(persisted.get("artifact_version_id") or "") == str(artifact_version_id or institutional_item.get("current_artifact_version_id") or "")))
                        )
                task_contract = task.get("task_contract") if isinstance(task.get("task_contract"), dict) else {}
                criteria = [str(value) for value in task_contract.get("guidance_points") or [] if str(value)]
                content = (
                    f"你收到一项新任务：{task.get('title') or title}\n"
                    f"原始要求：{task_contract.get('original_instruction') or task.get('source_text') or title}\n"
                    f"任务目标：{task_contract.get('business_goal') or task.get('title') or title}\n"
                    f"等级：{task.get('level') or level}\n"
                    f"截止：{task.get('due_at') or due_at or '请尽快处理'}\n"
                    f"安排人：{self.identity.person_name or self.identity.canonical_user_id}\n"
                    f"关联学生：{task.get('student_name') or student_name or '无'}\n"
                    "回复“开始”后，小优会结合这项任务陪你一步一步处理；遇到不会说或不会做的地方可以直接问。"
                )
                if criteria:
                    content += "\n处理时可参考：" + "；".join(criteria[:3])
                    content += "\n这些是小优陪你完成任务的参考，不会替代老板原始要求，也不是额外填表。"
                try:
                    from .runtime_foundation import current_ledger_id
                    ledger_id = current_ledger_id(self.identity.canonical_user_id)
                except Exception:
                    ledger_id = ""
                notifications = [{
                    "task_id": task_id,
                    "role": "teacher",
                    "touser": assignee_user_id,
                    "action": "task_created",
                    "content": content,
                    "ledger_id": ledger_id,
                }]
                if task.get("due_at") or due_at:
                    notifications.append({
                        "task_id": task_id,
                        "role": "teacher",
                        "touser": assignee_user_id,
                        "action": "task_due",
                        "deliver_at": str(task.get("due_at") or due_at),
                        "content": f"任务到期提醒：{task.get('title') or title}\n请及时处理并回复进展。",
                        "ledger_id": ledger_id,
                    })
                queue_receipt = self._enqueue_notifications(notifications)
                result.setdefault("notifications", []).extend(
                    {
                        "task_id": task_id,
                        "touser": assignee_user_id,
                        "action": item["action"],
                        "status": next(
                            (
                                str(outcome.get("delivery_state") or "not_deliverable")
                                for outcome in (queue_receipt.get("outcomes") or [])
                                if isinstance(outcome, dict) and str(outcome.get("action") or "") == str(item["action"])
                            ),
                            "not_deliverable",
                        ),
                    }
                    for item in notifications
                )
                result["delivery"] = {
                    "task_created": "task_created",
                    "notification_queued": str(queue_receipt.get("initial_delivery_state") or "") == "pending",
                    "notification_sent": bool(queue_receipt.get("initial_wecom_accepted")),
                    "delivery_status": str(queue_receipt.get("initial_delivery_state") or "not_deliverable"),
                    "delivery_reason": str(queue_receipt.get("initial_delivery_reason") or ""),
                    "wecom_accepted": bool(queue_receipt.get("initial_wecom_accepted")),
                    "due_reminder_state": "agenda_managed",
                    "queued_notification_ids": list(queue_receipt.get("queued_notification_ids") or []),
                }
                # Task Business Truth is its own Receipt/writeback fact.  A
                # later channel outcome cannot turn a verified task into a
                # failed business write, nor can a staged message prove that
                # WeCom accepted it.
                result["writeback_verified"] = bool(result.get("writeback_verified"))
                focus_expires_at = (datetime.now().astimezone() + timedelta(hours=36)).isoformat(timespec="seconds")
                self._write_focus(
                    task_id=task_id,
                    student_name=str(task.get("student_name") or student_name or ""),
                    focus_source="task_created",
                    focus_expires_at=focus_expires_at,
                )
                self._write_user_focus(
                    assignee_user_id,
                    task_id=task_id,
                    student_name=str(task.get("student_name") or student_name or ""),
                    task_type=str(task.get("type") or "manual_assignment"),
                    focus_source="task_created",
                    focus_expires_at=focus_expires_at,
                )
                self._remember_user_task_context(assignee_user_id, task, ttl_hours=36)
            if not result.get("ok"):
                return result
            task = result.get("task") if isinstance(result.get("task"), dict) else {}
            delivery = result.get("delivery") if isinstance(result.get("delivery"), dict) else {}
            delivery_state = str(delivery.get("delivery_status") or "not_deliverable")
            delivery_reason = str(delivery.get("delivery_reason") or "")
            if delivery_state == "delivered":
                message = f"任务已建立，企业微信已接受向{assignee_name}发送的任务通知。"
            elif delivery_state == "pending":
                message = f"任务已建立；给{assignee_name}的企业微信通知已进入当前投递链路，但尚未取得企业微信接受回执。"
            else:
                suffix = f"原因：{delivery_reason}。" if delivery_reason else ""
                message = f"任务已建立，但当前未创建向{assignee_name}的企业微信投递。{suffix}我不能说对方已收到。"
            return self._ok(
                "create_task",
                data={
                    "task_id": str(result.get("task_id") or task.get("id") or ""),
                    "task": deepcopy(task),
                    "task_created": True,
                    "notification_queued": queued,
                    "notification_sent": bool(delivery.get("notification_sent")),
                    "delivery_status": str(delivery.get("delivery_status") or "delivery_failed"),
                    "queued_notification_ids": list(delivery.get("queued_notification_ids") or []),
                    "notifications": deepcopy(result.get("notifications") or []),
                    "writeback_verified": bool(result.get("writeback_verified")),
                },
                message=message,
                already_applied=bool(result.get("already_applied")),
            )

        receipt = self._operation(operation_id, "create_task", execute)
        # Keep the legacy top-level fields for compatible callers while the
        # canonical execution receipt remains in `data`.
        data = receipt.get("data") if isinstance(receipt.get("data"), dict) else {}
        if data:
            for key in ("task_id", "task", "task_created", "notification_queued", "notification_sent", "delivery_status"):
                if key in data:
                    receipt[key] = deepcopy(data[key])
        return receipt

    def query_operations_report(self, *, report_type: str = "operations", query_type: str = "", teacher_name: str = "") -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role != "boss":
            return self._error("permission_denied", "当前经营统计只允许老板查询。")
        teacher_name = str(teacher_name or "").strip()
        if not teacher_name:
            teacher_name = self._teacher_name_from_current_raw_text()
        if teacher_name and not query_type:
            query_type = "teacher_activity"
        if query_type or teacher_name:
            result = query_operations(
                self.store,
                identity=self.identity,
                query_type=query_type or "overview",
                teacher_name=teacher_name,
            )
            return self._ok("query_operations_report", data=result, message=result.get("rendered_text", ""))
        result = operations_report(self.store, report_type=report_type)
        return self._ok("query_operations_report", data=result, message=result.get("rendered_text", ""))

    def verify_dashboard_visibility(self, *, student_name: str = "") -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"manager", "boss"}:
            return self._error("permission_denied", "只有老板或店长可以检查看板可见性。")
        result = verify_dashboard_visibility(self.store, student_name=student_name)
        return self._ok("verify_dashboard_visibility", data=result, message="已完成H5可见性只读校验。")

    def dashboard_link(self) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"teacher", "manager", "boss"}:
            return self._error("permission_denied", "当前账号暂时不能生成看板链接，请联系管理员确认权限。")
        base_url = str(os.getenv("HERMES_TUOGUAN_DASHBOARD_BASE_URL") or "").strip()
        if not base_url:
            return self._error(
                "dashboard_base_url_missing",
                "托管看板外部访问地址尚未配置，请联系管理员检查 HERMES_TUOGUAN_DASHBOARD_BASE_URL。",
            )
        route_ready, route_error = verify_public_dashboard_route(base_url)
        if not route_ready:
            return self._error(
                route_error or "dashboard_route_unavailable",
                "当前托管看板服务暂不可访问，已不发送可能失效的链接，请稍后再试。",
            )
        try:
            refresh_dashboard_cache(self.store)
        except Exception:
            pass
        try:
            token = sign_dashboard_token(self.identity, self.store)
        except DashboardAuthError:
            return self._error("permission_denied", "当前账号暂时不能生成看板链接，请联系管理员确认权限。")
        url = f"{base_url.rstrip('/')}/tuoguan/dashboard?token={quote(token, safe='')}"
        role_label = {"teacher": "老师", "manager": "店长", "boss": "老板"}.get(self.identity.role, "用户")
        expires_at = token_expiry_datetime().strftime("%Y-%m-%d %H:%M")
        rendered_text = (
            f"这是你的{role_label}端托管 AI 看板链接：\n"
            f"{url}\n\n"
            f"链接有效期至 {expires_at}。看板只读展示，记录和任务处理仍请回企业微信直接说。"
        )
        return self._ok(
            "dashboard_link",
            data={
                "url": url,
                "role": self.identity.role,
                "role_label": role_label,
                "expires_at": expires_at,
                "rendered_text": rendered_text,
                # A Tool may explicitly nominate one fully rendered external
                # artifact when a channel must preserve opaque signed material
                # byte-for-byte.  The Agent still decides whether to call the
                # Tool; delivery merely preserves its already verified result.
                "delivery_artifact": {
                    "version": "xiaoyou.delivery-artifact.v1",
                    "kind": "reply_text",
                    "text": rendered_text,
                },
                "render_verified": True,
                "writeback_verified": True,
            },
            message=rendered_text,
        )

    def query_tasks(
        self,
        *,
        task_id: str = "",
        student_name: str = "",
        teacher_name: str = "",
        assignee_user_id: str = "",
        status: str = "",
        level: str = "",
        date_scope: str = "",
        scope: str = "",
        limit: int = 20,
        offset: int = 0,
        write_focus: bool = True,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        requested_scope = str(scope or "").strip().lower()
        effective_scope = requested_scope or ("all" if self.identity.role in {"boss", "manager"} else "mine")
        scope_adjusted = False
        if effective_scope == "all" and self.identity.role not in {"boss", "manager"}:
            effective_scope = "mine"
            scope_adjusted = True
        tasks = self._query_visible_tasks()
        requested_teacher = str(teacher_name or "").strip()
        requested_assignee = str(assignee_user_id or "").strip()
        if not requested_teacher and not requested_assignee and self.identity.role in {"boss", "manager"}:
            requested_teacher = self._teacher_name_from_current_raw_text()
        target_teacher_id = ""
        if requested_teacher:
            if self.identity.role not in {"boss", "manager"}:
                return self._error("permission_denied", "当前账号不能代查其他老师的任务。")
            teacher_identity = self._query_teacher_identity_by_name(requested_teacher)
            if teacher_identity is None:
                return self._error("teacher_not_found", f"没有找到老师“{requested_teacher}”。")
            if not self._query_manager_can_view_teacher(teacher_identity.canonical_user_id):
                return self._error("permission_denied", f"老师“{requested_teacher}”不在当前店长管理范围内。")
            target_teacher_id = teacher_identity.canonical_user_id
            effective_scope = "all"
        if requested_assignee:
            if target_teacher_id and target_teacher_id != requested_assignee:
                return self._error("ambiguous_target", "teacher_name 与 assignee_user_id 指向不同执行人，请只保留一个明确对象。")
            if self.identity.role == "teacher" and requested_assignee != self.identity.canonical_user_id:
                return self._error("permission_denied", "老师只能按本人账号查询任务。")
            if self.identity.role == "manager" and not self._query_manager_can_view_teacher(requested_assignee):
                return self._error("permission_denied", "该执行人不在当前店长管理范围内。")
            target_teacher_id = requested_assignee
            effective_scope = "all"
            if not requested_teacher:
                entry = next(
                    (
                        row for row in self._current_query_staff_entries()
                        if str(row.get("user_id") or "") == requested_assignee
                    ),
                    {},
                )
                requested_teacher = str(
                    entry.get("business_name")
                    or entry.get("directory_name")
                    or requested_assignee
                )
        if effective_scope == "mine":
            tasks = [
                task
                for task in tasks
                if str(task.get("assignee_userid") or "") == self.identity.canonical_user_id
            ]
        if target_teacher_id:
            tasks = [
                task
                for task in tasks
                if str(task.get("assignee_userid") or "") == target_teacher_id
            ]
        if task_id:
            tasks = [task for task in tasks if str(task.get("id") or "") == task_id]
        if student_name:
            tasks = [
                task
                for task in tasks
                if str(task.get("student_name") or "") == student_name
            ]
        requested_status = str(status or "").strip().lower()
        if requested_status == "open":
            tasks = [task for task in tasks if task_is_open(task)]
        elif requested_status == "closed":
            tasks = [task for task in tasks if task_is_closed(task)]
        elif requested_status:
            tasks = [task for task in tasks if str(task.get("status") or "").lower() == requested_status]
        if level:
            tasks = [task for task in tasks if str(task.get("level") or "") == level]
        requested_date_scope = str(date_scope or "").strip().lower()
        if requested_date_scope and requested_date_scope != "today":
            return self._error("date_scope_invalid", "任务日期范围当前只支持 today。")
        if requested_date_scope == "today":
            today = datetime.now().astimezone().date()
            tasks = [
                task
                for task in tasks
                if (due := _parse_iso(task.get("due_at"))) is not None
                and due.date() == today
            ]
        tasks = sorted(
            tasks,
            key=lambda item: (
                str(item.get("status") or "") in _CLOSED_STATUSES,
                str(item.get("due_at") or ""),
            ),
        )
        status_counts: dict[str, int] = {}
        for task in tasks:
            task_status = str(task.get("status") or "pending")
            status_counts[task_status] = int(status_counts.get(task_status) or 0) + 1
        if write_focus and len(tasks) == 1:
            self._write_focus(
                task_id=str(tasks[0].get("id") or ""),
                student_name=str(tasks[0].get("student_name") or ""),
            )
        safe_limit = max(1, min(int(limit or 20), 100))
        safe_offset = max(0, int(offset or 0))
        visible_tasks = deepcopy(tasks[safe_offset:safe_offset + safe_limit])
        task_summaries = [
            {
                "task_id": str(task.get("id") or ""),
                "title": str(task.get("title") or task.get("task_name") or task.get("content") or "未命名任务"),
                "level": str(task.get("level") or ""),
                "status": str(task.get("status") or "pending"),
                "due_at": str(task.get("due_at") or ""),
                "student_name": str(task.get("student_name") or ""),
            }
            for task in visible_tasks
        ]
        if target_teacher_id:
            header = f"{requested_teacher}任务共 {len(tasks)} 条"
        else:
            header = (
                f"当前全员任务共 {len(tasks)} 条"
                if effective_scope == "all"
                else f"我的任务共 {len(tasks)} 条"
            )
        if requested_date_scope == "today":
            header = "今天" + header
        if scope_adjusted:
            header = f"你是老师，只能查看本人任务。{header}"
        lines = [header]
        for index, task in enumerate(task_summaries, 1):
            details = [task["level"], task["status"], task["due_at"]]
            details = [item for item in details if item]
            suffix = f"（{'｜'.join(details)}）" if details else ""
            student = f"【{task['student_name']}】" if task["student_name"] else ""
            lines.append(f"{index}. {student}{task['title']}{suffix}")
        if len(tasks) > len(task_summaries):
            lines.append(f"共 {len(tasks)} 条，以上展示前 {len(task_summaries)} 条。")
        data_version = ""
        as_of = ""
        try:
            tasks_path = self.store.data_dir / "tasks.json"
            stat = tasks_path.stat()
            data_version = str(int(stat.st_mtime_ns))
            as_of = datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds")
        except OSError:
            pass
        return self._ok(
            "query_tasks",
            data={
                "count": len(tasks),
                "result_count": len(tasks),
                "result_scope": effective_scope,
                "total_count": len(tasks),
                "returned_count": len(visible_tasks),
                "truncated": len(visible_tasks) < len(tasks),
                "offset": safe_offset,
                "next_offset": safe_offset + len(visible_tasks) if safe_offset + len(visible_tasks) < len(tasks) else None,
                "has_more": safe_offset + len(visible_tasks) < len(tasks),
                "as_of": as_of,
                "status_counts": status_counts,
                "tasks": visible_tasks,
                "task_ids": [item["task_id"] for item in task_summaries],
                "task_summaries": task_summaries,
                "requested_scope": requested_scope or effective_scope,
                "effective_scope": effective_scope,
                "scope_adjusted": scope_adjusted,
                "scope_user_id": target_teacher_id or (self.identity.canonical_user_id if effective_scope == "mine" else ""),
                "scope_person_name": requested_teacher or (self.identity.person_name if effective_scope == "mine" else ""),
                "date_scope": requested_date_scope,
                "rendered_count": len(task_summaries),
                "rendered_text": "\n".join(lines),
                "data_version": data_version,
            },
            message=f"查询到 {len(tasks)} 个任务。",
        )

    def cancel_task(
        self,
        *,
        task_id: str = "",
        reason: str = "",
        operation_id: str = "",
        student_name: str = "",
        teacher_name: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"boss", "manager", "teacher"}:
            return self._error("permission_denied", "当前账号无权取消托管任务。")

        def execute() -> dict[str, Any]:
            tasks = self.store.load_tasks()
            visible_tasks = self._visible_tasks()
            visible_ids = {str(item.get("id") or "") for item in visible_tasks}
            open_visible = [
                task for task in visible_tasks
                if str(task.get("status") or "") not in _CLOSED_STATUSES
            ]
            raw_text = ""
            try:
                from .runtime_foundation import current_raw_text
                raw_text = current_raw_text(self.identity.canonical_user_id)
            except Exception:
                raw_text = ""
            reason_text = str(raw_text or reason or "用户要求取消任务").strip()

            requested_teacher = str(teacher_name or "").strip()
            target_teacher_id = ""
            if requested_teacher:
                if self.identity.role not in {"boss", "manager"}:
                    return self._error("permission_denied", "当前账号不能代取消其他老师的任务。")
                teacher_identity = self._teacher_identity_by_name(requested_teacher)
                if teacher_identity is None:
                    return self._error("teacher_not_found", f"没有找到老师“{requested_teacher}”。")
                if not self._manager_can_view_teacher(teacher_identity.canonical_user_id):
                    return self._error("permission_denied", f"老师“{requested_teacher}”不在当前店长管理范围内。")
                target_teacher_id = teacher_identity.canonical_user_id

            candidates = open_visible
            if target_teacher_id:
                candidates = [
                    task for task in candidates
                    if str(task.get("assignee_userid") or "") == target_teacher_id
                ]
            if student_name:
                normalized_student = str(student_name or "").strip()
                candidates = [
                    task for task in candidates
                    if str(task.get("student_name") or "").strip() == normalized_student
                    or normalized_student in str(task.get("title") or task.get("source_text") or "")
                ]

            supplied_task_id = str(task_id or "").strip()
            active_context_task = self._active_context_open_task()
            active_context_task_id = str(active_context_task.get("id") or "") if active_context_task else ""
            target: dict[str, Any] | None = None
            if supplied_task_id:
                target = next(
                    (task for task in candidates if str(task.get("id") or "") == supplied_task_id),
                    None,
                )
                if target is None and supplied_task_id in visible_ids:
                    target = next(
                        (task for task in tasks if str(task.get("id") or "") == supplied_task_id),
                        None,
                    )

            if target is None:
                focus = self._read_focus()
                focus_task_id = str(focus.get("task_id") or "")
                focus_source = str(focus.get("focus_source") or "")
                focus_expires_at = str(focus.get("focus_expires_at") or "")
                try:
                    focus_not_expired = bool(focus_expires_at) and datetime.fromisoformat(focus_expires_at) >= datetime.now().astimezone()
                except (TypeError, ValueError):
                    focus_not_expired = False
                if focus_task_id and focus_source in {"task_created", "explicit_task_interaction", "task_created_by_me"} and focus_not_expired:
                    target = next(
                        (task for task in candidates if str(task.get("id") or "") == focus_task_id),
                        None,
                    )

            if target is None and len(candidates) == 1:
                target = candidates[0]

            if target is None:
                summaries = [
                    {
                        "task_id": str(task.get("id") or ""),
                        "title": str(task.get("title") or task.get("task_name") or "未命名任务"),
                        "assignee_userid": str(task.get("assignee_userid") or ""),
                        "status": str(task.get("status") or ""),
                        "student_name": str(task.get("student_name") or ""),
                    }
                    for task in candidates[:8]
                ]
                return self._ok(
                    "cancel_task",
                    data={
                        "result_action": "clarification_needed",
                        "reason_code": "ambiguous_or_missing_task_reference",
                        "candidate_count": len(candidates),
                        "candidate_tasks": summaries,
                        "writeback_verified": True,
                        "idempotency_verified": True,
                        "no_write_performed": True,
                    },
                    message="你要取消的是哪个任务？请补一下学生姓名、老师或任务内容。",
                    already_applied=True,
                )

            target_id = str(target.get("id") or "")
            if target_id not in visible_ids:
                return self._error("permission_denied", "当前账号无权取消该任务。")
            canonical_target = next(
                (task for task in tasks if str(task.get("id") or "") == target_id),
                None,
            )
            if canonical_target is None:
                return self._error("task_not_found", "没有找到指定任务。")
            target = canonical_target
            if str(target.get("status") or "") in _CLOSED_STATUSES:
                return self._ok(
                    "cancel_task",
                    data={
                        "task": deepcopy(target),
                        "result_action": "already_closed",
                        "writeback_verified": True,
                        "idempotency_verified": True,
                    },
                    message="这个任务已经是关闭状态，不需要重复取消。",
                    already_applied=True,
                )

            result_holder: dict[str, Any] = {}

            def cancel_current(current: dict[str, Any]) -> None:
                if str(current.get("status") or "") in _CLOSED_STATUSES:
                    result_holder["action"] = "already_closed"
                    result_holder["reply"] = "这个任务已经是关闭状态，不需要重复取消。"
                    return
                current_result = cancel_task_state(
                    current,
                    self.identity.canonical_user_id,
                    self.identity.role,
                    reason_text,
                    now=datetime.now().astimezone(),
                )
                result_holder["action"] = current_result.action
                result_holder["reply"] = current_result.reply
                if current_result.action == "forbidden":
                    return
                events = current.get("closure_events")
                if not isinstance(events, list):
                    events = []
                events.append(
                    {
                        "action": "cancelled",
                        "actor_userid": self.identity.canonical_user_id,
                        "actor_role": self.identity.role,
                        "text": reason_text,
                        "at": str(current.get("cancelled_at") or datetime.now().astimezone().isoformat(timespec="seconds")),
                    }
                )
                current["closure_events"] = events[-50:]

            persisted = self.store.update_task(target_id, cancel_current)
            if result_holder.get("action") == "forbidden":
                return self._error("permission_denied", str(result_holder.get("reply") or "当前账号无权取消该任务。"))
            if persisted is None:
                return self._error("task_not_found", "任务在更新前已不存在，请重新查询。")
            suppressed = self._suppress_pending_notifications_for_task(target_id, reason="task_cancelled")
            cleared = self._clear_task_context_for_task(target_id)
            linked_work = self._retire_task_linked_work(
                persisted or target,
                operation_id=operation_id,
                reason="task_cancelled",
            )
            result_action = str(result_holder.get("action") or "cancelled")
            persisted_status = str(persisted.get("status") or "") if isinstance(persisted, dict) else ""
            verified = bool(
                isinstance(persisted, dict)
                and (
                    persisted_status == "cancelled"
                    or (result_action == "already_closed" and persisted_status in _CLOSED_STATUSES)
                )
            )
            return self._ok(
                "cancel_task",
                data={
                    "task_id": target_id,
                    "task": deepcopy(persisted or target),
                    "result_action": result_action,
                    "suppressed_notification_count": suppressed,
                    "cleared_context": cleared,
                    "linked_work": linked_work,
                    "writeback_verified": bool(verified and linked_work.get("writeback_verified")),
                    "idempotency_verified": True,
                },
                message=(
                    "这个任务已经是关闭状态，相关提醒已收口。"
                    if verified and result_action == "already_closed"
                    else ("任务已取消，后续提醒已收口。" if verified else "任务取消已尝试，但写后反查没有通过。")
                ),
            )

        return self._operation(operation_id, "cancel_task", execute)

    def next_task(self) -> dict[str, Any]:
        """Select and focus the next real task instead of returning a list."""
        denied = self._approved()
        if denied:
            return denied
        exclusion_config = self.store.read_json("task_execution_exclusions.json", {})
        excluded_ids = {
            str(value)
            for value in (
                exclusion_config.get("excluded_task_ids", [])
                if isinstance(exclusion_config, dict)
                else []
            )
        }
        tasks = [
            task for task in self._visible_tasks()
            if str(task.get("assignee_userid") or "") == self.identity.canonical_user_id
            and str(task.get("id") or "") not in excluded_ids
            and str(task.get("status") or "") not in _CLOSED_STATUSES
            and "测试" not in str(task.get("title") or "")
            and str(task.get("environment") or "").lower() not in {"test", "demo"}
        ]
        priority = {"S": 0, "A": 1, "B": 2, "C": 3}
        tasks.sort(key=lambda item: (
            priority.get(str(item.get("level") or "").upper(), 9),
            str(item.get("due_at") or "9999-12-31"),
            str(item.get("created_at") or ""),
        ))
        if not tasks:
            return self._ok(
                "next_task",
                data={"result_count": 0, "rendered_text": "当前没有待处理任务，可以先完成今天的学生记录。", "render_verified": True},
                message="当前没有待处理任务。",
            )
        task = tasks[0]
        task_id = str(task.get("id") or "")
        student = str(task.get("student_name") or "")
        title = str(task.get("title") or "未命名任务")
        level = str(task.get("level") or "")
        task_type = str(task.get("type") or "")
        self._write_focus(
            task_id=task_id,
            student_name=student,
            focus_source="explicit_task_interaction",
            focus_expires_at=(datetime.now().astimezone() + timedelta(minutes=30)).isoformat(timespec="seconds"),
        )
        if task_type == "safety_incident" or level == "S":
            missing = closure_missing_fields(task, str(task.get("evidence_summary") or ""))
            missing_text = "、".join(_MISSING_FIELD_LABELS.get(item, item) for item in missing) if missing else "当前状态、处理措施、家长是否知情和后续安排"
            guidance = f"请补充：{missing_text}。信息完整后我会明确告诉你是否闭环。"
        elif task_type in {"parent_anxiety", "parent_complaint", "renewal_risk"} or "家长沟通" in title:
            reference = answer_script_request(f"{student} {title}", self.store)
            guidance = f"{reference}\n\n沟通后直接告诉我家长的反馈和下一步安排，我会自动判断并明确告诉你任务是否完成。"
        else:
            requirement = str(task.get("closure_requirement") or task.get("source_text") or "").strip()
            guidance = f"需要完成：{requirement or title}。做完后把实际结果告诉我，我会记录并明确确认任务状态。"
        rendered = f"接下来做：{title}（{level or '普通'}级）\n学生：{student or '未指定'}\n{guidance}"
        return self._ok(
            "next_task",
            data={
                "task_id": task_id,
                "task": deepcopy(task),
                "result_count": 1,
                "rendered_text": rendered,
                "render_verified": True,
            },
            message=rendered,
        )

    def current_task_guidance(self) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        focus = self._read_focus()
        task_id = str(focus.get("task_id") or "")
        task = next(
            (item for item in self._visible_tasks() if str(item.get("id") or "") == task_id),
            None,
        )
        if task is None:
            rendered = "当前没有锁定的任务。你可以回复“继续下一个任务”，我会直接带你进入下一项。"
            return self._ok(
                "current_task_guidance",
                data={"reason_code": "no_focus_task", "result_count": 0, "rendered_text": rendered, "render_verified": True},
                message=rendered,
            )
        title = str(task.get("title") or "该任务")
        status = str(task.get("status") or "pending")
        if status in _CLOSED_STATUSES:
            rendered = f"“{title}”已经完成，不需要重复处理。你可以回复“继续下一个任务”。"
            missing: list[str] = []
        else:
            missing = closure_missing_fields(task, str(task.get("evidence_summary") or ""))
            labels = [_MISSING_FIELD_LABELS.get(item, item) for item in missing]
            if labels:
                rendered = f"“{title}”还没有完成。请补充：{'、'.join(labels)}。你直接用平常说话的方式告诉我即可。"
            elif str(task.get("type") or "") == "safety_incident" or str(task.get("level") or "") == "S":
                rendered = f"“{title}”的闭环信息已经齐了。请回复“完成了”，我会确认关闭安全任务。"
            else:
                rendered = f"“{title}”的处理信息已经齐了，我会按真实状态完成任务。完成后可回复“继续下一个任务”。"
        return self._ok(
            "current_task_guidance",
            data={
                "task_id": task_id,
                "task": deepcopy(task),
                "missing_fields": missing,
                "result_count": 1,
                "rendered_text": rendered,
                "render_verified": True,
            },
            message=rendered,
        )

    def record_student(
        self,
        *,
        student_name: str = "",
        content: str,
        operation_id: str,
        student_id: str = "",
        object_ref: str = "",
        class_name: str = "",
        grade: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        requested_student_id = str(student_id or "").strip()
        requested_student_name = str(student_name or "").strip()
        requested_object_ref = str(object_ref or "").strip()
        requested_class_name = str(class_name or "").strip()
        requested_grade = str(grade or "").strip()
        confirmed_object: dict[str, Any] | None = None
        # A model-provided ref or ID is never authority.  There is one safe
        # continuation exception: the same authenticated session may already
        # contain a source turn that explicitly named an ambiguous subject and
        # a current source turn that uniquely confirms an original candidate.
        # In that case this server-side evidence selects the object; any model
        # ref/ID is merely checked against it below and cannot choose another
        # student or invent a new target.
        pending_confirmation: dict[str, Any] | None = None
        if self.session_key and requested_student_name:
            pending_confirmation = resolve_pending_candidate_confirmation(
                self.store,
                self.identity,
                session_key=self.session_key,
                object_type="student",
                display_name=requested_student_name,
                object_id=requested_student_id,
                trusted_confirmation_text=self._trusted_runtime_raw_text("record_student"),
            )
        if requested_object_ref:
            confirmed_object = resolve_confirmed_object(
                self.store,
                self.identity,
                session_key=self.session_key,
                object_type="student",
                object_ref=requested_object_ref,
                require_write_authorization=True,
            )
            if confirmed_object is None:
                confirmed_object = pending_confirmation
                if confirmed_object is None:
                    result = self._error(
                        "student_object_reference_invalid",
                        "该学生会话引用无效、已过期或不属于当前身份，未执行记录。",
                    )
                    result["data"] = {"no_write_performed": True}
                    return result
        elif requested_student_id and self.session_key:
            # An ID must be provenance-backed when it is used to continue a
            # session.  A coincidentally valid identifier is not an authority
            # to select a same-name student.
            confirmed_object = resolve_confirmed_object(
                self.store,
                self.identity,
                session_key=self.session_key,
                object_type="student",
                object_id=requested_student_id,
                require_write_authorization=True,
            )
            if confirmed_object is None:
                confirmed_object = pending_confirmation
                if confirmed_object is None:
                    result = self._error(
                        "student_id_not_authorized_for_session",
                        "该学生 ID 不是当前会话可信查询确认的对象，未执行记录。",
                    )
                    result["data"] = {"no_write_performed": True}
                    return result
        elif pending_confirmation is not None:
            confirmed_object = pending_confirmation
        if confirmed_object is not None:
            evidence_id = str(confirmed_object.get("object_id") or "").strip()
            evidence_name = str(confirmed_object.get("display_name") or "").strip()
            if requested_student_id and requested_student_id != evidence_id:
                result = self._error("student_object_reference_mismatch", "学生 ID 与当前会话可信对象不一致，未执行记录。")
                result["data"] = {"no_write_performed": True}
                return result
            if requested_student_name and evidence_name and requested_student_name != evidence_name:
                result = self._error("student_object_reference_mismatch", "学生姓名与当前会话可信对象不一致，未执行记录。")
                result["data"] = {"no_write_performed": True}
                return result
            requested_student_id = evidence_id
            requested_student_name = evidence_name
        inbound_text = self._trusted_runtime_raw_text("record_student")
        # A current task result must be returned to the task workflow before
        # the record-specific grounding guard.  This is a tool contract and
        # leaves the model free to choose its Tool; it prevents a genuine task
        # result from being misreported as a generic name-grounding failure.
        if self.identity.role == "teacher":
            compact_inbound = "".join(str(inbound_text or "").split())
            active_task = self._active_open_task()
            active_student = str((active_task or {}).get("student_name") or "").strip()
            task_result_like = any(
                term in compact_inbound
                for term in (
                    "已沟通", "沟通过了", "家长说", "家长反馈", "已经处理", "处理完了",
                    "任务完成", "完成了", "结果是", "回复说", "反馈的是", "家长态度",
                    "开学再考虑", "续费考虑", "暂时不续", "确定续费",
                )
            )
            if active_task and task_result_like and (
                not requested_student_name or not active_student or requested_student_name == active_student
            ):
                result = self._error(
                    "wrong_tool_for_active_task_update",
                    "这条消息是当前任务的处理结果，不应另建学生记录任务。请改用 tuoguan_update_task，并把 reply 保持为老师本轮原话。",
                )
                result["data"] = {
                    "suggested_tool": "tuoguan_update_task",
                    "task_id": str(active_task.get("id") or ""),
                    "student_name": active_student,
                    "trusted_reply": str(inbound_text or ""),
                    "no_write_performed": True,
                }
                return result
        # A model must not select a different student from a broad directory
        # result when the teacher did not name that child in this turn.  A
        # continued conversation remains supported through the existing
        # authorised ``student_id`` path below: for a same-name child the
        # teacher must additionally give the class/grade cue in this turn.
        # This is a write-boundary grounding check, not Robot-side parsing or
        # intent selection, and it performs no name extraction from the text.
        if (
            self.session_key
            and inbound_text
            and requested_student_name
            and not requested_student_id
            and confirmed_object is None
            and requested_student_name not in inbound_text
        ):
            result = self._error(
                "student_name_not_grounded_in_turn",
                "当前这句话没有明确该学生姓名；请先自然确认对象，或使用已授权查询返回的 student_id。",
            )
            result["data"] = {"no_write_performed": True}
            return result
        requested_reference = str(requested_student_id or requested_student_name or "").strip()
        name, student_profile = resolve_student_for_record(
            self.store,
            self.identity,
            requested_reference,
            allow_student_id=bool(requested_student_id),
        )
        if not name:
            reason_code = str(student_profile.get("reason_code") or "student_not_found")
            messages = {
                "student_name_ambiguous": "存在同名学生，请先查询并根据班级确认具体学生后再记录。",
                "permission_denied": "当前账号无权记录该学生。",
                "cross_tenant_denied": "当前账号不能记录其他租户的学生。",
            }
            result = self._error(reason_code, messages.get(reason_code, "没有找到可记录的学生。"))
            if reason_code == "student_name_ambiguous":
                result["data"] = {
                    "candidates": list(student_profile.get("candidates") or []),
                    "no_write_performed": True,
                }
            return result
        canonical_student_id = str(student_profile.get("student_id") or "").strip()
        if not self.permissions.can_write_student_record(self.identity, canonical_student_id):
            return self._error(
                "permission_denied",
                f"当前账号无权记录学生“{name}”。",
            )
        if requested_class_name or requested_grade:
            classroom_matches = filter_candidates_by_classroom(
                [{
                    "student_id": canonical_student_id,
                    "student_name": name,
                    "profile": student_profile,
                }],
                class_name=requested_class_name,
                grade=requested_grade,
            )
            if not classroom_matches:
                result = self._error(
                    "student_classroom_mismatch",
                    "提供的班级或年级与已确认学生不一致，未执行记录。",
                )
                result["data"] = {"no_write_performed": True}
                return result
        if requested_student_id and confirmed_object is None:
            # A duplicate display name cannot be resolved by the model merely
            # choosing an ID.  The teacher must provide a class/grade cue in
            # this new turn; only then may the model pass the canonical ID it
            # learned from an authorised Tool result.  This keeps the safety
            # decision in the existing Tool boundary, not in Robot transport.
            same_name_candidates = [
                entry for entry in find_student_candidates(self.store, name, allow_identifiers=False)
                if str(entry.get("student_name") or "") == name
            ]
            if len(same_name_candidates) > 1:
                raw_confirmation = self._trusted_runtime_raw_text("record_student")
                profile_labels = {
                    str(student_profile.get(key) or "").strip()
                    for key in ("class_name", "class", "grade")
                    if str(student_profile.get(key) or "").strip()
                }
                if not any(label in raw_confirmation for label in profile_labels):
                    result = self._error(
                        "student_disambiguation_confirmation_required",
                        "同名学生需要老师在本轮按班级或年级确认后才能记录。",
                    )
                    result["data"] = {
                        "candidates": safe_candidate_labels(same_name_candidates),
                        "no_write_performed": True,
                    }
                    return result
        if self.identity.role == "teacher":
            inbound_text = self._trusted_runtime_raw_text("record_student")
            compact_inbound = "".join(str(inbound_text or "").split())
            active_task = self._active_open_task()
            active_student = str((active_task or {}).get("student_name") or "").strip()
            task_result_like = any(
                term in compact_inbound
                for term in (
                    "已沟通", "沟通过了", "家长说", "家长反馈", "已经处理", "处理完了",
                    "任务完成", "完成了", "结果是", "回复说", "反馈的是", "家长态度",
                    "开学再考虑", "续费考虑", "暂时不续", "确定续费",
                )
            )
            if active_task and task_result_like and (not name or not active_student or name == active_student):
                result = self._error(
                    "wrong_tool_for_active_task_update",
                    "这条消息是当前任务的处理结果，不应另建学生记录任务。请改用 tuoguan_update_task，并把 reply 保持为老师本轮原话。",
                )
                result["data"] = {
                    "suggested_tool": "tuoguan_update_task",
                    "task_id": str(active_task.get("id") or ""),
                    "student_name": active_student,
                    "trusted_reply": str(inbound_text or ""),
                    "no_write_performed": True,
                }
                return result

        def execute() -> dict[str, Any]:
            text = str(content or "").strip()
            analysis = analyze_teacher_record(
                text,
                self.store,
                resolved_student_name=name,
                resolved_student_id=canonical_student_id,
                resolved_student_profile=student_profile,
            )
            program_id, _reason = resolve_record_program(self.store, self.identity, text)
            if not program_id:
                return self._error("program_required", "请先说明记录属于2026暑假班还是托管班。")
            analysis["program_id"] = program_id
            analysis["content"] = text
            analysis["source_text"] = text
            saved = save_analysis(
                analysis,
                self.identity.canonical_user_id,
                self.store,
            )
            task = saved.get("task")
            saved_record = saved["record"]
            record_id = str(saved_record.get("id") or saved_record.get("record_id") or "")
            persisted_records = self.store.read_json("records.json", [])
            record_verified = isinstance(persisted_records, list) and any(
                isinstance(item, dict)
                and (
                    (record_id and str(item.get("id") or item.get("record_id") or "") == record_id)
                    or (
                        not record_id
                        and str(item.get("student_name") or item.get("student") or "") == name
                        and str(item.get("content") or item.get("source_text") or "") == text
                    )
                )
                for item in persisted_records
            )
            self._write_focus(
                student_name=name,
                task_id=str(task.get("id") or "") if isinstance(task, dict) else "",
            )
            object_evidence = remember_resolved_object(
                self.store,
                self.identity,
                session_key=self.session_key,
                object_type="student",
                object_id=canonical_student_id,
                display_name=name,
                attributes={
                    key: student_profile.get(key)
                    for key in ("class_name", "class", "grade", "campus_id")
                    if student_profile.get(key) not in {None, ""}
                },
                source_tool="tuoguan_record_student",
                write_authorized=True,
                authority_basis="verified_protected_write",
            )
            return self._ok(
                "record_student",
                data={
                    "record": saved_record,
                    "record_id": record_id,
                    "student_id": canonical_student_id,
                    "task": task,
                    "task_created": bool(saved.get("created")),
                    "writeback_verified": record_verified,
                    "business_object_evidence": object_evidence or {},
                },
                message=(
                    f"已记录{name}的情况。"
                    if not task
                    else f"已记录{name}的情况，并{'创建' if saved.get('created') else '更新'}"
                    f"{task.get('level', 'C')}级任务。"
                ),
            )

        return self._operation(operation_id, "record_student", execute)

    def record_summer_lesson(
        self,
        *,
        raw_text: str,
        operation_id: str,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if not is_summer_operator(self.store, self.identity):
            return self._error("permission_denied", "当前账号没有暑假班课程记录权限，请联系机构负责人确认。")

        def execute() -> dict[str, Any]:
            trusted_text = str(raw_text or "").strip()
            try:
                from .runtime_foundation import current_raw_text
                inbound_text = current_raw_text(self.identity.canonical_user_id)
            except Exception:
                inbound_text = ""
            if inbound_text:
                trusted_text = inbound_text
            saved = save_summer_lesson_record(
                self.store,
                text=trusted_text,
                teacher_userid=self.identity.canonical_user_id,
                teacher_name=self.identity.person_name,
                actor_is_manager=self.identity.role in {"boss", "manager"},
            )
            if not saved.get("ok"):
                return self._error(
                    str(saved.get("error") or "lesson_scope_unclear"),
                    str(saved.get("clarification") or "课程覆盖范围还不明确，请补充班级和课节。"),
                )
            lesson = saved.get("lesson_record") or {}
            lesson_id = str(lesson.get("id") or "")
            persisted = self.store.read_json("summer_lesson_records.json", [])
            verified = isinstance(persisted, list) and any(
                isinstance(item, dict) and str(item.get("id") or "") == lesson_id
                for item in persisted
            )
            coverage = saved.get("coverage") or {}
            return self._ok(
                "record_summer_lesson",
                data={
                    "lesson_record": lesson,
                    "lesson_record_id": lesson_id,
                    "coverage": {
                        **coverage,
                        "overall_count": len(coverage.get("overall_students") or []),
                        "individual_count": len(coverage.get("individual_students") or []),
                    },
                    "student_record_ids": [
                        str(item.get("id") or item.get("record_id") or "")
                        for item in saved.get("student_records") or []
                        if isinstance(item, dict)
                    ],
                    "writeback_verified": verified,
                },
                message="课程整体记录已保存并完成覆盖反查。" if verified else "课程记录写入后的反查未通过。",
            )

        return self._operation(operation_id, "record_summer_lesson", execute)

    def update_task(
        self,
        *,
        task_id: str = "",
        reply: str = "",
        action: str = "",
        operation_id: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied

        def execute() -> dict[str, Any]:
            tasks = self.store.load_tasks()
            visible_ids = {
                str(task.get("id") or "") for task in self._visible_tasks()
            }
            raw_reply = str(reply or "").strip()
            trusted_raw = self._trusted_runtime_raw_text("update_task")
            # The explicit tool argument belongs to this invocation. A runtime
            # raw message is stronger evidence only while the live turn is
            # still present; stale process memory must never overwrite an
            # explicit cancellation phrase from the current tool call.
            evidence_text = trusted_raw or raw_reply
            visible_tasks = self._visible_tasks()
            open_visible = [task for task in visible_tasks if task.get("status") not in _CLOSED_STATUSES]
            supplied_task_id = str(task_id or "").strip()
            # The durable assignee context is authoritative for a natural
            # completion reply.  A model-provided id may only use this path
            # when it agrees with that context; it can never select a task
            # from another person or an old thread by itself.
            active_context_task = self._active_context_open_task()
            active_context_task_id = str(active_context_task.get("id") or "") if active_context_task else ""
            if _looks_like_task_cancel_intent(evidence_text):
                candidates = [
                    {
                        "task_id": str(task.get("id") or ""),
                        "title": str(task.get("title") or task.get("task_name") or "未命名任务"),
                        "status": str(task.get("status") or ""),
                        "student_name": str(task.get("student_name") or ""),
                        "assignee_userid": str(task.get("assignee_userid") or ""),
                    }
                    for task in open_visible[:8]
                ]
                return self._ok(
                    "update_task",
                    data={
                        "result_action": "wrong_tool_for_cancel_intent",
                        "reason_code": "use_tuoguan_cancel_task",
                        "suggested_tool": "tuoguan_cancel_task",
                        "task_id": supplied_task_id,
                        "candidate_task_count": len(open_visible),
                        "candidate_tasks": candidates,
                        "writeback_verified": False,
                        "idempotency_verified": False,
                        "no_write_performed": True,
                    },
                    message=(
                        "这是取消、关闭或停止提醒任务的意图，不能用任务更新工具处理。"
                        "请改用 tuoguan_cancel_task；如果任务不明确，先用学生、老师或任务内容定位。"
                    ),
                    already_applied=True,
                )
            compact_evidence = "".join(evidence_text.split()).rstrip("。！？!?")
            generic_complete = compact_evidence in {
                "这个任务已经完成",
                "这个任务完成了",
                "刚才那个任务完成了",
                "任务已经完成",
                "这个任务已经处理了",
                "已经处理了",
                "完成了",
                "处理完了",
            }
            requested_action = str(action or "").strip().lower()
            completion_intent = (
                requested_action == "request_completion"
                or (
                    requested_action != "progress"
                    and (
                        generic_complete
                        or (
                            "任务" in compact_evidence
                            and any(word in compact_evidence for word in ("完成", "处理了", "处理完"))
                        )
                    )
                )
            )
            ordinary_feedback = any(
                phrase in compact_evidence
                for phrase in (
                    "回访",
                    "后天再跟进",
                    "家长说还在考虑",
                    "已经沟通过了",
                    "这个任务已经完成",
                    "这个任务完成了",
                    "刚才那个任务完成了",
                )
            )
            ordinary_visible = [
                task for task in visible_tasks
                if str(task.get("type") or "") != "safety_incident"
                and str(task.get("level") or "") != "S"
            ]
            mention_pool = ordinary_visible if ordinary_feedback else visible_tasks
            mentioned = [
                task for task in mention_pool
                if str(task.get("student_name") or "")
                and str(task.get("student_name") or "").replace("测试", "") in evidence_text.replace("测试", "")
            ]
            resolved_task_id = ""
            explicit_task_id_in_text = bool(supplied_task_id and supplied_task_id in evidence_text)
            focus = self._read_focus()
            focus_task_id = str(focus.get("task_id") or "")
            focus_source = str(focus.get("focus_source") or "")
            focus_expires_at = str(focus.get("focus_expires_at") or "")
            try:
                focus_not_expired = bool(focus_expires_at) and datetime.fromisoformat(focus_expires_at) >= datetime.now().astimezone()
            except (TypeError, ValueError):
                focus_not_expired = False
            focus_task = next(
                (task for task in visible_tasks if str(task.get("id") or "") == focus_task_id),
                None,
            )
            valid_focus = bool(
                focus_task
                and focus_source in {"task_created", "explicit_task_interaction"}
                and focus_not_expired
            )
            if mentioned:
                unique_ids = {str(item.get("id") or "") for item in mentioned}
                if generic_complete and len(unique_ids) > 1:
                    return self._ok(
                        "update_task",
                        data={
                            "result_action": "clarification_needed",
                            "reason_code": "ambiguous_task_reference",
                            "writeback_verified": False,
                            "idempotency_verified": False,
                            "no_write_performed": True,
                            "candidate_task_ids": sorted(unique_ids),
                        },
                        message="你说的是哪个任务？请说一下学生姓名或任务内容，比如“测试小林回访任务完成了”。",
                        already_applied=True,
                    )
                resolved_task_id = str(sorted(
                    mentioned,
                    key=lambda item: (
                        item.get("status") not in _CLOSED_STATUSES,
                        str(item.get("updated_at") or item.get("created_at") or ""),
                    ),
                    reverse=True,
                )[0].get("id") or "")
            elif supplied_task_id and supplied_task_id in {str(task.get("id") or "") for task in open_visible}:
                # The model may receive a valid id from the current
                # WorkContextSnapshot even though a teacher did not repeat the
                # opaque id in natural language.  Visibility and openness are
                # verified here, so this cannot select another person's task.
                resolved_task_id = supplied_task_id
            elif supplied_task_id and supplied_task_id == active_context_task_id:
                resolved_task_id = supplied_task_id
            elif active_context_task and (generic_complete or ordinary_feedback):
                resolved_task_id = active_context_task_id
            elif valid_focus:
                resolved_task_id = focus_task_id
            elif explicit_task_id_in_text:
                # A model-supplied id is never authoritative by itself. It is
                # trusted only when the user actually included it in the
                # message; conversational references must resolve through the
                # scoped, non-expired focus above.
                resolved_task_id = supplied_task_id
            elif (generic_complete or ordinary_feedback) and len(open_visible) == 1:
                resolved_task_id = str(open_visible[0].get("id") or "")
            elif generic_complete or ordinary_feedback:
                reason_code = "focus_task_expired" if focus_task_id and focus_source and not focus_not_expired else "no_focus_task"
                return self._ok(
                    "update_task",
                    data={
                        "result_action": "clarification_needed",
                        "reason_code": reason_code,
                        "writeback_verified": False,
                        "idempotency_verified": False,
                        "no_write_performed": True,
                    },
                    message="你说的是哪个任务？请说一下学生姓名或任务内容，比如“位俊丞家长沟通任务完成了”。",
                    already_applied=True,
                )
            if not resolved_task_id:
                return self._ok(
                    "update_task",
                    data={
                        "result_action": "clarification_needed",
                        "reason_code": "no_focus_task",
                        "writeback_verified": False,
                        "idempotency_verified": False,
                        "no_write_performed": True,
                    },
                    message="你要处理的是哪个任务？请说一下学生姓名或任务内容。",
                    already_applied=True,
                )
            target = next(
                (
                    task
                    for task in tasks
                    if str(task.get("id") or "") == resolved_task_id
                ),
                None,
            )
            if target is None:
                return self._error("task_not_found", "没有找到指定任务。")
            if str(target.get("id") or "") not in visible_ids:
                return self._error("permission_denied", "当前账号无权处理该任务。")
            is_safety = str(target.get("type") or "") == "safety_incident" or str(target.get("level") or "") == "S"
            was_closed = str(target.get("status") or "") in _CLOSED_STATUSES
            status_question = any(mark in compact_evidence for mark in ("完成了没有", "完成了吗", "是否完成", "完成没"))
            if status_question:
                status = str(target.get("status") or "pending")
                completed = status in _CLOSED_STATUSES
                return self._ok(
                    "update_task",
                    data={
                        "result_action": "task_status",
                        "reason_code": "already_completed" if completed else "task_in_progress",
                        "task": deepcopy(target),
                        "writeback_verified": False,
                        "idempotency_verified": False,
                        "no_write_performed": True,
                        "missing_fields": closure_missing_fields(target, str(target.get("evidence_summary") or "")),
                    },
                    message="这个任务已经完成。" if completed else "这个任务还没有完成。",
                    already_applied=True,
                )
            if completion_intent and was_closed and not is_safety:
                return self._ok(
                    "update_task",
                    data={
                        "result_action": "idempotency_hit",
                        "reason_code": "already_completed",
                        "task": deepcopy(target),
                        "writeback_verified": True,
                        "idempotency_verified": True,
                        "missing_fields": [],
                    },
                    message="这个任务已经是完成状态，不需要重复处理。",
                    already_applied=True,
                )
            update_result: dict[str, Any] = {}

            def update_current(current: dict[str, Any]) -> None:
                current_is_safety = str(current.get("type") or "") == "safety_incident" or str(current.get("level") or "") == "S"
                current_was_closed = str(current.get("status") or "") in _CLOSED_STATUSES
                actor = (
                    str(current.get("assignee_userid") or "")
                    if self.identity.role in {"manager", "boss"}
                    else self.identity.canonical_user_id
                )
                contract = current.get("task_contract") if isinstance(current.get("task_contract"), dict) else {}
                natural_parent_confirmation = bool(
                    not current_is_safety
                    and str(contract.get("completion_policy") or "") != "strict_evidence"
                    and (
                        str(current.get("type") or "") in {"parent_anxiety", "parent_complaint", "renewal_risk"}
                        or "家长" in f"{current.get('title') or ''}{current.get('source_text') or ''}"
                    )
                    and any(term in compact_evidence for term in ("已沟通", "已经沟通", "联系过", "已联系", "已经联系", "妈妈说", "爸爸说", "家长说", "家长反馈"))
                )
                completion_requested = completion_intent or natural_parent_confirmation
                if current_was_closed and evidence_text and not current_is_safety and not completion_requested:
                    previous = str(current.get("evidence_summary") or "").strip()
                    evidence_lines = [line.strip() for line in previous.splitlines() if line.strip()]
                    if evidence_text not in evidence_lines:
                        current["evidence_summary"] = "\n".join([*evidence_lines, evidence_text]).strip()
                    stamp = datetime.now().isoformat(timespec="seconds")
                    current["updated_at"] = stamp
                    events = current.get("closure_events")
                    if not isinstance(events, list):
                        events = []
                    events.append({"action": "fact_added_after_completion", "actor_userid": actor, "text": evidence_text, "at": stamp})
                    current["closure_events"] = events[-50:]
                    result_action = "fact_added_after_completion"
                    result_reply = "已把这次回访情况补充到原任务记录中，任务仍保持已完成。"
                else:
                    current_result = apply_task_reply(
                        [current],
                        actor,
                        evidence_text,
                        action=requested_action,
                    )
                    result_action = current_result.action
                    result_reply = current_result.reply
                closure_gaps = closure_missing_fields(
                    current,
                    str(current.get("evidence_summary") or evidence_text),
                )
                auto_parent_complete = bool(
                    not current_is_safety
                    and (str(current.get("type") or "") in {"parent_anxiety", "parent_complaint", "renewal_risk"} or "家长沟通" in str(current.get("title") or ""))
                    and "沟通" in compact_evidence
                    and any(word in compact_evidence for word in ("家长", "妈妈", "爸爸"))
                    and any(word in compact_evidence for word in ("知道", "表示", "说", "反馈", "关注", "考虑", "同意", "认可"))
                    and any(word in compact_evidence for word in ("后期", "后续", "继续", "再", "关注", "跟进", "观察"))
                    and not closure_gaps
                )
                if auto_parent_complete:
                    fields = current.get("closure_fields") if isinstance(current.get("closure_fields"), dict) else {}
                    fields.update({"parent_informed": evidence_text, "parent_attitude": evidence_text, "followup_plan": evidence_text})
                    current["closure_fields"] = fields
                    completion_requested = True
                    result_action = "completed"
                    result_reply = "家长沟通结果和后续安排已记录，任务已完成。"
                current_missing = [] if auto_parent_complete else closure_gaps
                if completion_requested and not current_is_safety and not current_missing:
                    stamp = datetime.now().isoformat(timespec="seconds")
                    current["status"] = "completed"
                    current["coach_stage"] = "closed"
                    current["completed_at"] = stamp
                    current["updated_at"] = stamp
                    current["closure_summary"] = evidence_text
                update_result.update({"action": result_action, "reply": result_reply, "missing": current_missing})

            persisted = self.store.update_task(str(target.get("id") or ""), update_current)
            if persisted is None:
                return self._error("task_not_found", "任务在更新前已不存在，请重新查询。")
            target = persisted
            result_action = str(update_result.get("action") or "fact_added")
            result_reply = str(update_result.get("reply") or "已更新任务。")
            current_missing = update_result.get("missing") if isinstance(update_result.get("missing"), list) else []
            task_verified = isinstance(persisted, dict) and str(persisted.get("updated_at") or persisted.get("completed_at") or persisted.get("evidence_summary") or "") != ""
            coaching_event: dict[str, Any] = {
                "ok": True,
                "recorded": False,
                "reason_code": "not_teacher_task_interaction",
                "writeback_verified": True,
            }
            if (
                self.identity.role == "teacher"
                and str(target.get("assignee_userid") or "") == self.identity.canonical_user_id
            ):
                from .teacher_coaching import record_teacher_coaching_event

                coaching_event = record_teacher_coaching_event(
                    self.store,
                    task=target,
                    teacher_user_id=self.identity.canonical_user_id,
                    action=result_action,
                    operation_id=operation_id,
                    missing_fields=current_missing,
                )
            final_message = result_reply
            if task_verified and str(target.get("status") or "") in _CLOSED_STATUSES:
                completion_prefix = result_reply.rstrip("。")
                final_message = (
                    f"{completion_prefix}。谢谢老师认真反馈。"
                    "你可以直接回复“继续下一个任务”，我会带你进入下一项。"
                )
            elif current_missing:
                missing_text = "、".join(_MISSING_FIELD_LABELS.get(item, item) for item in current_missing[:1])
                final_message = (
                    f"已记录你刚才的反馈，但这个任务还没有完成。还需要补充：{missing_text}。"
                    "你直接按实际情况继续说，我会接着记录并告诉你何时完成。"
                )
            goal_action_update: dict[str, Any] = {}
            goal_id = str(target.get("goal_id") or "")
            goal_action_id = str(target.get("goal_action_id") or "")
            if goal_id and goal_action_id:
                from .proactive_work import submit_goal_action

                goal_action_status = (
                    "replied_sufficient"
                    if str(target.get("status") or "") in _CLOSED_STATUSES and not current_missing
                    else "replied_partial"
                )
                system_identity = UserIdentity(
                    platform="system",
                    platform_user_id="task_reply_sync",
                    canonical_user_id="task_reply_sync",
                    person_name="小优",
                    role="boss",
                    approval_state="approved",
                )
                goal_action_update = submit_goal_action(
                    self.store,
                    identity=system_identity,
                    goal_id=goal_id,
                    action_type="create_low_risk_task",
                    summary=str(target.get("title") or "目标内低风险子任务"),
                    operation_id=f"{operation_id}:goal_action_sync",
                    goal_action_id=goal_action_id,
                    status=goal_action_status,
                    evidence_requirement=str(target.get("evidence_requirement") or ""),
                    source_text=(
                        f"task_id={target.get('id')}; actor={self.identity.canonical_user_id}; "
                        f"task_status={target.get('status')}; evidence={evidence_text}"
                    ),
                )
                if not goal_action_update.get("writeback_verified"):
                    final_message = (
                        f"{final_message.rstrip('。')}。任务记录已经更新，但关联目标进度反查失败，"
                        "我暂时不能说目标已同步推进。"
                    )
            closed_cleanup: dict[str, Any] = {
                "suppressed_notification_count": 0,
                "cleared_context": {},
                "linked_work": {},
                "writeback_verified": True,
            }
            if str(target.get("status") or "") in _CLOSED_STATUSES:
                suppressed = self._suppress_pending_notifications_for_task(
                    str(target.get("id") or ""),
                    reason="task_closed",
                )
                cleared = self._clear_task_context_for_task(str(target.get("id") or ""))
                linked_work = self._retire_task_linked_work(
                    target,
                    operation_id=operation_id,
                    reason="task_closed",
                )
                closed_cleanup = {
                    "suppressed_notification_count": suppressed,
                    "cleared_context": cleared,
                    "linked_work": linked_work,
                    "writeback_verified": bool(linked_work.get("writeback_verified")),
                }
            else:
                self._write_focus(
                    task_id=str(target.get("id") or ""),
                    student_name=str(target.get("student_name") or ""),
                    focus_source="explicit_task_interaction" if (mentioned or supplied_task_id or valid_focus) else "",
                    focus_expires_at=(datetime.now().astimezone() + timedelta(minutes=30)).isoformat(timespec="seconds") if (mentioned or supplied_task_id or valid_focus) else "",
                )
            return self._ok(
                "update_task",
                data={
                    "result_action": result_action,
                    "task_id": str(target.get("id") or ""),
                    "task": deepcopy(target),
                    "updated_at": str(target.get("updated_at") or target.get("completed_at") or ""),
                    "writeback_verified": bool(
                        task_verified
                        and (not goal_action_id or goal_action_update.get("writeback_verified"))
                        and coaching_event.get("writeback_verified")
                        and closed_cleanup.get("writeback_verified")
                    ),
                    "task_writeback_verified": task_verified,
                    "goal_action_update": goal_action_update,
                    "teacher_coaching_event": coaching_event,
                    "closed_task_cleanup": closed_cleanup,
                    "missing_fields": current_missing,
                },
                message=final_message,
            )

        return self._operation(operation_id, "update_task", execute)

    def parent_script_context(
        self,
        *,
        request: str,
        student_name: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        student = str(student_name or "").strip()
        if self.identity.role == "teacher":
            try:
                from .runtime_foundation import current_raw_text
                inbound_text = current_raw_text(self.identity.canonical_user_id) or current_raw_text(self.user_id)
            except Exception:
                inbound_text = ""
            active_task = self._active_open_task()
            active_student = str((active_task or {}).get("student_name") or "").strip()
            explicit_other = bool(student and student in str(inbound_text or ""))
            if active_task and active_student and student and student != active_student and not explicit_other:
                result = self._error(
                    "active_task_entity_mismatch",
                    f"当前任务对象是“{active_student}”，不能从旧会话切到“{student}”。请按当前任务学生重新调用本工具。",
                )
                result["data"] = {
                    "task_id": str(active_task.get("id") or ""),
                    "expected_student_name": active_student,
                    "rejected_student_name": student,
                    "no_write_performed": True,
                }
                return result
            if active_task and active_student and not student:
                student = active_student
        student_data: dict[str, Any] | None = None
        if student:
            query = self.query_students(student_name=student)
            if not query.get("ok"):
                return query
            student_data = query["data"]["students"][0]
            # Preparing a parent communication script is an explicit task
            # interaction. Persist the matching ordinary task so the teacher's
            # next natural reply (for example "已经沟通过了" / "完成了")
            # continues the same task instead of scanning unrelated work.
            matching_tasks = [
                task for task in self._visible_tasks()
                if str(task.get("student_name") or "") == student
                and str(task.get("type") or "") != "safety_incident"
                and str(task.get("level") or "") != "S"
                and str(task.get("status") or "") not in _CLOSED_STATUSES
            ]
            if matching_tasks:
                target = sorted(
                    matching_tasks,
                    key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
                    reverse=True,
                )[0]
                self._write_focus(
                    task_id=str(target.get("id") or ""),
                    student_name=student,
                    focus_source="explicit_task_interaction",
                    focus_expires_at=(datetime.now().astimezone() + timedelta(minutes=30)).isoformat(timespec="seconds"),
                )
        return self._ok(
            "parent_script_context",
            data={
                "request": str(request or "").strip(),
                "student": student_data,
                "reference": answer_script_request(
                    f"{student} {request}".strip(),
                    self.store,
                ),
            },
            message="已准备家长沟通所需的业务资料，请根据当前对话自然组织话术。",
        )

    def report_safety_event(
        self,
        *,
        student_name: str,
        event: str,
        operation_id: str,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        name = str(student_name or "").strip()
        if not self.permissions.can_write_student_record(self.identity, name):
            return self._error(
                "permission_denied",
                f"当前账号无权上报学生“{name}”的事件。",
            )

        def execute() -> dict[str, Any]:
            text = str(event or "").strip()
            analysis = analyze_teacher_record(f"{name} 受伤 {text}", self.store)
            program_id, _reason = resolve_record_program(self.store, self.identity, text)
            if not program_id:
                return self._error("program_required", "请先说明事件属于2026暑假班还是托管班。")
            analysis.update(
                {
                    "student_name": name,
                    "content": text,
                    "source_text": text,
                    "record_types": ["safety_incident"],
                    "level": "S",
                    "should_create_task": True,
                    "trigger_reason": "模型识别并通过安全事件工具上报",
                    "program_id": program_id,
                }
            )
            saved = save_analysis(
                analysis,
                self.identity.canonical_user_id,
                self.store,
            )
            task = saved["task"]
            notifications = [
                {
                    "task_id": item.task_id,
                    "role": item.role,
                    "touser": item.touser,
                    "action": item.action,
                    "content": item.content,
                }
                for item in build_notification_plan([task], self.store)
            ]
            self._enqueue_notifications(notifications)
            persisted_task = next(
                (item for item in self.store.load_tasks() if str(item.get("id") or "") == str(task.get("id") or "")),
                None,
            )
            persisted_records = self.store.read_json("records.json", [])
            record_id = str(saved["record"].get("id") or saved["record"].get("record_id") or "")
            record_verified = isinstance(persisted_records, list) and any(
                isinstance(item, dict)
                and (
                    (record_id and str(item.get("id") or item.get("record_id") or "") == record_id)
                    or (not record_id and str(item.get("student_name") or "") == name and "safety_incident" in (item.get("record_types") or []))
                )
                for item in persisted_records
            )
            writeback_verified = isinstance(persisted_task, dict) and record_verified
            self._write_focus(task_id=task["id"], student_name=name)
            return self._ok(
                "report_safety_event",
                data={
                    "record": saved["record"],
                    "task": task,
                    "task_created": bool(saved.get("created")),
                    "notifications": notifications,
                    "writeback_verified": writeback_verified,
                },
                message=(
                    f"已上报{name}的S级安全事件并创建任务。"
                    if saved.get("created")
                    else f"已将补充情况并入{name}现有的S级安全任务。"
                ),
            )

        try:
            return self._operation(operation_id, "report_safety_event", execute)
        except TuoguanStoreError:
            return self._error(
                "store_unavailable",
                "托管业务数据暂时无法安全读写，本次事件未重复提交。",
            )


    def goal_workspace(
        self,
        *,
        action: str,
        goal_text: str = "",
        confirmation_text: str = "",
        goal_id: str = "",
        student_name: str = "",
        update_text: str = "",
        withdraw_reason: str = "",
        operation_id: str = "",
        goal_type: str = "parent_communication_coverage",
        program_id: str = "regular_tuoguan",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if str(goal_type or "parent_communication_coverage") != "parent_communication_coverage":
            return self._error("unsupported_goal_type", "当前第一版只支持家长沟通覆盖目标。")
        normalized_action = str(action or "").strip() or "review"
        if normalized_action in {"review", "confirm", "withdraw", "cancel", "supersede"} and self.identity.role != "boss":
            return self._error("permission_denied", "只有老板可以审查、确认或撤回机构级目标。")
        if normalized_action in {"query_progress", "next_step"} and self.identity.role not in {"manager", "boss"}:
            return self._error("permission_denied", "只有店长或老板可以查看机构目标进度。")
        if normalized_action == "record_progress" and self.identity.role not in {"teacher", "manager", "boss"}:
            return self._error("permission_denied", "当前账号不能更新机构目标进度。")

        def run_workspace() -> dict[str, Any]:
            result = operate_goal_workspace(
                self.store,
                identity=self.identity,
                action=normalized_action,
                goal_text=goal_text,
                confirmation_text=confirmation_text,
                goal_id=goal_id,
                student_name=student_name,
                update_text=update_text,
                withdraw_reason=withdraw_reason,
                operation_id=operation_id,
                program_id=program_id or "regular_tuoguan",
            )
            if not result.get("ok"):
                return self._error(str(result.get("error") or "goal_workspace_failed"), str(result.get("message") or "目标工作区处理失败。"))
            data = result.get("data") if isinstance(result.get("data"), dict) else {**result}
            if normalized_action in {"confirm", "record_progress", "withdraw", "cancel", "supersede"}:
                data = {**result, "writeback_verified": bool(result.get("writeback_verified"))}
            return self._ok("goal_workspace", data=data, message=str(result.get("rendered_text") or result.get("message") or ""))

        if normalized_action == "confirm":
            return self._operation(operation_id, "confirm_goal", run_workspace)
        if normalized_action == "record_progress":
            return self._operation(operation_id, "update_goal_progress", run_workspace)
        if normalized_action in {"withdraw", "cancel", "supersede"}:
            return self._operation(operation_id, "withdraw_goal", run_workspace)
        return run_workspace()

    def review_goal(self, *, goal_text: str, goal_type: str = "parent_communication_coverage", program_id: str = "regular_tuoguan") -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role != "boss":
            return self._error("permission_denied", "只有老板可以发起机构级目标审查。")
        if str(goal_type or "parent_communication_coverage") != "parent_communication_coverage":
            return self._error("unsupported_goal_type", "当前第一版只支持家长沟通覆盖目标。")
        result = review_parent_communication_goal(self.store, goal_text=goal_text, program_id=program_id or "regular_tuoguan")
        return self._ok("review_goal", data=result, message=result.get("rendered_text", ""))

    def confirm_goal(self, *, goal_text: str, confirmation_text: str, operation_id: str, goal_type: str = "parent_communication_coverage", program_id: str = "regular_tuoguan") -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role != "boss":
            return self._error("permission_denied", "只有老板可以确认机构级执行目标。")
        if str(goal_type or "parent_communication_coverage") != "parent_communication_coverage":
            return self._error("unsupported_goal_type", "当前第一版只支持家长沟通覆盖目标。")

        def execute() -> dict[str, Any]:
            result = confirm_goal(
                self.store,
                identity=self.identity,
                goal_text=goal_text,
                confirmation_text=confirmation_text,
                operation_id=operation_id,
                program_id=program_id or "regular_tuoguan",
            )
            if not result.get("ok"):
                return self._error(str(result.get("error") or "goal_confirm_failed"), str(result.get("message") or "目标确认失败。"))
            data = {**result, "writeback_verified": bool(result.get("writeback_verified"))}
            return self._ok("confirm_goal", data=data, message=result.get("rendered_text", ""))

        return self._operation(operation_id, "confirm_goal", execute)

    def update_goal_progress(self, *, student_name: str, update_text: str, operation_id: str, goal_id: str = "") -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"teacher", "manager", "boss"}:
            return self._error("permission_denied", "当前账号不能更新机构目标进度。")

        def execute() -> dict[str, Any]:
            result = update_goal_progress(
                self.store,
                identity=self.identity,
                goal_id=goal_id,
                student_name=student_name,
                update_text=update_text,
                operation_id=operation_id,
            )
            if not result.get("ok"):
                return self._error(str(result.get("error") or "goal_update_failed"), str(result.get("message") or "目标进度更新失败。"))
            data = {**result, "writeback_verified": bool(result.get("writeback_verified"))}
            return self._ok("update_goal_progress", data=data, message=result.get("rendered_text", ""))

        return self._operation(operation_id, "update_goal_progress", execute)

    def query_goal_progress(self, *, goal_id: str = "") -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"manager", "boss"}:
            return self._error("permission_denied", "只有店长或老板可以查看机构目标进度。")
        result = query_goal_progress(self.store, goal_id=goal_id)
        if not result.get("ok"):
            return self._error(str(result.get("error") or "goal_not_found"), str(result.get("message") or "当前没有正在推进的目标。"))
        return self._ok("query_goal_progress", data=result, message=result.get("rendered_text", ""))


    def resolve_student_responsibility(self, *, student_name: str, purpose: str = "parent_communication") -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"teacher", "manager", "boss"}:
            return self._error("permission_denied", "当前账号不能查询学生责任关系。")
        result = resolve_student_responsibility(self.store, student_name, purpose=purpose or "parent_communication")
        if not result.get("ok"):
            return self._error(str(result.get("error") or "responsibility_unavailable"), str(result.get("message") or "暂时无法确认责任关系。"))
        lines = [f"{student_name}责任关系："]
        if not result.get("in_scope"):
            lines.append(str(result.get("reason") or "当前不在本阶段责任解析范围。"))
        else:
            lines.append(f"- 责任状态：{result.get('resolution_status')}")
            if result.get("responsible_name"):
                lines.append(f"- 建议联系：{result.get('responsible_name')}（{result.get('responsible_operating_role')}）")
            lines.append(f"- 原因：{result.get('reason')}")
            if result.get("missing_fields"):
                lines.append(f"- 需要补齐：{', '.join(result.get('missing_fields') or [])}")
                lines.append("这些缺口需要先问店长或老板确认，不能凭空安排。")
        data = {**result, "rendered_text": "\n".join(lines), "render_verified": True}
        return self._ok("resolve_student_responsibility", data=data, message=data["rendered_text"])

    def query_institution_onboarding_gaps(self, *, program_id: str = "regular_tuoguan") -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"manager", "boss"}:
            return self._error("permission_denied", "只有店长或老板可以查看机构入职调研缺口。")
        result = onboarding_gap_audit(self.store, program_id=program_id or "regular_tuoguan")
        return self._ok("query_institution_onboarding_gaps", data=result, message=result.get("rendered_text", ""))

    def query_operational_facts(
        self,
        *,
        fact_type: str = "",
        subject: str = "",
        scope: str = "",
        include_pending: bool = False,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"teacher", "manager", "boss"}:
            return self._error("permission_denied", "当前账号不能查询运营事实。")
        result = query_operational_facts(self.store, fact_type=fact_type, subject=subject, scope=scope, include_pending=include_pending)
        return self._ok("query_operational_facts", data=result, message=result.get("rendered_text", ""))

    def query_staff_directory(
        self,
        *,
        query: str = "",
        role: str = "",
        include_inactive: bool = False,
        limit: int = 30,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"teacher", "manager", "boss"}:
            return self._error("permission_denied", "当前账号不能查询人员目录。")
        result = build_staff_directory_report(
            self.store,
            query=query,
            role=role,
            include_inactive=include_inactive,
            include_pending_verified=self.identity.role == "boss",
            limit=limit,
        )
        return self._ok("query_staff_directory", data=result, message=result.get("rendered_text", ""))

    def offboard_staff(
        self,
        *,
        operation_id: str,
        target_name: str = "",
        target_user_id: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role != "boss":
            return self._error("permission_denied", "只有老板可以办理人员离职停用。")
        raw_text = self._trusted_runtime_raw_text("offboard_staff")
        if raw_text and not any(term in raw_text for term in ("离职", "停用", "删除", "移除", "开除", "不干了", "不用他了", "不用她了")):
            return self._error("missing_explicit_offboarding_intent", "本轮没有明确的离职、停用、删除或移除人员要求，未修改权限。")

        def execute() -> dict[str, Any]:
            result = offboard_staff_member(
                self.store,
                identity=self.identity,
                target_name=target_name,
                target_user_id=target_user_id,
                reason=reason,
                source_text=raw_text,
            )
            if not result.get("ok"):
                return result
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            return self._ok(
                "offboard_staff",
                data=data,
                message=str(result.get("message") or "人员已完成离职停用。"),
            )

        return self._operation(operation_id, "offboard_staff", execute)

    def query_person_workstyle_profile(
        self,
        *,
        target_user_id: str = "",
        target_role: str = "",
        scope: str = "",
        limit: int = 30,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"teacher", "manager", "boss"}:
            return self._error("permission_denied", "当前账号不能查询服务方式档案。")
        result = build_person_workstyle_profile(
            self.store,
            identity=self.identity,
            target_user_id=target_user_id,
            target_role=target_role,
            scope=scope,
            limit=limit,
        )
        if not result.get("ok"):
            return self._error(str(result.get("error") or "workstyle_profile_unavailable"), str(result.get("message") or "服务方式档案查询失败。"))
        return self._ok("query_person_workstyle_profile", data=result, message=result.get("rendered_text", ""))

    def submit_person_workstyle_preference(
        self,
        *,
        preference_type: str = "other_low_risk",
        scope: str = "all_communication",
        preference_text: str = "",
        operation_id: str = "",
        preference: str = "",
        normalized_rule: str = "",
        target_user_id: str = "",
        target_name: str = "",
        target_role: str = "",
        source_text: str = "",
        dimension_key: str = "",
        presentation_contract: dict[str, Any] | None = None,
        confidence: float | str = 1.0,
        source_turn_id: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"teacher", "manager", "boss"}:
            return self._error("permission_denied", "当前账号不能提交服务方式偏好。")
        actual_preference_text = preference_text or preference

        def execute() -> dict[str, Any]:
            result = save_person_workstyle_preference(
                self.store,
                identity=self.identity,
                preference_type=preference_type,
                scope=scope,
                preference_text=actual_preference_text,
                normalized_rule=normalized_rule,
                target_user_id=target_user_id,
                target_name=target_name,
                target_role=target_role,
                source_text=source_text,
                dimension_key=dimension_key,
                presentation_contract=presentation_contract,
                confidence=confidence,
                source_turn_id=source_turn_id,
                operation_id=operation_id,
            )
            if not result.get("ok"):
                return self._error(str(result.get("error") or "workstyle_preference_failed"), str(result.get("message") or "服务方式偏好没有保存成功。"))
            data = {**result, "writeback_verified": bool(result.get("writeback_verified"))}
            return self._ok("submit_person_workstyle_preference", data=data, message=result.get("rendered_text", ""))

        return self._operation(operation_id, "submit_person_workstyle_preference", execute)

    def query_workstyle_adaptation_health(self, *, target_user_id: str = "", scope: str = "", limit: int = 30) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"manager", "boss"}:
            return self._error("permission_denied", "只有店长或老板可以查看小优工作方式自适应健康度。")
        result = build_workstyle_adaptation_health(
            self.store,
            identity=self.identity,
            target_user_id=target_user_id,
            scope=scope,
            limit=limit,
        )
        if not result.get("ok"):
            return self._error(str(result.get("error") or "workstyle_adaptation_health_unavailable"), str(result.get("message") or "工作方式自适应健康度查询失败。"))
        return self._ok("query_workstyle_adaptation_health", data=result, message=str(result.get("rendered_text") or ""))

    def submit_operational_fact(
        self,
        *,
        fact_type: str,
        subject: str,
        value: Any,
        operation_id: str,
        scope: str = "institution",
        source_text: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"teacher", "manager", "boss"}:
            return self._error("permission_denied", "当前账号不能提交运营事实。")

        def execute() -> dict[str, Any]:
            result = submit_operational_fact_candidate(
                self.store,
                identity=self.identity,
                fact_type=fact_type,
                subject=subject,
                value=value,
                scope=scope,
                source_text=source_text,
                operation_id=operation_id,
            )
            if not result.get("ok"):
                return self._error(str(result.get("error") or "operational_fact_failed"), str(result.get("message") or "运营事实记录失败。"))
            data = {**result, "writeback_verified": bool(result.get("writeback_verified"))}
            return self._ok("submit_operational_fact", data=data, message=result.get("rendered_text", ""))

        return self._operation(operation_id, "submit_operational_fact", execute)

    def confirm_operational_fact(
        self,
        *,
        candidate_id: str,
        decision: str,
        operation_id: str,
        note: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role != "boss":
            return self._error("permission_denied", "只有老板可以确认或驳回运营事实。")

        def execute() -> dict[str, Any]:
            result = confirm_operational_fact(
                self.store,
                identity=self.identity,
                candidate_id=candidate_id,
                decision=decision,
                note=note,
            )
            if not result.get("ok"):
                return self._error(str(result.get("error") or "operational_fact_review_failed"), str(result.get("message") or "运营事实确认失败。"))
            data = {**result, "writeback_verified": bool(result.get("writeback_verified"))}
            return self._ok("confirm_operational_fact", data=data, message=result.get("rendered_text", ""))

        return self._operation(operation_id, "confirm_operational_fact", execute)

    def query_student_service_relations(self, *, student_name: str = "", program_id: str = "regular_tuoguan") -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_student_service_relations(
            self.store,
            identity=self.identity,
            student_name=student_name,
            program_id=program_id,
        )
        return self._ok("query_student_service_relations", data=result, message=str(result.get("rendered_text") or ""))

    def query_parent_communication_coverage(
        self,
        *,
        days: int = 31,
        program_id: str = "regular_tuoguan",
        student_name: str = "",
        limit: int = 30,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_parent_communication_coverage(
            self.store,
            identity=self.identity,
            days=days,
            program_id=program_id,
        )
        filtered = self._filter_student_coverage(result, student_name=student_name, limit=limit, label="家校沟通证据")
        if not filtered.get("ok"):
            return filtered
        result = filtered
        return self._ok("query_parent_communication_coverage", data=result, message=str(result.get("rendered_text") or ""))

    def query_weekly_record_coverage(
        self,
        *,
        days: int = 7,
        program_id: str = "regular_tuoguan",
        student_name: str = "",
        limit: int = 30,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_weekly_record_coverage(
            self.store,
            identity=self.identity,
            days=days,
            program_id=program_id,
        )
        filtered = self._filter_student_coverage(result, student_name=student_name, limit=limit, label="表现记录")
        if not filtered.get("ok"):
            return filtered
        result = filtered
        return self._ok("query_weekly_record_coverage", data=result, message=str(result.get("rendered_text") or ""))

    def query_active_goal_work_state(self, *, goal_id: str = "") -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_active_goal_work_state(self.store, identity=self.identity, goal_id=goal_id)
        return self._ok("query_active_goal_work_state", data=result, message=str(result.get("rendered_text") or ""))

    def query_profile_candidates(self, *, subject: str = "", include_expired: bool = False, limit: int = 30) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_profile_candidates(
            self.store,
            subject=subject,
            include_expired=include_expired,
            limit=limit,
        )
        return self._ok("query_profile_candidates", data=result, message=str(result.get("rendered_text") or ""))

    def query_value_ledger(self, *, subject: str = "", limit: int = 30) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role != "boss":
            return self._error("permission_denied", "价值账本第一阶段仅允许老板查看。")
        result = query_value_ledger(self.store, subject=subject, limit=limit)
        return self._ok("query_value_ledger", data=result, message=str(result.get("rendered_text") or ""))

    def submit_service_relation_fact_candidate(
        self,
        *,
        student_name: str,
        operation_id: str,
        service_type: str = "",
        responsible_teacher_user_id: str = "",
        unified_owner_user_id: str = "",
        program_id: str = "regular_tuoguan",
        effective_from: str = "",
        source_text: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        def execute() -> dict[str, Any]:
            result = submit_service_relation_fact_candidate(
                self.store,
                identity=self.identity,
                student_name=student_name,
                service_type=service_type,
                responsible_teacher_user_id=responsible_teacher_user_id,
                unified_owner_user_id=unified_owner_user_id,
                program_id=program_id,
                effective_from=effective_from,
                source_text=source_text,
                operation_id=operation_id,
            )
            return result if not result.get("ok") else self._ok("submit_service_relation_fact_candidate", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "submit_service_relation_fact_candidate", execute)

    def submit_information_request_record(
        self,
        *,
        target_person: str,
        reason: str,
        question: str,
        operation_id: str,
        value_level: str = "normal",
        request_type: str = "formal",
        status: str = "asked",
        source_text: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        def execute() -> dict[str, Any]:
            result = submit_information_request_record(
                self.store,
                identity=self.identity,
                target_person=target_person,
                reason=reason,
                question=question,
                value_level=value_level,
                request_type=request_type,
                status=status,
                source_text=source_text,
                operation_id=operation_id,
            )
            return result if not result.get("ok") else self._ok("submit_information_request_record", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "submit_information_request_record", execute)

    def query_information_requests(
        self,
        *,
        target_person: str = "",
        status: str = "",
        include_closed: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_information_requests(
            self.store,
            target_person=target_person,
            status=status,
            include_closed=include_closed,
            limit=limit,
        )
        return self._ok("query_information_requests", data=result, message=str(result.get("rendered_text") or ""))

    def submit_information_request_update(
        self,
        *,
        request_id: str,
        status: str,
        update_text: str,
        operation_id: str,
        response_text: str = "",
        needs_escalation: bool = False,
        source_text: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        def execute() -> dict[str, Any]:
            result = submit_information_request_update(
                self.store,
                identity=self.identity,
                request_id=request_id,
                status=status,
                update_text=update_text,
                response_text=response_text,
                needs_escalation=needs_escalation,
                source_text=source_text,
                operation_id=operation_id,
            )
            return result if not result.get("ok") else self._ok("submit_information_request_update", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "submit_information_request_update", execute)

    def submit_profile_candidate(
        self,
        *,
        subject: str,
        subject_type: str,
        profile_text: str,
        evidence_text: str,
        operation_id: str,
        confidence: float = 0.5,
        expires_at: str = "",
        source_text: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        def execute() -> dict[str, Any]:
            result = submit_profile_candidate(
                self.store,
                identity=self.identity,
                subject=subject,
                subject_type=subject_type,
                profile_text=profile_text,
                evidence_text=evidence_text,
                confidence=confidence,
                expires_at=expires_at,
                source_text=source_text,
                operation_id=operation_id,
            )
            return result if not result.get("ok") else self._ok("submit_profile_candidate", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "submit_profile_candidate", execute)

    def submit_profile_candidate_correction(
        self,
        *,
        candidate_id: str,
        correction_text: str,
        operation_id: str,
        decision: str = "correct",
        source_text: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        def execute() -> dict[str, Any]:
            result = submit_profile_candidate_correction(
                self.store,
                identity=self.identity,
                candidate_id=candidate_id,
                correction_text=correction_text,
                decision=decision,
                source_text=source_text,
                operation_id=operation_id,
            )
            return result if not result.get("ok") else self._ok("submit_profile_candidate_correction", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "submit_profile_candidate_correction", execute)

    def submit_goal_evidence(
        self,
        *,
        evidence_text: str,
        operation_id: str,
        goal_id: str = "",
        subject: str = "",
        source_text: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        def execute() -> dict[str, Any]:
            result = submit_goal_evidence(
                self.store,
                identity=self.identity,
                goal_id=goal_id,
                evidence_text=evidence_text,
                subject=subject,
                source_text=source_text,
                operation_id=operation_id,
            )
            return result if not result.get("ok") else self._ok("submit_goal_evidence", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "submit_goal_evidence", execute)

    def submit_performance_evidence_candidate(
        self,
        *,
        staff_user_id: str,
        evidence_text: str,
        operation_id: str,
        evidence_type: str = "execution",
        source_text: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        def execute() -> dict[str, Any]:
            result = submit_performance_evidence_candidate(
                self.store,
                identity=self.identity,
                staff_user_id=staff_user_id,
                evidence_text=evidence_text,
                evidence_type=evidence_type,
                source_text=source_text,
                operation_id=operation_id,
            )
            return result if not result.get("ok") else self._ok("submit_performance_evidence_candidate", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "submit_performance_evidence_candidate", execute)

    def query_performance_evidence_candidates(
        self,
        *,
        staff_user_id: str = "",
        include_closed: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_performance_evidence_candidates(
            self.store,
            identity=self.identity,
            staff_user_id=staff_user_id,
            include_closed=include_closed,
            limit=limit,
        )
        if not result.get("ok"):
            return self._error(str(result.get("error") or "query_failed"), str(result.get("message") or "绩效证据候选查询失败。"))
        return self._ok("query_performance_evidence_candidates", data=result, message=str(result.get("rendered_text") or ""))

    def submit_performance_evidence_response(
        self,
        *,
        candidate_id: str,
        response_type: str,
        response_text: str,
        operation_id: str,
        source_text: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        def execute() -> dict[str, Any]:
            result = submit_performance_evidence_response(
                self.store,
                identity=self.identity,
                candidate_id=candidate_id,
                response_type=response_type,
                response_text=response_text,
                source_text=source_text,
                operation_id=operation_id,
            )
            return result if not result.get("ok") else self._ok("submit_performance_evidence_response", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "submit_performance_evidence_response", execute)

    def submit_value_ledger_entry(
        self,
        *,
        discovered: str,
        hermes_action: str,
        operation_id: str,
        human_action: str = "",
        outcome: str = "",
        subject: str = "",
        attribution: str = "participated",
        source_text: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        def execute() -> dict[str, Any]:
            result = submit_value_ledger_entry(
                self.store,
                identity=self.identity,
                discovered=discovered,
                hermes_action=hermes_action,
                human_action=human_action,
                outcome=outcome,
                subject=subject,
                attribution=attribution,
                source_text=source_text,
                operation_id=operation_id,
            )
            return result if not result.get("ok") else self._ok("submit_value_ledger_entry", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "submit_value_ledger_entry", execute)


    def query_hermes_work_items(self, *, status: str = "", focus_key: str = "", include_closed: bool = False, limit: int = 50) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_hermes_work_items(self.store, identity=self.identity, status=status, focus_key=focus_key, include_closed=include_closed, limit=limit)
        return self._ok("query_hermes_work_items", data=result, message=str(result.get("rendered_text") or ""))

    def query_institution_work(
        self,
        *,
        focus_key: str = "",
        institution_stage: str = "",
        include_closed: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_institution_work(
            self.store,
            identity=self.identity,
            focus_key=focus_key,
            institution_stage=institution_stage,
            include_closed=include_closed,
            limit=limit,
        )
        return self._ok("query_institution_work", data=result, message=str(result.get("rendered_text") or ""))

    def advance_institution_work(
        self,
        *,
        action: str,
        operation_id: str,
        focus_key: str = "",
        work_item_id: str = "",
        title: str = "",
        summary: str = "",
        evidence: list[Any] | None = None,
        artifact_title: str = "",
        artifact_content: str = "",
        submit_for_review: bool = False,
        artifact_version_id: str = "",
        evidence_ids: list[Any] | None = None,
        pending_items: list[Any] | None = None,
        decision: str = "",
        implementation_scope: dict[str, Any] | None = None,
        execution_link: dict[str, Any] | None = None,
        verification: dict[str, Any] | None = None,
        source_text: str = "",
        source_message_id: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied

        def execute() -> dict[str, Any]:
            result = advance_institution_work(
                self.store,
                identity=self.identity,
                action=action,
                operation_id=operation_id,
                focus_key=focus_key,
                work_item_id=work_item_id,
                title=title,
                summary=summary,
                evidence=evidence,
                artifact_title=artifact_title,
                artifact_content=artifact_content,
                submit_for_review=submit_for_review,
                artifact_version_id=artifact_version_id,
                evidence_ids=evidence_ids,
                pending_items=pending_items,
                decision=decision,
                implementation_scope=implementation_scope,
                execution_link=execution_link,
                verification=verification,
                source_text=source_text,
                source_message_id=source_message_id,
            )
            return result if not result.get("ok") else self._ok(
                "advance_institution_work",
                data=result,
                message=str(result.get("rendered_text") or ""),
            )

        return self._operation(operation_id, "advance_institution_work", execute)

    def submit_hermes_work_item(
        self,
        *,
        focus_key: str,
        title: str,
        focus_summary: str,
        operation_id: str,
        related_objects: list[Any] | None = None,
        related_staff_user_ids: list[str] | None = None,
        execution_plan: list[Any] | None = None,
        current_phase: dict[str, Any] | None = None,
        next_actions: list[Any] | None = None,
        progress_evidence: list[Any] | None = None,
        confirmed_facts: list[Any] | None = None,
        pending_judgements: list[Any] | None = None,
        completed_actions: list[Any] | None = None,
        current_waiting: dict[str, Any] | None = None,
        blocked_by: list[Any] | None = None,
        ask_candidates: list[Any] | None = None,
        last_human_contact_at: str = "",
        next_contact_after: str = "",
        owner_escalation_reason: str = "",
        value_progress_note: str = "",
        next_attention_at: str = "",
        status: str = "active",
        source_text: str = "",
        source_message_id: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        def execute() -> dict[str, Any]:
            result = submit_hermes_work_item(
                self.store,
                identity=self.identity,
                focus_key=focus_key,
                title=title,
                focus_summary=focus_summary,
                related_objects=related_objects,
                related_staff_user_ids=related_staff_user_ids,
                execution_plan=execution_plan,
                current_phase=current_phase,
                next_actions=next_actions,
                progress_evidence=progress_evidence,
                confirmed_facts=confirmed_facts,
                pending_judgements=pending_judgements,
                completed_actions=completed_actions,
                current_waiting=current_waiting,
                blocked_by=blocked_by,
                ask_candidates=ask_candidates,
                last_human_contact_at=last_human_contact_at,
                next_contact_after=next_contact_after,
                owner_escalation_reason=owner_escalation_reason,
                value_progress_note=value_progress_note,
                next_attention_at=next_attention_at,
                status=status,
                source_text=source_text,
                source_message_id=source_message_id,
                operation_id=operation_id,
            )
            return result if not result.get("ok") else self._ok("submit_hermes_work_item", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "submit_hermes_work_item", execute)

    def update_hermes_work_item(
        self,
        *,
        operation_id: str,
        work_item_id: str = "",
        focus_key: str = "",
        status: str = "",
        focus_summary: str = "",
        execution_plan: list[Any] | None = None,
        current_phase: dict[str, Any] | None = None,
        next_actions: list[Any] | None = None,
        progress_evidence: list[Any] | None = None,
        confirmed_facts: list[Any] | None = None,
        pending_judgements: list[Any] | None = None,
        completed_actions: list[Any] | None = None,
        current_waiting: dict[str, Any] | None = None,
        blocked_by: list[Any] | None = None,
        ask_candidates: list[Any] | None = None,
        last_human_contact_at: str = "",
        next_contact_after: str = "",
        owner_escalation_reason: str = "",
        value_progress_note: str = "",
        next_attention_at: str = "",
        stop_reason: str = "",
        update_text: str = "",
        source_text: str = "",
        source_message_id: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        # A server-attested Agenda service has no inherited manager authority.
        # Enforce this independent policy here too, so direct in-process
        # invocation cannot widen the public Tool boundary.
        try:
            from .agenda_service_policy import enforce_agenda_work_item_update
            policy_args = {
                "operation_id": operation_id, "work_item_id": work_item_id,
                "focus_key": focus_key, "status": status,
                "focus_summary": focus_summary, "execution_plan": execution_plan,
                "current_phase": current_phase, "next_actions": next_actions,
                "progress_evidence": progress_evidence, "confirmed_facts": confirmed_facts,
                "pending_judgements": pending_judgements, "completed_actions": completed_actions,
                "current_waiting": current_waiting, "blocked_by": blocked_by,
                "ask_candidates": ask_candidates, "last_human_contact_at": last_human_contact_at,
                "next_contact_after": next_contact_after, "owner_escalation_reason": owner_escalation_reason,
                "value_progress_note": value_progress_note, "next_attention_at": next_attention_at,
                "stop_reason": stop_reason, "update_text": update_text,
                "source_text": source_text, "source_message_id": source_message_id,
            }
            policy_denial = enforce_agenda_work_item_update(
                service=self,
                args={
                    key: value for key, value in policy_args.items()
                    if value is not None and value != "" and value != [] and value != {}
                },
            )
        except Exception:
            policy_denial = {"ok": False, "error": "agenda_service_policy_unavailable", "message": "后台服务策略不可用，本轮未执行。", "data": {}}
        if policy_denial is not None:
            return policy_denial
        def execute() -> dict[str, Any]:
            result = update_hermes_work_item(
                self.store,
                identity=self.identity,
                operation_id=operation_id,
                work_item_id=work_item_id,
                focus_key=focus_key,
                status=status,
                focus_summary=focus_summary,
                execution_plan=execution_plan,
                current_phase=current_phase,
                next_actions=next_actions,
                progress_evidence=progress_evidence,
                confirmed_facts=confirmed_facts,
                pending_judgements=pending_judgements,
                completed_actions=completed_actions,
                current_waiting=current_waiting,
                blocked_by=blocked_by,
                ask_candidates=ask_candidates,
                last_human_contact_at=last_human_contact_at,
                next_contact_after=next_contact_after,
                owner_escalation_reason=owner_escalation_reason,
                value_progress_note=value_progress_note,
                next_attention_at=next_attention_at,
                stop_reason=stop_reason,
                update_text=update_text,
                source_text=source_text,
                source_message_id=source_message_id,
            )
            return result if not result.get("ok") else self._ok("update_hermes_work_item", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "update_hermes_work_item", execute)

    def query_work_commitments(self, *, include_closed: bool = False, limit: int = 20) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_work_commitments(
            self.store,
            identity=self.identity,
            include_closed=include_closed,
            limit=limit,
        )
        return self._ok("query_work_commitments", data=result, message=str(result.get("rendered_text") or ""))

    def submit_work_commitment(
        self,
        *,
        focus_key: str,
        title: str,
        commitment_statement: str,
        deliverable: str,
        next_action: str,
        operation_id: str,
        source_message_id: str = "",
        planned_at: str = "",
        evidence_requirement: str = "",
        related_authority_refs: list[Any] | None = None,
        related_objects: list[Any] | None = None,
        related_staff_user_ids: list[str] | None = None,
        risk_level: str = "low",
        source_text: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied

        def execute() -> dict[str, Any]:
            result = submit_work_commitment(
                self.store,
                identity=self.identity,
                focus_key=focus_key,
                title=title,
                commitment_statement=commitment_statement,
                deliverable=deliverable,
                next_action=next_action,
                operation_id=operation_id,
                source_message_id=source_message_id,
                planned_at=planned_at,
                evidence_requirement=evidence_requirement,
                related_authority_refs=related_authority_refs,
                related_objects=related_objects,
                related_staff_user_ids=related_staff_user_ids,
                risk_level=risk_level,
                source_text=source_text,
            )
            return result if not result.get("ok") else self._ok("submit_work_commitment", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "submit_work_commitment", execute)

    def update_work_commitment(
        self,
        *,
        operation_id: str,
        commitment_stage: str,
        work_item_id: str = "",
        focus_key: str = "",
        progress_evidence: list[Any] | None = None,
        next_action: str = "",
        planned_at: str = "",
        evidence_requirement: str = "",
        stop_reason: str = "",
        source_text: str = "",
        source_message_id: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied

        def execute() -> dict[str, Any]:
            result = update_work_commitment(
                self.store,
                identity=self.identity,
                operation_id=operation_id,
                commitment_stage=commitment_stage,
                work_item_id=work_item_id,
                focus_key=focus_key,
                progress_evidence=progress_evidence,
                next_action=next_action,
                planned_at=planned_at,
                evidence_requirement=evidence_requirement,
                stop_reason=stop_reason,
                source_text=source_text,
                source_message_id=source_message_id,
            )
            return result if not result.get("ok") else self._ok("update_work_commitment", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "update_work_commitment", execute)

    def query_wakeup_requests(self, *, status: str = "", wakeup_source: str = "", limit: int = 50) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_wakeup_requests(self.store, identity=self.identity, status=status, wakeup_source=wakeup_source, limit=limit)
        return self._ok("query_wakeup_requests", data=result, message=str(result.get("rendered_text") or ""))

    def submit_wakeup_request(
        self,
        *,
        wakeup_source: str,
        reason: str,
        operation_id: str,
        related_work_item_id: str = "",
        related_objects: list[Any] | None = None,
        scheduled_for: str = "",
        status: str = "pending",
        source_text: str = "",
        source_message_id: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        def execute() -> dict[str, Any]:
            result = submit_wakeup_request(self.store, identity=self.identity, wakeup_source=wakeup_source, reason=reason, operation_id=operation_id, related_work_item_id=related_work_item_id, related_objects=related_objects, scheduled_for=scheduled_for, status=status, source_text=source_text, source_message_id=source_message_id)
            return result if not result.get("ok") else self._ok("submit_wakeup_request", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "submit_wakeup_request", execute)

    def query_business_events(self, *, event_type: str = "", related_object: str = "", limit: int = 50) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_business_events(self.store, identity=self.identity, event_type=event_type, related_object=related_object, limit=limit)
        return self._ok("query_business_events", data=result, message=str(result.get("rendered_text") or ""))

    def submit_business_event(self, *, event_type: str, event_text: str, operation_id: str, related_objects: list[Any] | None = None, occurred_at: str = "", source_text: str = "", source_message_id: str = "") -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        def execute() -> dict[str, Any]:
            result = submit_business_event(self.store, identity=self.identity, event_type=event_type, event_text=event_text, operation_id=operation_id, related_objects=related_objects, occurred_at=occurred_at, source_text=source_text, source_message_id=source_message_id)
            return result if not result.get("ok") else self._ok("submit_business_event", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "submit_business_event", execute)

    def query_action_executions(self, *, status: str = "", action_type: str = "", limit: int = 50) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_action_executions(self.store, identity=self.identity, status=status, action_type=action_type, limit=limit)
        return self._ok("query_action_executions", data=result, message=str(result.get("rendered_text") or ""))

    def submit_action_execution(self, *, action_type: str, action_summary: str, status: str, operation_id: str, related_work_item_id: str = "", idempotency_key: str = "", receipt: dict[str, Any] | None = None, result_text: str = "", source_text: str = "", source_message_id: str = "") -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        def execute() -> dict[str, Any]:
            result = submit_action_execution(self.store, identity=self.identity, action_type=action_type, action_summary=action_summary, status=status, operation_id=operation_id, related_work_item_id=related_work_item_id, idempotency_key=idempotency_key, receipt=receipt, result_text=result_text, source_text=source_text, source_message_id=source_message_id)
            return result if not result.get("ok") else self._ok("submit_action_execution", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "submit_action_execution", execute)

    def query_autonomous_work_brief(self, *, limit: int = 20) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_autonomous_work_brief(self.store, identity=self.identity, limit=limit)
        return self._ok("query_autonomous_work_brief", data=result, message=str(result.get("rendered_text") or ""))

    def query_proactive_work_radar(self, *, limit: int = 12) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"manager", "boss"}:
            return self._error("permission_denied", "只有店长或老板可以查看小优主动工作雷达。")
        result = query_proactive_work_radar(self.store, identity=self.identity, limit=limit)
        return self._ok("query_proactive_work_radar", data=result, message=str(result.get("rendered_text") or ""))

    def query_active_work_context(self, *, limit: int = 5) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = build_active_work_context(self.store, identity=self.identity, limit=limit)
        return self._ok("query_active_work_context", data=result, message=str(result.get("rendered_text") or ""))

    def query_attention_threads(
        self,
        *,
        status: str = "",
        focus_key: str = "",
        include_closed: bool = False,
        limit: int = 30,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_attention_threads(
            self.store,
            identity=self.identity,
            status=status,
            focus_key=focus_key,
            include_closed=include_closed,
            limit=limit,
        )
        if not result.get("ok"):
            return self._error(str(result.get("error") or "attention_query_failed"), str(result.get("message") or "提醒线程查询失败。"))
        return self._ok("query_attention_threads", data=result, message=str(result.get("rendered_text") or ""))

    def update_attention_thread(
        self,
        *,
        attention_id: str,
        status: str,
        operation_id: str,
        owner_message_id: str = "",
        owner_message_text: str = "",
        reply_relevance: str = "",
        reply_sufficiency: str = "",
        model_judgment: str = "",
        resolution_note: str = "",
        failure_reason: str = "",
        source_text: str = "",
        source_message_id: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied

        def execute() -> dict[str, Any]:
            result = update_attention_thread(
                self.store,
                identity=self.identity,
                attention_id=attention_id,
                status=status,
                operation_id=operation_id,
                owner_message_id=owner_message_id,
                owner_message_text=owner_message_text,
                reply_relevance=reply_relevance,
                reply_sufficiency=reply_sufficiency,
                model_judgment=model_judgment,
                resolution_note=resolution_note,
                failure_reason=failure_reason,
                source_text=source_text,
                source_message_id=source_message_id,
            )
            return result if not result.get("ok") else self._ok("update_attention_thread", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "update_attention_thread", execute)

    def query_relationship_touch_candidates(
        self,
        *,
        target_user_id: str = "",
        target_role: str = "",
        include_closed: bool = False,
        limit: int = 30,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_relationship_touch_candidates(
            self.store,
            identity=self.identity,
            target_user_id=target_user_id,
            target_role=target_role,
            include_closed=include_closed,
            limit=limit,
        )
        if not result.get("ok"):
            return self._error(str(result.get("error") or "relationship_touch_query_failed"), str(result.get("message") or "主动触达候选查询失败。"))
        return self._ok("query_relationship_touch_candidates", data=result, message=str(result.get("rendered_text") or ""))

    def submit_relationship_touch_candidate(
        self,
        *,
        target_role: str,
        touch_type: str,
        message: str,
        reason: str,
        operation_id: str,
        target_user_id: str = "",
        target_name: str = "",
        value: str = "",
        work_related: bool = False,
        private_emotional_support: bool = False,
        suggested_send_at: str = "",
        source_text: str = "",
        source_message_id: str = "",
        action_type: str = "ask_work_fact",
        goal_id: str = "",
        goal_action_id: str = "",
        related_task_id: str = "",
        evidence_requirement: str = "",
        execute_if_authorized: bool = False,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"boss", "manager"}:
            return self._error("permission_denied", "只有老板或店长会话可以提交主动触达候选。")
        role = str(target_role or "").strip()
        if role == "parent":
            return self._error("relationship_touch_parent_disabled", "当前阶段小优不能主动联系家长。")

        def execute() -> dict[str, Any]:
            target = str(target_user_id or "").strip()
            resolved_target_name = str(target_name or "").strip()
            if not target:
                if not resolved_target_name:
                    return self._error(
                        "relationship_touch_target_required",
                        "请先用当前人员目录确定唯一联系人；本轮没有创建候选或投递。",
                    )
                directory = build_staff_directory_report(
                    self.store,
                    query=resolved_target_name,
                    role=role,
                    include_inactive=False,
                    limit=5,
                )
                matches = [
                    row for row in (directory.get("staff") or [])
                    if isinstance(row, dict)
                    and str(row.get("role") or "") == role
                    and resolved_target_name in {
                        str(row.get("business_name") or ""),
                        str(row.get("staff_name") or ""),
                        str(row.get("directory_name") or ""),
                        *{str(alias) for alias in (row.get("known_aliases") or [])},
                    }
                ]
                if len(matches) != 1:
                    return self._error(
                        "relationship_touch_target_ambiguous" if matches else "relationship_touch_target_not_found",
                        "当前人员目录无法唯一确认这位联系人，请先查询人员目录后再发送；本轮没有创建候选或投递。",
                    )
                target = str(matches[0].get("user_id") or "").strip()
                resolved_target_name = str(matches[0].get("business_name") or resolved_target_name)
            active_target = build_staff_directory_report(
                self.store,
                query=target,
                role=role,
                include_inactive=False,
                limit=5,
            )
            if not any(
                isinstance(row, dict)
                and str(row.get("user_id") or "") == target
                and bool(row.get("is_active_staff"))
                for row in (active_target.get("staff") or [])
            ):
                return self._error(
                    "relationship_touch_target_identity_unconfirmed",
                    "当前联系人尚未形成有效的正式业务身份绑定；本轮没有创建候选或投递。",
                )
            task_ref = str(related_task_id or "").strip()
            if not task_ref and str(action_type or "") in {"ask_task_fact", "ask_task_result", "task_companion_followup"}:
                focus_task_id = str(self._read_focus().get("task_id") or "")
                open_target_tasks = [
                    task
                    for task in self._visible_tasks()
                    if str(task.get("assignee_userid") or "") == target
                    and str(task.get("status") or "") not in _CLOSED_STATUSES
                ]
                if focus_task_id and any(str(task.get("id") or "") == focus_task_id for task in open_target_tasks):
                    task_ref = focus_task_id
                else:
                    context_text = f"{message}{reason}{source_text}"
                    named = [
                        task
                        for task in open_target_tasks
                        if str(task.get("student_name") or "")
                        and str(task.get("student_name") or "") in context_text
                    ]
                    if len(named) == 1:
                        task_ref = str(named[0].get("id") or "")
                    elif len(open_target_tasks) == 1:
                        task_ref = str(open_target_tasks[0].get("id") or "")
                if not task_ref:
                    return self._error(
                        "active_task_reference_required",
                        "这是任务内追问，但当前无法唯一确定任务。请先查询任务并传 related_task_id，不能把它当普通主动消息发送。",
                    )
            # Recipient eligibility is one current Runtime fact: the active
            # institution grant plus a trusted, effective WeCom binding.  The
            # retired relationship-touch test allowlist is intentionally not
            # consulted here and cannot contradict this authoritative result.
            try:
                from .proactive_delivery_authority import ProactiveDeliveryAuthority
                from .work_runtime import ReplyDestination

                authority = ProactiveDeliveryAuthority(self.store.data_dir)
                decision = authority.decide(ReplyDestination(
                    tenant_id=str(current_tenant_id() or ""),
                    channel="wecom_callback",
                    recipient_id=target,
                    source_identity="relationship_touch:" + str(current_tenant_id() or ""),
                ))
            except Exception:
                decision = None
            external_send_allowed = bool(decision is not None and decision.allowed)
            requires_authorization = not external_send_allowed
            status = "candidate"
            result = submit_relationship_touch_candidate(
                self.store,
                identity=self.identity,
                target_role=role,
                touch_type=touch_type,
                message=message,
                reason=reason,
                operation_id=operation_id,
                target_user_id=target,
                target_name=resolved_target_name,
                value=value,
                work_related=work_related,
                private_emotional_support=private_emotional_support,
                requires_authorization=requires_authorization,
                external_send_allowed=external_send_allowed,
                suggested_send_at=suggested_send_at,
                status=status,
                source_text=source_text,
                source_message_id=source_message_id,
                action_type=action_type,
                goal_id=goal_id,
                goal_action_id=goal_action_id,
                related_task_id=task_ref,
                evidence_requirement=evidence_requirement,
            )
            if not result.get("ok"):
                return result
            data = {
                **result,
                "current_institutional_delivery_authority": {
                    "allowed": external_send_allowed,
                    "reason": str(getattr(decision, "reason", "institutional_proactive_authority_unavailable")),
                    "recipient_role": str(getattr(decision, "recipient_role", "")),
                },
            }
            if external_send_allowed and execute_if_authorized:
                candidate_id = str((result.get("candidate") or {}).get("candidate_id") or "")
                execution = execute_relationship_touch_state(
                    self.store,
                    identity=self.identity,
                    candidate_id=candidate_id,
                    operation_id=f"{operation_id}:execute",
                )
                data["execution"] = execution
                if not execution.get("ok"):
                    return self._error(
                        str(execution.get("error") or "relationship_touch_execute_failed"),
                        str(execution.get("message") or "主动候选已保存，但没有进入发送队列。"),
                    )
            message_text = (
                "主动联系已进入当前投递链路；尚未取得企业微信接受回执，不能声称对方已经收到。"
                if external_send_allowed and execute_if_authorized
                else "已保存可主动触达候选；该对象当前满足机构级主动外发资格，是否实际发送仍取决于后续执行回执。"
                if external_send_allowed
                else "已保存内部候选；当前未创建外发投递。实际原因已在可信执行结果中记录。"
            )
            return self._ok("submit_relationship_touch_candidate", data=data, message=message_text)

        return self._operation(operation_id, "submit_relationship_touch_candidate", execute)

    def query_proactive_authorizations(self, *, include_inactive: bool = False, now_at: str = "") -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = build_proactive_authorizations(
            self.store,
            identity=self.identity,
            include_inactive=include_inactive,
            now_at=now_at,
        )
        if not result.get("ok"):
            return self._error(str(result.get("error") or "authorization_query_failed"), str(result.get("message") or "主动授权查询失败。"))
        # The historical candidate ledger remains read-only audit material.
        # Current Work Runtime delivery authority is a separate, durable fact;
        # include it so a normal WeCom conversation cannot wrongly report
        # "outbound not enabled" after the owner has granted it.
        try:
            from .proactive_delivery_authority import ProactiveDeliveryAuthority

            current_delivery = ProactiveDeliveryAuthority(self.store.data_dir).status(
                tenant_id=current_tenant_id()
            )
        except Exception:
            current_delivery = {"active": False, "unavailable": True}
        merged = {**result, "current_work_runtime_delivery_authority": current_delivery}
        if current_delivery.get("active"):
            message = "当前机构级主动企业微信授权已生效；小优只能向当前可信、有效且在其角色与数据范围内的工作人员主动发送工作消息。"
        else:
            message = str(result.get("rendered_text") or "")
        return self._ok("query_proactive_authorizations", data=merged, message=message)

    def submit_proactive_authorization(
        self,
        *,
        subject_role: str,
        action_types: list[str],
        operation_id: str,
        subject_user_ids: list[str] | None = None,
        status: str = "active",
        authorization_id: str = "",
        goal_ids: list[str] | None = None,
        daily_limit: int = 1,
        effective_at: str = "",
        expires_at: str = "",
        rollout_stage: str = "pilot",
        source_text: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied

        def execute() -> dict[str, Any]:
            result = save_proactive_authorization(
                self.store,
                identity=self.identity,
                operation_id=operation_id,
                subject_role=subject_role,
                subject_user_ids=subject_user_ids,
                action_types=action_types,
                status=status,
                authorization_id=authorization_id,
                goal_ids=goal_ids,
                daily_limit=daily_limit,
                effective_at=effective_at,
                expires_at=expires_at,
                rollout_stage=rollout_stage,
                source_text=source_text,
            )
            return result if not result.get("ok") else self._ok("submit_proactive_authorization", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "submit_proactive_authorization", execute)

    def execute_relationship_touch(self, *, candidate_id: str, operation_id: str) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied

        def execute() -> dict[str, Any]:
            result = execute_relationship_touch_state(
                self.store,
                identity=self.identity,
                candidate_id=candidate_id,
                operation_id=operation_id,
            )
            return result if not result.get("ok") else self._ok("execute_relationship_touch", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "execute_relationship_touch", execute)

    def update_relationship_touch(
        self,
        *,
        candidate_id: str,
        status: str,
        operation_id: str,
        reply_text: str = "",
        evidence_complete: bool | None = None,
        failure_reason: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied

        def execute() -> dict[str, Any]:
            result = update_relationship_touch_state(
                self.store,
                identity=self.identity,
                candidate_id=candidate_id,
                status=status,
                operation_id=operation_id,
                reply_text=reply_text,
                evidence_complete=evidence_complete,
                failure_reason=failure_reason,
            )
            return result if not result.get("ok") else self._ok("update_relationship_touch", data=result, message=str(result.get("rendered_text") or "主动联系状态已更新并完成反查。"))

        return self._operation(operation_id, "update_relationship_touch", execute)

    def query_goal_actions(
        self,
        *,
        goal_id: str = "",
        include_closed: bool = False,
        due_only: bool = False,
        now_at: str = "",
        limit: int = 30,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = build_goal_actions(
            self.store,
            identity=self.identity,
            goal_id=goal_id,
            include_closed=include_closed,
            due_only=due_only,
            now_at=now_at,
            limit=limit,
        )
        return self._ok("query_goal_actions", data=result, message=str(result.get("rendered_text") or ""))

    def submit_goal_action(
        self,
        *,
        goal_id: str,
        action_type: str,
        summary: str,
        operation_id: str,
        target_role: str = "",
        target_user_id: str = "",
        target_name: str = "",
        student_names: list[str] | None = None,
        planned_at: str = "",
        due_at: str = "",
        evidence_requirement: str = "",
        escalation_path: list[str] | None = None,
        status: str = "planned",
        goal_action_id: str = "",
        source_text: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied

        def execute() -> dict[str, Any]:
            result = save_goal_action(
                self.store,
                identity=self.identity,
                goal_id=goal_id,
                action_type=action_type,
                summary=summary,
                operation_id=operation_id,
                target_role=target_role,
                target_user_id=target_user_id,
                target_name=target_name,
                student_names=student_names,
                planned_at=planned_at,
                due_at=due_at,
                evidence_requirement=evidence_requirement,
                escalation_path=escalation_path,
                status=status,
                goal_action_id=goal_action_id,
                source_text=source_text,
            )
            return result if not result.get("ok") else self._ok("submit_goal_action", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "submit_goal_action", execute)

    def query_employee_work_map(self, *, limit: int = 12) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"manager", "boss"}:
            return self._error("permission_denied", "只有店长或老板可以查看小优机构工作地图。")
        result = query_employee_work_map(self.store, identity=self.identity, limit=limit)
        return self._ok("query_employee_work_map", data=result, message=str(result.get("rendered_text") or ""))

    def query_fact_gap_candidates(self, *, ask_role: str = "", status: str = "", limit: int = 50) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"manager", "boss"}:
            return self._error("permission_denied", "只有店长或老板可以查看事实缺口候选。")
        result = query_fact_gap_candidates(self.store, identity=self.identity, ask_role=ask_role, status=status, limit=limit)
        return self._ok("query_fact_gap_candidates", data=result, message=str(result.get("rendered_text") or ""))

    def submit_fact_gap_candidate(
        self,
        *,
        gap_key: str,
        gap_text: str,
        fact_owner_role: str,
        operation_id: str,
        suggested_question: str = "",
        target_user_id: str = "",
        target_name: str = "",
        impact: str = "",
        urgency: str = "normal",
        target_time: str = "",
        related_objects: list[Any] | None = None,
        source_text: str = "",
        source_message_id: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied

        def execute() -> dict[str, Any]:
            result = submit_fact_gap_candidate(
                self.store,
                identity=self.identity,
                gap_key=gap_key,
                gap_text=gap_text,
                fact_owner_role=fact_owner_role,
                operation_id=operation_id,
                suggested_question=suggested_question,
                target_user_id=target_user_id,
                target_name=target_name,
                impact=impact,
                urgency=urgency,
                target_time=target_time,
                related_objects=related_objects,
                source_text=source_text,
                source_message_id=source_message_id,
            )
            return result if not result.get("ok") else self._ok("submit_fact_gap_candidate", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "submit_fact_gap_candidate", execute)

    def submit_staff_voice_signal(
        self,
        *,
        operation_id: str,
        source_role: str = "",
        category: str = "",
        risk_level: str = "",
        signal_summary: str = "",
        impact: str = "",
        suggested_owner_action: str = "",
        source_user_id: str = "",
        source_name: str = "",
        evidence_excerpt: str = "",
        source_text: str = "",
        source_message_id: str = "",
        occurred_at: str = "",
        status: str = "open",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied

        def execute() -> dict[str, Any]:
            result = submit_staff_voice_signal(
                self.store,
                identity=self.identity,
                operation_id=operation_id,
                source_role=source_role,
                category=category,
                risk_level=risk_level,
                signal_summary=signal_summary,
                impact=impact,
                suggested_owner_action=suggested_owner_action,
                source_user_id=source_user_id,
                source_name=source_name,
                evidence_excerpt=evidence_excerpt,
                source_text=source_text,
                source_message_id=source_message_id,
                occurred_at=occurred_at,
                status=status,
            )
            return result if not result.get("ok") else self._ok("submit_staff_voice_signal", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "submit_staff_voice_signal", execute)

    def query_staff_voice_radar(self, *, risk_level: str = "", status: str = "", now_at: str = "", since_hours: int = 168, limit: int = 50) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role != "boss":
            return self._error("permission_denied", "只有老板可以查看员工声音雷达。")
        result = query_staff_voice_radar(
            self.store,
            identity=self.identity,
            risk_level=risk_level,
            status=status,
            now_at=now_at,
            since_hours=since_hours,
            limit=limit,
        )
        if not result.get("ok"):
            return self._error(str(result.get("error") or "staff_voice_unavailable"), str(result.get("message") or "员工声音雷达查询失败。"))
        return self._ok("query_staff_voice_radar", data=result, message=str(result.get("rendered_text") or ""))

    def query_staff_conversation_activity(
        self,
        *,
        period: str = "today",
        since_hours: int = 24,
        include_latest_excerpt: bool = True,
        teacher_name: str = "",
        now_at: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role != "boss":
            return self._error("permission_denied", "只有老板可以查看员工对话活动。")
        result = build_staff_conversation_activity(
            self.store,
            identity=self.identity,
            period=period,
            since_hours=since_hours,
            include_latest_excerpt=include_latest_excerpt,
            staff_name=teacher_name,
            now_at=now_at,
            limit=limit,
        )
        if not result.get("ok"):
            return self._error(str(result.get("error") or "staff_conversation_activity_unavailable"), str(result.get("message") or "员工对话活动查询失败。"))
        return self._ok("query_staff_conversation_activity", data=result, message=str(result.get("rendered_text") or ""))

    def query_xiaoyou_health(self, *, now_at: str = "", limit: int = 20) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"manager", "boss"}:
            return self._error("permission_denied", "只有店长或老板可以查看小优健康度。")
        result = query_xiaoyou_health(self.store, identity=self.identity, now_at=now_at, limit=limit)
        return self._ok("query_xiaoyou_health", data=result, message=str(result.get("rendered_text") or ""))

    def query_supervision_status(self, *, limit: int = 20) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"boss", "manager", "system"}:
            return self._error("permission_denied", "当前身份无权查看小优内部监督状态。")
        result = query_supervision_status(self.store, limit=limit)
        return self._ok("query_supervision_status", data=result, message=str(result.get("rendered_text") or ""))

    def query_self_evolution_ledger(self, *, candidate_type: str = "", status: str = "", limit: int = 30) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"manager", "boss"}:
            return self._error("permission_denied", "只有店长或老板可以查看小优自我进化账本。")
        result = build_self_evolution_ledger(
            self.store,
            identity=self.identity,
            candidate_type=candidate_type,
            status=status,
            limit=limit,
        )
        if not result.get("ok"):
            return self._error(str(result.get("error") or "self_evolution_unavailable"), str(result.get("message") or "小优自我进化账本查询失败。"))
        return self._ok("query_self_evolution_ledger", data=result, message=str(result.get("rendered_text") or ""))

    def query_industry_learning_candidates(self, *, status: str = "", limit: int = 30) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_industry_learning_candidates(self.store, identity=self.identity, status=status, limit=limit)
        return self._ok("query_industry_learning_candidates", data=result, message=str(result.get("rendered_text") or ""))

    def query_external_research_runs(self, *, mode: str = "", limit: int = 20) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_external_research_runs(self.store, identity=self.identity, mode=mode, limit=limit)
        return self._ok("query_external_research_runs", data=result, message=str(result.get("rendered_text") or ""))

    def query_market_research_candidates(self, *, status: str = "", limit: int = 30) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_market_research_candidates(self.store, identity=self.identity, status=status, limit=limit)
        return self._ok("query_market_research_candidates", data=result, message=str(result.get("rendered_text") or ""))

    def query_competitor_profiles(self, *, limit: int = 30) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_competitor_profiles(self.store, identity=self.identity, limit=limit)
        return self._ok("query_competitor_profiles", data=result, message=str(result.get("rendered_text") or ""))

    def query_external_learning_brief(self, *, limit: int = 5) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_external_learning_brief(self.store, identity=self.identity, limit=limit)
        return self._ok("query_external_learning_brief", data=result, message=str(result.get("rendered_text") or ""))

    def query_social_market_research(self, *, platform: str = "", status: str = "", limit: int = 30) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"manager", "boss"}:
            return self._error("permission_denied", "只有店长或老板可以查看社交平台市场观察。")
        result = build_social_market_research(self.store, identity=self.identity, platform=platform, status=status, limit=limit)
        if not result.get("ok"):
            return self._error(str(result.get("error") or "social_market_research_unavailable"), str(result.get("message") or "社交平台市场观察查询失败。"))
        return self._ok("query_social_market_research", data=result, message=str(result.get("rendered_text") or ""))

    def query_project_opportunities(
        self,
        *,
        project_id: str = "",
        status: str = "",
        include_internal: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role != "boss":
            return self._error("permission_denied", "只有老板可以查询新项目机会候选。")
        result = build_project_opportunities(
            self.store,
            project_id=str(project_id or "").strip(),
            status=str(status or "").strip(),
            include_internal=bool(include_internal),
            limit=max(1, min(int(limit or 20), 100)),
        )
        return self._ok(
            "query_project_opportunities",
            data=result,
            message=str(result.get("rendered_text") or ""),
        )

    def review_project_opportunity(
        self,
        *,
        opportunity_id: str,
        decision: str,
        operation_id: str,
        note: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role != "boss":
            return self._error("permission_denied", "只有老板可以审核新项目机会候选。")

        def execute() -> dict[str, Any]:
            result = review_project_opportunity_state(
                self.store,
                opportunity_id=str(opportunity_id or "").strip(),
                decision=str(decision or "").strip(),
                actor_user_id=self.identity.canonical_user_id,
                operation_id=str(operation_id or "").strip(),
                note=str(note or "").strip(),
            )
            if not result.get("ok"):
                return result
            candidate = result.get("candidate") if isinstance(result.get("candidate"), dict) else {}
            messages = {
                "validation_approved": "已批准验证这个机会；这不等于正式立项，后续人员联系仍受主动授权约束。",
                "deferred": "已暂缓这个机会，不会据此创建验证任务。",
                "dismissed": "已驳回这个机会，30天内不会重复进入决策。",
                "decision_pending": "已重新打开这个机会，仍需基于当前证据决定是否验证。",
                "evidence_ready": "已重新打开这个机会，等待小优补齐验证方案。",
            }
            return self._ok(
                "review_project_opportunity",
                data={"candidate": candidate, "writeback_verified": bool(result.get("writeback_verified"))},
                message=messages.get(str(candidate.get("status") or ""), "项目机会状态已更新。"),
            )

        return self._operation(operation_id, "review_project_opportunity", execute)

    def submit_industry_learning_candidate(
        self,
        *,
        topic: str,
        summary: str,
        operation_id: str,
        sources: list[Any] | None = None,
        applicability: str = "",
        status: str = "pending_review",
        source_text: str = "",
        source_message_id: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"manager", "boss"}:
            return self._error("permission_denied", "只有老板或店长可以提交行业学习候选。")

        def execute() -> dict[str, Any]:
            result = submit_industry_learning_candidate(
                self.store,
                identity=self.identity,
                topic=topic,
                summary=summary,
                sources=sources or [],
                applicability=applicability,
                status=status,
                source_text=source_text,
                source_message_id=source_message_id,
                operation_id=operation_id,
            )
            if not result.get("ok"):
                return self._error(str(result.get("error") or "industry_learning_failed"), str(result.get("message") or "行业学习候选保存失败。"))
            data = {**result, "writeback_verified": bool(result.get("writeback_verified"))}
            return self._ok("submit_industry_learning_candidate", data=data, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "submit_industry_learning_candidate", execute)


    def generate_autonomous_recovery_report(self, *, focus_key: str = "", limit: int = 30) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = generate_autonomous_recovery_report(self.store, identity=self.identity, focus_key=focus_key, limit=limit)
        return self._ok("generate_autonomous_recovery_report", data=result, message=str(result.get("rendered_text") or ""))


    def generate_due_wakeup_candidates(self, *, now_at: str = "", limit: int = 50) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = generate_due_wakeup_candidates(self.store, identity=self.identity, now_at=now_at, limit=limit)
        return self._ok("generate_due_wakeup_candidates", data=result, message=str(result.get("rendered_text") or ""))


    def generate_autonomous_log_review(self, *, limit: int = 80, include_gray_observations: bool = True) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = generate_autonomous_log_review(
            self.store,
            identity=self.identity,
            limit=limit,
            include_gray_observations=include_gray_observations,
        )
        if not result.get("ok"):
            return self._error(str(result.get("error") or "generate_autonomous_log_review_failed"), str(result.get("message") or "自主工作真实日志复盘失败。"))
        return self._ok("generate_autonomous_log_review", data=result, message=str(result.get("rendered_text") or ""))


    def submit_due_wakeup_candidate(
        self,
        *,
        candidate_id: str,
        operation_id: str,
        now_at: str = "",
        source_text: str = "",
        source_message_id: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        def execute() -> dict[str, Any]:
            result = submit_due_wakeup_candidate(
                self.store,
                identity=self.identity,
                candidate_id=candidate_id,
                now_at=now_at,
                source_text=source_text,
                source_message_id=source_message_id,
                operation_id=operation_id,
            )
            return result if not result.get("ok") else self._ok("submit_due_wakeup_candidate", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "submit_due_wakeup_candidate", execute)


    def update_wakeup_request(
        self,
        *,
        wakeup_request_id: str,
        status: str,
        update_text: str,
        operation_id: str,
        source_text: str = "",
        source_message_id: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        def execute() -> dict[str, Any]:
            result = update_wakeup_request(
                self.store,
                identity=self.identity,
                wakeup_request_id=wakeup_request_id,
                status=status,
                update_text=update_text,
                source_text=source_text,
                source_message_id=source_message_id,
                operation_id=operation_id,
            )
            return result if not result.get("ok") else self._ok("update_wakeup_request", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "update_wakeup_request", execute)


    def generate_autonomous_acceptance_pack(self, *, include_teacher: bool = True) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = generate_autonomous_acceptance_pack(include_teacher=include_teacher)
        return self._ok("generate_autonomous_acceptance_pack", data=result, message=str(result.get("rendered_text") or ""))

    def query_gray_observations(
        self,
        *,
        scenario_id: str = "",
        outcome: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_gray_observations(
            self.store,
            identity=self.identity,
            scenario_id=scenario_id,
            outcome=outcome,
            limit=limit,
        )
        if not result.get("ok"):
            return self._error(str(result.get("error") or "query_gray_observations_failed"), str(result.get("message") or "灰度观察查询失败。"))
        return self._ok("query_gray_observations", data=result, message=str(result.get("rendered_text") or ""))

    def submit_gray_observation(
        self,
        *,
        scenario_id: str,
        observation_text: str,
        operation_id: str,
        outcome: str = "note",
        conversation_ref: str = "",
        actor_user_id: str = "",
        source_text: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        def execute() -> dict[str, Any]:
            result = submit_gray_observation(
                self.store,
                identity=self.identity,
                scenario_id=scenario_id,
                observation_text=observation_text,
                outcome=outcome,
                conversation_ref=conversation_ref,
                actor_user_id=actor_user_id,
                source_text=source_text,
                operation_id=operation_id,
            )
            return result if not result.get("ok") else self._ok("submit_gray_observation", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "submit_gray_observation", execute)

    def query_gray_rollout_decisions(
        self,
        *,
        decision_type: str = "",
        limit: int = 30,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_gray_rollout_decisions(
            self.store,
            identity=self.identity,
            decision_type=decision_type,
            limit=limit,
        )
        if not result.get("ok"):
            return self._error(str(result.get("error") or "query_gray_rollout_decisions_failed"), str(result.get("message") or "灰度放量决策查询失败。"))
        return self._ok("query_gray_rollout_decisions", data=result, message=str(result.get("rendered_text") or ""))

    def generate_gray_review(
        self,
        *,
        write_report: bool = False,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"boss", "manager"}:
            return self._error("permission_denied", "灰度复盘包含验收、观察和巡店材料，第一阶段仅允许老板或店长查看。")
        from .gray_review_v1 import run_gray_review_v1

        result = run_gray_review_v1(self.store, write_report=bool(write_report))
        result["tool_boundary"] = {
            "read_only_business_state": True,
            "limits_model": False,
            "auto_apply": False,
            "auto_expand_rollout": False,
            "auto_update_handbook": False,
            "auto_create_learning_candidate": False,
            "auto_create_tasks": False,
            "auto_send_notifications": False,
            "auto_change_salary": False,
        }
        return self._ok("generate_gray_review", data=result, message=str(result.get("rendered_text") or ""))

    def query_gray_scenario_cards(
        self,
        *,
        role: str = "",
        scenario_id: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_gray_scenario_cards(role=role, scenario_id=scenario_id)
        return self._ok("query_gray_scenario_cards", data=result, message=str(result.get("rendered_text") or ""))

    def generate_gray_trial_start_pack(
        self,
        *,
        role: str = "",
        include_examples: bool = True,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"boss", "manager"}:
            return self._error("permission_denied", "灰度试用启动包用于组织真实灰度试用，第一阶段仅允许老板或店长查看。")
        result = generate_gray_trial_start_pack(role=role, include_examples=include_examples)
        return self._ok("generate_gray_trial_start_pack", data=result, message=str(result.get("rendered_text") or ""))

    def generate_gray_observation_candidates(
        self,
        *,
        outcome: str = "issue",
        limit: int = 50,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = generate_gray_observation_candidates(
            self.store,
            identity=self.identity,
            outcome=outcome,
            limit=limit,
        )
        if not result.get("ok"):
            return self._error(str(result.get("error") or "generate_gray_observation_candidates_failed"), str(result.get("message") or "灰度观察优化候选生成失败。"))
        return self._ok("generate_gray_observation_candidates", data=result, message=str(result.get("rendered_text") or ""))

    def submit_gray_rollout_decision(
        self,
        *,
        decision_type: str,
        decision_text: str,
        operation_id: str,
        scope: str = "",
        reason: str = "",
        source_report_path: str = "",
        source_text: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        def execute() -> dict[str, Any]:
            result = submit_gray_rollout_decision(
                self.store,
                identity=self.identity,
                decision_type=decision_type,
                decision_text=decision_text,
                scope=scope,
                reason=reason,
                source_report_path=source_report_path,
                source_text=source_text,
                operation_id=operation_id,
            )
            return result if not result.get("ok") else self._ok("submit_gray_rollout_decision", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "submit_gray_rollout_decision", execute)

    def query_gray_optimization_decisions(
        self,
        *,
        candidate_id: str = "",
        decision_type: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_gray_optimization_decisions(
            self.store,
            identity=self.identity,
            candidate_id=candidate_id,
            decision_type=decision_type,
            limit=limit,
        )
        if not result.get("ok"):
            return self._error(str(result.get("error") or "query_gray_optimization_decisions_failed"), str(result.get("message") or "灰度优化确认记录查询失败。"))
        return self._ok("query_gray_optimization_decisions", data=result, message=str(result.get("rendered_text") or ""))

    def submit_gray_optimization_decision(
        self,
        *,
        candidate_id: str,
        decision_type: str,
        decision_text: str,
        operation_id: str,
        source_observation_id: str = "",
        candidate_type: str = "",
        reason: str = "",
        source_text: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        def execute() -> dict[str, Any]:
            result = submit_gray_optimization_decision(
                self.store,
                identity=self.identity,
                candidate_id=candidate_id,
                decision_type=decision_type,
                decision_text=decision_text,
                source_observation_id=source_observation_id,
                candidate_type=candidate_type,
                reason=reason,
                source_text=source_text,
                operation_id=operation_id,
            )
            return result if not result.get("ok") else self._ok("submit_gray_optimization_decision", data=result, message=str(result.get("rendered_text") or ""))

        return self._operation(operation_id, "submit_gray_optimization_decision", execute)

    def submit_learning_candidate(
        self,
        *,
        title: str,
        content: str,
        category: str = "general",
        candidate_type: str = "knowledge",
        proposed_triggers: list[str] | None = None,
        source: str = "hermes_tool",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        item = submit_learning_candidate(
            self.store,
            source=source,
            category=category,
            title=str(title or "").strip() or "托管新说法候选",
            content=str(content or "").strip(),
            candidate_type=candidate_type,
            proposed_triggers=proposed_triggers or [],
            created_by=self.identity.canonical_user_id,
            evidence=[
                {
                    "platform": self.platform,
                    "user_id": self.user_id,
                    "role": self.identity.role,
                }
            ],
        )
        return self._ok(
            "submit_learning_candidate",
            data={"candidate": item},
            message="已提交待审核学习候选，老板批准前不会改变正式规则。",
        )

    def list_learning_candidates(self) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"manager", "boss"}:
            return self._error("permission_denied", "只有店长或老板可以查看学习候选。")
        items = list_pending_learning_candidates(self.store)
        return self._ok(
            "list_learning_candidates",
            data={"count": len(items), "candidates": deepcopy(items[:20])},
            message=f"当前有 {len(items)} 条待审核学习候选。",
        )

    def review_learning_candidate(
        self,
        *,
        candidate_id: str,
        decision: str,
        revised_title: str = "",
        revised_content: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role != "boss":
            return self._error("permission_denied", "只有老板可以批准或驳回学习候选。")
        try:
            item = review_learning_candidate(
                self.store,
                candidate_id=str(candidate_id or "").strip(),
                decision=decision,
                reviewer=self.identity.canonical_user_id,
                revised_title=str(revised_title or "").strip(),
                revised_content=str(revised_content or "").strip(),
            )
        except ValueError as exc:
            return self._error("invalid_learning_candidate", str(exc))
        return self._ok(
            "review_learning_candidate",
            data={"candidate": item},
            message=(
                "已批准学习候选，内容进入正式知识库。"
                if item.get("status") == "approved"
                else "已驳回学习候选，正式规则不受影响。"
            ),
        )
