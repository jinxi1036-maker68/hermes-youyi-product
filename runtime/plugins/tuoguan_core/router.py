"""Deterministic tutoring-center message router."""

from __future__ import annotations

import os
import json
import re
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from .dashboard_auth import (
    DashboardAuthError,
    parent_report_token_expiry_datetime,
    sign_dashboard_token,
    sign_parent_report_token,
    token_expiry_datetime,
)
from .config_changes import (
    build_config_change_proposal,
    cancel_config_change,
    confirm_config_change,
    confirmation_reply,
    is_config_cancel,
    is_config_change_request,
    is_config_confirmation,
    proposal_reply,
)
from .conversation_state import (
    clear_conversation_state,
    compact_text as compact_conversation_text,
    conversation_state_summary,
    is_short_followup_reply,
    is_state_expired,
    load_conversation_state,
    matches_expected_reply,
    remember_conversation_state,
    remember_conversation_state_for_user,
)
from .dashboard_builder import CACHE_FILE, build_dashboard_snapshot, refresh_dashboard_cache
from .escalation import build_notification_plan
from .growth_reports import (
    approve_growth_report,
    build_growth_report_draft,
    create_parent_report_short_link,
    latest_growth_report_context,
    latest_growth_report,
    mark_growth_report_shared,
    pending_growth_reports_for_actor,
    remember_growth_report_context,
    save_growth_report_draft,
)
from .identity import IdentityService
from .permission_guard import guard_permission_request
from .knowledge import (
    answer_script_feedback,
    answer_script_request,
    is_script_feedback,
    is_script_request,
    list_pending_learning_candidates,
    recent_script_context,
    remember_script_context,
    review_learning_candidate,
    submit_learning_candidate,
)
from .models import RouteResult, UserIdentity
from .message_history import (
    conversation_id_for,
    history_summary,
    recent_message_history,
    record_inbound_message,
)
from .operations_focus import (
    classify_operations_focus,
    looks_like_operations_focus,
    operations_focus_reply,
    save_operations_focus,
)
from .payroll import (
    append_payroll_event,
    append_payroll_review,
    classify_payroll_event,
    classify_payroll_export,
    classify_payroll_review,
    classify_payroll_settlement,
    create_payroll_settlement,
    export_payroll_settlement,
    payroll_event_reply,
    payroll_export_reply,
    payroll_review_reply,
    payroll_settlement_reply,
)
from .permissions import PermissionService
from .programs import (
    REGULAR_PROGRAM_ID,
    SUMMER_PROGRAM_ID,
    canonical_program_id,
    is_summer_operator,
    item_program_id,
    program_label,
    resolve_record_program,
    student_program_ids,
    user_program_ids,
)
from .queries import answer_student_query, is_student_query
from .records import (
    AmbiguousStudentError,
    UnknownStudentError,
    analyze_teacher_record,
    remove_record_by_id,
    recognize_student,
    save_analysis,
)
from .reports import build_role_report
from .runtime import build_status
from .semantic_router import SemanticRouteResult, route_message_semantically
from .store import JSON_NO_CHANGE, TuoguanStore, TuoguanStoreError
from .summer_enrollment import (
    guess_unknown_student_name,
    remember_unknown_summer_record,
    resolve_duplicate_issue,
    safety_attention_reminders,
)
from .summer_records import looks_like_summer_lesson_record, save_summer_lesson_record
from .summer_reports import prepare_summer_reports
from .summer_course_coverage import lesson_recording_help
from .staff_config import (
    build_staff_config_proposal,
    cancel_staff_config,
    confirm_staff_config,
    is_staff_candidate_selection,
    is_staff_config_cancel,
    is_staff_config_confirmation,
    is_staff_config_query,
    is_staff_config_request,
    proposal_reply as staff_config_proposal_reply,
    refresh_latest_staff_config_proposal,
    select_staff_candidate,
    staff_roster_reply,
)
from .tasks import (
    CLOSED_TASK_STATUSES,
    apply_task_reply,
    build_task_contract,
    classify_task_reply,
    closure_missing_fields,
    current_task_for_user,
)
from .temporal_grounding import parse_business_due_at


_SENSITIVE_TERMS = (
    "导出全部",
    "删除学生",
    "清空",
    "修改权限",
    "改权限",
    "读取文件",
    "执行命令",
    "终端",
)

_CLOSED_STATUSES = CLOSED_TASK_STATUSES
_ACTIVE_TASK_CONTEXT_FILE = "active_task_context.json"
_ADMIN_TASK_CLOSE_FILE = "task_admin_closure_events.json"
_PENDING_CLOSE_TASK_FILE = "pending_close_task_context.json"
_PENDING_NEXT_TASK_FILE = "pending_next_task_context.json"
_PENDING_STUDENT_CONTEXT_FILES = (
    "pending_student_confirm_context.json",
    "pending_student_context.json",
    "student_confirm_context.json",
)
_KEY_TASK_TYPES = {
    "parent_anxiety",
    "renewal_risk",
    "new_trial",
    "parent_complaint",
    "safety_incident",
}
_LEVEL_ORDER = {"S": 0, "A": 1, "B": 2, "C": 3}
_DASHBOARD_COMMANDS = {
    "看板",
    "我的看板",
    "老师看板",
    "老板看板",
    "管理看板",
    "数据看板",
}
_GROWTH_PERIOD_ALIASES = {
    "本周报告": "weekly",
    "周报告": "weekly",
    "周": "weekly",
    "本周": "weekly",
    "周报": "weekly",
    "本月报告": "monthly",
    "月报告": "monthly",
    "月": "monthly",
    "本月": "monthly",
    "月报": "monthly",
    "本学期报告": "semester",
    "整学期报告": "semester",
    "学期": "semester",
    "本学期": "semester",
    "整学期": "semester",
    "学期报告": "semester",
    "结业汇报": "graduation",
    "暑假班结业汇报": "graduation",
}
_GROWTH_PERIOD_DAYS = {"weekly": 7, "monthly": 30, "semester": 120, "graduation": 60}
_GROWTH_PERIOD_LABELS = {"weekly": "周报告", "monthly": "月报告", "semester": "学期报告", "graduation": "结业汇报"}
_GROWTH_GENERATE_WORDS = (
    "生成",
    "整理",
    "做",
    "出",
    "弄",
    "准备",
    "写",
    "汇总",
)
_GROWTH_APPROVE_WORDS = (
    "确认",
    "审核通过",
    "通过",
    "可以了",
    "没问题",
    "无误",
    "行",
)
_GROWTH_REPORT_WORDS = (
    "报告",
    "周报",
    "月报",
    "总结",
    "反馈",
    "成长",
    "表现",
    "情况",
)
_GROWTH_PARENT_WORDS = (
    "家长",
    "妈妈",
    "爸爸",
    "父母",
    "奶奶",
    "爷爷",
    "发给",
    "转发",
    "分享",
    "给看",
    "给他看",
    "给她看",
)
_RECORD_INTENT_WORDS = (
    "记录",
    "记一下",
    "记下",
    "补记",
    "补充记录",
    "登记",
    "录入",
    "写入",
    "存一下",
    "沉淀",
)
_UNDO_RECORD_WORDS = (
    "撤销上一条记录",
    "撤回上一条记录",
    "删除上一条记录",
    "删掉上一条记录",
    "上一条不是记录",
    "刚才那条不是记录",
    "刚才记错了",
    "误记录",
)
_AUTO_RECORD_TYPES = {
    "safety_incident",
    "parent_complaint",
    "renewal_risk",
    "parent_anxiety",
    "new_trial",
    "academic_issue",
    "learning_habit",
    "meal_care",
    "nap_care",
    "life_care",
    "activity_care",
    "behavior_observation",
}
_MANUAL_ASSIGNMENT_WORDS = (
    "安排",
    "派给",
    "交给",
    "指派",
    "布置",
    "下发",
    "让",
)
_TASK_SOURCE_LABELS = {
    "manual_assignment": "手动安排",
    "record_triggered": "记录触发",
    "system_risk": "系统风险",
}


