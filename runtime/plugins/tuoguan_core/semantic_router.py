"""Semantic first-stage routing for tutoring-center messages.

This module is intentionally isolated from the business handlers.  The router
asks it for a structured intent first, then applies permission and data rules.
The current implementation is deterministic so tests and local deployments do
not depend on an external model; the public function is the seam for swapping in
an LLM-backed classifier later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import UserIdentity


@dataclass(frozen=True)
class SemanticRouteResult:
    intent: str
    confidence: float
    student_name: str = ""
    task_id: str | None = None
    task_type: str = ""
    record_type: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    requires_permission_check: bool = True
    needs_clarification: bool = False
    reason: str = ""


_SYSTEM_WORDS = (
    "gateway",
    "wecom",
    "scheduler",
    "服务",
    "进程",
    "企业微信通道",
    "端口",
    "8646",
)
_CLOSE_WORDS = ("关闭", "关掉", "关了", "闭关", "强制关闭", "强制完成", "取消")
_BUSINESS_TASK_WORDS = ("任务", "S级", "A级", "B级", "C级", "安全", "跟进", "闭环")
_ASSIGN_WORDS = ("安排", "派给", "交给", "指派", "布置", "下发", "让", "请")
_STUDENT_SCENE_WORDS = (
    "学习",
    "作业",
    "订正",
    "计算",
    "阅读",
    "书写",
    "背诵",
    "表现",
    "纪律",
    "吃饭",
    "午餐",
    "米饭",
    "青菜",
    "进餐",
    "挑食",
    "午休",
    "活动",
    "科学实验",
    "体育",
    "坐不住",
    "安静",
    "情绪",
    "速度",
    "错了",
    "漏了",
    "检查",
    "提醒",
    "关注",
    "今天",
)
_TEACHER_ACTION_WORDS = ("我让", "老师", "提醒", "重新", "补完", "检查", "关注", "沟通", "处理")
_GROWTH_TASK_TYPES = {
    "student_daily",
    "growth_observation",
    "daily_observation",
    "behavior_followup",
    "life_care",
}
_GROWTH_TASK_TITLE_WORDS = (
    "成长观察",
    "本周成长",
    "表现",
    "状态",
    "午休",
    "吃饭",
    "生活",
    "行为",
    "习惯",
)
_GROWTH_EVIDENCE_WORDS = (
    "表现",
    "主动",
    "帮助同学",
    "吃饭",
    "不挑食",
    "饭吃完",
    "午休",
    "听话",
    "转变",
    "变化",
    "进步",
    "作业",
    "完成",
    "阅读",
    "背诵",
    "情绪",
    "纪律",
    "提醒",
    "关注",
    "后续",
    "继续",
)
_SAFETY_EVIDENCE_WORDS = (
    "孩子当前",
    "当前状态",
    "疼痛",
    "出血",
    "红肿",
    "活动正常",
    "情绪稳定",
    "已采取",
    "提醒",
    "家长已知情",
    "已告知家长",
    "家长表示",
    "后续",
    "继续观察",
    "观察安排",
)
_TASK_COMPLETE_EXACT = {
    "完成了",
    "已完成",
    "处理完了",
    "这个任务完成了",
    "任务完成了",
    "提交任务闭环",
}
_SHORT_CONTEXT_COMMANDS = {
    "继续",
    "开始",
    "完成了",
    "好的",
    "好",
    "可以",
    "处理",
    "下一个",
    "记一下",
    "收到",
    "确认",
    "打印",
    "这个可以吧",
    "先看看",
}
_STRICT_NO_CONTEXT_COMMANDS = {"确认", "打印", "这个可以吧", "先看看"}


def route_message_semantically(
    message: str,
    user_identity: UserIdentity,
    context: dict[str, Any],
) -> SemanticRouteResult:
    """Classify a message into a structured semantic route result."""

    raw = str(message or "").strip()
    compact = raw.replace(" ", "")
    lower = compact.lower()
    student_name = str(context.get("student_name") or "")
    active_task = context.get("active_task") if isinstance(context.get("active_task"), dict) else None
    current_task = context.get("current_task") if isinstance(context.get("current_task"), dict) else None
    task_context = active_task or current_task
    safety_evidence_task_context = active_task
    if (
        safety_evidence_task_context is None
        and current_task
        and str(current_task.get("type") or "") == "safety_incident"
    ):
        safety_evidence_task_context = current_task
    task_evidence_context = active_task
    if task_evidence_context is None and current_task and _looks_like_task_evidence(compact, current_task, student_name):
        task_evidence_context = current_task
    pending_next = context.get("pending_next_task") if isinstance(context.get("pending_next_task"), dict) else None
    pending_close = context.get("pending_close_task") if isinstance(context.get("pending_close_task"), dict) else None
    conversation_state = context.get("conversation_state") if isinstance(context.get("conversation_state"), dict) else None
    growth_report_context = context.get("growth_report_context") if isinstance(context.get("growth_report_context"), dict) else None
    message_history = context.get("message_history") if isinstance(context.get("message_history"), list) else []
    last_outbound = next(
        (
            item for item in reversed(message_history)
            if isinstance(item, dict) and item.get("message_role") == "assistant"
        ),
        None,
    )
    open_tasks = context.get("open_tasks") if isinstance(context.get("open_tasks"), list) else []
    assignee_userid = str(context.get("assignee_userid") or "")
    assignee_name = str(context.get("assignee_name") or "")

    if not raw:
        return SemanticRouteResult("empty", 1.0, requires_permission_check=False, reason="empty message")

    if _is_system_service_command(compact, lower):
        return SemanticRouteResult(
            "system_service_command",
            0.99,
            requires_permission_check=True,
            reason="明确出现 gateway/wecom/scheduler/服务/端口等系统运维词",
        )

    if conversation_state and _matches_conversation_state(compact, conversation_state):
        return SemanticRouteResult(
            "conversation_state_reply",
            0.98,
            fields={
                "state_type": str(conversation_state.get("state_type") or ""),
                "source_handler": str(conversation_state.get("source_handler") or ""),
            },
            requires_permission_check=True,
            reason="用户正在回复上一轮系统提示的待确认状态",
        )

    if pending_next and _is_pending_next_trigger(compact, pending_next):
        pending_source = str(pending_next.get("source") or "")
        if pending_source != "new_task_notification" and len(open_tasks) > 1:
            pass
        else:
            return SemanticRouteResult(
                "task_start",
                0.98,
                student_name=str(pending_next.get("student_name") or student_name),
                task_id=str(pending_next.get("task_id") or "") or None,
                task_type=str(pending_next.get("task_type") or ""),
                fields={"source": "pending_next_task_context"},
                reason="用户回复了刚才下一个任务提示中的继续/开始下一个短命令",
            )

    if pending_close and not any(word in lower for word in _SYSTEM_WORDS):
        return SemanticRouteResult(
            "close_task",
            0.96,
            student_name=str(pending_close.get("student_name") or student_name),
            task_id=str(pending_close.get("task_id") or "") or None,
            task_type=str(pending_close.get("task_type") or ""),
            fields={"source": "pending_close_task_context"},
            reason="存在待补原因的关闭任务上下文，本条优先承接关闭原因",
        )

    if (
        compact in _STRICT_NO_CONTEXT_COMMANDS
        and not task_context
        and not growth_report_context
        and not conversation_state
    ):
        return SemanticRouteResult(
            "no_context_command",
            0.98,
            needs_clarification=True,
            requires_permission_check=False,
            reason="高风险短命令缺少正式待确认状态，禁止根据普通消息历史推断执行",
        )

    if last_outbound and not _is_p4_test_outbound(last_outbound) and (
        compact in _SHORT_CONTEXT_COMMANDS
        or compact in {"那就这样", "刚才那个", "就按刚才的", "按刚才说的"}
    ) and not task_context and not conversation_state:
        return SemanticRouteResult(
            "history_followup",
            0.82,
            fields={
                "last_outbound_source": str(last_outbound.get("source") or ""),
                "last_outbound_text": str(last_outbound.get("message_text") or "")[:500],
            },
            requires_permission_check=True,
            reason="用户正在承接最近一条 Hermes 对外回复，需结合统一消息历史理解",
        )

    if compact in _SHORT_CONTEXT_COMMANDS and not task_context and not growth_report_context and not conversation_state:
        return SemanticRouteResult(
            "no_context_command",
            0.92,
            needs_clarification=True,
            requires_permission_check=False,
            reason="短命令缺少可承接上下文，不能进入学生记录或任务执行",
        )
    if _looks_like_manual_assignment(compact, user_identity, assignee_userid, assignee_name):
        return SemanticRouteResult(
            "create_task",
            0.93,
            student_name=student_name,
            fields={"assignee_userid": assignee_userid, "assignee_name": assignee_name},
            reason="管理角色明确安排老师/店长执行任务",
        )

    if _looks_like_business_task_close(compact):
        return SemanticRouteResult(
            "close_task",
            0.95,
            student_name=student_name,
            task_type=_semantic_task_type(compact),
            fields={"reason": _extract_reason(raw)},
            reason="关闭/强制关闭与学生或业务任务词共同出现，属于业务任务关闭",
        )

    if safety_evidence_task_context and _looks_like_safety_evidence(compact, safety_evidence_task_context):
        return SemanticRouteResult(
            "safety_task_evidence_update",
            0.94,
            student_name=str(safety_evidence_task_context.get("student_name") or student_name),
            task_id=str(safety_evidence_task_context.get("id") or "") or None,
            task_type=str(safety_evidence_task_context.get("type") or ""),
            fields={"source": "active_or_current_task_context"},
            reason="当前存在安全任务上下文，本条是在补充安全闭环证据",
        )

    if task_evidence_context and _looks_like_task_evidence(compact, task_evidence_context, student_name):
        return SemanticRouteResult(
            "task_evidence_update",
            0.9,
            student_name=str(task_evidence_context.get("student_name") or student_name),
            task_id=str(task_evidence_context.get("id") or "") or None,
            task_type=str(task_evidence_context.get("type") or ""),
            fields={"source": "active_or_current_task_context"},
            reason="当前存在任务上下文，且消息与任务学生、处理情况、结果或后续安排相关，优先作为任务证据",
        )

    if _looks_like_student_record(compact, student_name, user_identity):
        return SemanticRouteResult(
            "student_record",
            0.95,
            student_name=student_name,
            record_type=_record_type(compact),
            fields=_record_fields(raw),
            reason="包含学生名、学生场景和老师观察/处理动作，属于学生记录",
        )

    if compact in _TASK_COMPLETE_EXACT or re.fullmatch(r"(完成|提交).{0,12}(任务|闭环)", compact):
        if task_context:
            return SemanticRouteResult(
                "task_complete",
                0.93,
                student_name=str(task_context.get("student_name") or student_name),
                task_id=str(task_context.get("id") or "") or None,
                task_type=str(task_context.get("type") or ""),
                reason="存在当前任务上下文，且消息是明确任务完成表达",
            )
        return SemanticRouteResult(
            "no_active_task_complete",
            0.9,
            needs_clarification=True,
            requires_permission_check=False,
            reason="消息是完成任务表达，但当前没有正在处理的任务",
        )

    if re.fullmatch(r"(?:处理|选|选择|第)?[1-9](?:个|条|号|任务)?", compact):
        return SemanticRouteResult(
            "task_start",
            0.92,
            reason="用户正在从待处理任务列表中选择具体任务",
        )

    if _looks_like_start_current_task(compact):
        if current_task:
            return SemanticRouteResult(
                "task_start",
                0.94,
                student_name=str(current_task.get("student_name") or ""),
                task_id=str(current_task.get("id") or "") or None,
                task_type=str(current_task.get("type") or ""),
                reason="用户表达要开始处理当前/下一个任务，优先绑定当前待处理任务",
            )
        if len(open_tasks) == 1:
            task = open_tasks[0]
            return SemanticRouteResult(
                "task_start",
                0.9,
                student_name=str(task.get("student_name") or ""),
                task_id=str(task.get("id") or "") or None,
                task_type=str(task.get("type") or ""),
                reason="只有一个待处理任务，开始处理该任务",
            )
        return SemanticRouteResult(
            "no_active_task_start",
            0.9,
            needs_clarification=True,
            requires_permission_check=False,
            reason="消息是开始任务表达，但当前没有待处理任务",
        )

    if compact in {"开始", "处理", "开始处理", "需要"}:
        if len(open_tasks) > 1:
            return SemanticRouteResult(
                "task_start",
                0.82,
                needs_clarification=True,
                reason="存在多个待处理任务，需要选择具体任务",
            )
        if len(open_tasks) == 1:
            task = open_tasks[0]
            return SemanticRouteResult(
                "task_start",
                0.9,
                student_name=str(task.get("student_name") or ""),
                task_id=str(task.get("id") or "") or None,
                task_type=str(task.get("type") or ""),
                reason="只有一个待处理任务，开始处理该任务",
            )
        return SemanticRouteResult(
            "no_active_task_start",
            0.9,
            needs_clarification=True,
            requires_permission_check=False,
            reason="消息是开始任务表达，但当前没有待处理任务",
        )

    return SemanticRouteResult(
        "unknown",
        0.5,
        student_name=student_name,
        requires_permission_check=False,
        reason="没有足够语义信号，交给后续非抢占式业务兜底",
    )


def _is_p4_test_outbound(message: dict[str, Any]) -> bool:
    text = str(message.get("message_text") or "")
    task_id = str(message.get("related_task_id") or "").upper()
    return bool(
        re.search(r"P4-(?:12|13|14|15).*(?:测试|预演)", text, re.IGNORECASE)
        or re.match(r"P4-(?:12|13|14|15)-TEST-", task_id, re.IGNORECASE)
    )


def _looks_like_start_current_task(compact: str) -> bool:
    if compact in {"现在开始处理", "开始处理我现在的任务", "开始处理我的任务", "一项一项的开始现在处理", "继续下一个任务", "开始下一个任务", "处理下一个任务"}:
        return True
    if "开始处理" in compact and any(word in compact for word in ("现在", "当前", "我的任务", "我现在的任务")):
        return True
    if "继续" in compact and "下一个任务" in compact:
        return True
    if "一项一项" in compact and any(word in compact for word in ("开始", "处理")):
        return True
    return False


def _is_system_service_command(compact: str, lower: str) -> bool:
    if not any(word in compact for word in ("关闭", "停止", "停掉", "关掉", "重启")):
        return False
    return any(word in lower for word in _SYSTEM_WORDS)


def _is_pending_next_trigger(compact: str, pending_next: dict[str, Any]) -> bool:
    triggers = pending_next.get("trigger_words")
    words = [str(word).replace(" ", "") for word in triggers] if isinstance(triggers, list) else []
    words.extend(["继续", "开始下一个", "处理下一个", "开始", "处理", "1"])
    return compact in {word for word in words if word}


def _matches_conversation_state(compact: str, state: dict[str, Any]) -> bool:
    expected = {
        str(item).replace(" ", "")
        for item in (state.get("expected_replies") or [])
        if str(item)
    }
    if compact in expected:
        return True
    state_type = str(state.get("state_type") or "")
    if state_type == "pending_student_duplicate_confirm":
        return compact in {"关联", "关联为同一个学生", "1", "新建", "创建为新学生", "2", "跳过", "暂不导入", "3"}
    if state_type in {"pending_config_change_confirm", "pending_teacher_handoff_confirm"}:
        return compact in {"确认", "确认执行", "取消", "取消执行"}
    if state_type == "pending_staff_config_confirm":
        return compact in {"确认配置", "确认执行", "确认", "取消配置", "取消执行", "取消"} or bool(
            re.fullmatch(r"(?:选择)?[\u4e00-\u9fffA-Za-z·]{1,8}老师(?:选|选择)?[1-9]", compact)
        )
    if state_type == "pending_unregistered_student_confirm":
        return compact in {"补录这个孩子", "确认补录", "补录", "暂不补录", "跳过"}
    if state_type in {"pending_weekly_feedback_review", "pending_weekly_feedback_child_review"}:
        return compact in {"确认", "可以", "修改", "跳过", "下一个", "简短生成", "重新生成", "补充"}
    if state_type == "pending_task_next_choice":
        return compact in {"继续", "开始", "处理", "1", "开始下一个", "处理下一个"}
    return False


def _looks_like_business_task_close(compact: str) -> bool:
    if any(word in compact for word in _ASSIGN_WORDS):
        return False
    if not any(word in compact for word in _CLOSE_WORDS):
        return False
    if any(word in compact.lower() for word in _SYSTEM_WORDS) and "任务" not in compact:
        return False
    return any(word in compact for word in _BUSINESS_TASK_WORDS)


def _looks_like_manual_assignment(
    compact: str,
    user_identity: UserIdentity,
    assignee_userid: str,
    assignee_name: str,
) -> bool:
    if user_identity.role not in {"boss", "manager"}:
        return False
    if not any(word in compact for word in _ASSIGN_WORDS):
        return False
    if not (assignee_userid or assignee_name or "老师" in compact or "店长" in compact):
        return False
    return "任务" in compact or any(word in compact for word in ("跟进", "沟通", "处理", "完成", "闭环"))


def _looks_like_student_record(compact: str, student_name: str, user_identity: UserIdentity) -> bool:
    if user_identity.role not in {"teacher", "manager"}:
        return False
    if not student_name:
        return False
    if any(word in compact for word in ("安排", "派给", "交给", "指派", "布置", "下发")) and "任务" in compact:
        return False
    scene_hits = sum(1 for word in _STUDENT_SCENE_WORDS if word in compact)
    action_hit = any(word in compact for word in _TEACHER_ACTION_WORDS)
    return scene_hits >= 2 or (scene_hits >= 1 and action_hit)


def _looks_like_safety_evidence(compact: str, task: dict[str, Any]) -> bool:
    if str(task.get("type") or "") != "safety_incident":
        return False
    return any(word in compact for word in _SAFETY_EVIDENCE_WORDS)


def _looks_like_task_evidence(compact: str, task: dict[str, Any], student_name: str = "") -> bool:
    if _looks_like_safety_evidence(compact, task):
        return True
    task_student = str(task.get("student_name") or "")
    if task_student and student_name and task_student != student_name:
        return False
    status = str(task.get("status") or "")
    evidence_words = (
        "重新做",
        "重新",
        "订正",
        "错",
        "错因",
        "列竖式",
        "讲了一遍",
        "思路",
        "我又让",
        "我让",
        "已处理",
        "处理结果",
        "结果",
        "家长",
        "沟通",
        "暂未",
        "同步",
        "后续",
        "如果",
        "明天",
        "继续关注",
        "准确率",
        "跟进",
        "反馈",
    )
    hits = sum(1 for word in evidence_words if word in compact)
    if status == "pending":
        action_hit = any(word in compact for word in ("我让", "我又让", "提醒", "处理", "重新做", "重新列", "讲了一遍", "订正"))
        result_hit = any(word in compact for word in ("最后", "结果", "能说出", "还错", "错因", "完成结果"))
        followup_hit = any(word in compact for word in ("明天", "后续", "继续关注", "如果", "再和", "同步"))
        return bool(
            task_student
            and task_student in compact
            and hits >= 4
            and action_hit
            and result_hit
            and followup_hit
        )
    if status not in {"active", "waiting_confirmation"}:
        return False
    if _looks_like_growth_task_evidence(compact, task, student_name):
        return True
    if task_student and task_student in compact and hits >= 1:
        return True
    return hits >= 2


def _looks_like_growth_task_evidence(compact: str, task: dict[str, Any], student_name: str = "") -> bool:
    task_type = str(task.get("type") or "")
    title = str(task.get("title") or "")
    if task_type not in _GROWTH_TASK_TYPES and not any(word in title for word in _GROWTH_TASK_TITLE_WORDS):
        return False
    task_student = str(task.get("student_name") or "")
    if task_student and student_name and task_student != student_name:
        return False
    references_current_student = bool(task_student and task_student in compact)
    pronoun_reference = not student_name and any(word in compact for word in ("孩子", "他", "她"))
    if not (references_current_student or pronoun_reference):
        return False
    scene_hits = sum(1 for word in _STUDENT_SCENE_WORDS if word in compact)
    growth_hits = sum(1 for word in _GROWTH_EVIDENCE_WORDS if word in compact)
    action_hit = any(word in compact for word in _TEACHER_ACTION_WORDS)
    enough_detail = len(compact) >= 35
    return (scene_hits + growth_hits) >= 2 and (action_hit or enough_detail)


def _semantic_task_type(compact: str) -> str:
    if "安全" in compact or "摔" in compact or "碰" in compact:
        return "safety_incident"
    if "续费" in compact:
        return "renewal_risk"
    if "投诉" in compact:
        return "parent_complaint"
    return ""


def _record_type(compact: str) -> str:
    if any(word in compact for word in ("作业", "数学", "计算", "订正", "阅读", "书写", "背诵")):
        return "learning"
    if any(word in compact for word in ("午餐", "吃饭", "米饭", "青菜", "进餐", "挑食")):
        return "meal_care"
    if any(word in compact for word in ("午休", "睡觉", "休息")):
        return "nap_care"
    if any(word in compact for word in ("情绪", "午休", "吃饭", "纪律", "表现")):
        return "daily_behavior"
    return "student_daily"


def _record_fields(raw: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    if "我让" in raw:
        problem, action = raw.split("我让", 1)
        fields["observation"] = problem.strip(" ，,。")
        fields["teacher_action"] = ("我让" + action).strip(" ，,。")
    elif "提醒" in raw:
        fields["teacher_action"] = raw[raw.find("提醒") :].strip(" ，,。")
    return fields


def _extract_reason(raw: str) -> str:
    for marker in ("原因：", "原因:", "原因，", "原因,", "原因是", "理由：", "理由:", "理由是"):
        if marker in raw:
            return raw.split(marker, 1)[1].strip(" ，,。")
    match = re.search(r"(本次.*|本回.*|这次.*|此次.*|因为.*|孩子.*|家长.*|后续.*)", raw)
    return match.group(1).strip(" ，,。") if match else ""
