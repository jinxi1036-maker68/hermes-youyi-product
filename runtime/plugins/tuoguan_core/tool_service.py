"""Permission-scoped business operations exposed to the Hermes model."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import os
import re
import uuid
from typing import Any, Callable
from urllib.parse import quote

from .dashboard_auth import DashboardAuthError, sign_dashboard_token, token_expiry_datetime
from .dashboard_builder import refresh_dashboard_cache
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
from .store import TuoguanStore, TuoguanStoreError
from .summer_points import change_points, query_points_ranking, query_student_points
from .tasks import apply_task_reply, closure_missing_fields, current_task_for_user
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
from .self_evolution import query_self_evolution_ledger as build_self_evolution_ledger
from .workstyle_profiles import (
    query_person_workstyle_profile as build_person_workstyle_profile,
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
    query_gray_optimization_decisions,
    query_gray_observations,
    query_gray_rollout_decisions,
    query_parent_communication_coverage,
    query_performance_evidence_candidates,
    query_profile_candidates,
    query_information_requests,
    query_competitor_profiles,
    query_external_learning_brief,
    query_external_research_runs,
    query_industry_learning_candidates,
    query_market_research_candidates,
    query_proactive_work_radar,
    query_student_service_relations,
    query_value_ledger,
    query_value_progress_ledger,
    query_weekly_record_coverage,
    submit_industry_learning_candidate,
    submit_action_execution,
    submit_business_event,
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
    submit_service_relation_fact_candidate,
    submit_value_ledger_entry,
    update_hermes_work_item,
)


_CLOSED_STATUSES = {"completed", "cancelled", "closed", "done", "closed_by_admin", "completed_by_admin"}

_MISSING_FIELD_LABELS = {
    "parent_attitude": "家长的反馈或态度",
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

    def _write_focus(self, **updates: Any) -> None:
        data = self.store.read_json("model_focus.json", {})
        if not isinstance(data, dict):
            data = {}
        item = data.get(self._focus_key(), {})
        if not isinstance(item, dict):
            item = {}
        item.update({key: value for key, value in updates.items() if value is not None})
        item["updated_at"] = datetime.now().isoformat(timespec="seconds")
        data[self._focus_key()] = item
        self.store.write_json("model_focus.json", data)

    def _write_user_focus(self, user_id: str, **updates: Any) -> None:
        data = self.store.read_json("model_focus.json", {})
        if not isinstance(data, dict):
            data = {}
        key = f"{self.platform}:{str(user_id or '').strip()}"
        item = data.get(key, {})
        if not isinstance(item, dict):
            item = {}
        item.update({name: value for name, value in updates.items() if value is not None})
        item["updated_at"] = datetime.now().isoformat(timespec="seconds")
        data[key] = item
        self.store.write_json("model_focus.json", data)

    def _remember_user_task_context(self, user_id: str, task: dict[str, Any], *, ttl_hours: int = 36) -> None:
        user_id = str(user_id or "").strip()
        task_id = str(task.get("id") or "")
        if not user_id or not task_id:
            return
        now = datetime.now()
        active = self.store.read_json("active_task_context.json", {})
        if not isinstance(active, dict):
            active = {}
        active[user_id] = {
            "user_id": user_id,
            "task_id": task_id,
            "student_id": str(task.get("student_id") or task.get("student_name") or ""),
            "student_name": str(task.get("student_name") or ""),
            "task_type": str(task.get("type") or "manual_assignment"),
            "status": "selected",
            "started_at": now.isoformat(timespec="seconds"),
            "expires_at": (now + timedelta(hours=ttl_hours)).isoformat(timespec="seconds"),
            "candidate_task_ids": [],
            "source": "assigned_task_created",
        }
        self.store.write_json("active_task_context.json", active)
        pending = self.store.read_json("pending_next_task_context.json", {})
        if not isinstance(pending, dict):
            pending = {}
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
            "created_at": now.isoformat(timespec="seconds"),
            "expires_at": (now + timedelta(hours=ttl_hours)).isoformat(timespec="seconds"),
        }
        self.store.write_json("pending_next_task_context.json", pending)

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
        compact = "".join(str(text or "").split())
        match = re.search(r"(\d+)分钟后", compact)
        minutes = int(match.group(1)) if match else 0
        if not minutes:
            chinese = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "十": 10}
            match = re.search(r"([一两二三四五十])分钟后", compact)
            minutes = chinese.get(match.group(1), 0) if match else 0
        if minutes <= 0:
            return ""
        return (datetime.now().astimezone() + timedelta(minutes=minutes)).isoformat(timespec="seconds")

    def _operation(
        self,
        operation_id: str,
        operation: str,
        execute: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        key = str(operation_id or "").strip()
        if not key:
            return self._error(
                "operation_id_required",
                "写操作缺少 operation_id，未执行，避免产生重复数据。",
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
            return self._error(
                "ambiguous_retry_write_not_executed",
                "当前消息没有明确说明要写入的对象、动作和内容，不能沿用上文执行真实写入。请把要记录或修改的内容重新说完整。",
            )
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
            return self._error(
                "capability_self_check_write_not_executed",
                "这是能力检查问题，不能通过真实写入来测试。请改用只读查询、工具目录或测试环境检查。",
            )

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
                "YOUYI_WRITE_AUTH_MISS operation=%s actor=%s platform_user=%s role=%s raw=%s ledger_probe=%s session_user=%s session_id=%s module=%s",
                operation,
                self.identity.canonical_user_id,
                self.user_id,
                self.identity.role,
                raw_text[:80],
                ledger_probe,
                session_user_probe,
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
            return self._error(
                "unauthorized_write_blocked",
                f"当前请求没有通过业务写入校验，未修改任何业务数据。审计编号：{audit_id}",
            )
        receipts = self.store.read_json("tool_operations.json", {})
        if not isinstance(receipts, dict):
            receipts = {}
        receipt_key = f"{self.identity.canonical_user_id}:{operation}:{key}"
        existing = receipts.get(receipt_key)
        if isinstance(existing, dict) and isinstance(existing.get("result"), dict):
            result = deepcopy(existing["result"])
            result["already_applied"] = True
            return result
        runtime_auth = runtime_auth or {"ledger_id": "test_or_offline_runtime"}
        preaudit_id = f"audit_write_authorized_{uuid.uuid4().hex}"
        with authorized_business_write(
            source="trusted_tool",
            operation_id=key,
            ledger_id=runtime_auth["ledger_id"],
            audit_id=preaudit_id,
        ):
            result = execute()
        explicit_writeback = result.get("writeback_verified")
        if explicit_writeback is None and isinstance(result.get("data"), dict):
            explicit_writeback = result["data"].get("writeback_verified")
        if result.get("ok") and explicit_writeback is False:
            result["ok"] = False
            result["error"] = "writeback_consistency_failed"
            result["message"] = "已理解并执行该操作，但写入反查没有完全通过，暂时不能确认成功。"
        if result.get("ok"):
            result["already_applied"] = False
            receipts[receipt_key] = {
                "actor": self.identity.canonical_user_id,
                "operation": operation,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "result": deepcopy(result),
            }
            self.store.write_json("tool_operations.json", receipts)
        return result

    def _enqueue_notifications(self, notifications: list[dict[str, Any]]) -> None:
        outbox = self.store.read_json("notification_outbox.json", [])
        if not isinstance(outbox, list):
            outbox = []
        existing = {
            str(item.get("id") or "")
            for item in outbox
            if isinstance(item, dict)
        }
        stamp = datetime.now().isoformat(timespec="seconds")
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
        self.store.write_json("notification_outbox.json", outbox[-2000:])

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

    def query_students(self, *, student_name: str = "", teacher_name: str = "", query_scope: str = "", grade: str = "") -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
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
        requested = str(student_name or "").strip()
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
            try:
                from .runtime_foundation import current_raw_text
                raw_text = current_raw_text(self.identity.canonical_user_id) or current_raw_text(self.user_id)
            except Exception:
                raw_text = ""
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
            payload = [{"name": name, "profile": visible[name], "recent_records": []} for name in names]
            if scope == "summer":
                title = "暑假班"
            elif requested_teacher:
                title = f"{target_identity.person_name}名下"
            else:
                title = "当前可见范围内"
            if _normalize_grade(grade):
                title += f"{grade}"
            shown = "、".join(names[:30])
            suffix = "等" if len(names) > 30 else ""
            rendered_text = f"{title}共{len(names)}名学生" + (f"：{shown}{suffix}。" if shown else "。")
        data_version = ""
        try:
            data_version = str(int((self.store.data_dir / "records.json").stat().st_mtime_ns))
        except OSError:
            pass
        return self._ok(
            "query_students",
            data={
                "count": len(payload),
                "result_count": len(payload),
                "students": payload,
                "rendered_text": rendered_text,
                "render_verified": True,
                "writeback_verified": True,
                "data_version": data_version,
                "scope_user_id": target_identity.canonical_user_id,
                "scope_person_name": target_identity.person_name,
            },
            message=f"查询到 {len(payload)} 名有权限查看的学生。",
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
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if not self.permissions.can_manage_tasks(self.identity):
            return self._error("permission_denied", "只有老板或店长可以创建并分配任务。")
        try:
            from .runtime_foundation import current_raw_text
            raw_text_for_boundary = current_raw_text(self.identity.canonical_user_id)
        except Exception:
            raw_text_for_boundary = ""
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
            try:
                from .runtime_foundation import current_raw_text
                due_at = self._relative_due_at(current_raw_text(self.identity.canonical_user_id))
            except Exception:
                due_at = ""
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
            )
            if result.get("ok") and not result.get("already_applied"):
                task = result.get("task") if isinstance(result.get("task"), dict) else {}
                task_id = str(result.get("task_id") or task.get("id") or "")
                content = (
                    f"你收到一项新任务：{task.get('title') or title}\n"
                    f"等级：{task.get('level') or level}\n"
                    f"截止：{task.get('due_at') or due_at or '请尽快处理'}\n"
                    f"安排人：{self.identity.person_name or self.identity.canonical_user_id}\n"
                    f"关联学生：{task.get('student_name') or student_name or '无'}\n"
                    "请直接回复处理进展；完成后可说“这个任务已经完成”。"
                )
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
                self._write_user_focus(assignee_user_id, task_id=task_id, student_name=str(task.get("student_name") or student_name or ""), task_type=str(task.get("type") or "manual_assignment"))
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
        status: str = "",
        level: str = "",
        scope: str = "",
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
        if not requested_teacher and self.identity.role in {"boss", "manager"}:
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
        visible_tasks = deepcopy(tasks[:20])
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
            try:
                from .runtime_foundation import current_raw_text
                trusted_raw = current_raw_text(self.identity.canonical_user_id)
            except Exception:
                trusted_raw = ""
            evidence_text = trusted_raw or raw_reply
            visible_tasks = self._visible_tasks()
            open_visible = [task for task in visible_tasks if task.get("status") not in _CLOSED_STATUSES]
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
            supplied_task_id = str(task_id or "").strip()
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
            actor = (
                str(target.get("assignee_userid") or "")
                if self.identity.role in {"manager", "boss"}
                else self.identity.canonical_user_id
            )
            # The current inbound user text is authoritative; model-generated
            # elaboration must never become safety or closure evidence.
            if was_closed and ordinary_feedback and not is_safety and not completion_intent:
                previous = str(target.get("evidence_summary") or "").strip()
                evidence_lines = [line.strip() for line in previous.splitlines() if line.strip()]
                if evidence_text not in evidence_lines:
                    target["evidence_summary"] = "\n".join([*evidence_lines, evidence_text]).strip()
                stamp = datetime.now().isoformat(timespec="seconds")
                target["updated_at"] = stamp
                events = target.get("closure_events")
                if not isinstance(events, list):
                    events = []
                events.append(
                    {
                        "action": "fact_added_after_completion",
                        "actor_userid": actor,
                        "text": evidence_text,
                        "at": stamp,
                    }
                )
                target["closure_events"] = events[-50:]
                result_action = "fact_added_after_completion"
                result_reply = "已把这次回访情况补充到原任务记录中，任务仍保持已完成。"
            else:
                result = apply_task_reply([target], actor, evidence_text)
                result_action = result.action
                result_reply = result.reply
            auto_parent_complete = bool(
                not is_safety
                and (str(target.get("type") or "") in {"parent_anxiety", "parent_complaint", "renewal_risk"} or "家长沟通" in str(target.get("title") or ""))
                and "沟通" in compact_evidence
                and any(word in compact_evidence for word in ("家长", "妈妈", "爸爸"))
                and any(word in compact_evidence for word in ("知道", "表示", "说", "反馈", "关注", "考虑", "同意", "认可"))
                and any(word in compact_evidence for word in ("后期", "后续", "继续", "再", "关注", "跟进", "观察"))
            )
            if auto_parent_complete:
                fields = target.get("closure_fields") if isinstance(target.get("closure_fields"), dict) else {}
                fields.update({"parent_informed": evidence_text, "parent_attitude": evidence_text, "followup_plan": evidence_text})
                target["closure_fields"] = fields
                completion_intent = True
                result_action = "completed"
                result_reply = "家长沟通结果和后续安排已记录，任务已完成。"
            current_missing = [] if auto_parent_complete else closure_missing_fields(target, str(target.get("evidence_summary") or evidence_text))
            if completion_intent and not is_safety and not current_missing:
                stamp = datetime.now().isoformat(timespec="seconds")
                target["status"] = "completed"
                target["completed_at"] = stamp
                target["updated_at"] = stamp
                target["closure_summary"] = evidence_text
            self.store.save_tasks(tasks)
            persisted = next(
                (item for item in self.store.load_tasks() if str(item.get("id") or "") == str(target.get("id") or "")),
                None,
            )
            task_verified = isinstance(persisted, dict) and str(persisted.get("updated_at") or persisted.get("completed_at") or persisted.get("evidence_summary") or "") != ""
            final_message = result_reply
            if task_verified and str(target.get("status") or "") in _CLOSED_STATUSES:
                completion_prefix = result_reply.rstrip("。")
                final_message = (
                    f"{completion_prefix}。谢谢老师认真反馈。"
                    "你可以直接回复“继续下一个任务”，我会带你进入下一项。"
                )
            elif current_missing:
                missing_text = "、".join(_MISSING_FIELD_LABELS.get(item, item) for item in current_missing)
                final_message = (
                    f"已记录你刚才的反馈，但这个任务还没有完成。还需要补充：{missing_text}。"
                    "你直接按实际情况继续说，我会接着记录并告诉你何时完成。"
                )
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
                    "writeback_verified": task_verified,
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
        preference_type: str,
        scope: str,
        preference_text: str,
        operation_id: str,
        normalized_rule: str = "",
        target_user_id: str = "",
        target_name: str = "",
        target_role: str = "",
        source_text: str = "",
    ) -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        if self.identity.role not in {"teacher", "manager", "boss"}:
            return self._error("permission_denied", "当前账号不能提交服务方式偏好。")

        def execute() -> dict[str, Any]:
            result = save_person_workstyle_preference(
                self.store,
                identity=self.identity,
                preference_type=preference_type,
                scope=scope,
                preference_text=preference_text,
                normalized_rule=normalized_rule,
                target_user_id=target_user_id,
                target_name=target_name,
                target_role=target_role,
                source_text=source_text,
                operation_id=operation_id,
            )
            if not result.get("ok"):
                return self._error(str(result.get("error") or "workstyle_preference_failed"), str(result.get("message") or "服务方式偏好没有保存成功。"))
            data = {**result, "writeback_verified": bool(result.get("writeback_verified"))}
            return self._ok("submit_person_workstyle_preference", data=data, message=result.get("rendered_text", ""))

        return self._operation(operation_id, "submit_person_workstyle_preference", execute)

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

    def query_parent_communication_coverage(self, *, days: int = 31, program_id: str = "regular_tuoguan") -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_parent_communication_coverage(
            self.store,
            identity=self.identity,
            days=days,
            program_id=program_id,
        )
        return self._ok("query_parent_communication_coverage", data=result, message=str(result.get("rendered_text") or ""))

    def query_weekly_record_coverage(self, *, days: int = 7, program_id: str = "regular_tuoguan") -> dict[str, Any]:
        denied = self._approved()
        if denied:
            return denied
        result = query_weekly_record_coverage(
            self.store,
            identity=self.identity,
            days=days,
            program_id=program_id,
        )
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
