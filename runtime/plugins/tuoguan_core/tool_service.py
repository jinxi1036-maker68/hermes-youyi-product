"""Permission-scoped business operations exposed to the Hermes model."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import os
import re
import uuid
from typing import Any, Callable
from urllib.parse import quote

from .dashboard_auth import DashboardAuthError, sign_dashboard_token, token_expiry_datetime
from .dashboard_builder import refresh_dashboard_cache
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
from .programs import resolve_record_program
from .programs import is_summer_operator
from .records import analyze_teacher_record, save_analysis
from .summer_records import save_summer_lesson_record
from .store import JSON_NO_CHANGE, TuoguanStore, TuoguanStoreError
from .summer_points import change_points, query_points_ranking, query_student_points
from .tasks import apply_task_reply, cancel_task as cancel_task_state, closure_missing_fields, current_task_for_user
from .temporal_grounding import parse_business_due_at
from .youyi_batch_capabilities import (
    create_assigned_task,
    create_trial_lead,
    operations_report,
    register_official_student,
    register_summer_student,
    verify_dashboard_visibility,
)
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
from .active_work_context import query_active_work_context as build_active_work_context
from .staff_conversation_activity import query_staff_conversation_activity as build_staff_conversation_activity
from .self_evolution import query_self_evolution_ledger as build_self_evolution_ledger
from .social_market_research import query_social_market_research as build_social_market_research
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
    query_business_events,
    query_hermes_work_items,
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
)


_CLOSED_STATUSES = {
    "completed", "cancelled", "closed", "done", "closed_by_admin", "completed_by_admin",
    "superseded", "expired",
}

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
    ) -> None:
        self.store = store or TuoguanStore()
        self.platform = str(platform or "")
        self.user_id = str(user_id or "")
        self.user_name = str(user_name or "")
        self.chat_id = str(chat_id or "")
        self.session_key = str(session_key or "")
        self.identity = IdentityService(self.store).resolve(
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
            "当前渠道账号尚未通过托管系统身份审核。",
        )

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
            and self.permissions.can_view_student(identity, str(name))
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
                    return UserIdentity(self.platform, str(user_id), str(user_id), str(profile.get("name") or requested), "teacher", "approved")
        mapping = self.store.read_json("teacher_wecom_map.json", {})
        user_id = str(mapping.get(requested) or "") if isinstance(mapping, dict) else ""
        if user_id:
            return UserIdentity(self.platform, user_id, user_id, requested, "teacher", "approved")
        return None

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
        staff = self.store.read_json("staff.json", {})
        if isinstance(staff, dict):
            for user_id, profile in staff.items():
                if not isinstance(profile, dict) or str(profile.get("role") or "") != "teacher":
                    continue
                candidates.extend([str(profile.get("name") or ""), str(user_id)])
        mapping = self.store.read_json("teacher_wecom_map.json", {})
        if isinstance(mapping, dict):
            candidates.extend(str(name or "") for name in mapping)
        candidates = sorted({name.strip() for name in candidates if name and name.strip()}, key=len, reverse=True)
        for name in candidates:
            if name in raw_text:
                return name
        return ""

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
            "submit_fact_gap_candidate",
            "submit_staff_voice_signal",
            "submit_relationship_touch_candidate",
            "submit_proactive_authorization",
            "execute_relationship_touch",
            "update_relationship_touch",
            "submit_goal_action",
            "update_attention_thread",
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
                from gateway.session_context import get_session_env
                session_user_probe = get_session_env("HERMES_SESSION_USER_ID", "")
                session_id_probe = get_session_env("HERMES_SESSION_ID", "")
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
            result = deepcopy(receipt_claim["existing_result"])
            result["already_applied"] = True
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
            result = self._error(
                "system_error",
                "本轮操作在执行阶段失败，未确认成功；失败状态已经记录，可按同一操作编号安全复查。",
            )
            result["diagnostic_type"] = type(exc).__name__
        explicit_writeback = result.get("writeback_verified")
        if explicit_writeback is None and isinstance(result.get("data"), dict):
            explicit_writeback = result["data"].get("writeback_verified")
        if result.get("ok") and explicit_writeback is not True:
            result["ok"] = False
            result["error"] = "writeback_consistency_failed"
            result["message"] = "已理解并执行该操作，但写入反查没有完全通过，暂时不能确认成功。"
        if result.get("ok"):
            result["already_applied"] = False
        result = build_execution_receipt(
            result, operation_id=key, operation=operation, idempotency_result="applied"
        )
        def finish_receipt(receipts: Any) -> dict[str, Any]:
            receipts = receipts if isinstance(receipts, dict) else {}
            receipts[receipt_key] = {
                "actor": self.identity.canonical_user_id,
                "operation": operation,
                "operation_id": key,
                "status": "completed" if result.get("ok") else "failed",
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

    def _enqueue_notifications(self, notifications: list[dict[str, Any]]) -> None:
        stamp = datetime.now().isoformat(timespec="seconds")

        def append_notifications(outbox: Any) -> list[dict[str, Any]]:
            outbox = outbox if isinstance(outbox, list) else []
            existing = {
                str(item.get("id") or "")
                for item in outbox
                if isinstance(item, dict)
            }
            for item in notifications:
                notification_id = ":".join(
                    (
                        str(item.get("task_id") or ""),
                        str(item.get("role") or ""),
                        str(item.get("action") or ""),
                    )
                )
                if not notification_id or notification_id in existing:
                    continue
                outbox.append(
                    {
                        **deepcopy(item),
                        "id": notification_id,
                        "status": "pending",
                        "delivery_mode": "direct_wecom",
                        "created_at": stamp,
                        "attempt_count": 0,
                    }
                )
                existing.add(notification_id)
            return outbox[-2000:]

        self.store.update_json("notification_outbox.json", [], append_notifications)

    def context(self) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        visible_students = sorted(self._visible_students())
        visible_tasks = self._visible_tasks()
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
        teacher_name: str = "",
        query_scope: str = "",
        grade: str = "",
        name: str = "",
        role: str = "",
        limit: int = 30,
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
        alias_name = str(name or "").strip()
        if requested and alias_name and requested != alias_name:
            return self._error("ambiguous_target", "student_name 与 name 指向不同学生，请只保留一个明确姓名。")
        requested = requested or alias_name
        try:
            from .runtime_foundation import current_raw_text
            raw_text = current_raw_text(self.identity.canonical_user_id) or current_raw_text(self.user_id)
        except Exception:
            raw_text = ""
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
        safe_limit = max(1, min(int(limit or 30), 100))
        target_identity = self.identity
        requested_teacher = str(teacher_name or "").strip()
        if not requested_teacher and self.identity.role in {"boss", "manager"}:
            requested_teacher = self._teacher_name_from_current_raw_text()
        if requested_teacher:
            if self.identity.role not in {"boss", "manager"}:
                return self._error("permission_denied", "当前账号不能代查其他老师负责的学生。")
            target_identity = self._teacher_identity_by_name(requested_teacher)
            if target_identity is None:
                return self._error("teacher_not_found", f"没有找到老师“{requested_teacher}”。")
            if not self._manager_can_view_teacher(target_identity.canonical_user_id):
                return self._error("permission_denied", f"老师“{requested_teacher}”不在当前店长管理范围内。")
        visible = self._visible_students_for(target_identity)
        if requested:
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
            scope = str(query_scope or "").strip().lower()
            if "暑假班" in str(raw_text or ""):
                scope = "summer"
            if scope == "summer":
                summer_names = self._summer_student_names()
                names = sorted(name for name in visible if name in summer_names)
            else:
                names = sorted(visible)
            requested_grade = _normalize_grade(grade)
            if requested_grade:
                names = [name for name in names if requested_grade in _student_grades(visible.get(name, {}))]
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
                payload.append({"name": name, "profile": visible[name], "recent_records": recent})
            rendered_text = _render_student_records(payload)
        else:
            payload = [{"name": name, "profile": visible[name], "recent_records": []} for name in names[:safe_limit]]
            if scope == "summer":
                title = "暑假班"
            elif requested_teacher:
                title = f"{target_identity.person_name}名下"
            else:
                title = "当前可见范围内"
            if _normalize_grade(grade):
                title += f"{grade}"
            shown = "、".join(names[:safe_limit])
            suffix = "等" if len(names) > safe_limit else ""
            rendered_text = f"{title}共{len(names)}名学生" + (f"：{shown}{suffix}。" if shown else "。")
        data_version = ""
        try:
            data_version = str(int((self.store.data_dir / "records.json").stat().st_mtime_ns))
        except OSError:
            pass
        return self._ok(
            "query_students",
            data={
                "count": len(names),
                "result_count": len(payload),
                "students": payload,
                "rendered_text": rendered_text,
                "render_verified": True,
                "writeback_verified": True,
                "data_version": data_version,
                "scope_user_id": target_identity.canonical_user_id,
                "scope_person_name": target_identity.person_name,
                "scope_reason": scope_reason or "visible_scope",
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
        assignee_user_id: str,
        operation_id: str,
        due_at: str = "",
        level: str = "A",
        student_name: str = "",
        goal_id: str = "",
        goal_action_id: str = "",
        evidence_requirement: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if not self.permissions.can_manage_tasks(self.identity):
            return self._error("permission_denied", "只有老板或店长可以创建并分配任务。")
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
        if candidate_source and (
            (student_name and str(student_name) in candidate_source)
            or trusted_task_source in candidate_source
            or candidate_source in trusted_task_source
        ):
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
            )
            if result.get("ok") and not result.get("already_applied"):
                task = result.get("task") if isinstance(result.get("task"), dict) else {}
                task_id = str(result.get("task_id") or task.get("id") or "")
                if goal_id and task_id:
                    persisted = self.store.update_task(
                        task_id,
                        lambda current: {
                            **current,
                            "goal_id": str(goal_id),
                            "goal_action_id": str(goal_action_id or ""),
                            "evidence_requirement": str(evidence_requirement or "").strip(),
                            "responsibility_evidence": responsibility.get("evidence") or [],
                            "created_autonomously_within_goal": True,
                        },
                    )
                    if isinstance(persisted, dict):
                        task = persisted
                        result["task"] = deepcopy(persisted)
                        result["writeback_verified"] = (
                            str(persisted.get("goal_id") or "") == str(goal_id)
                            and str(persisted.get("goal_action_id") or "") == str(goal_action_id or "")
                        )
                task_contract = task.get("task_contract") if isinstance(task.get("task_contract"), dict) else {}
                criteria = [str(value) for value in task_contract.get("success_criteria") or [] if str(value)]
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
                    content += "\n完成时至少需要说明：" + "；".join(criteria[:4])
                    content += "\n以上是最低闭环证据，不会缩窄老板原要求；实际处理仍以原始要求和现场事实为准。"
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
                self._enqueue_notifications(notifications)
                result.setdefault("notifications", []).extend(
                    {"task_id": task_id, "touser": assignee_user_id, "action": item["action"], "status": "scheduled"}
                    for item in notifications
                )
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
            return result

        return self._operation(operation_id, "create_task", execute)

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
        scope: str = "",
        limit: int = 20,
        write_focus: bool = True,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        requested_scope = str(scope or "").strip().lower()
        effective_scope = requested_scope or ("all" if self.identity.role == "boss" else "mine")
        scope_adjusted = False
        if effective_scope == "all" and self.identity.role != "boss":
            effective_scope = "mine"
            scope_adjusted = True
        tasks = self._visible_tasks()
        requested_teacher = str(teacher_name or "").strip()
        requested_assignee = str(assignee_user_id or "").strip()
        if not requested_teacher and not requested_assignee and self.identity.role in {"boss", "manager"}:
            requested_teacher = self._teacher_name_from_current_raw_text()
        target_teacher_id = ""
        if requested_teacher:
            if self.identity.role not in {"boss", "manager"}:
                return self._error("permission_denied", "当前账号不能代查其他老师的任务。")
            teacher_identity = self._teacher_identity_by_name(requested_teacher)
            if teacher_identity is None:
                return self._error("teacher_not_found", f"没有找到老师“{requested_teacher}”。")
            if not self._manager_can_view_teacher(teacher_identity.canonical_user_id):
                return self._error("permission_denied", f"老师“{requested_teacher}”不在当前店长管理范围内。")
            target_teacher_id = teacher_identity.canonical_user_id
            effective_scope = "all"
        if requested_assignee:
            if target_teacher_id and target_teacher_id != requested_assignee:
                return self._error("ambiguous_target", "teacher_name 与 assignee_user_id 指向不同执行人，请只保留一个明确对象。")
            if self.identity.role == "teacher" and requested_assignee != self.identity.canonical_user_id:
                return self._error("permission_denied", "老师只能按本人账号查询任务。")
            if self.identity.role == "manager" and not self._manager_can_view_teacher(requested_assignee):
                return self._error("permission_denied", "该执行人不在当前店长管理范围内。")
            target_teacher_id = requested_assignee
            effective_scope = "all"
            if not requested_teacher:
                staff = self.store.read_json("staff.json", {})
                profile = staff.get(requested_assignee, {}) if isinstance(staff, dict) else {}
                requested_teacher = str(profile.get("name") or requested_assignee) if isinstance(profile, dict) else requested_assignee
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
        if status:
            tasks = [task for task in tasks if str(task.get("status") or "") == status]
        if level:
            tasks = [task for task in tasks if str(task.get("level") or "") == level]
        tasks = sorted(
            tasks,
            key=lambda item: (
                str(item.get("status") or "") in _CLOSED_STATUSES,
                str(item.get("due_at") or ""),
            ),
        )
        if write_focus and len(tasks) == 1:
            self._write_focus(
                task_id=str(tasks[0].get("id") or ""),
                student_name=str(tasks[0].get("student_name") or ""),
            )
        safe_limit = max(1, min(int(limit or 20), 100))
        visible_tasks = deepcopy(tasks[:safe_limit])
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
        try:
            data_version = str(int((self.store.data_dir / "tasks.json").stat().st_mtime_ns))
        except OSError:
            pass
        return self._ok(
            "query_tasks",
            data={
                "count": len(tasks),
                "result_count": len(tasks),
                "tasks": visible_tasks,
                "task_ids": [item["task_id"] for item in task_summaries],
                "task_summaries": task_summaries,
                "requested_scope": requested_scope or effective_scope,
                "effective_scope": effective_scope,
                "scope_adjusted": scope_adjusted,
                "scope_user_id": target_teacher_id or (self.identity.canonical_user_id if effective_scope == "mine" else ""),
                "scope_person_name": requested_teacher or (self.identity.person_name if effective_scope == "mine" else ""),
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
        student_name: str,
        content: str,
        operation_id: str,
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        name = str(student_name or "").strip()
        if not self.permissions.can_write_student_record(self.identity, name):
            return self._error(
                "permission_denied",
                f"当前账号无权记录学生“{name}”。",
            )
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
            analysis = analyze_teacher_record(f"{name} {text}", self.store)
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
            return self._ok(
                "record_student",
                data={
                    "record": saved_record,
                    "record_id": record_id,
                    "task": task,
                    "task_created": bool(saved.get("created")),
                    "writeback_verified": record_verified,
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
            return self._error("permission_denied", "当前账号没有暑假班课程记录权限，请联系金总确认。")

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
                        "writeback_verified": True,
                        "idempotency_verified": True,
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
            completion_intent = generic_complete or (
                "任务" in compact_evidence
                and any(word in compact_evidence for word in ("完成", "处理了", "处理完"))
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
                            "writeback_verified": True,
                            "idempotency_verified": True,
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
            elif valid_focus:
                resolved_task_id = focus_task_id
            elif explicit_task_id_in_text:
                # A model-supplied id is never authoritative by itself. It is
                # trusted only when the user actually included it in the
                # message; conversational references must resolve through the
                # scoped, non-expired focus above.
                resolved_task_id = supplied_task_id
            elif generic_complete or ordinary_feedback:
                reason_code = "focus_task_expired" if focus_task_id and focus_source and not focus_not_expired else "no_focus_task"
                return self._ok(
                    "update_task",
                    data={
                        "result_action": "clarification_needed",
                        "reason_code": reason_code,
                        "writeback_verified": True,
                        "idempotency_verified": True,
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
                        "writeback_verified": True,
                        "idempotency_verified": True,
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
                        "writeback_verified": True,
                        "idempotency_verified": True,
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
                completion_requested = completion_intent
                if current_was_closed and ordinary_feedback and not current_is_safety and not completion_requested:
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
                    current_result = apply_task_reply([current], actor, evidence_text)
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
                    "task": deepcopy(target),
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
            limit=limit,
        )
        return self._ok("query_staff_directory", data=result, message=result.get("rendered_text", ""))

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
            policy = relationship_touch_policy(self.store)
            role_policy = policy.get(role) if isinstance(policy.get(role), dict) else {}
            mode = str(role_policy.get("mode") or "candidate").strip()
            allowed_user_ids = {
                str(item).strip()
                for item in (role_policy.get("allowed_target_user_ids") or [])
                if str(item).strip()
            }
            target = str(target_user_id or "").strip()
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
            # Direct staff outreach is a test-only privilege and therefore
            # requires an explicit non-empty allowlist. An empty list must fail
            # closed instead of silently meaning "all staff".
            allowed_by_whitelist = role == "boss" or bool(
                target and allowed_user_ids and target in allowed_user_ids
            )
            external_send_allowed = bool(mode == "direct" and allowed_by_whitelist)
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
                target_name=target_name,
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
                "relationship_touch_policy": policy,
                "target_allowed_by_test_whitelist": allowed_by_whitelist,
                "external_send_allowed_by_policy": external_send_allowed,
                "policy_mode": mode,
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
                "主动联系已安排发送；当前只有入队回执，尚不能声称对方已经收到。"
                if external_send_allowed and execute_if_authorized
                else "已保存可主动触达候选；当前测试白名单允许该对象进入外发候选。"
                if external_send_allowed
                else "已保存内部候选；该对象不在当前直接主动外发范围内，不会真实外发。"
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
        return self._ok("query_proactive_authorizations", data=result, message=str(result.get("rendered_text") or ""))

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