class TuoguanRouter:
    def __init__(self, store: TuoguanStore | None = None) -> None:
        self.store = store or TuoguanStore()
        self.identities = IdentityService(self.store)
        self.permissions = PermissionService(self.store)

    @staticmethod
    def _has_record_intent(text: str) -> bool:
        compact = str(text or "").replace(" ", "")
        return any(word in compact for word in _RECORD_INTENT_WORDS)

    def _looks_like_student_record(self, identity: UserIdentity, text: str) -> bool:
        if identity.role not in {"teacher", "manager"}:
            return False
        if self._looks_like_explicit_task_assignment(text):
            return False
        try:
            student_name = recognize_student(text, self.store)
        except (UnknownStudentError, AmbiguousStudentError):
            return False
        if not student_name or not self.permissions.can_write_student_record(identity, student_name):
            return False
        compact = str(text or "").replace(" ", "")
        scene_words = (
            "学习",
            "作业",
            "订正",
            "计算",
            "阅读",
            "语文",
            "数学",
            "英语",
            "表现",
            "纪律",
            "吃饭",
            "午休",
            "情绪",
            "专注",
            "注意力",
            "完成质量",
            "错了",
            "漏了",
            "检查",
            "重新",
            "提醒",
            "让他",
            "让她",
            "我让",
            "老师",
            "继续关注",
            "继续观察",
        )
        action_words = ("提醒", "订正", "检查", "沟通", "观察", "关注", "让他", "让她", "我让", "老师")
        return any(word in compact for word in scene_words) and any(word in compact for word in action_words)

    def _looks_like_explicit_task_assignment(self, text: str) -> bool:
        compact = str(text or "").replace(" ", "")
        if not compact:
            return False
        if "任务" in compact and any(word in compact for word in _MANUAL_ASSIGNMENT_WORDS):
            return True
        if any(word in compact for word in ("工资闭环", "安排任务", "下发任务", "指派任务", "布置任务")):
            return True
        assignee_userid, _assignee_name = self._resolve_assignee(text)
        return bool(assignee_userid and any(word in compact for word in _MANUAL_ASSIGNMENT_WORDS) and any(word in compact for word in ("跟进", "沟通", "处理", "完成")))

    @staticmethod
    def _is_undo_record(text: str) -> bool:
        compact = str(text or "").replace(" ", "")
        return any(word in compact for word in _UNDO_RECORD_WORDS)

    def _last_record_contexts(self) -> dict[str, Any]:
        data = self.store.read_json("last_record_context.json", {})
        return data if isinstance(data, dict) else {}

    def _remember_last_record(
        self,
        identity: UserIdentity,
        record: dict[str, Any],
    ) -> None:
        contexts = self._last_record_contexts()
        contexts[identity.canonical_user_id] = {
            "record_id": record.get("id"),
            "student_name": record.get("student_name"),
            "content": record.get("content") or record.get("source_text"),
            "timestamp": record.get("timestamp"),
        }
        self.store.write_json("last_record_context.json", contexts)

    def _route_undo_record(
        self,
        identity: UserIdentity,
        text: str,
    ) -> RouteResult | None:
        if not self._is_undo_record(text):
            return None
        context = self._last_record_contexts().get(identity.canonical_user_id)
        record_id = str((context or {}).get("record_id") or "")
        if not record_id:
            return RouteResult(handled=True, reply="没有找到可撤销的上一条记录。")
        removed = remove_record_by_id(
            self.store,
            record_id=record_id,
            actor_userid=identity.canonical_user_id,
        )
        if not removed:
            return RouteResult(handled=True, reply="上一条记录已经不存在，或不是你写入的记录。")
        contexts = self._last_record_contexts()
        contexts.pop(identity.canonical_user_id, None)
        self.store.write_json("last_record_context.json", contexts)
        return RouteResult(
            handled=True,
            reply=f"已撤销上一条{removed.get('student_name', '')}的记录。",
        )

    def _identity(
        self,
        platform: str,
        sender_id: str,
        user_name: str,
        chat_id: str,
        text: str,
    ) -> UserIdentity:
        return self.identities.resolve(
            platform,
            sender_id,
            user_name=user_name,
            chat_id=chat_id,
            message_text=text,
        )

    @staticmethod
    def _unknown_reply(identity: UserIdentity) -> str:
        if identity.approval_state == "rejected":
            return "你的账号未通过审核，如需使用请联系管理员。"
        return (
            "当前企业微信账号还没有绑定到 Hermes，暂时不能使用托管业务功能。"
            "我已登记这次访问，请联系金总添加身份后再使用。"
        )

    @staticmethod
    def _route_identity_claim(identity: UserIdentity, text: str) -> RouteResult | None:
        compact = str(text or "").replace(" ", "")
        if not compact:
            return None
        claim_patterns = (
            "我是金总",
            "我是老板",
            "我是店长",
            "我是老师",
            "我是金总账号",
            "我是老板账号",
            "我是店长账号",
            "我是老师账号",
        )
        if not any(pattern in compact for pattern in claim_patterns):
            return None
        role_label = {
            "teacher": "老师",
            "manager": "店长",
            "boss": "老板",
        }.get(identity.role, identity.role or "未知")
        name = identity.person_name or identity.platform_user_id
        return RouteResult(
            handled=True,
            reply=(
                f"身份不能通过聊天内容切换。当前企业微信账号绑定身份是：{name}（{role_label}）。\n"
                "如果身份配置不对，请让金总在身份权限里调整；调整前我只按当前企业微信 userid 的绑定权限处理。"
            ),
        )

    @staticmethod
    def _route_identity_safe_smalltalk(identity: UserIdentity, text: str) -> RouteResult | None:
        if identity.role == "boss":
            return None
        compact = str(text or "").replace(" ", "").strip()
        if compact not in {
            "你好",
            "您好",
            "在吗",
            "在不在",
            "哈喽",
            "hello",
            "hi",
            "早",
            "早上好",
            "下午好",
            "晚上好",
            "谢谢",
            "谢谢你",
            "好的谢谢",
        }:
            return None
        role_label = {
            "teacher": "老师",
            "manager": "店长",
            "boss": "老板",
        }.get(identity.role, "已授权用户")
        name = identity.person_name or identity.platform_user_id
        if identity.role == "teacher":
            actions = "你可以直接发学生记录、回复“我的任务”，或回复“看板”。"
        elif identity.role == "manager":
            actions = "你可以查看店长看板、处理老师任务和数据问题。"
        elif identity.role == "boss":
            actions = "你可以查看老板看板、安排任务或确认配置变更。"
        else:
            actions = "你可以发送“帮助”查看可用功能。"
        return RouteResult(
            handled=True,
            reply=f"{name}，你好。当前企业微信账号识别为：{role_label}。{actions}",
        )

    def _route_active_task(
        self,
        identity: UserIdentity,
        text: str,
    ) -> RouteResult | None:
        compact_command = str(text or "").strip()
        if compact_command in _DASHBOARD_COMMANDS or compact_command in {"帮助", "怎么用", "如何使用", "使用说明", "能做什么", "状态", "运行状态", "日报", "今日日报", "任务日报", "任务", "我的任务", "当前任务", "待办"}:
            return None
        tasks = self.store.load_tasks()
        open_tasks = self._open_tasks_for_user(tasks, identity.canonical_user_id)
        if not open_tasks:
            if classify_task_reply(text)["intent"] in {"start_processing", "complete_pending_confirmation"}:
                return RouteResult(handled=True, reply="你当前暂无待处理任务。")
            return None
        selected = self._selected_task_from_reply(identity, open_tasks, text)
        if selected is not None:
            result_holder: dict[str, Any] = {}

            def start_selected(current: dict[str, Any]) -> None:
                result_holder["result"] = apply_task_reply(
                    [current],
                    identity.canonical_user_id,
                    "开始",
                    task_id=str(current.get("id") or ""),
                )

            persisted = self.store.update_task(str(selected.get("id") or ""), start_selected)
            result = result_holder.get("result")
            if persisted is None or result is None:
                return RouteResult(handled=True, reply="任务在开始前发生变化，请重新发送“任务”查看当前待办。")
            self._remember_active_task_context(identity, persisted, candidates=[])
            self._clear_pending_next_task_context(identity)
            self._record_legacy_router_coaching(identity, persisted, result, text="开始")
            self._append_task_closure_ledger(self.store.load_tasks(), result.task_id)
            return RouteResult(handled=True, reply=result.reply)
        intent = classify_task_reply(text)["intent"]
        context_task = self._active_task_from_context(identity, open_tasks)
        task = context_task or current_task_for_user(tasks, identity.canonical_user_id)
        if not task:
            return None
        if intent == "start_processing" and context_task is None and len(open_tasks) > 1:
            self._remember_active_task_context(identity, task, candidates=open_tasks)
            return RouteResult(handled=True, reply=self._task_selection_reply(open_tasks))
        try:
            mentioned_student = recognize_student(text, self.store)
        except (UnknownStudentError, AmbiguousStudentError):
            mentioned_student = ""
        if (
            mentioned_student
            and mentioned_student != str(task.get("student_name") or "")
            and classify_task_reply(text)["intent"] == "fact_supplement"
        ):
            return None
        if (
            intent == "fact_supplement"
            and context_task is None
            and str(task.get("status") or "") not in {"active", "waiting_confirmation"}
            and not self._looks_like_task_evidence(text, task)
        ):
            return None
        result_holder: dict[str, Any] = {}

        def update_current(current: dict[str, Any]) -> None:
            result_holder["result"] = apply_task_reply(
                [current],
                identity.canonical_user_id,
                text,
                task_id=str(current.get("id") or ""),
            )

        persisted = self.store.update_task(str(task.get("id") or ""), update_current)
        result = result_holder.get("result")
        if persisted is None or result is None:
            return RouteResult(handled=True, reply="任务在更新前发生变化，请重新发送“任务”查看当前状态。")
        task = persisted
        if result.action in {"started", "fact_added", "needs_closure_evidence", "closure_ready", "helped"}:
            self._remember_active_task_context(identity, task, candidates=[])
        elif result.action in {"completed", "cancelled"}:
            self._clear_active_task_context(identity)
            self._clear_pending_student_confirm_context(identity)
            self._suppress_pending_notifications_for_task(
                str(task.get("id") or ""),
                reason=f"task_{result.action}",
            )
        self._record_legacy_router_coaching(identity, task, result, text=text)
        current_tasks = self.store.load_tasks()
        self._append_task_closure_ledger(current_tasks, result.task_id)
        self._refresh_dashboard_cache_best_effort()
        reply_text = result.reply
        if result.action == "completed":
            next_task = current_task_for_user(current_tasks, identity.canonical_user_id)
            if next_task is not None:
                self._remember_pending_next_task_context(identity, current_tasks)
                reply_text = (
                    f"{reply_text.rstrip()}\n当前还有 1 个 {next_task.get('level') or 'C'} 级任务待处理："
                    f"{next_task.get('title') or '未命名任务'}。回复“继续”即可开始。"
                )
            else:
                self._clear_pending_next_task_context(identity)
        elif result.action in {"started", "cancelled"}:
            self._clear_pending_next_task_context(identity)
        notifications = self._task_completion_notifications(task, identity) if result.action == "completed" else []
        return RouteResult(handled=True, reply=reply_text, notifications=notifications)

    def _start_task_by_id(
        self,
        identity: UserIdentity,
        task_id: str,
    ) -> RouteResult | None:
        if not task_id:
            return None
        tasks = self.store.load_tasks()
        task = next(
            (
                item
                for item in tasks
                if str(item.get("id") or "") == str(task_id)
                and item.get("assignee_userid") == identity.canonical_user_id
                and item.get("status") not in _CLOSED_STATUSES
            ),
            None,
        )
        if task is None:
            self._clear_pending_next_task_context(identity)
            return RouteResult(handled=True, reply="刚才提示的下一个任务已经不存在或已处理，请发送“任务”查看当前待办。")
        result_holder: dict[str, Any] = {}

        def start_current(current: dict[str, Any]) -> None:
            result_holder["result"] = apply_task_reply(
                [current],
                identity.canonical_user_id,
                "开始",
                task_id=str(current.get("id") or ""),
            )

        persisted = self.store.update_task(str(task.get("id") or ""), start_current)
        result = result_holder.get("result")
        if persisted is None or result is None:
            return RouteResult(handled=True, reply="任务在开始前发生变化，请重新发送“任务”查看当前待办。")
        task = persisted
        self._remember_active_task_context(identity, task, candidates=[])
        self._clear_pending_next_task_context(identity)
        self._record_legacy_router_coaching(identity, task, result, text="开始")
        self._append_task_closure_ledger(self.store.load_tasks(), result.task_id)
        self._refresh_dashboard_cache_best_effort()
        title = str(task.get("title") or "未命名任务")
        reply = result.reply if title in result.reply else f"已进入任务：{title}\n{result.reply}"
        return RouteResult(handled=True, reply=reply)

    def _route_config_change(self, identity: UserIdentity, text: str) -> RouteResult | None:
        if is_config_confirmation(text):
            if identity.role != "boss":
                return RouteResult(handled=True, reply="只有金总/老板账号可以确认执行身份、老师交接或项目配置变更。")
            item = confirm_config_change(store=self.store, identity=identity, text=text)
            if item is None:
                return RouteResult(handled=True, reply="当前没有等待你确认的配置变更。")
            clear_conversation_state(self.store, identity)
            return RouteResult(handled=True, reply=confirmation_reply(item))
        if is_config_cancel(text):
            if identity.role != "boss":
                return RouteResult(handled=True, reply="只有金总/老板账号可以取消配置变更。")
            item = cancel_config_change(store=self.store, identity=identity, text=text)
            if item is None:
                return RouteResult(handled=True, reply="当前没有等待取消的配置变更。")
            clear_conversation_state(self.store, identity)
            return RouteResult(handled=True, reply=f"已取消配置变更：{item.get('id')}。")
        if not is_config_change_request(text):
            return None
        if identity.role != "boss":
            return RouteResult(handled=True, reply="这属于身份、交接或项目配置变更，必须由金总/老板账号发起并确认。")
        item = build_config_change_proposal(store=self.store, identity=identity, text=text)
        reply = proposal_reply(item)
        state_type = (
            "pending_teacher_handoff_confirm"
            if str(item.get("kind") or "") == "teacher_handover"
            else "pending_config_change_confirm"
        )
        remember_conversation_state(
            self.store,
            identity,
            state_type=state_type,
            last_system_prompt=reply,
            expected_replies=["确认执行", "取消执行"],
            payload={"change_id": str(item.get("id") or ""), "kind": str(item.get("kind") or "")},
            source_handler="config_changes",
        )
        return RouteResult(handled=True, reply=reply)

    def _route_staff_config(self, identity: UserIdentity, text: str) -> RouteResult | None:
        compact = compact_conversation_text(text)
        if is_staff_config_query(text):
            if identity.role == "boss" or self._is_summer_manager(identity.role, identity.canonical_user_id):
                return RouteResult(handled=True, reply=staff_roster_reply(self.store))
            return RouteResult(handled=True, reply="暑假班人员名单仅金总和2026暑假班店长可以查看。")

        if compact == "继续配置暑假班老师":
            if identity.role != "boss":
                return RouteResult(handled=True, reply="只有金总/老板账号可以继续人员配置。")
            proposal = refresh_latest_staff_config_proposal(self.store, identity)
            if proposal is None:
                return RouteResult(handled=True, reply="当前没有可继续匹配的暑假班人员方案，请重新发送老师名单。")
        elif is_staff_config_request(text):
            if identity.role != "boss":
                return RouteResult(
                    handled=True,
                    reply="人员与项目权限只能由金总/老板账号配置；暑假班店长可以查看名单，但不能配置全局权限。",
                )
            proposal = build_staff_config_proposal(self.store, identity, text)
            if not proposal.get("staff_candidates"):
                return RouteResult(
                    handled=True,
                    reply="我识别到你要配置暑假班人员，但没有读到具体老师。请按“张老师，语文老师；李老师，数学老师”发送。",
                )
        elif compact in {"确认配置", "取消配置"}:
            return RouteResult(handled=True, reply="当前没有等待确认的人员配置方案，请先发送暑假班老师名单。")
        else:
            return None

        reply = staff_config_proposal_reply(proposal)
        remember_conversation_state(
            self.store,
            identity,
            state_type="pending_staff_config_confirm",
            last_system_prompt=reply,
            expected_replies=["确认配置", "取消配置"],
            payload={"proposal_id": str(proposal.get("id") or ""), "program_id": SUMMER_PROGRAM_ID},
            source_handler="staff_config",
            ttl_minutes=24 * 60,
        )
        return RouteResult(handled=True, reply=reply)

    def _route_conversation_state(self, identity: UserIdentity, text: str) -> RouteResult | None:
        state = load_conversation_state(self.store, identity, include_expired=True)
        if not isinstance(state, dict):
            return None
        if is_state_expired(state):
            if is_short_followup_reply(text):
                clear_conversation_state(self.store, identity)
                return RouteResult(handled=True, reply="该确认已过期，请重新发起操作。")
            clear_conversation_state(self.store, identity)
            return None
        state_type = str(state.get("state_type") or "")
        staff_selection = state_type == "pending_staff_config_confirm" and is_staff_candidate_selection(text)
        if state_type not in {"pending_weekly_feedback_review", "pending_weekly_feedback_child_review"} and not matches_expected_reply(text, state) and not staff_selection:
            return None

        compact = compact_conversation_text(text)
        if state_type == "pending_staff_config_confirm":
            if identity.role != "boss":
                return RouteResult(handled=True, reply="这条人员配置确认只对发起方案的金总/老板账号有效。")
            payload = state.get("payload") if isinstance(state.get("payload"), dict) else {}
            proposal_id = str(payload.get("proposal_id") or "")
            if is_staff_config_cancel(text):
                result = cancel_staff_config(self.store, identity, proposal_id)
                clear_conversation_state(self.store, identity)
                if not result.get("ok"):
                    return RouteResult(handled=True, reply="当前没有可取消的人员配置方案。")
                return RouteResult(handled=True, reply=f"已取消人员配置方案：{proposal_id}。未写入任何权限。")
            if is_staff_candidate_selection(text):
                result = select_staff_candidate(self.store, identity, proposal_id, text)
                if not result.get("ok"):
                    return RouteResult(handled=True, reply="没有找到对应候选人，请按方案中的姓名和序号选择，例如“张老师选1”。")
                proposal = result["proposal"]
                reply = staff_config_proposal_reply(proposal)
                remember_conversation_state(
                    self.store,
                    identity,
                    state_type="pending_staff_config_confirm",
                    last_system_prompt=reply,
                    expected_replies=["确认配置", "取消配置"],
                    payload={"proposal_id": proposal_id, "program_id": SUMMER_PROGRAM_ID},
                    source_handler="staff_config",
                    ttl_minutes=24 * 60,
                )
                return RouteResult(handled=True, reply=reply)
            result = confirm_staff_config(self.store, identity, proposal_id)
            if not result.get("ok"):
                error = str(result.get("error") or "")
                if error == "proposal_expired":
                    clear_conversation_state(self.store, identity)
                    return RouteResult(handled=True, reply="该人员配置确认已过期，请重新发送暑假班老师名单。")
                if error == "proposal_not_ready":
                    return RouteResult(handled=True, reply="方案中仍有同名人员未选择，暂不能执行配置。")
                clear_conversation_state(self.store, identity)
                return RouteResult(handled=True, reply="当前没有可执行的人员配置方案。")
            clear_conversation_state(self.store, identity)
            applied = result.get("applied") or []
            return RouteResult(
                handled=True,
                reply=(
                    f"人员配置已确认并写入 2026暑假班权限，共配置{len(applied)}人。\n"
                    "未匹配人员仍保留在待添加名单中，没有伪造 userid，也没有改动托管班人员。"
                ),
            )

        if state_type in {"pending_config_change_confirm", "pending_teacher_handoff_confirm"}:
            if identity.role != "boss":
                return RouteResult(handled=True, reply="这条确认只对发起该操作的老板账号有效。")
            if compact in {"取消", "取消执行", "取消变更", "先不执行"}:
                item = cancel_config_change(store=self.store, identity=identity, text=text)
                clear_conversation_state(self.store, identity)
                if item is None:
                    return RouteResult(handled=True, reply="当前没有等待取消的配置变更。")
                return RouteResult(handled=True, reply=f"已取消配置变更：{item.get('id')}。")
            item = confirm_config_change(store=self.store, identity=identity, text=text)
            clear_conversation_state(self.store, identity)
            if item is None:
                return RouteResult(handled=True, reply="当前没有等待你确认的配置变更。")
            return RouteResult(handled=True, reply=confirmation_reply(item))

        if state_type == "pending_student_duplicate_confirm":
            if not self._is_summer_manager(identity.role, identity.canonical_user_id):
                return RouteResult(handled=True, reply="只有老板或暑假班店长可以确认疑似重复学生。")
            payload = state.get("payload") if isinstance(state.get("payload"), dict) else {}
            issue_id = str(payload.get("issue_id") or "")
            if compact in {"关联", "关联为同一个学生", "1"}:
                action = "link_existing"
            elif compact in {"新建", "创建", "创建为新学生", "2"}:
                action = "create_new"
            elif compact in {"跳过", "暂不导入", "3"}:
                action = "skip"
            else:
                return None
            result = resolve_duplicate_issue(
                self.store,
                issue_id,
                action=action,
                actor_userid=identity.canonical_user_id,
                existing_student_name=str(payload.get("existing_student_name") or payload.get("candidate_student_name") or ""),
            )
            clear_conversation_state(self.store, identity)
            if not result.get("ok"):
                return RouteResult(handled=True, reply=f"疑似重复学生处理失败：{result.get('error') or '未找到待处理项'}。")
            issue = result.get("issue") if isinstance(result.get("issue"), dict) else {}
            status_label = {
                "linked_existing": "已关联为同一个学生",
                "created_new_student": "已创建为新学生",
                "skipped": "已暂不导入",
            }.get(str(issue.get("status") or ""), "已处理")
            student = str(issue.get("resolved_student_name") or (issue.get("import_student") or {}).get("student_name") or "")
            return RouteResult(handled=True, reply=f"{status_label}：{student}。")

        if state_type == "pending_unregistered_student_confirm":
            if not self._is_summer_manager(identity.role, identity.canonical_user_id):
                return RouteResult(handled=True, reply="只有老板或2026暑假班店长可以处理未入库学生。")
            payload = state.get("payload") if isinstance(state.get("payload"), dict) else {}
            student_name = str(payload.get("student_name") or "这个孩子")
            if compact in {"暂不补录", "跳过"}:
                clear_conversation_state(self.store, identity)
                return RouteResult(handled=True, reply=f"已将{student_name}保留在待确认名单中，本次不补录。")
            clear_conversation_state(self.store, identity)
            return RouteResult(
                handled=True,
                reply=(
                    f"已接上刚才的未入库学生：{student_name}。\n"
                    "正式补录前请提供年级、暑假班分组和家长联系电话；信息不足时只保留待确认，不会直接建档。"
                ),
            )

        if state_type in {"pending_weekly_feedback_review", "pending_weekly_feedback_child_review"}:
            payload = state.get("payload") if isinstance(state.get("payload"), dict) else {}
            student_name = str(payload.get("student_name") or "这个孩子")
            if compact == "简短生成":
                clear_conversation_state(self.store, identity)
                return RouteResult(handled=True, reply=f"已为{student_name}生成简短反馈草稿，请继续核对后再发送。")
            if compact in {"跳过", "下一个"}:
                clear_conversation_state(self.store, identity)
                return RouteResult(handled=True, reply=f"已跳过{student_name}，继续处理下一份反馈。")
            if compact.startswith("确认") or compact == "可以" or "发给家长" in compact or "没问题" in compact:
                period_type = str(payload.get("period_type") or "")
                if period_type in {"weekly", "monthly", "semester", "graduation"} and student_name != "这个孩子":
                    clear_conversation_state(self.store, identity)
                    return self._approve_growth_report(identity, student_name, period_type)
                clear_conversation_state(self.store, identity)
                return RouteResult(handled=True, reply=f"已确认{student_name}这份反馈。")
            return RouteResult(handled=True, reply=f"已按你的要求调整{student_name}这份反馈草稿，本次只影响这个孩子。")

        if state_type == "pending_task_next_choice":
            payload = state.get("payload") if isinstance(state.get("payload"), dict) else {}
            task_id = str(payload.get("task_id") or "")
            if task_id:
                clear_conversation_state(self.store, identity)
                return self._start_task_by_id(identity, task_id)
            return None

        if state_type == "pending_program_record_confirm":
            payload = state.get("payload") if isinstance(state.get("payload"), dict) else {}
            if compact in {"取消", "先不记录"}:
                clear_conversation_state(self.store, identity)
                return RouteResult(handled=True, reply="已取消本次记录。")
            original = str(payload.get("raw_text") or "").strip()
            source_meta = payload.get("source_meta") if isinstance(payload.get("source_meta"), dict) else {}
            if compact in {"暑假班", "记录到暑假班"}:
                selected = "暑假班"
            elif compact in {"托管班", "记录到托管班"}:
                selected = "托管班"
            else:
                return None
            clear_conversation_state(self.store, identity)
            return self._route_record(identity, f"{selected}：{original}", source_meta=source_meta)

        clear_conversation_state(self.store, identity)
        return RouteResult(handled=True, reply="已收到确认。")

    def _is_summer_manager(self, role: str, userid: str) -> bool:
        """判断是否为暑假班店长或老板（复用 summer_enrollment 的逻辑）。"""
        from .summer_enrollment import _is_summer_manager as _summer_mgr_check
        return _summer_mgr_check(role, userid, self.store)

    def _summer_manager_notification_targets(self) -> list[str]:
        whitelist = self.store.read_json("wecom_whitelist.json", {})
        if not isinstance(whitelist, dict):
            return []
        targets: list[str] = []
        
        # 优先使用独立的暑假班店长列表
        summer_managers = whitelist.get("summer_manager_ids", [])
        if isinstance(summer_managers, list) and len(summer_managers) > 0:
            targets.extend(str(item) for item in summer_managers if str(item))
        else:
            # 如果没有配置，fallback 到原有逻辑
            roles = whitelist.get("user_roles") or {}
            if isinstance(roles, dict):
                targets.extend(
                    str(user_id)
                    for user_id, role in roles.items()
                    if str(role) == "manager"
                )
            targets.extend(str(item) for item in whitelist.get("manager_ids") or [] if str(item))
        
        # 始终包含老板
        if not targets:
            targets.extend(str(item) for item in whitelist.get("super_users") or [] if str(item))
            roles = whitelist.get("user_roles") or {}
            if isinstance(roles, dict):
                targets.extend(
                    str(user_id)
                    for user_id, role in roles.items()
                    if str(role) in {"boss", "super_admin"}
                )
        return sorted(set(targets))

    def _route_unknown_summer_student_record(
        self,
        identity: UserIdentity,
        text: str,
    ) -> RouteResult | None:
        if identity.role not in {"teacher", "manager"} or not is_summer_operator(self.store, identity):
            return None
        student_name = guess_unknown_student_name(text)
        if not student_name:
            return None
        pending = remember_unknown_summer_record(
            self.store,
            student_name=student_name,
            text=text,
            teacher_userid=identity.canonical_user_id,
            teacher_name=identity.person_name,
            source="summer_teacher_record",
        )
        notifications = [
            {
                "touser": target,
                "content": (
                    f"检测到未入库学生：{student_name}。\n"
                    f"来源：{identity.person_name or identity.platform_user_id}课节记录。\n"
                    "请确认是否补录为暑假班学生。"
                ),
                "task_id": str(pending.get("id") or ""),
                "action": "summer_unknown_student_detected",
            }
            for target in self._summer_manager_notification_targets()
        ]
        for target in self._summer_manager_notification_targets():
            remember_conversation_state_for_user(
                self.store,
                user_id=target,
                role="manager",
                state_type="pending_unregistered_student_confirm",
                last_system_prompt=(
                    f"检测到未入库学生：{student_name}。来源：{identity.person_name or identity.platform_user_id}课节记录。"
                    "请确认是否补录为暑假班学生。"
                ),
                expected_replies=["补录这个孩子", "暂不补录"],
                payload={
                    "issue_id": str(pending.get("id") or ""),
                    "student_name": student_name,
                    "teacher_userid": identity.canonical_user_id,
                    "program_id": SUMMER_PROGRAM_ID,
                },
                source_handler="summer_unknown_student",
                ttl_minutes=24 * 60,
            )
        return RouteResult(
            handled=True,
            reply=(
                f"未找到“{student_name}”的暑假班学生档案。\n\n"
                "本条已暂存为待确认记录，请联系暑假班店长确认是否补录该学生。"
            ),
            notifications=notifications,
        )

    def _task_completion_notifications(
        self,
        task: dict[str, Any],
        identity: UserIdentity,
    ) -> list[dict[str, Any]]:
        level = str(task.get("level") or "C")
        task_type = str(task.get("type") or "")
        source_type = str(task.get("source_type") or "")
        should_notify = level == "S" or (
            level == "A"
            and (
                source_type == "manual_assignment"
                or task_type in {"parent_anxiety", "parent_complaint", "renewal_risk", "academic_issue"}
            )
        )
        if not should_notify:
            return []
        whitelist = self.store.read_json("wecom_whitelist.json", {})
        boss_ids: list[str] = []
        if isinstance(whitelist, dict):
            boss_ids.extend(str(item) for item in whitelist.get("super_users") or [] if str(item))
            roles = whitelist.get("user_roles") or {}
            if isinstance(roles, dict):
                boss_ids.extend(
                    str(user_id)
                    for user_id, role in roles.items()
                    if str(role) in {"boss", "super_admin"}
                )
        boss_ids = sorted(set(boss_ids))
        if not boss_ids:
            return []
        evidence = str(task.get("closure_summary") or task.get("evidence_summary") or "")
        followup = ""
        fields = task.get("closure_fields")
        if isinstance(fields, dict):
            followup = str(fields.get("followup_plan") or fields.get("next_step") or "")
        dashboard_line = ""
        base_url = str(os.getenv("HERMES_TUOGUAN_DASHBOARD_BASE_URL") or "").strip()
        if base_url:
            try:
                boss_identity = UserIdentity(
                    platform=identity.platform,
                    platform_user_id=boss_ids[0],
                    canonical_user_id=boss_ids[0],
                    person_name="老板",
                    role="boss",
                    approval_state="approved",
                )
                token = sign_dashboard_token(boss_identity, self.store)
                dashboard_line = f"\n老板看板：{base_url.rstrip()}/tuoguan/dashboard?token={quote(token, safe='')}"
            except Exception:
                dashboard_line = f"\n老板看板：{base_url.rstrip()}/tuoguan/dashboard"
        content = (
            f"【任务完成】{task.get('title', '未命名任务')}\n"
            f"学生：{task.get('student_name') or '未关联学生'}\n"
            f"执行老师：{identity.person_name or identity.platform_user_id}\n"
            f"等级：{level}\n"
            f"闭环结果：{evidence[:180] or '已完成'}\n"
            f"后续安排：{followup or '见闭环记录'}"
            f"{dashboard_line}"
        )
        return [
            {
                "task_id": str(task.get("id") or ""),
                "role": "boss",
                "touser": boss_id,
                "action": "task_completed",
                "content": content,
            }
            for boss_id in boss_ids
        ]

    @staticmethod
    def _open_tasks_for_user(tasks: list[dict[str, Any]], user_id: str) -> list[dict[str, Any]]:
        return [
            task
            for task in tasks
            if task.get("assignee_userid") == user_id
            and task.get("status") not in _CLOSED_STATUSES
        ]

    def _task_contexts(self) -> dict[str, Any]:
        data = self.store.read_json(_ACTIVE_TASK_CONTEXT_FILE, {})
        return data if isinstance(data, dict) else {}

    def _remember_active_task_context(
        self,
        identity: UserIdentity,
        task: dict[str, Any],
        *,
        candidates: list[dict[str, Any]] | None = None,
        ttl_minutes: int = 30,
    ) -> None:
        now = datetime.now()
        row = {
            "user_id": identity.canonical_user_id,
            "task_id": str(task.get("id") or ""),
            "student_id": str(task.get("student_id") or task.get("student_name") or ""),
            "student_name": str(task.get("student_name") or ""),
            "task_type": str(task.get("type") or ""),
            "status": "processing" if str(task.get("status") or "") == "active" else "selected",
            "started_at": now.isoformat(timespec="seconds"),
            "expires_at": (now + timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds"),
            "candidate_task_ids": [str(item.get("id") or "") for item in (candidates or []) if item.get("id")],
        }

        def update_contexts(contexts: Any) -> dict[str, Any]:
            contexts = contexts if isinstance(contexts, dict) else {}
            contexts[identity.canonical_user_id] = row
            return contexts

        self.store.update_json(_ACTIVE_TASK_CONTEXT_FILE, {}, update_contexts)

    def _clear_active_task_context(self, identity: UserIdentity) -> None:
        def clear_context(contexts: Any) -> Any:
            contexts = contexts if isinstance(contexts, dict) else {}
            if identity.canonical_user_id not in contexts:
                return JSON_NO_CHANGE
            contexts.pop(identity.canonical_user_id, None)
            return contexts

        self.store.update_json(_ACTIVE_TASK_CONTEXT_FILE, {}, clear_context)

    def _pending_next_context(self, identity: UserIdentity) -> dict[str, Any] | None:
        contexts = self.store.read_json(_PENDING_NEXT_TASK_FILE, {})
        if not isinstance(contexts, dict):
            return None
        item = contexts.get(identity.canonical_user_id)
        if not isinstance(item, dict):
            return None
        expires_at = self._parse_datetime(item.get("expires_at"))
        if expires_at and expires_at < datetime.now():
            def expire_context(existing: Any) -> Any:
                existing = existing if isinstance(existing, dict) else {}
                if identity.canonical_user_id not in existing:
                    return JSON_NO_CHANGE
                existing.pop(identity.canonical_user_id, None)
                return existing

            self.store.update_json(_PENDING_NEXT_TASK_FILE, {}, expire_context)
            return None
        return item

    def _remember_pending_next_task_context(
        self,
        identity: UserIdentity,
        tasks: list[dict[str, Any]],
    ) -> None:
        next_task = current_task_for_user(tasks, identity.canonical_user_id)
        if not next_task:
            self._clear_pending_next_task_context(identity)
            return
        self._remember_pending_next_task_for_user(
            identity.canonical_user_id,
            next_task,
            source="next_task_prompt",
        )

    def _remember_pending_next_task_for_user(
        self,
        user_id: str,
        task: dict[str, Any],
        *,
        source: str,
        ttl_minutes: int = 30,
    ) -> None:
        now = datetime.now()
        row = {
            "user_id": user_id,
            "task_id": str(task.get("id") or ""),
            "task_title": str(task.get("title") or ""),
            "task_level": str(task.get("level") or "C"),
            "task_type": str(task.get("type") or ""),
            "student_id": str(task.get("student_id") or task.get("student_name") or ""),
            "student_name": str(task.get("student_name") or ""),
            "source": source,
            "trigger_words": ["继续", "开始", "处理", "1", "开始下一个", "处理下一个"],
            "created_at": now.isoformat(timespec="seconds"),
            "expires_at": (now + timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds"),
        }

        def update_contexts(contexts: Any) -> dict[str, Any]:
            contexts = contexts if isinstance(contexts, dict) else {}
            contexts[user_id] = row
            return contexts

        self.store.update_json(_PENDING_NEXT_TASK_FILE, {}, update_contexts)
        remember_conversation_state_for_user(
            self.store,
            user_id=user_id,
            role=self._role_for_user_id(user_id),
            state_type="pending_task_next_choice",
            last_system_prompt=f"当前还有一个{task.get('level', 'C')}级任务待处理：{task.get('title', '未命名任务')}。回复“继续”即可开始。",
            expected_replies=["继续", "开始", "处理", "1", "开始下一个", "处理下一个"],
            payload={
                "task_id": str(task.get("id") or ""),
                "task_title": str(task.get("title") or ""),
                "task_level": str(task.get("level") or "C"),
                "student_name": str(task.get("student_name") or ""),
                "source": source,
            },
            source_handler="tasks",
            ttl_minutes=ttl_minutes,
        )

    def _clear_pending_next_task_context(self, identity: UserIdentity) -> None:
        def clear_context(contexts: Any) -> Any:
            contexts = contexts if isinstance(contexts, dict) else {}
            if identity.canonical_user_id not in contexts:
                return JSON_NO_CHANGE
            contexts.pop(identity.canonical_user_id, None)
            return contexts

        self.store.update_json(_PENDING_NEXT_TASK_FILE, {}, clear_context)

    def _role_for_user_id(self, user_id: str) -> str:
        whitelist = self.store.read_json("wecom_whitelist.json", {})
        if not isinstance(whitelist, dict):
            return ""
        roles = whitelist.get("user_roles") if isinstance(whitelist.get("user_roles"), dict) else {}
        return str(roles.get(user_id) or ("boss" if user_id in (whitelist.get("super_users") or []) else ""))

    def _clear_pending_student_confirm_context(self, identity: UserIdentity) -> None:
        for file_name in _PENDING_STUDENT_CONTEXT_FILES:
            contexts = self.store.read_json(file_name, {})
            if not isinstance(contexts, dict):
                continue
            if identity.canonical_user_id in contexts:
                contexts.pop(identity.canonical_user_id, None)
                self.store.write_json(file_name, contexts)

    def _active_task_from_context(
        self,
        identity: UserIdentity,
        open_tasks: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        contexts = self._task_contexts()
        item = contexts.get(identity.canonical_user_id)
        if not isinstance(item, dict):
            return None
        expires_at = self._parse_datetime(item.get("expires_at"))
        if expires_at and expires_at < datetime.now():
            contexts.pop(identity.canonical_user_id, None)
            self.store.write_json(_ACTIVE_TASK_CONTEXT_FILE, contexts)
            return None
        task_id = str(item.get("task_id") or "")
        if not task_id:
            return None
        task = next((task for task in open_tasks if str(task.get("id") or "") == task_id), None)
        if task is None or str(task.get("status") or "") in _CLOSED_STATUSES:
            contexts.pop(identity.canonical_user_id, None)
            self.store.write_json(_ACTIVE_TASK_CONTEXT_FILE, contexts)
            return None
        return task

    def _refresh_dashboard_cache_best_effort(self) -> None:
        try:
            self.store.write_json(CACHE_FILE, build_dashboard_snapshot(self.store))
        except Exception:
            pass

    def _teacher_task_list_reply(self, tasks: list[dict[str, Any]], identity: UserIdentity) -> str:
        open_tasks = self._open_tasks_for_user(tasks, identity.canonical_user_id)
        if not open_tasks:
            return "你当前暂无待处理任务。"
        ordered = sorted(open_tasks, key=self._supervisor_task_key)
        show_all_b = str(getattr(self, "_current_compact_command", "") or "") == "更多B级任务"
        high_tasks = [task for task in ordered if str(task.get("level") or "C") in {"S", "A"}]
        b_tasks = [task for task in ordered if str(task.get("level") or "C") == "B"]
        other_tasks = [task for task in ordered if str(task.get("level") or "C") not in {"S", "A", "B"}]
        visible_b = b_tasks if show_all_b or len(b_tasks) <= 5 else b_tasks[:5]
        visible = high_tasks + visible_b + other_tasks
        lines = [f"【待处理 — {len(ordered)}个】"]
        for task in visible:
            assignee = str(task.get("assigned_by_name") or task.get("source_label") or "")
            due = str(task.get("due_at") or "").replace("T", " ")
            detail = "，".join(part for part in (assignee, due) if part)
            suffix = f"（{detail}）" if detail else ""
            lines.append(f"{task.get('level', 'C')}级：{task.get('title', '未命名任务')}{suffix}")
        hidden_b = len(b_tasks) - len(visible_b)
        if hidden_b > 0:
            lines.append(f"还有 {hidden_b} 个 B 级任务，回复“更多B级任务”查看全部。")
        return "\n".join(lines)

    def _selected_task_from_reply(
        self,
        identity: UserIdentity,
        open_tasks: list[dict[str, Any]],
        text: str,
    ) -> dict[str, Any] | None:
        compact = str(text or "").replace(" ", "")
        match = re.fullmatch(r"(?:处理|选|选择|第)?([1-9])(?:个|条|号|任务)?", compact)
        if not match:
            return None
        index = int(match.group(1)) - 1
        contexts = self._task_contexts()
        item = contexts.get(identity.canonical_user_id)
        candidate_ids = []
        if isinstance(item, dict):
            candidate_ids = [str(task_id) for task_id in item.get("candidate_task_ids") or []]
        candidates = [task for task in open_tasks if str(task.get("id") or "") in candidate_ids] if candidate_ids else open_tasks
        ordered = sorted(candidates, key=self._supervisor_task_key)
        if 0 <= index < len(ordered):
            return ordered[index]
        return None

    def _task_selection_reply(self, open_tasks: list[dict[str, Any]]) -> str:
        ordered = sorted(open_tasks, key=self._supervisor_task_key)
        lines = [f"你当前有 {len(ordered)} 个待处理任务："]
        for index, task in enumerate(ordered[:8], 1):
            lines.append(f"{index}. {task.get('level', 'C')}级：{task.get('title', '未命名任务')}")
        if any(str(task.get("level") or "") == "S" for task in ordered):
            lines.append("建议先处理 S级。回复“处理1”或“处理2”选择。")
        else:
            lines.append("回复“处理1”或“处理2”选择。")
        return "\n".join(lines)

    @staticmethod
    def _looks_like_task_evidence(text: str, task: dict[str, Any]) -> bool:
        compact = str(text or "").replace(" ", "")
        if str(task.get("type") or "") == "safety_incident":
            return any(
                word in compact
                for word in (
                    "安全闭环",
                    "孩子当前",
                    "当前状态",
                    "没有疼",
                    "出血",
                    "红肿",
                    "活动正常",
                    "情绪稳定",
                    "已采取处理",
                    "提醒",
                    "家长是否知情",
                    "已告知家长",
                    "家长表示知道",
                    "后续观察",
                    "继续观察",
                )
            )
        return any(word in compact for word in ("家长", "下一步", "已处理", "处理结果", "后续", "沟通"))

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        try:
            return datetime.fromisoformat(str(value or ""))
        except ValueError:
            return None

    def _route_admin_close_task(
        self,
        identity: UserIdentity,
        text: str,
    ) -> RouteResult | None:
        compact = str(text or "").replace(" ", "")
        if not any(word in compact for word in ("关闭", "强制关闭", "完成", "强制完成", "闭关", "关了", "关掉", "取消")):
            return None
        if not any(word in compact for word in ("任务", "S级", "A级", "安全", "闭环")):
            return None
        if any(word in compact.lower() for word in ("gateway", "wecom", "scheduler", "服务", "通道", "企业微信通道")) and "任务" not in compact:
            return None
        if identity.role not in {"boss", "manager"}:
            return RouteResult(handled=True, reply="只有老板或授权店长可以强制关闭任务。")
        tasks = self.store.load_tasks()
        candidates = self._find_admin_close_candidates(identity, tasks, text)
        if not candidates:
            return RouteResult(handled=True, reply="没有找到可关闭的具体任务，请写清楚学生、等级或任务标题。")
        if len(candidates) > 1:
            lines = ["找到多条任务，请回复更具体的学生、等级或标题："]
            for index, task in enumerate(candidates[:8], 1):
                lines.append(f"{index}. [{task.get('level', 'C')}级] {task.get('title', '未命名任务')} · {task.get('student_name') or '无学生'}")
            return RouteResult(handled=True, reply="\n".join(lines))
        task = candidates[0]
        reason = self._extract_admin_close_reason(text)
        if not reason:
            self._remember_pending_close_task(identity, task)
            return RouteResult(
                handled=True,
                reply=(
                    f"我已找到要关闭的任务：{task.get('title', '未命名任务')}。\n"
                    "还缺关闭原因。请直接回复原因，例如：本次为系统测试任务。"
                ),
            )
        return self._close_admin_task(identity, task, tasks, text, reason)

    def _route_pending_admin_close_task(
        self,
        identity: UserIdentity,
        text: str,
    ) -> RouteResult | None:
        item = self._pending_close_context(identity)
        if not item:
            return None
        if self._is_close_system_service_command(text):
            return None
        tasks = self.store.load_tasks()
        task_id = str(item.get("task_id") or "")
        task = next(
            (
                task
                for task in self._visible_supervisor_tasks(identity, tasks)
                if str(task.get("id") or "") == task_id
            ),
            None,
        )
        if task is None:
            self._clear_pending_close_task(identity)
            return None
        reason = self._extract_admin_close_reason(text) or self._reason_from_followup(text)
        if not reason:
            return RouteResult(handled=True, reply="关闭任务还缺原因，请直接回复：本次为系统测试任务，或说明为什么关闭。")
        result = self._close_admin_task(identity, task, tasks, text, reason)
        self._clear_pending_close_task(identity)
        return result

    def _close_admin_task(
        self,
        identity: UserIdentity,
        task: dict[str, Any],
        tasks: list[dict[str, Any]],
        text: str,
        reason: str,
    ) -> RouteResult:
        compact = str(text or "").replace(" ", "")
        now = datetime.now().isoformat(timespec="seconds")
        force_complete = "完成" in compact and "关闭" not in compact
        task["status"] = "completed_by_admin" if force_complete else "closed_by_admin"
        task["updated_at"] = now
        task["closed_at"] = now
        task["closed_by"] = identity.canonical_user_id
        task["closed_by_name"] = identity.person_name or identity.platform_user_id
        task["admin_close_reason"] = reason
        events = task.get("closure_events")
        if not isinstance(events, list):
            events = []
        events.append(
            {
                "at": now,
                "by": identity.canonical_user_id,
                "action": task["status"],
                "text": text,
                "missing_fields": closure_missing_fields(task, str(task.get("evidence_summary") or "")),
            }
        )
        task["closure_events"] = events
        self.store.save_tasks(tasks)
        self._suppress_pending_notifications_for_task(
            str(task.get("id") or ""),
            reason="task_closed_by_supervisor",
        )
        self._append_admin_task_close_event(task, identity, text, reason)
        self._append_task_closure_ledger(tasks, str(task.get("id") or ""))
        notification = {
            "task_id": str(task.get("id") or ""),
            "role": str(task.get("assignee_role") or "teacher"),
            "touser": str(task.get("assignee_userid") or ""),
            "action": task["status"],
            "content": (
                f"【任务已由管理端关闭】{task.get('title', '未命名任务')}\n"
                f"处理人：{identity.person_name or identity.platform_user_id}\n"
                f"原因：{reason}"
            ),
        }
        reply = (
            f"已关闭任务：{task.get('title', '未命名任务')}。\n"
            f"状态：{task['status']}\n"
            f"原因：{reason}\n"
            "已写入审计日志，并会通知执行老师。"
        )
        notifications = [notification] if notification["touser"] else []
        return RouteResult(handled=True, reply=reply, notifications=notifications)

    @staticmethod
    def _extract_admin_close_reason(text: str) -> str:
        raw = str(text or "").strip()
        for marker in ("原因：", "原因:", "原因，", "原因,", "原因是", "理由：", "理由:", "理由是"):
            if marker in raw:
                return raw.split(marker, 1)[1].strip(" ，,。")
        match = re.search(r"(本次.*|本回.*|这次.*|此次.*|因为.*|孩子.*|家长.*|后续.*)", raw)
        if match and any(word in raw for word in ("关闭", "关掉", "关了", "闭关", "强制关闭", "任务")):
            return match.group(1).strip(" ，,。")
        return ""

    @staticmethod
    def _reason_from_followup(text: str) -> str:
        raw = str(text or "").strip(" ，,。")
        if not raw:
            return ""
        if any(word in raw for word in ("gateway", "WECOM_CALLBACK", "白名单", "服务", "通道", "端口", "8646")):
            return ""
        return raw

    def _pending_close_context(self, identity: UserIdentity) -> dict[str, Any] | None:
        contexts = self.store.read_json(_PENDING_CLOSE_TASK_FILE, {})
        if not isinstance(contexts, dict):
            return None
        item = contexts.get(identity.canonical_user_id)
        if not isinstance(item, dict):
            return None
        expires_at = self._parse_datetime(item.get("expires_at"))
        if expires_at and expires_at < datetime.now():
            return None
        return item

    def _remember_pending_close_task(self, identity: UserIdentity, task: dict[str, Any]) -> None:
        now = datetime.now()
        contexts = self.store.read_json(_PENDING_CLOSE_TASK_FILE, {})
        if not isinstance(contexts, dict):
            contexts = {}
        contexts[identity.canonical_user_id] = {
            "user_id": identity.canonical_user_id,
            "task_id": str(task.get("id") or ""),
            "student_id": str(task.get("student_id") or task.get("student_name") or ""),
            "student_name": str(task.get("student_name") or ""),
            "task_type": str(task.get("type") or ""),
            "requested_action": "close_task",
            "missing_field": "reason",
            "created_at": now.isoformat(timespec="seconds"),
            "expires_at": (now + timedelta(minutes=30)).isoformat(timespec="seconds"),
        }
        self.store.write_json(_PENDING_CLOSE_TASK_FILE, contexts)

    def _clear_pending_close_task(self, identity: UserIdentity) -> None:
        contexts = self.store.read_json(_PENDING_CLOSE_TASK_FILE, {})
        if not isinstance(contexts, dict):
            return
        if identity.canonical_user_id in contexts:
            contexts.pop(identity.canonical_user_id, None)
            self.store.write_json(_PENDING_CLOSE_TASK_FILE, contexts)

    def _find_admin_close_candidates(
        self,
        identity: UserIdentity,
        tasks: list[dict[str, Any]],
        text: str,
    ) -> list[dict[str, Any]]:
        compact = str(text or "").replace(" ", "")
        try:
            student = recognize_student(text, self.store)
        except (UnknownStudentError, AmbiguousStudentError):
            student = ""
        visible = self._visible_supervisor_tasks(identity, tasks)
        candidates = []
        assignee_userid, _assignee_name = self._resolve_assignee(text)
        for task in visible:
            if student and str(task.get("student_name") or "") != student:
                continue
            if assignee_userid and str(task.get("assignee_userid") or "") != assignee_userid:
                continue
            level = str(task.get("level") or "")
            if "S级" in compact and level != "S":
                continue
            if "A级" in compact and level != "A":
                continue
            task_text = f"{task.get('title') or ''}{task.get('source_text') or ''}{task.get('student_name') or ''}"
            if student or level in {"S", "A"} or any(token and token in task_text for token in re.split(r"[，。,.；;：:\\s]+", str(text or ""))):
                candidates.append(task)
        if candidates:
            return sorted(candidates, key=self._supervisor_task_key)
        focus_candidate = self._focused_supervisor_task(identity, visible)
        if focus_candidate is not None:
            return [focus_candidate]
        if assignee_userid:
            assigned = [task for task in visible if str(task.get("assignee_userid") or "") == assignee_userid]
            if len(assigned) == 1:
                return assigned
            if assigned and any(word in compact for word in ("这个任务", "刚才", "刚安排", "上个任务", "闭关", "关了", "关掉")):
                return [sorted(assigned, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)[0]]
        if any(word in compact for word in ("这个任务", "刚才", "刚安排", "上个任务")):
            own = [
                task for task in visible
                if str(task.get("assigned_by") or task.get("created_by") or "") == identity.canonical_user_id
            ]
            if own:
                return [sorted(own, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)[0]]
        return []

    def _focused_supervisor_task(
        self,
        identity: UserIdentity,
        visible: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        focus = self.store.read_json("model_focus.json", {})
        if not isinstance(focus, dict):
            return None
        visible_by_id = {str(task.get("id") or ""): task for task in visible}
        candidates: list[tuple[str, dict[str, Any]]] = []
        for key, item in focus.items():
            if not isinstance(item, dict) or identity.canonical_user_id not in str(key):
                continue
            task_id = str(item.get("task_id") or "")
            if task_id in visible_by_id:
                candidates.append((str(item.get("updated_at") or ""), visible_by_id[task_id]))
        if not candidates:
            return None
        return sorted(candidates, key=lambda row: row[0], reverse=True)[0][1]

    def _append_admin_task_close_event(
        self,
        task: dict[str, Any],
        identity: UserIdentity,
        text: str,
        reason: str,
    ) -> None:
        ledger = self.store.read_json(_ADMIN_TASK_CLOSE_FILE, [])
        if not isinstance(ledger, list):
            ledger = []
        ledger.append(
            {
                "task_id": str(task.get("id") or ""),
                "task_title": str(task.get("title") or ""),
                "student_name": str(task.get("student_name") or ""),
                "level": str(task.get("level") or ""),
                "status": str(task.get("status") or ""),
                "by": identity.canonical_user_id,
                "by_name": identity.person_name or identity.platform_user_id,
                "at": datetime.now().isoformat(timespec="seconds"),
                "reason": reason,
                "source_text": text,
            }
        )
        self.store.write_json(_ADMIN_TASK_CLOSE_FILE, ledger[-1000:])

    def _teacher_mapping(self) -> dict[str, str]:
        mapping = self.store.read_json("teacher_wecom_map.json", {})
        return mapping if isinstance(mapping, dict) else {}

    def _role_for_user(self, user_id: str) -> str:
        whitelist = self.store.read_json("wecom_whitelist.json", {})
        if not isinstance(whitelist, dict):
            return "teacher"
        roles = whitelist.get("user_roles") or {}
        if isinstance(roles, dict):
            return str(roles.get(user_id) or "teacher")
        return "teacher"

    def _resolve_assignee(self, text: str) -> tuple[str, str]:
        compact = str(text or "").replace(" ", "")
        candidates: list[tuple[int, str, str]] = []
        for name, user_id in self._teacher_mapping().items():
            label = str(name).strip()
            uid = str(user_id).strip()
            if not label or not uid:
                continue
            aliases = {label}
            if label.endswith("老师") and len(label) > 2:
                aliases.add(label[:-2])
            if label.endswith("店长") and len(label) > 2:
                aliases.add(label[:-2])
            for alias in aliases:
                if alias and alias in compact:
                    candidates.append((len(alias), label, uid))
        if not candidates:
            return "", ""
        _score, label, user_id = sorted(candidates, reverse=True)[0]
        return user_id, label

    @staticmethod
    def _manual_task_level(text: str) -> str:
        compact = str(text or "").replace(" ", "").upper()
        for level in ("S", "A", "B", "C"):
            if f"{level}级" in compact or f"{level}任务" in compact or f"{level}类" in compact:
                return level
        if any(word in compact for word in ("安全", "受伤", "投诉", "重大", "紧急")):
            return "A"
        return "B"

    @staticmethod
    def _manual_task_type(text: str) -> str:
        compact = str(text or "").replace(" ", "")
        if any(word in compact for word in ("安全", "受伤", "摔", "磕", "碰", "流血", "发烧", "不舒服")):
            return "safety_incident"
        if "投诉" in compact:
            return "parent_complaint"
        if "续费" in compact or "流失" in compact:
            return "renewal_risk"
        if any(word in compact for word in ("家长", "沟通", "妈妈", "爸爸")):
            return "parent_anxiety"
        return "manual_assignment"

    @staticmethod
    def _manual_due_at(text: str, *, level: str) -> str:
        return parse_business_due_at(text, level=level, allow_default=True)

    @staticmethod
    def _manual_task_title(text: str, student_name: str, task_type: str) -> str:
        compact = str(text or "").strip()
        cleaned = re.sub(r"[SABCＳＡＢＣ]\s*级", "", compact, flags=re.IGNORECASE)
        cleaned = re.sub(r"(今天|今日|今晚|明天|明日|后天)(?:前|之前)?", "", cleaned)
        cleaned = re.sub(r"(安排|派给|交给|指派|布置|下发|让|请).{0,8}?老师", "", cleaned)
        cleaned = cleaned.strip(" ，,。；;：:")
        if cleaned:
            return cleaned[:60]
        if task_type == "parent_anxiety" and student_name:
            return f"跟进{student_name}家长沟通"
        if task_type == "safety_incident" and student_name:
            return f"闭环{student_name}安全事项"
        if task_type == "renewal_risk" and student_name:
            return f"跟进{student_name}续费风险"
        return f"完成{student_name}跟进任务" if student_name else "完成手动安排任务"

    def _is_manual_assignment_text(self, text: str) -> bool:
        compact = str(text or "").replace(" ", "")
        if not compact:
            return False
        if not any(word in compact for word in _MANUAL_ASSIGNMENT_WORDS):
            return False
        if any(word in compact for word in ("工资闭环", "安排任务", "下发任务", "指派任务", "布置任务")):
            return True
        if "任务" in compact and any(word in compact for word in ("老师", "店长", "跟进", "沟通", "处理", "完成")):
            return True
        assignee_userid, _assignee_name = self._resolve_assignee(text)
        return bool(assignee_userid and any(word in compact for word in ("跟进", "沟通", "处理", "完成")))

    def _route_manual_task_assignment(
        self,
        identity: UserIdentity,
        text: str,
    ) -> RouteResult | None:
        if not self._is_manual_assignment_text(text):
            return None
        if not self.permissions.can_manage_tasks(identity):
            return RouteResult(handled=True, reply="只有店长或老板可以给老师安排工资闭环任务。")

        assignee_userid, assignee_name = self._resolve_assignee(text)
        if not assignee_userid:
            return RouteResult(handled=True, reply="请写清楚安排给哪位老师，例如：安排刘老师今天跟进小金家长沟通。")

        try:
            student_name = recognize_student(text, self.store)
        except (UnknownStudentError, AmbiguousStudentError):
            student_name = ""
        if student_name and not self.permissions.can_view_student(identity, student_name):
            return RouteResult(handled=True, reply=f"你没有权限给学生“{student_name}”安排任务。")

        students = self.store.read_json("students.json", {})
        student_profile = students.get(student_name, {}) if isinstance(students, dict) else {}
        assignee_role = self._role_for_user(assignee_userid)
        explicit_program, _program_reason = resolve_record_program(self.store, identity, text)
        if student_name:
            candidate_programs = student_program_ids(student_profile)
            if explicit_program in candidate_programs:
                program_id = explicit_program
            elif len(candidate_programs) == 1:
                program_id = next(iter(candidate_programs))
            else:
                program_id = explicit_program or REGULAR_PROGRAM_ID
        else:
            assignee_scope = user_program_ids(
                self.store,
                UserIdentity("wecom", assignee_userid, assignee_userid, assignee_name, assignee_role, "approved"),
            )
            program_id = next(iter(assignee_scope)) if assignee_scope and len(assignee_scope) == 1 else (explicit_program or REGULAR_PROGRAM_ID)
        level = self._manual_task_level(text)
        task_type = self._manual_task_type(text)
        stamp = datetime.now().isoformat(timespec="seconds")
        due_at = self._manual_due_at(text, level=level)
        title = self._manual_task_title(text, student_name, task_type)
        source_label = "老板安排" if identity.role == "boss" else "店长安排"
        task = {
            "id": f"task_manual_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}",
            "title": title,
            "student_name": student_name,
            "source_text": text,
            "source_type": "manual_assignment",
            "source_label": source_label,
            "assigned_by": identity.canonical_user_id,
            "created_by": identity.canonical_user_id,
            "assigned_by_name": identity.person_name or identity.platform_user_id,
            "created_by_name": identity.person_name or identity.platform_user_id,
            "assigned_by_role": identity.role,
            "created_by_role": identity.role,
            "assigned_at": stamp,
            "type": task_type,
            "level": level,
            "status": "pending",
            "assignee_userid": assignee_userid,
            "assignee_name": assignee_name,
            "assignee_role": assignee_role,
            "campus_id": str((student_profile or {}).get("campus_id") or "main"),
            "program_id": canonical_program_id(program_id),
            "created_at": stamp,
            "updated_at": stamp,
            "due_at": due_at,
            "evidence_required": True,
            "task_contract": build_task_contract(
                title=title,
                source_text=text,
                student_name=student_name,
                due_at=due_at,
                business_goal=title,
                assignee_user_id=assignee_userid,
                assignee_name=assignee_name,
                assigned_by_user_id=identity.canonical_user_id,
                assigned_by_role=identity.role,
            ),
            "source_meta": {
                "kind": "manual_assignment",
                "raw_text": text,
                "actor_userid": identity.canonical_user_id,
                "actor_role": identity.role,
            },
        }
        tasks = self.store.load_tasks()
        tasks.append(task)
        self.store.save_tasks(tasks)
        assignee_identity = UserIdentity(
            platform=identity.platform,
            platform_user_id=assignee_userid,
            canonical_user_id=assignee_userid,
            person_name=assignee_name,
            role=assignee_role,
            approval_state="approved",
        )
        self._remember_active_task_context(
            assignee_identity,
            task,
            candidates=[],
            ttl_minutes=36 * 60,
        )
        self._remember_pending_next_task_for_user(
            assignee_userid,
            task,
            source="new_task_notification",
            ttl_minutes=36 * 60,
        )
        self._refresh_dashboard_cache_best_effort()
        criteria = [
            str(value)
            for value in (task.get("task_contract") or {}).get("success_criteria") or []
            if str(value)
        ]
        notification = {
            "task_id": task["id"],
            "role": assignee_role,
            "touser": assignee_userid,
            "action": "manual_assignment",
            "content": (
                f"【{level}级任务安排】{title}\n"
                f"来源：{source_label}\n"
                f"安排人：{task['assigned_by_name']}\n"
                f"学生：{student_name or '无指定学生'}\n"
                f"截止：{due_at.replace('T', ' ')}\n"
                "回复“开始”后，小优会结合当前任务陪你一步一步处理；不会做或不知道怎么说时可以直接问。\n"
                + ("完成时需要说明：" + "；".join(criteria[:4]) if criteria else "完成时请补齐处理动作、结果和下一步。")
            ),
        }
        reply = (
            f"已安排{level}级任务给{assignee_name}：{title}。\n"
            f"截止：{due_at.replace('T', ' ')}\n"
            "我已通知执行人，后续闭环会进入任务证据和工作记录。"
        )
        return RouteResult(handled=True, reply=reply, notifications=[notification])

    def _append_task_closure_ledger(
        self,
        tasks: list[dict[str, Any]],
        task_id: str,
    ) -> None:
        if not task_id:
            return
        task = next((item for item in tasks if str(item.get("id") or "") == task_id), None)
        if not task:
            return
        events = task.get("closure_events")
        if not isinstance(events, list) or not events:
            return
        latest = events[-1]
        if not isinstance(latest, dict):
            return
        ledger = self.store.read_json("task_closure_events.json", [])
        if not isinstance(ledger, list):
            ledger = []
        entry = {
            **latest,
            "task_id": task_id,
            "task_title": str(task.get("title") or ""),
            "student_name": str(task.get("student_name") or ""),
            "task_type": str(task.get("type") or ""),
            "level": str(task.get("level") or "C"),
            "assignee_userid": str(task.get("assignee_userid") or ""),
            "source_type": str(task.get("source_type") or ""),
            "source_label": str(task.get("source_label") or ""),
            "assigned_by": str(task.get("assigned_by") or ""),
        }
        fingerprint = (
            entry.get("task_id"),
            entry.get("at"),
            entry.get("by"),
            entry.get("action"),
        )
        existing = {
            (
                item.get("task_id"),
                item.get("at"),
                item.get("by"),
                item.get("action"),
            )
            for item in ledger
            if isinstance(item, dict)
        }
        if fingerprint in existing:
            return
        ledger.append(entry)
        self.store.write_json("task_closure_events.json", ledger[-1000:])

    def _route_command(
        self,
        identity: UserIdentity,
        text: str,
    ) -> RouteResult | None:
        compact = text.strip()
        compact_no_space = compact.replace(" ", "")
        if compact in {"你是谁", "你现在是谁", "身份", "你的身份"}:
            role_name = {
                "teacher": "老师",
                "manager": "管理人员",
                "boss": "老板",
            }.get(identity.role, "已授权用户")
            user_name = identity.person_name or identity.platform_user_id
            return RouteResult(
                handled=True,
                reply=(
                    "我是优益托管正在使用的 Hermes 工作助手。"
                    "学生、任务、记录、日报和提醒都由本机原 Hermes 处理，"
                    "服务器只负责企业微信回调转发。\n"
                    f"你当前识别为：{user_name}（{role_name}权限）。"
                ),
            )
        if self._is_usage_help_request(compact_no_space):
            reply = self._usage_help_reply(identity)
            return RouteResult(handled=True, reply=reply)
        if compact in _DASHBOARD_COMMANDS:
            return self._route_dashboard_command(identity, compact)
        if self._is_close_system_service_command(compact):
            if identity.role != "boss":
                return RouteResult(handled=True, reply="关闭 gateway 或企业微信通道属于系统操作，请联系老板账号确认。")
            return RouteResult(
                handled=True,
                reply=(
                    "你说的是系统服务关闭，不是业务任务关闭。\n"
                    "为避免误停服务，我不会仅凭一句话关闭 gateway。请在服务器上执行明确的运维命令，"
                    "或说明要关闭 gateway / 企业微信通道 / scheduler 哪一项。"
                ),
            )
        growth = self._route_growth_report_command(identity, compact)
        if growth is not None:
            return growth
        growth_context = self._route_growth_report_context_confirmation(identity, compact)
        if growth_context is not None:
            return growth_context
        if compact in {"状态", "运行状态"}:
            try:
                from cron.jobs import list_jobs

                active_jobs = len(list_jobs(include_disabled=False))
            except Exception:
                active_jobs = 0
            return RouteResult(
                handled=True,
                reply=build_status(
                    self.store,
                    gateway_running=True,
                    callback_connected=True,
                    model_name="Hermes 当前配置模型",
                    active_jobs=active_jobs,
                    active_sessions=0,
                ),
            )
        if compact in {"日报", "今日日报", "任务日报"}:
            return RouteResult(
                handled=True,
                reply=build_role_report(self.store.load_tasks(), identity, self.store),
            )
        if compact in {"任务", "我的任务", "当前任务", "待办", "任务列表", "我的所有任务", "全部任务", "所有任务", "更多B级任务"}:
            if identity.role in {"manager", "boss"}:
                tasks = self.store.load_tasks()
                report = build_role_report(tasks, identity, self.store)
                focus = self._focused_supervisor_task(identity, tasks)
                if focus is not None:
                    self._remember_supervisor_task(identity, focus)
                    report = "\n\n".join(
                        [
                            report,
                            self._supervisor_task_focus_reply(focus),
                        ]
                    )
                return RouteResult(
                    handled=True,
                    reply=report,
                )
            tasks = self.store.load_tasks()
            open_tasks = self._open_tasks_for_user(tasks, identity.canonical_user_id)
            task = self._active_task_from_context(identity, open_tasks)
            if compact in {"任务列表", "我的所有任务", "全部任务", "所有任务", "更多B级任务"} or task is None:
                self._current_compact_command = compact
                try:
                    reply = self._teacher_task_list_reply(tasks, identity)
                finally:
                    self._current_compact_command = ""
                return RouteResult(handled=True, reply=reply)
            return RouteResult(
                handled=True,
                reply=(
                    f"【当前正在处理】{task.get('title', '未命名任务')}\n"
                    f"等级：{task.get('level', 'C')}\n"
                    "回复：开始 / 稍后 / 帮我写 / 完成了"
                ),
            )
        return None

    @staticmethod
    def _is_usage_help_request(compact: str) -> bool:
        if compact in {"帮助", "怎么用", "如何使用", "使用说明", "能做什么", "教程", "操作教程", "使用教程"}:
            return True
        help_words = ("怎么用", "如何使用", "能怎么用", "可以怎么用", "能做什么", "使用教程", "操作教程", "教我用")
        system_words = ("Hermes", "hermes", "系统", "这个系统", "功能")
        return any(word in compact for word in help_words) and any(word in compact for word in system_words)

    @staticmethod
    def _usage_help_reply(identity: UserIdentity) -> str:
        if identity.role == "teacher":
            return (
                "【老师使用教程】\n"
                "1. 记录孩子：直接说孩子姓名 + 具体表现 + 老师处理。\n"
                "例：小金今天数学作业完成较慢，计算错4道，我让他订正后明天继续关注。\n"
                "2. 生活/行为记录：午餐、午休、情绪、纪律也可以直接记录。\n"
                "例：李四今天午休有点坐不住，我提醒后能安静下来。\n"
                "3. 安全情况：摔倒、磕碰、夹手、不舒服要马上说清状态和处理。\n"
                "例：安全记录：李四手指被门缝夹了一下，目前无破皮出血，已提醒并继续观察。\n"
                "4. 处理任务：回复“我的任务”查看；回复“开始/处理1”；补充处理情况后回复“完成了”。\n"
                "5. 看工资和看板：回复“看板”。\n"
                "6. 撤销误记：回复“撤销上一条记录”。"
            )
        if identity.role == "manager":
            return (
                "【店长使用教程】\n"
                "1. 看当天重点：回复“看板”或“我的任务”。\n"
                "2. 跟进老师：查看未完成任务、低质量记录、缺家长沟通和风险闭环。\n"
                "3. 安排任务：可以说“安排刘老师明天前跟进小金午休状态，A级”。\n"
                "4. 处理安全/服务风险：收到 S/A 级提醒后督促老师补齐状态、处理、家长知情、后续观察。\n"
                "5. 暑假班导入问题：电话待补、疑似重复、未入库孩子需要店长确认。\n"
                "6. 不确定时：回复“帮助”随时看这份教程。"
            )
        return (
            "【老板使用教程】\n"
            "1. 看经营全局：回复“老板看板”。\n"
            "2. 安排任务：例“安排李老师明天前跟进李四午休状态，A级”。\n"
            "3. 看任务进展：回复“任务”或“我的所有任务”。\n"
            "4. 身份/交接/配置：说清变更内容后，Hermes 会先生成方案；你回复“确认执行”才会进入执行。\n"
            "5. 暑假班名单：老板或店长批量导入，疑似重复/缺电话会进入待确认。\n"
            "6. 工资和绩效：看板里看预估、缺项、排除记录；最终仍由老板确认。\n"
            "7. 普通聊天：直接问就行；只有明确业务指令时，托管系统才会介入。"
        )

    @staticmethod
    def _is_close_system_service_command(text: str) -> bool:
        compact = str(text or "").replace(" ", "").lower()
        if not any(word in compact for word in ("关闭", "停止", "停掉", "关掉", "重启")):
            return False
        return any(word in compact for word in ("gateway", "wecom", "scheduler", "服务", "进程", "企业微信通道", "端口", "8646"))

    def _route_growth_report_command(
        self,
        identity: UserIdentity,
        compact: str,
    ) -> RouteResult | None:
        parsed = self._parse_growth_report_command(compact)
        if parsed is None:
            return None
        action, student_name, period_type = parsed
        if not student_name:
            return RouteResult(handled=True, reply="请带上学生姓名，例如：生成张三周报告。")
        if not self.permissions.can_view_student(identity, student_name):
            return RouteResult(handled=True, reply=f"你没有权限生成或确认学生“{student_name}”的家长报告。")
        if action == "generate":
            return self._generate_growth_report(identity, student_name, period_type)
        return self._approve_growth_report(identity, student_name, period_type)

    def _generate_growth_report(
        self,
        identity: UserIdentity,
        student_name: str,
        period_type: str,
    ) -> RouteResult:
        scope = user_program_ids(self.store, identity)
        if scope == {SUMMER_PROGRAM_ID}:
            program_id = SUMMER_PROGRAM_ID
        else:
            profile = self.store.read_json("students.json", {}).get(student_name, {})
            available = student_program_ids(profile if isinstance(profile, dict) else {})
            program_id = SUMMER_PROGRAM_ID if available == {SUMMER_PROGRAM_ID} else REGULAR_PROGRAM_ID
        try:
            draft = build_growth_report_draft(
                self.store,
                student_name=student_name,
                period_days=_GROWTH_PERIOD_DAYS[period_type],
                period_type=period_type,
                program_id=program_id,
            )
        except ValueError as exc:
            return RouteResult(handled=True, reply=str(exc))
        if int(draft.get("record_count") or 0) <= 0:
            return RouteResult(
                handled=True,
                reply=(
                    f"{student_name}当前周期内还没有可用记录，暂不生成空报告。\n"
                    "建议老师先补充：学习状态、作业完成、近期进步、需要家长配合的点。"
                ),
            )
        item = save_growth_report_draft(
            self.store,
            draft,
            actor=identity.canonical_user_id,
        )
        remember_growth_report_context(
            self.store,
            user_id=identity.canonical_user_id,
            report=item,
        )
        preview = [
            f"已生成{student_name}{_GROWTH_PERIOD_LABELS[period_type]}草稿，先不发给家长。",
            f"报告 id：{item.get('id')}",
            "",
            "【家长可读预览】",
            str(item.get("parent_draft") or "").strip(),
            "",
            f"老师核对无误后回复：确认{student_name}{_GROWTH_PERIOD_LABELS[period_type]}",
        ]
        reply = "\n".join(preview)
        remember_conversation_state(
            self.store,
            identity,
            state_type="pending_weekly_feedback_child_review" if period_type == "weekly" else "pending_graduation_report_review",
            last_system_prompt=reply,
            expected_replies=[f"确认{student_name}{_GROWTH_PERIOD_LABELS[period_type]}", "确认", "修改", "跳过", "重新生成"],
            payload={
                "report_id": str(item.get("id") or ""),
                "student_name": student_name,
                "period_type": period_type,
            },
            source_handler="growth_reports",
            ttl_minutes=24 * 60,
        )
        return RouteResult(handled=True, reply=reply)

    def _route_growth_report_context_confirmation(
        self,
        identity: UserIdentity,
        compact: str,
    ) -> RouteResult | None:
        if not self._is_growth_context_confirmation(compact):
            return None
        report = latest_growth_report_context(
            self.store,
            user_id=identity.canonical_user_id,
        )
        if report is None:
            if not self._mentions_growth_share(compact):
                return None
            pending = pending_growth_reports_for_actor(
                self.store,
                actor=identity.canonical_user_id,
            )
            if len(pending) > 1:
                names = [
                    f"{item.get('student_name')}{_GROWTH_PERIOD_LABELS.get(str(item.get('period_type') or ''), '报告')}"
                    for item in pending[:5]
                ]
                return RouteResult(
                    handled=True,
                    reply="你最近有多份待确认报告，请带上学生名确认：" + "、".join(names),
                )
            if len(pending) == 1:
                report = pending[0]
            else:
                return RouteResult(handled=True, reply="请先生成某个孩子的周/月/学期报告，再确认发给家长。")
        student_name = str(report.get("student_name") or "")
        period_type = str(report.get("period_type") or "monthly")
        if not self.permissions.can_view_student(identity, student_name):
            return RouteResult(handled=True, reply=f"你没有权限确认学生“{student_name}”的家长报告。")
        return self._approve_growth_report(identity, student_name, period_type)

    def _approve_growth_report(
        self,
        identity: UserIdentity,
        student_name: str,
        period_type: str,
    ) -> RouteResult:
        report = latest_growth_report(
            self.store,
            student_name=student_name,
            period_type=period_type,
        )
        if not report:
            return RouteResult(
                handled=True,
                reply=f"没有找到{student_name}{_GROWTH_PERIOD_LABELS[period_type]}草稿，请先发送：生成{student_name}{_GROWTH_PERIOD_LABELS[period_type]}。",
            )
        if report.get("status") != "approved":
            report = approve_growth_report(
                self.store,
                report_id=str(report.get("id") or ""),
                actor=identity.canonical_user_id,
            )
        if not report:
            return RouteResult(handled=True, reply="报告审核失败，请稍后重试。")
        base_url = str(os.getenv("HERMES_TUOGUAN_DASHBOARD_BASE_URL") or "").strip()
        if not base_url:
            return RouteResult(
                handled=True,
                reply=(
                    "报告已确认，但家长 H5 外部访问地址尚未配置。\n"
                    "请先设置 HERMES_TUOGUAN_DASHBOARD_BASE_URL。"
                ),
            )
        try:
            token = sign_parent_report_token(
                self.store,
                report_id=str(report.get("id") or ""),
                student_name=student_name,
            )
        except DashboardAuthError:
            return RouteResult(handled=True, reply="报告已确认，但生成家长链接失败，请联系管理员检查。")
        mark_growth_report_shared(self.store, report_id=str(report.get("id") or ""))
        expiry_dt = parent_report_token_expiry_datetime()
        short = create_parent_report_short_link(
            self.store,
            report_id=str(report.get("id") or ""),
            token=token,
            expires_at=expiry_dt,
            created_by=identity.canonical_user_id,
        )
        url = f"{base_url.rstrip('/')}/tuoguan/r/{quote(str(short.get('code') or ''), safe='')}"
        expiry = expiry_dt.strftime("%Y-%m-%d %H:%M")
        title = str(report.get("share_title") or f"{student_name}{_GROWTH_PERIOD_LABELS[period_type]}")
        summary = str(report.get("parent_summary") or "老师已整理本期阶段成长反馈。")
        return RouteResult(
            handled=True,
            reply=(
                "下面这段可以直接复制到微信发给家长：\n\n"
                f"【优益托管｜{title}】\n"
                f"{summary}\n\n"
                f"点击查看孩子本期成长反馈：\n{url}\n\n"
                f"链接有效期至 {expiry}。"
            ),
        )

    def _parse_growth_report_command(self, compact: str) -> tuple[str, str, str] | None:
        text = compact.replace(" ", "")
        action = ""
        explicit_command = False
        if text.startswith(("生成", "出", "做")) and "报告" in text:
            action = "generate"
            explicit_command = True
            for prefix in ("生成", "出", "做"):
                if text.startswith(prefix):
                    text = text[len(prefix):]
                    break
        elif text.startswith(("确认", "审核通过", "通过")) and "报告" in text:
            action = "approve"
            explicit_command = True
            for prefix in ("审核通过", "确认", "通过"):
                if text.startswith(prefix):
                    text = text[len(prefix):]
                    break
        else:
            action = self._infer_growth_report_action(text)
            if not action:
                return None
        period_type = ""
        period_text = ""
        for alias, value in sorted(_GROWTH_PERIOD_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
            if alias in text:
                period_type = value
                period_text = alias
                break
        if not period_type:
            return None
        student_name = ""
        try:
            student_name = recognize_student(text, self.store)
        except (UnknownStudentError, AmbiguousStudentError):
            if explicit_command:
                student_name = (
                    text.replace(period_text, "", 1)
                    .replace("成长报告", "")
                    .replace("报告", "")
                    .replace("成长反馈", "")
                    .strip()
                )
            elif action:
                return action, "", period_type
            else:
                return None
        return action, student_name, period_type

    @staticmethod
    def _infer_growth_report_action(text: str) -> str:
        if not any(word in text for word in _GROWTH_REPORT_WORDS):
            return ""
        has_parent_intent = any(word in text for word in _GROWTH_PARENT_WORDS)
        wants_generate = any(word in text for word in _GROWTH_GENERATE_WORDS)
        wants_approve = any(word in text for word in _GROWTH_APPROVE_WORDS)
        if wants_approve and has_parent_intent:
            return "approve"
        if wants_generate and (has_parent_intent or any(word in text for word in ("周报", "月报", "学期报告"))):
            return "generate"
        if wants_approve and any(word in text for word in ("报告", "反馈", "总结")):
            return "approve"
        return ""

    @staticmethod
    def _is_growth_context_confirmation(text: str) -> bool:
        compact = str(text or "").replace(" ", "")
        if not compact:
            return False
        exact = {
            "可以",
            "可以了",
            "行",
            "没问题",
            "无误",
            "确认",
            "通过",
            "发吧",
            "发家长",
            "转家长",
        }
        if compact in exact:
            return True
        return any(word in compact for word in _GROWTH_APPROVE_WORDS) and any(
            word in compact
            for word in (
                "发家长",
                "转家长",
                "发给家长",
                "给家长",
                "给妈妈",
                "给爸爸",
                "家长看",
                "妈妈看",
                "爸爸看",
            )
        )

    @staticmethod
    def _mentions_growth_share(text: str) -> bool:
        compact = str(text or "").replace(" ", "")
        return any(word in compact for word in _GROWTH_REPORT_WORDS) or any(
            word in compact
            for word in (
                "发家长",
                "转家长",
                "发给家长",
                "给妈妈",
                "给爸爸",
                "家长看",
            )
        )

    def _route_learning_review(
        self,
        identity: UserIdentity,
        text: str,
    ) -> RouteResult | None:
        compact = str(text or "").strip()
        normalized = compact.replace(" ", "")
        if normalized in {"待学习", "学习审核", "学习候选", "待审核学习"}:
            if identity.role not in {"manager", "boss"}:
                return RouteResult(handled=True, reply="只有店长或老板可以查看待审核学习候选。")
            items = list_pending_learning_candidates(self.store)
            if not items:
                return RouteResult(handled=True, reply="当前没有待审核学习候选。")
            lines = [f"当前有 {len(items)} 条待审核学习候选："]
            for index, item in enumerate(items[:8], 1):
                lines.append(
                    f"{index}. {item.get('id')}｜{item.get('title') or '未命名'}｜"
                    f"{item.get('category') or 'general'}"
                )
                content = str(item.get("content") or "").strip()
                if content:
                    lines.append(f"   {content[:80]}")
            lines.append("老板可回复：批准学习 <id>，或：驳回学习 <id>。")
            return RouteResult(handled=True, reply="\n".join(lines))
        if normalized.startswith("批准学习") or normalized.startswith("驳回学习"):
            if identity.role != "boss":
                return RouteResult(handled=True, reply="只有老板可以批准或驳回学习候选。")
            decision = "approve" if normalized.startswith("批准学习") else "reject"
            candidate_id = (
                compact.replace("批准学习", "", 1)
                .replace("驳回学习", "", 1)
                .strip()
            )
            if not candidate_id:
                return RouteResult(handled=True, reply="请带上候选 id，例如：批准学习 learn_20260619120000_1")
            try:
                item = review_learning_candidate(
                    self.store,
                    candidate_id=candidate_id,
                    decision=decision,
                    reviewer=identity.canonical_user_id,
                )
            except ValueError:
                return RouteResult(handled=True, reply=f"没有找到学习候选：{candidate_id}")
            if item.get("status") == "approved":
                return RouteResult(handled=True, reply=f"已批准学习候选：{item.get('title') or candidate_id}。")
            return RouteResult(handled=True, reply=f"已驳回学习候选：{item.get('title') or candidate_id}。")
        feedback_triggers = (
            "这个不是安全事件",
            "这个不算安全事件",
            "这个应该是S级",
            "这个应该算S级",
            "这个闭环不对",
            "这个判断不对",
            "这个误判了",
            "以后这种说法也算",
        )
        if any(trigger in compact for trigger in feedback_triggers):
            if identity.role not in {"manager", "boss"}:
                return RouteResult(handled=True, reply="这类规则反馈需要店长或老板提交。")
            category = "general"
            candidate_type = "rule_suggestion"
            if "安全事件" in compact or "S级" in compact:
                category = "safety"
            if "闭环" in compact:
                category = "closure"
                candidate_type = "closure_phrase"
            if "不是" in compact or "不算" in compact or "误判" in compact:
                candidate_type = "false_positive"
            item = submit_learning_candidate(
                self.store,
                source="wecom_feedback",
                category=category,
                title="托管误判/新说法反馈",
                content=compact,
                candidate_type=candidate_type,
                proposed_triggers=[],
                created_by=identity.canonical_user_id,
                evidence=[
                    {
                        "platform_user_id": identity.platform_user_id,
                        "role": identity.role,
                    }
                ],
            )
            return RouteResult(
                handled=True,
                reply=(
                    "已收到规则反馈，先放入待审核学习队列，老板批准前不会改变正式规则。\n"
                    f"候选 id：{item.get('id')}"
                ),
            )
        return None

    def _route_operations_focus(
        self,
        identity: UserIdentity,
        text: str,
    ) -> RouteResult | None:
        if not looks_like_operations_focus(text):
            return None
        if identity.role != "boss":
            return RouteResult(handled=True, reply="经营重点只能由老板账号调整。")
        draft = classify_operations_focus(text, identity)
        if draft is None:
            return None
        item = save_operations_focus(self.store, draft, identity)
        try:
            refresh_dashboard_cache(self.store)
        except Exception:
            pass
        return RouteResult(handled=True, reply=operations_focus_reply(item))

    def _route_dashboard_command(
        self,
        identity: UserIdentity,
        compact: str,
    ) -> RouteResult:
        if compact in {"老板看板", "管理看板", "数据看板"} and identity.role == "teacher":
            return RouteResult(handled=True, reply="老师账号只能打开老师端看板。")
        base_url = str(os.getenv("HERMES_TUOGUAN_DASHBOARD_BASE_URL") or "").strip()
        if not base_url:
            return RouteResult(
                handled=True,
                reply=(
                    "托管看板外部访问地址尚未配置。\n"
                    "请先设置 HERMES_TUOGUAN_DASHBOARD_BASE_URL，"
                    "配置公网访问地址后我会发送你的专属签名看板链接。"
                ),
            )
        try:
            refresh_dashboard_cache(self.store)
        except Exception:
            pass
        try:
            token = sign_dashboard_token(identity, self.store)
        except DashboardAuthError:
            return RouteResult(handled=True, reply="当前账号暂时不能生成看板链接，请联系管理员确认权限。")
        url = f"{base_url.rstrip('/')}/tuoguan/dashboard?token={quote(token, safe='')}"
        role_label = {"teacher": "老师", "manager": "店长", "boss": "老板"}.get(identity.role, "用户")
        expiry = token_expiry_datetime().strftime("%Y-%m-%d %H:%M")
        return RouteResult(
            handled=True,
            reply=(
                f"这是你的{role_label}端托管 AI 看板链接：\n"
                f"{url}\n\n"
                f"链接有效期至 {expiry}。看板只读展示，记录和任务处理仍请回企业微信直接说。"
            ),
        )

    @staticmethod
    def _is_supervisor_ack(text: str) -> bool:
        compact = str(text or "").replace(" ", "")
        if not compact:
            return False
        exact = {
            "知道了",
            "我知道了",
            "收到",
            "已收到",
            "我收到了",
            "知晓",
            "已知晓",
            "我已知晓",
            "已关注",
            "我关注了",
            "不用再提醒我",
            "别推了",
            "停止提醒",
        }
        return compact in exact

    def _route_supervisor_ack(
        self,
        identity: UserIdentity,
        text: str,
    ) -> RouteResult | None:
        if identity.role not in {"manager", "boss"}:
            return None
        if not self._is_supervisor_ack(text):
            return None
        role = identity.role
        tasks = self.store.load_tasks()
        campuses: set[str] = set()
        if role == "manager":
            staff = self.store.read_json("staff.json", {})
            profile = (
                staff.get(identity.canonical_user_id, {})
                if isinstance(staff, dict)
                else {}
            )
            campuses = (
                {str(item) for item in profile.get("campus_ids") or []}
                if isinstance(profile, dict)
                else set()
            )
        open_s_tasks = [
            task
            for task in tasks
            if task.get("level") == "S"
            and task.get("status") not in _CLOSED_STATUSES
            and (role == "boss" or str(task.get("campus_id") or "") in campuses)
        ]
        if not open_s_tasks:
            return RouteResult(
                handled=True,
                reply="已收到。当前没有需要你确认的未完成S级任务。",
            )
        stamp = datetime.now().isoformat(timespec="seconds")
        changed = False
        for task in open_s_tasks:
            acknowledgements = task.get("supervisor_ack")
            if not isinstance(acknowledgements, dict):
                acknowledgements = {}
            if not isinstance(acknowledgements.get(role), dict):
                acknowledgements[role] = {
                    "by": identity.canonical_user_id,
                    "at": stamp,
                    "text": text,
                }
                task["supervisor_ack"] = acknowledgements
                task["updated_at"] = stamp
                changed = True
        if changed:
            self.store.save_tasks(tasks)
        role_label = "老板" if role == "boss" else "店长"
        return RouteResult(
            handled=True,
            reply=(
                f"{role_label}已确认知晓{len(open_s_tasks)}个S级任务，"
                "后续不会再向你重复推送这些任务。老师仍需完成安全闭环，"
                "任务不会因为本次确认而关闭。"
            ),
        )

    def _visible_supervisor_tasks(
        self,
        identity: UserIdentity,
        tasks: list[dict],
    ) -> list[dict]:
        if identity.role == "boss":
            return [
                task
                for task in tasks
                if task.get("status") not in _CLOSED_STATUSES
                and (
                    task.get("level") == "S"
                    or str(task.get("type") or "") in _KEY_TASK_TYPES
                    or str(task.get("source_type") or "") == "manual_assignment"
                    or str(task.get("type") or "") == "manual_assignment"
                    or str(task.get("assigned_by") or task.get("created_by") or "") == identity.canonical_user_id
                )
            ]
        if identity.role != "manager":
            return []
        staff = self.store.read_json("staff.json", {})
        profile = (
            staff.get(identity.canonical_user_id, {})
            if isinstance(staff, dict)
            else {}
        )
        campuses = (
            {str(item) for item in profile.get("campus_ids") or []}
            if isinstance(profile, dict)
            else set()
        )
        return [
            task
            for task in tasks
            if task.get("status") not in _CLOSED_STATUSES
            and str(task.get("campus_id") or "") in campuses
        ]

    @staticmethod
    def _supervisor_task_key(task: dict) -> tuple:
        try:
            due = datetime.fromisoformat(str(task.get("due_at") or ""))
        except ValueError:
            due = datetime.max
        status_order = {
            "waiting_confirmation": 0,
            "active": 1,
            "pending": 2,
            "deferred": 3,
        }
        return (
            _LEVEL_ORDER.get(str(task.get("level") or "C"), 3),
            status_order.get(str(task.get("status") or ""), 4),
            due,
            str(task.get("created_at") or ""),
        )

    def _suppress_pending_notifications_for_task(
        self,
        task_id: str,
        *,
        reason: str,
    ) -> None:
        if not task_id:
            return
        now = datetime.now().isoformat(timespec="seconds")

        def suppress(outbox: Any) -> Any:
            outbox = outbox if isinstance(outbox, list) else []
            changed = False
            for item in outbox:
                if not isinstance(item, dict):
                    continue
                if str(item.get("task_id") or "") != task_id:
                    continue
                status = str(item.get("status") or "")
                if status not in {"pending", "retry_pending", "sending"}:
                    continue
                if status == "sending":
                    item["status"] = "result_unknown"
                    item["result_unknown_at"] = now
                    item["last_error"] = f"{reason}_while_send_in_flight"
                else:
                    item["status"] = "suppressed"
                    item["suppressed_at"] = now
                    item["suppressed_reason"] = reason
                changed = True
            return outbox[-2000:] if changed else JSON_NO_CHANGE

        self.store.update_json("notification_outbox.json", [], suppress)

    def _record_legacy_router_coaching(
        self,
        identity: UserIdentity,
        task: dict[str, Any],
        result: Any,
        *,
        text: str,
    ) -> None:
        if identity.role != "teacher" or str(task.get("assignee_userid") or "") != identity.canonical_user_id:
            return
        from .teacher_coaching import record_teacher_coaching_event

        stamp = str(task.get("updated_at") or datetime.now().astimezone().isoformat(timespec="seconds"))
        record_teacher_coaching_event(
            self.store,
            task=task,
            teacher_user_id=identity.canonical_user_id,
            action=str(getattr(result, "action", "") or ""),
            operation_id=f"legacy-router:{task.get('id')}:{stamp}:{len(str(text or ''))}",
            missing_fields=closure_missing_fields(task, str(task.get("evidence_summary") or "")),
        )

    def _remember_supervisor_task(
        self,
        identity: UserIdentity,
        task: dict,
    ) -> None:
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            return
        context = self.store.read_json("task_context.json", {})
        if not isinstance(context, dict):
            context = {}
        context[identity.canonical_user_id] = {
            "task_id": task_id,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.store.write_json("task_context.json", context)

    def _remembered_supervisor_task(
        self,
        identity: UserIdentity,
        tasks: list[dict],
    ) -> dict | None:
        context = self.store.read_json("task_context.json", {})
        if not isinstance(context, dict):
            return None
        item = context.get(identity.canonical_user_id)
        if not isinstance(item, dict):
            return None
        task_id = str(item.get("task_id") or "")
        if not task_id:
            return None
        visible = self._visible_supervisor_tasks(identity, tasks)
        return next((task for task in visible if str(task.get("id") or "") == task_id), None)

    def _focused_supervisor_task(
        self,
        identity: UserIdentity,
        tasks: list[dict],
    ) -> dict | None:
        visible = self._visible_supervisor_tasks(identity, tasks)
        if not visible:
            return None
        return sorted(visible, key=self._supervisor_task_key)[0]

    def _supervisor_task_focus_reply(self, task: dict) -> str:
        return "\n".join(
            [
                "我先把最优先的任务接上：",
                self._supervisor_task_detail(task),
                "",
                "你现在可以直接回复：",
                "1. 我知道了：老板侧停止重复提醒。",
                "2. 催执行老师补齐缺口：我会告诉你当前应催哪一项。",
                "3. 继续/这个任务：继续查看这条任务进展。",
            ]
        )

    @staticmethod
    def _is_supervisor_task_reference(text: str) -> bool:
        compact = str(text or "").replace(" ", "")
        if not compact:
            return False
        triggers = (
            "开始办理任务",
            "办理任务",
            "处理任务",
            "跟进任务",
            "完成任务",
            "任务我弄好了",
            "任务已完成",
            "查看任务",
            "我的任务",
            "今日任务",
            "催执行老师",
            "补齐任务缺口",
            "S级任务",
            "s级任务",
            "A级任务",
            "a级任务",
            "B级任务",
            "b级任务",
            "这个任务",
            "该任务",
            "第几个任务",
            "第一个任务",
            "第一条任务",
        )
        return any(item in compact for item in triggers)

    @staticmethod
    def _supervisor_task_detail(task: dict) -> str:
        missing = closure_missing_fields(task, str(task.get("evidence_summary") or ""))
        labels = {
            "parent_attitude": "家长当前态度",
            "next_step": "下一步跟进安排",
            "parent_informed": "家长是否已知情",
            "child_status": "孩子当前状态",
            "action_taken": "已做处理",
            "follow_up_needed": "后续观察/跟进安排",
            "result": "处理结果和时间",
        }
        lines = [
            f"你说的是这条任务：{task.get('title', '未命名任务')}",
            f"等级：{task.get('level', 'C')}级",
            f"状态：{task.get('status', 'pending')}",
            f"执行老师：{task.get('assignee_userid') or '未指定'}",
        ]
        if task.get("source_text"):
            lines.append(f"原始记录：{task['source_text']}")
        if task.get("evidence_summary"):
            lines.append(f"老师已补充：{task['evidence_summary']}")
        if missing:
            lines.append(
                "当前缺口：" + "；".join(labels.get(item, item) for item in missing)
            )
        else:
            lines.append("当前闭环信息已经齐全，等待老师确认完成或系统关闭。")
        lines.extend(
            [
                "",
                "老板侧可以做两件事：",
                "1. 回复“我知道了”：停止向老板重复提醒，不关闭老师任务。",
                "2. 让执行老师补缺口：不是让你补发事情经过，当前应由老师补齐上面的闭环信息。",
            ]
        )
        if missing == ["child_status"]:
            lines.append("建议直接让老师回复：孩子现在状态正常，没有红肿也没有疼。")
        return "\n".join(lines)

    def _route_supervisor_task_reference(
        self,
        identity: UserIdentity,
        text: str,
    ) -> RouteResult | None:
        if identity.role not in {"manager", "boss"}:
            return None
        if not self._is_supervisor_task_reference(text):
            return None
        all_tasks = self.store.load_tasks()
        remembered = self._remembered_supervisor_task(identity, all_tasks)
        if remembered is not None:
            return RouteResult(
                handled=True,
                reply=self._supervisor_task_detail(remembered),
            )
        tasks = self._visible_supervisor_tasks(identity, all_tasks)
        if not tasks:
            return RouteResult(
                handled=True,
                reply="当前没有你权限范围内需要处理的未完成关键任务。",
            )
        s_tasks = [task for task in tasks if task.get("level") == "S"]
        candidates = s_tasks or tasks
        if len(candidates) > 1:
            lines = ["当前有多条关键任务，请回复编号："]
            for index, task in enumerate(candidates[:8], 1):
                lines.append(
                    f"{index}. [{task.get('level', 'C')}级] "
                    f"{task.get('title', '未命名任务')}"
                )
            return RouteResult(handled=True, reply="\n".join(lines))
        self._remember_supervisor_task(identity, candidates[0])
        return RouteResult(
            handled=True,
            reply=self._supervisor_task_detail(candidates[0]),
        )

    def _route_record(
        self,
        identity: UserIdentity,
        text: str,
        *,
        source_meta: dict[str, Any] | None = None,
    ) -> RouteResult:
        program_id, program_reason = resolve_record_program(self.store, identity, text)
        if not program_id:
            if program_reason == "forbidden":
                return RouteResult(handled=True, reply="你没有权限向这个项目写入记录。")
            prompt = (
                "这条记录的项目归属还不明确。当前托管班已进入只读保留范围，"
                "请回复“记录到暑假班”或“取消”。"
            )
            remember_conversation_state(
                self.store,
                identity,
                state_type="pending_program_record_confirm",
                last_system_prompt=prompt,
                expected_replies=["记录到暑假班", "取消"],
                payload={"raw_text": text, "source_meta": source_meta or {}},
                source_handler="program_scope",
            )
            return RouteResult(handled=True, reply=prompt)
        analysis = analyze_teacher_record(text, self.store)
        analysis["program_id"] = canonical_program_id(program_id)
        student_name = str(analysis["student_name"])
        record_types = set(analysis.get("record_types") or [])
        if not self._has_record_intent(text) and not (record_types & _AUTO_RECORD_TYPES):
            return RouteResult(
                handled=True,
                reply=(
                    f"我识别到这是关于{student_name}的内容，但没有明确写入正式记录。\n"
                    "为避免误记，我先不入库。如果要记录，请重新发送："
                    f"记录{student_name}：{text}"
                ),
            )
        if not self.permissions.can_write_student_record(identity, student_name):
            return RouteResult(
                handled=True,
                reply=f"你没有权限记录或查看学生“{student_name}”的资料。",
            )
        saved = save_analysis(
            analysis,
            identity.canonical_user_id,
            self.store,
            source_meta={**(source_meta or {}), "program_id": canonical_program_id(program_id)},
        )
        self._remember_last_record(identity, saved["record"])
        feedback = self._record_feedback(saved["record"])
        safety_reminders = safety_attention_reminders(self.store, student_name, text)
        if safety_reminders:
            feedback = f"{feedback}\n" + "\n".join(safety_reminders)
        task = saved["task"]
        if not task:
            summer_reply = (
                "\n已归入 2026暑假班，用于暑假班服务过程、学生反馈和结业汇报。"
                if canonical_program_id(program_id) == SUMMER_PROGRAM_ID
                else ""
            )
            return RouteResult(
                handled=True,
                reply=f"已记录{student_name}的情况。{summer_reply}\n{feedback}\n如误记，可回复“撤销上一条记录”。",
            )
        created_label = "已生成" if saved["created"] else "已更新"
        reply = (
            f"已记录{student_name}的情况，{created_label}"
            f"{task.get('level', 'C')}级任务：{task.get('title', '未命名任务')}。"
            f"\n{feedback}"
            "如误记，可回复“撤销上一条记录”。"
        )
        notifications: list[dict] = []
        if task.get("level") == "S":
            notifications = [
                asdict(item)
                for item in build_notification_plan([task], self.store)
            ]
        return RouteResult(
            handled=True,
            reply=reply,
            notifications=notifications,
        )

    @staticmethod
    def _record_type_label(record: dict[str, Any]) -> str:
        record_types = set(record.get("record_types") or [])
        if "safety_incident" in record_types:
            return "安全事件"
        if "parent_anxiety" in record_types or "parent_complaint" in record_types:
            return "家长沟通"
        if "meal_care" in record_types:
            return "午餐/生活照护"
        if "nap_care" in record_types:
            return "午休/行为表现"
        if "life_care" in record_types:
            return "生活照护"
        if "activity_care" in record_types:
            return "活动照护"
        if "behavior_observation" in record_types:
            return "行为表现"
        if "academic_issue" in record_types or "learning_habit" in record_types:
            return "学习"
        return "日常观察"

    def _record_feedback(self, record: dict[str, Any]) -> str:
        evaluation = record.get("record_evaluation")
        if not isinstance(evaluation, dict):
            evaluation = {}
        type_label = self._record_type_label(record)
        if item_program_id(record) == SUMMER_PROGRAM_ID:
            quality = str(evaluation.get("quality_level") or "valid")
            return f"类型：{type_label}；质量：{self._quality_label(quality, True)}；已作为暑假班服务证据保存。"
        eligible = bool(evaluation.get("payroll_eligible"))
        quality = str(evaluation.get("quality_level") or "low")
        quality_label = self._quality_label(quality, eligible)
        reasons = [str(item) for item in evaluation.get("reason_texts") or [] if str(item)]
        reason_codes = {str(item) for item in evaluation.get("reason_codes") or [] if str(item)}
        if eligible:
            score = evaluation.get("score") or 0
            line = f"类型：{type_label}；已计入绩效；质量：{quality_label}；本条{score}分。"
        else:
            reason_text = "；".join(reasons) if reasons else "内容还不够具体，暂不计入绩效"
            line = f"类型：{type_label}；已入档，但不计入绩效。原因：{reason_text}。"
        tip = str(evaluation.get("improvement_tip") or "")
        if not tip and not eligible:
            tip = self._record_improvement_tip(reason_codes)
        return f"{line}" + (f"\n建议：{tip}" if tip else "")

    @staticmethod
    def _quality_label(quality: str, eligible: bool) -> str:
        mapping = {
            "excellent": "优质记录",
            "quality": "优质记录",
            "valid": "有效记录",
            "normal": "普通记录",
            "regular": "普通记录",
            "low": "简单记录",
            "weak": "简单记录",
            "simple": "简单记录",
            "duplicate": "重复记录",
            "invalid": "未计绩效",
            "no_score": "未计绩效",
        }
        if not eligible and quality not in mapping:
            return "未计绩效"
        return mapping.get(quality, "普通记录")

    @staticmethod
    def _record_improvement_tip(reason_codes: set[str]) -> str:
        if reason_codes & {"duplicate_same_student_day", "duplicate_same_day_type", "similar_duplicate"}:
            return "同一学生同一天已有相似记录，本条不重复计入绩效。如后续有新的变化，比如晚餐表现、家长反馈或明天变化，可以继续补充。"
        tips: list[str] = []
        if "missing_teacher_action" in reason_codes:
            tips.append("补充老师当时如何提醒、引导或处理")
        if "missing_result" in reason_codes:
            tips.append("补充孩子后来的结果变化")
        if "missing_next_step" in reason_codes or "missing_followup" in reason_codes:
            tips.append("补充后续观察或下一步安排")
        return "；".join(tips) + "。" if tips else ""

    def _route_payroll_event(
        self,
        identity: UserIdentity,
        text: str,
    ) -> RouteResult | None:
        draft = classify_payroll_event(text, identity, self.store)
        if draft is None:
            return None
        event = append_payroll_event(draft, identity, self.store)
        return RouteResult(handled=True, reply=payroll_event_reply(event))

    def _route_payroll_review(
        self,
        identity: UserIdentity,
        text: str,
    ) -> RouteResult | None:
        review = classify_payroll_review(text, identity)
        if review is None:
            return None
        item = append_payroll_review(review, identity, self.store)
        return RouteResult(handled=True, reply=payroll_review_reply(item))

    def _route_payroll_settlement(
        self,
        identity: UserIdentity,
        text: str,
    ) -> RouteResult | None:
        if not classify_payroll_settlement(text, identity):
            return None
        settlement = create_payroll_settlement(self.store, identity)
        return RouteResult(handled=True, reply=payroll_settlement_reply(settlement))

    def _route_payroll_export(
        self,
        identity: UserIdentity,
        text: str,
    ) -> RouteResult | None:
        if not classify_payroll_export(text, identity):
            return None
        result = export_payroll_settlement(self.store, identity)
        return RouteResult(handled=True, reply=payroll_export_reply(result))

    def confirm_notifications_delivered(
        self,
        notifications: list[dict],
    ) -> None:
        actions = {
            str(item.get("task_id") or ""): str(item.get("action") or "")
            for item in notifications
            if item.get("task_id") and item.get("action")
        }
        if not actions:
            return
        tasks = self.store.load_tasks()
        stamp = datetime.now().isoformat(timespec="seconds")
        changed = False
        for task in tasks:
            task_id = str(task.get("id") or "")
            if task_id not in actions:
                continue
            task["last_escalation_action"] = actions[task_id]
            task["last_escalated_at"] = stamp
            task["escalation_count"] = int(task.get("escalation_count") or 0) + 1
            if actions[task_id] == "manual_assignment":
                assignee_userid = str(task.get("assignee_userid") or "")
                if assignee_userid:
                    self._remember_pending_next_task_for_user(
                        assignee_userid,
                        task,
                        source="new_task_notification",
                    )
            changed = True
        if changed:
            self.store.save_tasks(tasks)

    def _semantic_context(
        self,
        identity: UserIdentity,
        text: str,
        *,
        conversation_id: str,
    ) -> dict[str, Any]:
        tasks = self.store.load_tasks()
        open_tasks = self._open_tasks_for_user(tasks, identity.canonical_user_id)
        active_task = self._active_task_from_context(identity, open_tasks)
        current_task = current_task_for_user(tasks, identity.canonical_user_id)
        pending_next = self._pending_next_context(identity)
        pending_close = self._pending_close_context(identity)
        conversation_state = load_conversation_state(self.store, identity)
        growth_report_context = latest_growth_report_context(
            self.store,
            user_id=identity.canonical_user_id,
        )
        try:
            student_name = recognize_student(text, self.store)
        except (UnknownStudentError, AmbiguousStudentError):
            student_name = ""
        assignee_userid, assignee_name = self._resolve_assignee(text)
        program_scope = user_program_ids(self.store, identity)
        if identity.role == "boss":
            program_role = "global_owner"
        elif program_scope == {SUMMER_PROGRAM_ID}:
            program_role = "summer_manager" if identity.role == "manager" else "summer_teacher"
        else:
            program_role = "regular_manager" if identity.role == "manager" else "regular_teacher"
        recent_history = recent_message_history(
            self.store,
            canonical_user_id=identity.canonical_user_id,
            conversation_id=conversation_id,
            limit=12,
        )
        return {
            "active_task": active_task,
            "current_task": current_task,
            "pending_next_task": pending_next,
            "pending_close_task": pending_close,
            "conversation_state": conversation_state,
            "growth_report_context": growth_report_context,
            "open_tasks": open_tasks,
            "open_task_count": len(open_tasks),
            "student_name": student_name,
            "assignee_userid": assignee_userid,
            "assignee_name": assignee_name,
            "role": identity.role,
            "program_ids": sorted(program_scope) if program_scope is not None else ["global", REGULAR_PROGRAM_ID, SUMMER_PROGRAM_ID],
            "program_role": program_role,
            "conversation_id": conversation_id,
            "message_history": history_summary(recent_history),
        }

    @staticmethod
    def _semantic_context_summary(context: dict[str, Any]) -> dict[str, Any]:
        active = context.get("active_task") if isinstance(context.get("active_task"), dict) else None
        current = context.get("current_task") if isinstance(context.get("current_task"), dict) else None
        pending_next = context.get("pending_next_task") if isinstance(context.get("pending_next_task"), dict) else None
        pending = context.get("pending_close_task") if isinstance(context.get("pending_close_task"), dict) else None
        conversation_state = context.get("conversation_state") if isinstance(context.get("conversation_state"), dict) else None
        growth = context.get("growth_report_context") if isinstance(context.get("growth_report_context"), dict) else None
        return {
            "active_task_id": str(active.get("id") or "") if active else "",
            "active_task_type": str(active.get("type") or "") if active else "",
            "current_task_id": str(current.get("id") or "") if current else "",
            "current_task_type": str(current.get("type") or "") if current else "",
            "pending_next_task_id": str(pending_next.get("task_id") or "") if pending_next else "",
            "pending_close_task_id": str(pending.get("task_id") or "") if pending else "",
            "conversation_state": conversation_state_summary(conversation_state),
            "growth_report_id": str(growth.get("id") or "") if growth else "",
            "open_task_count": int(context.get("open_task_count") or 0),
            "student_name": str(context.get("student_name") or ""),
            "assignee_userid": str(context.get("assignee_userid") or ""),
            "program_ids": list(context.get("program_ids") or []),
            "program_role": str(context.get("program_role") or ""),
            "conversation_id": str(context.get("conversation_id") or ""),
            "message_history": list(context.get("message_history") or [])[-6:],
        }

    def _route_semantic_result(
        self,
        identity: UserIdentity,
        text: str,
        semantic: SemanticRouteResult,
        *,
        platform: str,
        sender_id: str,
        user_name: str,
        chat_id: str,
        message_id: str,
    ) -> tuple[RouteResult | None, str, str]:
        intent = semantic.intent
        if semantic.confidence < 0.75 and semantic.needs_clarification:
            return RouteResult(handled=True, reply=semantic.reason or "这条消息我没有判断清楚，请补充一下要记录、查学生还是处理任务。"), "clarification", "low_confidence"
        if intent == "conversation_state_reply":
            return self._route_conversation_state(identity, text), intent, "conversation_state"
        if intent == "system_service_command":
            return self._route_command(identity, text), "system_service_command", "command"
        if intent == "close_task":
            result = self._route_pending_admin_close_task(identity, text)
            if result is None:
                result = self._route_admin_close_task(identity, text)
            return result, "close_task", "task_admin_close"
        if intent == "student_record":
            return (
                self._route_record(
                    identity,
                    text,
                    source_meta={
                        "platform": platform,
                        "sender_id": sender_id,
                        "user_name": user_name,
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "raw_text": text,
                        "intent": "student_record_semantic",
                        "semantic_reason": semantic.reason,
                    },
                ),
                "student_record",
                "records",
            )
        if intent == "create_task":
            return self._route_manual_task_assignment(identity, text), "create_task", "manual_task_assignment"
        if intent in {"task_start", "task_complete", "task_evidence_update", "safety_task_evidence_update"}:
            if intent == "task_start" and semantic.task_id:
                return self._start_task_by_id(identity, str(semantic.task_id)), intent, "tasks"
            return self._route_active_task(identity, text), intent, "tasks"
        if intent in {"no_active_task_complete", "no_active_task_start"}:
            return RouteResult(handled=True, reply="你当前暂无待处理任务。"), intent, "tasks"
        if intent == "no_context_command":
            compact = str(text or "").replace(" ", "")
            boss_guarded_commands = {"确认", "打印", "这个可以吧", "先看看"}
            if identity.role == "boss" and compact not in boss_guarded_commands:
                return None, intent, "boss_general_chat"
            if compact in {"继续", "开始", "处理", "下一个"}:
                reply = "当前没有可继续的任务。你可以回复“我的任务”查看待处理任务。"
            elif compact == "完成了":
                reply = "当前没有正在处理的任务。你可以回复“我的任务”查看待处理任务。"
            elif compact == "打印":
                reply = "当前没有明确的审核任务。Hermes 不执行打印；审核完成后系统生成 Word，由机构人员人工打开、打印并线下交给家长。请先说明要审核哪名学生的哪份资料。"
            elif compact == "这个可以吧":
                reply = "请说明你指的是哪名学生的哪份审核资料。当前没有明确审核对象，我不会直接判定通过或改变状态。"
            else:
                reply = "我还没有识别到具体要处理的事项。请说明要记录哪个学生、查看什么信息，或回复“我的任务”查看待处理任务。"
            return RouteResult(handled=True, reply=reply), intent, "context_guard"
        return None, intent, "fallback"

    def _append_semantic_route_log(
        self,
        *,
        identity: UserIdentity,
        raw_text: str,
        context: dict[str, Any],
        semantic: SemanticRouteResult,
        final_intent: str,
        permission_result: str,
        handler: str,
        response: RouteResult | None,
    ) -> None:
        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "user_id": identity.canonical_user_id,
            "role": identity.role,
            "raw_text": raw_text,
            "loaded_context_summary": self._semantic_context_summary(context),
            "model_intent": semantic.intent,
            "confidence": semantic.confidence,
            "final_intent": final_intent,
            "permission_result": permission_result,
            "handler": handler,
            "response_type": "handled" if response and response.handled else "unhandled",
            "reason": semantic.reason,
        }
        path = self.store.path_for("semantic_route_logs.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.store._lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def route(
        self,
        *,
        platform: str,
        sender_id: str,
        text: str,
        user_name: str = "",
        chat_id: str = "",
        message_id: str = "",
    ) -> RouteResult:
        raw = str(text or "").strip()
        identity = self._identity(
            platform,
            sender_id,
            user_name,
            chat_id,
            raw,
        )
        if not raw:
            return RouteResult(handled=True, reply="请发送文字内容。")

        conversation_id = conversation_id_for(platform, identity.canonical_user_id, chat_id or sender_id, "dm")
        record_inbound_message(
            self.store,
            identity,
            text=raw,
            conversation_id=conversation_id,
            message_id=message_id,
        )
        if identity.approval_state != "approved":
            return RouteResult(handled=True, reply=self._unknown_reply(identity))

        permission_guard = guard_permission_request(self.store, identity, raw)
        if permission_guard is not None:
            return RouteResult(handled=True, reply=permission_guard)

        # 老板要求所有消息走模型，跳过确定性路由
        if identity.role == "boss":
            return RouteResult(handled=False)

        identity_claim = self._route_identity_claim(identity, raw)
        if identity_claim is not None:
            return identity_claim

        identity_safe_smalltalk = self._route_identity_safe_smalltalk(identity, raw)
        if identity_safe_smalltalk is not None:
            return identity_safe_smalltalk

        pending_conversation = self._route_conversation_state(identity, raw)
        if pending_conversation is not None:
            return pending_conversation

        staff_config = self._route_staff_config(identity, raw)
        if staff_config is not None:
            return staff_config

        undo_record = self._route_undo_record(identity, raw)
        if undo_record is not None:
            return undo_record

        if any(term in raw for term in _SENSITIVE_TERMS):
            if identity.role != "boss":
                return RouteResult(
                    handled=True,
                    reply="你没有权限执行这项敏感操作，请联系老板账号确认。",
                )
            return RouteResult(handled=False)

        config_change = self._route_config_change(identity, raw)
        if config_change is not None:
            return config_change

        compact_help = raw.replace(" ", "")
        if is_summer_operator(self.store, identity) and any(
            phrase in compact_help
            for phrase in ("不会写", "怎么记", "帮我写个模板", "课程怎么记录", "课怎么记录")
        ):
            return RouteResult(handled=True, reply=lesson_recording_help(raw))

        if looks_like_summer_lesson_record(raw) and is_summer_operator(self.store, identity):
            saved_lesson = save_summer_lesson_record(
                self.store,
                text=raw,
                teacher_userid=identity.canonical_user_id,
                teacher_name=identity.person_name,
                actor_is_manager=self._is_summer_manager(identity.role, identity.canonical_user_id),
            )
            if not saved_lesson.get("ok"):
                if saved_lesson.get("error") == "only_summer_manager_can_submit_daily_summary":
                    return RouteResult(handled=True, reply="只有2026暑假班店长可以提交每日总评。")
                return RouteResult(
                    handled=True,
                    reply=str(saved_lesson.get("clarification") or "课程覆盖范围还不明确，请补充课程和课节。"),
                )
            lesson = saved_lesson["lesson_record"]
            coverage = saved_lesson.get("coverage") or {}
            focus_count = len(coverage.get("individual_students") or lesson.get("focus_students") or [])
            overall_count = len(coverage.get("overall_students") or [])
            absent_focus = coverage.get("ignored_absent_focus_students") or []
            coverage_line = (
                f"整体覆盖：{overall_count}人；个别记录：{focus_count}人。"
                if lesson.get("coverage_scope") == "schedule_attendance"
                else "课程整体已保存；当前尚未配置课程表和出勤名单，未自动生成学生覆盖。"
            )
            if absent_focus:
                coverage_line += f" 缺勤学生未计入覆盖：{'、'.join(absent_focus)}。"
            return RouteResult(
                handled=True,
                reply=(
                    "已记录到 2026暑假班，用于暑假班服务过程、学生反馈和结业汇报。\n"
                    f"课程：{lesson.get('course') or '未填写'}；课节：{lesson.get('lesson') or '未填写'}。\n{coverage_line}"
                ),
            )

        compact_summer_report = raw.replace(" ", "")
        if self._is_summer_manager(identity.role, identity.canonical_user_id) and (
            "生成暑假班周反馈" in compact_summer_report or "生成暑假班结业汇报" in compact_summer_report
        ):
            report_type = "graduation" if "结业" in compact_summer_report else "weekly"
            include_history = "参考历史托管表现" in compact_summer_report
            prepared = prepare_summer_reports(
                self.store,
                report_type=report_type,
                actor_userid=identity.canonical_user_id,
                include_regular_history=include_history,
                require_cycle_window=(report_type == "weekly"),
            )
            if not prepared.get("ok"):
                return RouteResult(handled=True, reply="2026暑假班当前未启用，暂不能生成反馈草稿。")
            if prepared.get("skipped") == "outside_weekly_generation_window":
                period = prepared.get("period") or {}
                return RouteResult(
                    handled=True,
                    reply=(
                        "本期周报按周三至周日统计，周日课程结束后或周一生成草稿。"
                        f"当前周期：{period.get('start')}至{period.get('end')}，请在生成窗口再发起。"
                    ),
                )
            created = prepared.get("created") or []
            needs = prepared.get("needs_observation") or []
            lines = [
                f"已准备暑假班{'结业汇报' if report_type == 'graduation' else '周反馈'}草稿 {len(created)} 份，未自动发送。",
                f"记录较少待补观察 {len(needs)} 人。",
            ]
            if created:
                first = created[0]
                lines.extend(["", f"先审核第1份：{first.get('student_name')}", str(first.get("parent_draft") or "")])
                reply = "\n".join(lines)
                remember_conversation_state(
                    self.store,
                    identity,
                    state_type="pending_weekly_feedback_child_review" if report_type == "weekly" else "pending_graduation_report_review",
                    last_system_prompt=reply,
                    expected_replies=["确认", "修改", "跳过", "下一个", "简短生成"],
                    payload={
                        "report_id": str(first.get("id") or ""),
                        "student_name": str(first.get("student_name") or ""),
                        "period_type": "weekly" if report_type == "weekly" else "graduation",
                    },
                    source_handler="summer_reports",
                    ttl_minutes=24 * 60,
                )
                return RouteResult(handled=True, reply=reply)
            if needs:
                lines.append("请先补充观察，或逐个选择“简短生成/跳过”。")
            return RouteResult(handled=True, reply="\n".join(lines))

        try:
            semantic_context = self._semantic_context(identity, raw, conversation_id=conversation_id)
            semantic = route_message_semantically(raw, identity, semantic_context)
            semantic_result, final_intent, handler = self._route_semantic_result(
                identity,
                raw,
                semantic,
                platform=platform,
                sender_id=sender_id,
                user_name=user_name,
                chat_id=chat_id,
                message_id=message_id,
            )
            if semantic_result is not None:
                self._append_semantic_route_log(
                    identity=identity,
                    raw_text=raw,
                    context=semantic_context,
                    semantic=semantic,
                    final_intent=final_intent,
                    permission_result="checked" if semantic.requires_permission_check else "not_required",
                    handler=handler,
                    response=semantic_result,
                )
                return semantic_result

            command = self._route_command(identity, raw)
            if command is not None:
                self._append_semantic_route_log(
                    identity=identity,
                    raw_text=raw,
                    context=semantic_context,
                    semantic=semantic,
                    final_intent="command",
                    permission_result="checked",
                    handler="command",
                    response=command,
                )
                return command

            supervisor_ack = self._route_supervisor_ack(identity, raw)
            if supervisor_ack is not None:
                self._append_semantic_route_log(
                    identity=identity,
                    raw_text=raw,
                    context=semantic_context,
                    semantic=semantic,
                    final_intent="supervisor_ack",
                    permission_result="checked",
                    handler="supervisor",
                    response=supervisor_ack,
                )
                return supervisor_ack

            learning = self._route_learning_review(identity, raw)
            if learning is not None:
                self._append_semantic_route_log(
                    identity=identity,
                    raw_text=raw,
                    context=semantic_context,
                    semantic=semantic,
                    final_intent="learning_review",
                    permission_result="checked",
                    handler="knowledge",
                    response=learning,
                )
                return learning

            operations_focus = self._route_operations_focus(identity, raw)
            if operations_focus is not None:
                self._append_semantic_route_log(
                    identity=identity,
                    raw_text=raw,
                    context=semantic_context,
                    semantic=semantic,
                    final_intent="operations_focus",
                    permission_result="checked",
                    handler="operations_focus",
                    response=operations_focus,
                )
                return operations_focus

            payroll_settlement = self._route_payroll_settlement(identity, raw)
            if payroll_settlement is not None:
                self._append_semantic_route_log(
                    identity=identity,
                    raw_text=raw,
                    context=semantic_context,
                    semantic=semantic,
                    final_intent="payroll_settlement",
                    permission_result="checked",
                    handler="payroll",
                    response=payroll_settlement,
                )
                return payroll_settlement

            payroll_export = self._route_payroll_export(identity, raw)
            if payroll_export is not None:
                self._append_semantic_route_log(
                    identity=identity,
                    raw_text=raw,
                    context=semantic_context,
                    semantic=semantic,
                    final_intent="payroll_export",
                    permission_result="checked",
                    handler="payroll",
                    response=payroll_export,
                )
                return payroll_export

            payroll_review = self._route_payroll_review(identity, raw)
            if payroll_review is not None:
                self._append_semantic_route_log(
                    identity=identity,
                    raw_text=raw,
                    context=semantic_context,
                    semantic=semantic,
                    final_intent="payroll_review",
                    permission_result="checked",
                    handler="payroll",
                    response=payroll_review,
                )
                return payroll_review

            payroll = self._route_payroll_event(identity, raw)
            if payroll is not None:
                self._append_semantic_route_log(
                    identity=identity,
                    raw_text=raw,
                    context=semantic_context,
                    semantic=semantic,
                    final_intent="payroll_event",
                    permission_result="checked",
                    handler="payroll",
                    response=payroll,
                )
                return payroll

            manual_assignment = self._route_manual_task_assignment(identity, raw)
            if manual_assignment is not None:
                self._append_semantic_route_log(
                    identity=identity,
                    raw_text=raw,
                    context=semantic_context,
                    semantic=semantic,
                    final_intent="manual_assignment_fallback",
                    permission_result="checked",
                    handler="manual_task_assignment",
                    response=manual_assignment,
                )
                return manual_assignment

            supervisor_task = self._route_supervisor_task_reference(identity, raw)
            if supervisor_task is not None:
                self._append_semantic_route_log(
                    identity=identity,
                    raw_text=raw,
                    context=semantic_context,
                    semantic=semantic,
                    final_intent="supervisor_task_reference",
                    permission_result="checked",
                    handler="supervisor",
                    response=supervisor_task,
                )
                return supervisor_task

            if is_student_query(raw):
                result = RouteResult(
                    handled=True,
                    reply=answer_student_query(raw, identity, self.store),
                )
                self._append_semantic_route_log(
                    identity=identity,
                    raw_text=raw,
                    context=semantic_context,
                    semantic=semantic,
                    final_intent="student_query",
                    permission_result="checked",
                    handler="queries",
                    response=result,
                )
                return result

            if is_script_request(raw):
                reply = answer_script_request(raw, self.store)
                remember_script_context(self.store, identity.canonical_user_id, raw)
                result = RouteResult(handled=True, reply=reply)
                self._append_semantic_route_log(
                    identity=identity,
                    raw_text=raw,
                    context=semantic_context,
                    semantic=semantic,
                    final_intent="script_request",
                    permission_result="checked",
                    handler="knowledge",
                    response=result,
                )
                return result

            if is_script_feedback(raw):
                base_request = recent_script_context(
                    self.store,
                    identity.canonical_user_id,
                )
                if base_request:
                    reply = answer_script_feedback(base_request, raw, self.store)
                    remember_script_context(
                        self.store,
                        identity.canonical_user_id,
                        base_request,
                    )
                    result = RouteResult(handled=True, reply=reply)
                    self._append_semantic_route_log(
                        identity=identity,
                        raw_text=raw,
                        context=semantic_context,
                        semantic=semantic,
                        final_intent="script_feedback",
                        permission_result="checked",
                        handler="knowledge",
                        response=result,
                    )
                    return result

            compact_meta = raw.replace(" ", "")
            active_meta_task = semantic_context.get("active_task") if isinstance(semantic_context.get("active_task"), dict) else None
            if active_meta_task and any(word in compact_meta for word in ("我是在处理任务", "我在处理任务", "正在处理任务", "这是任务处理", "这个是任务")):
                result = RouteResult(
                    handled=True,
                    reply=(
                        f"收到，你当前正在处理：{active_meta_task.get('level', 'C')}级："
                        f"{active_meta_task.get('title') or active_meta_task.get('student_name') or '当前任务'}。\n"
                        "请直接补充这项任务的处理情况；处理完后回复“完成了”。"
                    ),
                )
                self._append_semantic_route_log(
                    identity=identity,
                    raw_text=raw,
                    context=semantic_context,
                    semantic=semantic,
                    final_intent="active_task_meta_reply",
                    permission_result="checked",
                    handler="tasks",
                    response=result,
                )
                return result

            if identity.role not in {"teacher", "manager"}:
                return RouteResult(handled=False)
            result = self._route_record(
                identity,
                raw,
                source_meta={
                    "platform": platform,
                    "sender_id": sender_id,
                    "user_name": user_name,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "raw_text": raw,
                },
            )
            self._append_semantic_route_log(
                identity=identity,
                raw_text=raw,
                context=semantic_context,
                semantic=semantic,
                final_intent="record_fallback",
                permission_result="checked",
                handler="records",
                response=result,
            )
            return result
        except UnknownStudentError:
            unknown_summer = self._route_unknown_summer_student_record(identity, raw)
            if unknown_summer is not None:
                return unknown_summer
            return RouteResult(handled=False)
        except AmbiguousStudentError:
            return RouteResult(handled=False)
        except TuoguanStoreError:
            return RouteResult(
                handled=True,
                reply="托管业务数据暂时无法安全读取，已停止本次操作，请稍后重试。",
            )
