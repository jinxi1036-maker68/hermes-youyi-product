"""Single multi-business context arbitration policy for Core Sprint V1."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


TASK_QUERY_TEXTS = {"我的任务", "我的今日任务", "查看我的任务", "查看我的今日任务", "查看我的待办任务"}
NEXT_TASK_TEXTS = {"继续下一个任务", "继续下一个", "下一个任务", "开始下一个任务", "处理下一个任务"}
TASK_TYPE_MARKERS = ("观察任务", "回访任务", "学习任务", "普通任务")
SAFETY_OBJECT_MARKERS = ("安全测试", "安全事件", "摔倒的事情", "受伤的事情")
SAFETY_FACT_MARKERS = (
    "出血", "流血", "肿胀", "能走路", "正常走路", "不能走", "冲洗", "消毒", "冰敷",
    "伤口", "没有加重", "情绪", "精神", "创可贴", "包扎", "涂药",
)
SAFETY_PARENT_FACT_MARKERS = ("跟家长说", "告知家长", "通知家长", "家长说", "家长知道", "继续观察")
SAFETY_REVIEW_MARKERS = (
    "审核刚才的安全测试", "安全测试事件", "摔伤测试", "安全事件审核",
    "审核通过", "同意闭环", "闭环吧", "先别关", "再观察一下",
)
STUDENT_RECORD_MARKERS = ("今天", "课堂", "作业", "纪律", "午休", "吃饭", "学习", "表现", "主动", "订正", "计算")
TASK_OBSERVATION_FACT_MARKERS = ("这个孩子", "当时的表现", "写作业", "磨蹭", "开小差", "挑食", "午休", "吃饭", "学习", "课堂", "表现")
TASK_FEEDBACK_MARKERS = ("家长说", "联系过", "沟通过", "没联系上", "再跟进", "处理完", "任务完成", "完成了")
BOSS_MARKERS = ("需要我注意", "最该先处理", "执行得怎么样", "执行的怎么样", "执行怎么样", "老师今天执行", "今天老师执行", "管理重点", "今天店里", "当前风险", "有什么问题")
SHORT_CONTEXT_WORDS = {"说过了", "弄好了", "完成了", "好了", "处理了"}


def compact(text: str) -> str:
    return "".join(str(text or "").split()).rstrip("。！？!?；;")


def _updated_at(context: dict[str, Any] | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str((context or {}).get("updated_at") or ""))
    except ValueError:
        return None


def _task_is_fresher(task_context: dict[str, Any] | None, safety_context: dict[str, Any] | None) -> bool:
    task_time, safety_time = _updated_at(task_context), _updated_at(safety_context)
    if task_time is None:
        return False
    return safety_time is None or task_time >= safety_time


@dataclass(frozen=True)
class ArbitrationDecision:
    route_status: str
    selected_capability: str = ""
    selected_business_object: str = ""
    routing_evidence: tuple[str, ...] = ()
    context_used: str = ""
    context_ignored_reason: str = ""
    conflict_detected: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["routing_evidence"] = list(self.routing_evidence)
        return value


def arbitrate(
    *, text: str, role: str, visible_tasks: list[dict[str, Any]], student_names: list[str],
    task_context: dict[str, Any] | None, safety_context: dict[str, Any] | None,
) -> ArbitrationDecision:
    raw, value = str(text or ""), compact(text)
    task_titles = [(str(row.get("id") or ""), str(row.get("title") or "")) for row in visible_tasks]
    exact_tasks = [(tid, title) for tid, title in task_titles if title and title in raw]
    named_students = [name for name in student_names if name and name in raw]
    task_student = str((task_context or {}).get("student_name") or "")
    safety_stage = str((safety_context or {}).get("workflow_state") or "")

    # Priority 1: explicit business object or business type.
    if value in TASK_QUERY_TEXTS:
        return ArbitrationDecision("defer_existing_capability", routing_evidence=("explicit_task_query",), context_ignored_reason="explicit_business_switch")
    if value in NEXT_TASK_TEXTS:
        return ArbitrationDecision("selected", "teacher_task_guidance", "next_task", ("explicit_next_task_command",), context_ignored_reason="explicit_business_command")
    if len(exact_tasks) == 1:
        return ArbitrationDecision("selected", "teacher_task_guidance", exact_tasks[0][0], ("explicit_task_title",), context_used="task_repository", context_ignored_reason="explicit_object_overrides_residual_context")
    if len(exact_tasks) > 1:
        return ArbitrationDecision("ambiguous", routing_evidence=("multiple_exact_task_titles",), context_ignored_reason="no_safe_binding", conflict_detected=True)
    if any(mark in value for mark in TASK_TYPE_MARKERS) and task_context:
        return ArbitrationDecision("selected", "teacher_task_guidance", str(task_context.get("task_id") or ""), ("explicit_task_type",), context_used="ordinary_task")
    if "safety_test=true" in value.lower() or any(mark in value for mark in SAFETY_OBJECT_MARKERS):
        return ArbitrationDecision("selected", "safety_workflow_coach", str((safety_context or {}).get("event_id") or "new_safety_test"), ("explicit_safety_type",), context_used="safety_workflow" if safety_context else "")
    if (role in {"boss", "manager"} or safety_context) and any(mark in value for mark in SAFETY_REVIEW_MARKERS):
        return ArbitrationDecision("selected", "safety_workflow_coach", "pending_safety_review", ("explicit_safety_review",))
    if role in {"boss", "manager"} and any(mark in value for mark in BOSS_MARKERS):
        return ArbitrationDecision("selected", "management_boss_advisor", "youyi_tuoguan", ("explicit_management_query",))
    if named_students and any(mark in value for mark in STUDENT_RECORD_MARKERS):
        if task_context and task_student in named_students:
            return ArbitrationDecision("selected", "teacher_task_guidance", str(task_context.get("task_id") or ""), ("task_contract_student_observation",), context_used="ordinary_task")
        return ArbitrationDecision("defer_existing_capability", routing_evidence=("explicit_student_record",), context_ignored_reason="explicit_business_switch")

    # A generic short acknowledgement is never a strong contract fact when
    # multiple business contexts are active.
    if value in SHORT_CONTEXT_WORDS and task_context and safety_context:
        return ArbitrationDecision("ambiguous", routing_evidence=("unsafe_short_context",), context_ignored_reason="no_safe_binding", conflict_detected=True)

    # Priority 2: strong contract facts.
    if safety_context and any(mark in value for mark in SAFETY_FACT_MARKERS):
        return ArbitrationDecision("selected", "safety_workflow_coach", str(safety_context.get("event_id") or ""), ("safety_contract_fact",), context_used="safety_workflow")
    if safety_context and any(mark in value for mark in SAFETY_PARENT_FACT_MARKERS):
        return ArbitrationDecision("selected", "safety_workflow_coach", str(safety_context.get("event_id") or ""), ("safety_parent_communication_fact",), context_used="safety_workflow")
    if task_context and any(mark in value for mark in TASK_OBSERVATION_FACT_MARKERS) and (
        not safety_context or _task_is_fresher(task_context, safety_context)
    ):
        return ArbitrationDecision("selected", "teacher_task_guidance", str(task_context.get("task_id") or ""), ("recent_task_contract_observation",), context_used="ordinary_task", context_ignored_reason="newer_explicit_task_focus")
    if task_context and any(mark in value for mark in TASK_FEEDBACK_MARKERS):
        return ArbitrationDecision("selected", "teacher_task_guidance", str(task_context.get("task_id") or ""), ("task_contract_fact",), context_used="ordinary_task")

    # Priority 3: one valid context, only where the stage permits shorthand.
    if value in SHORT_CONTEXT_WORDS:
        if safety_context and safety_stage == "parent_communication" and not task_context:
            return ArbitrationDecision("selected", "safety_workflow_coach", str(safety_context.get("event_id") or ""), ("stage_safe_short_context",), context_used="safety_workflow")
        if task_context and not safety_context:
            return ArbitrationDecision("selected", "teacher_task_guidance", str(task_context.get("task_id") or ""), ("unique_task_context",), context_used="ordinary_task")
        return ArbitrationDecision("ambiguous", routing_evidence=("unsafe_short_context",), context_ignored_reason="no_safe_binding", conflict_detected=bool(task_context and safety_context))
    if task_context and not safety_context:
        return ArbitrationDecision("selected", "teacher_task_guidance", str(task_context.get("task_id") or ""), ("unique_active_context",), context_used="ordinary_task")

    return ArbitrationDecision("no_match", context_ignored_reason="no_safe_binding")
