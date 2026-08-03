"""Deterministic task queue, coaching, and closure state machine."""

from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Any

from .models import TaskReplyResult


_CLOSED_STATUSES = {"completed", "cancelled", "closed", "done", "closed_by_admin", "completed_by_admin"}
_LEVEL_ORDER = {"S": 0, "A": 1, "B": 2, "C": 3}


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value or ""))
    except ValueError:
        return datetime.max


def _queue_key(task: dict[str, Any]) -> tuple:
    return (
        _LEVEL_ORDER.get(str(task.get("level") or "C"), 3),
        _parse_datetime(task.get("due_at")),
        -int(task.get("defer_count") or 0),
        str(task.get("created_at") or ""),
    )


def current_task_for_user(
    tasks: list[dict[str, Any]],
    userid: str,
) -> dict[str, Any] | None:
    candidates = [
        task
        for task in tasks
        if task.get("assignee_userid") == userid
        and task.get("status") not in _CLOSED_STATUSES
    ]
    active = [
        task
        for task in candidates
        if task.get("status") in {"active", "waiting_confirmation"}
    ]
    if active:
        return sorted(active, key=_queue_key)[0]
    return sorted(candidates, key=_queue_key)[0] if candidates else None


def classify_task_reply(text: str) -> dict[str, str]:
    raw = str(text or "").strip()
    compact = raw.replace(" ", "")
    if not compact:
        return {"intent": "empty", "text": raw}
    if any(word in compact for word in ("取消", "不用处理", "不用做", "误触发")):
        intent = "cancel"
    elif any(
        word in compact
        for word in (
            "帮我写",
            "该怎么",
            "应该怎么",
            "怎么说",
            "怎么回复",
            "怎么沟通",
            "怎么给",
            "怎么跟",
            "怎么和",
            "怎么聊",
            "怎么办",
            "怎么解释",
            "怎么介绍",
            "咋回答",
            "说什么",
            "注意什么",
            "流程",
            "教我",
            "帮我整理",
            "给我一个",
            "换个说法",
            "话术",
        )
    ):
        intent = "request_help"
    elif any(word in compact for word in ("稍后", "等会", "一会儿", "现在忙", "晚点")):
        intent = "defer"
    elif compact in {
        "开始",
        "好",
        "行",
        "可以",
        "继续",
        "需要",
        "你问吧",
        "现在弄",
        "现在开始处理",
        "开始处理",
        "开始处理我的任务",
        "开始处理我现在的任务",
        "一项一项的开始现在处理",
        "继续下一个任务",
        "开始下一个任务",
        "处理下一个任务",
        "收到",
        "我收到了",
        "知道了",
        "我知道了",
    } or ("\u4efb\u52a1" in compact and any(word in compact for word in ("\u5148\u505a", "\u5f00\u59cb", "\u5904\u7406", "\u505aA\u7ea7", "\u505aA", "\u505a\u4e00\u4e0b"))):
        intent = "start_processing"
    elif any(
        word in compact
        for word in (
            "完成了",
            "弄好了",
            "已处理",
            "给家长说了",
            "跟家长说了",
            "已沟通",
        )
    ):
        intent = "complete_pending_confirmation"
    else:
        intent = "fact_supplement"
    return {"intent": intent, "text": raw}


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _task_type(task: dict[str, Any]) -> str:
    return str(task.get("type") or "")


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _is_parent_follow_up_task(task: dict[str, Any]) -> bool:
    task_type = _task_type(task)
    return task_type in {"parent_anxiety", "parent_complaint", "renewal_risk"} or _contains_any(
        task_type,
        ("家长", "续费", "投诉", "沟通"),
    )


def _is_new_student_task(task: dict[str, Any], keyword: str) -> bool:
    task_type = _task_type(task)
    return "新生跟进" in task_type and keyword in task_type


def closure_missing_fields(task: dict[str, Any], evidence: str) -> list[str]:
    text = str(evidence or "").replace(" ", "")
    task_type = _task_type(task)
    missing: list[str] = []
    if _is_parent_follow_up_task(task):
        if not _contains_any(
            text,
            (
                "妈妈说",
                "爸爸说",
                "家长说",
                "家长表示",
                "家长反馈",
                "家长回复",
                "家长已知情",
                "家长知情",
                "家长沟通了",
                "已沟通家长",
                "已经沟通",
                "已沟通",
                "同意",
                "认可",
                "观察",
                "不满",
                "没回",
                "接受",
                "理解",
                "担心",
                "还可以",
            ),
        ):
            missing.append("parent_attitude")
        if not _contains_any(
            text,
            ("下一步", "明天", "后天", "再反馈", "再联系", "继续", "跟进", "观察两天"),
        ):
            missing.append("next_step")
    elif task_type == "safety_incident":
        fields = _as_dict(task.get("closure_fields"))
        if not _contains_any(
            text,
            (
                "家长已知情",
                "妈妈已知情",
                "爸爸已知情",
                "已告知家长",
                "通知家长",
                "已经给家长说",
                "已经跟家长说",
                "给家长说了",
                "跟家长说了",
                "家长已知晓",
                "家长知情",
                "家长也知情",
                "家长已经知道",
                "家长表示知道",
                "家长表示知道了",
                "家长不知道",
                "家长不知情",
                "不知情",
                "还不知道",
                "还没给",
                "没有给",
                "暂未通知",
                "没通知",
            ),
        ) and not fields.get("parent_informed"):
            missing.append("parent_informed")
        if not _contains_any(
            text,
            (
                "孩子目前",
                "孩子现在",
                "当前状态",
                "孩子当前状态",
                "状态",
                "正常",
                "还可以",
                "没事",
                "没啥事",
                "没什么事",
                "没有大碍",
                "无大碍",
                "问题不大",
                "无异常",
                "没有红肿",
                "没有疼",
                "不舒服",
                "已恢复",
                "疼",
                "出血",
                "红肿",
            ),
        ) and not fields.get("child_status"):
            missing.append("child_status")
        if not _contains_any(
            text,
            (
                "已处理",
                "做了处理",
                "已经处理",
                "处理了",
                "安抚",
                "已安抚",
                "进行了安抚",
                "消毒",
                "冰敷",
                "冷敷",
                "凉毛巾",
                "扶起来",
                "查看膝盖",
                "没有破皮",
                "送医",
                "检查",
                "采取",
                "已采取处理",
                "提醒",
                "叮嘱",
                "靠右",
                "慢走",
                "简单清理",
                "清理",
                "擦药",
                "包扎",
            ),
        ) and not fields.get("action_taken"):
            missing.append("action_taken")
        if not _contains_any(
            text,
            (
                "继续观察",
                "观察",
                "无需跟进",
                "无需要跟进",
                "不用跟进",
                "不用再跟进",
                "不需要跟进",
                "不需要了",
                "需要跟进",
                "明天复查",
                "明天回访",
                "明天再回访",
                "明天再看",
                "后续",
                "如果",
                "马上通知",
            ),
        ) and not fields.get("followup_plan"):
            missing.append("follow_up_needed")
    elif not _contains_any(text, ("时间", "今天", "明天", "已", "结果", "完成")):
        missing.append("result")
    return missing


def extract_safety_closure_fields(evidence: str) -> dict[str, str]:
    """Extract the four safety-closure fields from natural or labelled text."""
    raw = str(evidence or "").strip()
    compact = raw.replace(" ", "")
    fields: dict[str, str] = {}
    label_patterns = {
        "child_status": r"(?:孩子当前状态|当前状态)[:：]\s*(.*?)(?=(?:已采取处理|处理措施|家长是否知情|后续观察安排|后续安排)[:：]|$)",
        "action_taken": r"(?:已采取处理|处理措施|已经处理|已做处理)[:：]\s*(.*?)(?=(?:孩子当前状态|当前状态|家长是否知情|后续观察安排|后续安排)[:：]|$)",
        "parent_informed": r"(?:家长是否知情|家长知情|是否告知家长)[:：]\s*(.*?)(?=(?:孩子当前状态|当前状态|已采取处理|处理措施|后续观察安排|后续安排)[:：]|$)",
        "followup_plan": r"(?:后续观察安排|后续安排|后续跟进)[:：]\s*(.*?)(?=(?:孩子当前状态|当前状态|已采取处理|处理措施|家长是否知情)[:：]|$)",
    }
    for key, pattern in label_patterns.items():
        match = re.search(pattern, raw, flags=re.S)
        if match:
            value = _clean_field_value(match.group(1))
            if value:
                fields[key] = value
    clauses = [
        _clean_field_value(part)
        for part in re.split(r"[；;\n。]+", raw)
        if _clean_field_value(part)
    ]
    if "child_status" not in fields:
        status_parts = [
            part
            for part in clauses
            if _contains_any(
                part.replace(" ", ""),
                ("孩子当前", "孩子现在", "当前", "状态", "疼", "出血", "红肿", "活动正常", "情绪稳定", "无异常", "没有大碍"),
            )
        ]
        if status_parts:
            fields["child_status"] = "；".join(status_parts[:2])
    if "action_taken" not in fields:
        action_parts = [
            part
            for part in clauses
            if _contains_any(
                part.replace(" ", ""),
                ("已处理", "做了处理", "已经处理", "提醒", "叮嘱", "靠右", "慢走", "消毒", "冰敷", "冷敷", "凉毛巾", "扶起来", "查看膝盖", "没有破皮", "检查", "安抚", "送医"),
            )
        ]
        if action_parts:
            fields["action_taken"] = "；".join(action_parts[:2])
    if "parent_informed" not in fields:
        parent_parts = [
            part
            for part in clauses
            if _contains_any(part.replace(" ", ""), ("家长", "妈妈", "爸爸"))
            and _contains_any(part.replace(" ", ""), ("告知", "通知", "知情", "知道", "沟通"))
        ]
        if parent_parts:
            fields["parent_informed"] = "；".join(parent_parts[:2])
    if "followup_plan" not in fields:
        follow_parts = [
            part
            for part in clauses
            if _contains_any(part.replace(" ", ""), ("后续", "继续观察", "明天", "这两天", "跟进", "复查", "回访"))
        ]
        if follow_parts:
            fields["followup_plan"] = "；".join(follow_parts[:2])
    return fields


def _clean_field_value(value: str) -> str:
    return str(value or "").strip(" \t\r\n；;。")


def _merge_safety_closure_fields(task: dict[str, Any], evidence: str) -> None:
    if task.get("type") != "safety_incident":
        return
    current = _as_dict(task.get("closure_fields")).copy()
    for key, value in extract_safety_closure_fields(evidence).items():
        if value:
            current[key] = value
    if current:
        task["closure_fields"] = current


def _merge_general_closure_fields(task: dict[str, Any], evidence: str) -> None:
    if task.get("type") == "safety_incident":
        return
    raw = str(evidence or "").strip()
    compact = raw.replace(" ", "")
    current = _as_dict(task.get("closure_fields")).copy()
    if "action_taken" not in current:
        match = re.search(r"(我(?:又)?让.*?)(?=。|；|;|$)", raw)
        if match:
            current["action_taken"] = _clean_field_value(match.group(1))
        elif "重新做" in compact or "订正" in compact:
            current["action_taken"] = "已安排学生重新订正并讲解思路"
    if "result" not in current:
        result_parts: list[str] = []
        if "订正后还错" in compact:
            match = re.search(r"订正后还错\d+道", raw)
            result_parts.append(match.group(0) if match else "订正后仍有错误")
        if "能说出错因" in compact:
            result_parts.append("最后能说出错因")
        if result_parts:
            current["result"] = "；".join(result_parts)
    if "parent_informed" not in current:
        if any(word in compact for word in ("家长这边暂未", "暂未单独沟通", "未单独沟通", "没沟通家长")):
            current["parent_informed"] = "家长暂未单独沟通"
        elif any(word in compact for word in ("家长已知情", "已和家长沟通", "已经和家长沟通", "家长表示")):
            current["parent_informed"] = "已和家长沟通"
    if "followup_plan" not in current:
        follow_match = re.search(r"(如果.*?。|明天.*?。|后续.*?。)", raw)
        if follow_match:
            current["followup_plan"] = _clean_field_value(follow_match.group(1))
        elif any(word in compact for word in ("明天", "继续关注", "后续", "如果")):
            current["followup_plan"] = "后续继续跟进观察"
    if current:
        task["closure_fields"] = current


def _closure_prompt(task: dict[str, Any], missing: list[str]) -> str:
    labels = {
        "parent_attitude": "家长现在是什么态度",
        "next_step": "下一步准备何时跟进",
        "parent_informed": "家长是否已知情",
        "child_status": "孩子当前状态如何",
        "action_taken": "已经采取了什么处理",
        "follow_up_needed": "后续是否还需要跟进",
        "result": "具体处理结果和时间",
    }
    prefix = "安全任务需要严格闭环。" if task.get("type") == "safety_incident" else "还差一点闭环信息。"
    questions = "；".join(labels[item] for item in missing)
    return f"{prefix}\n请补充：{questions}。"


def _start_guidance(task: dict[str, Any]) -> str:
    if task.get("type") == "safety_incident":
        student = str(task.get("student_name") or "孩子")
        source = _parent_safe_text(str(task.get("source_text") or "今天发生了安全情况"))
        return "\n".join(
            [
                "好，我们先按安全事件跟进处理。",
                "",
                f"已记录：{source}",
                "",
                "请先确认孩子当前状态：有没有疼痛、出血、红肿、活动受限或情绪异常。",
                "现场先做基础处理：需要清理、消毒、冰敷或送医的先处理，并拍照留档。",
                "如果家长还不知情，请现在通知家长，说明事实、当前状态、已处理措施和后续观察安排。",
                "",
                "可直接这样说：",
                f"{student}家长您好，跟您同步一下孩子今天的安全情况：{source}。我们已经先确认孩子当前状态并做了基础处理，后续会继续观察，如有不舒服或异常会马上联系您。",
                "",
                "处理后回复我：孩子当前状态、已做处理、家长是否知情、后续观察安排。信息齐了再回复“完成了”。",
            ]
        )
    student = str(task.get("student_name") or "孩子")
    source = _parent_safe_text(str(task.get("source_text") or task.get("title") or "这件事"))
    if _is_new_student_task(task, "当晚总结"):
        return "\n".join(
            [
                f"好，我们来补 {student} 的新生当晚总结。",
                "",
                "请按这几个点回复我：",
                "1. 午休：是否睡着、情绪是否稳定。",
                "2. 吃饭：饭量、挑食、是否需要提醒。",
                "3. 和小朋友相处：是否主动、有没有冲突或明显不适应。",
                "4. 学习/作业：配合度、注意力、需要明天重点关注什么。",
                "",
                "你可以直接自然描述，我会帮你整理成记录。信息齐了再回复“完成了”。",
            ]
        )
    if _is_new_student_task(task, "家长问候"):
        return "\n".join(
            [
                f"好，我们处理 {student} 的新生家长问候任务。",
                "",
                "我不会替你转发给家长，只帮你整理沟通重点和记录闭环。",
                "建议先和家长同步孩子适应情况：情绪、吃饭、午休、和同伴相处，以及明天会继续关注的点。",
                "",
                "可参考话术：",
                f"{student}家长您好，跟您同步一下孩子这两天在托管的适应情况。整体我们会重点关注孩子的情绪、吃饭午休和同伴相处，明天也会继续观察，有情况及时跟您反馈。",
                "",
                "沟通后把你怎么说的、家长反应、下一步安排回复我。信息齐了再回复“完成了”。",
            ]
        )
    if _is_new_student_task(task, "催单报告"):
        return "\n".join(
            [
                f"好，我们核对 {student} 的新生观察报告。",
                "",
                "请先确认报告里的事实是否准确：到校表现、作业/学习状态、吃饭午休、同伴相处、需要家长配合的点。",
                "如果已经和家长沟通，请记录你怎么说的、家长反应、后续是否继续跟进。",
                "",
                "确认或补充后回复我，信息齐了再回复“完成了”。",
            ]
        )
    if _is_parent_follow_up_task(task):
        if _contains_any(source, ("已沟通", "沟通了", "和家长沟通", "跟家长沟通", "妈妈沟通", "爸爸沟通", "家长表示")):
            return "\n".join(
                [
                    "好，这条按已沟通后的跟进任务处理。",
                    "",
                    f"已有情况：{source}",
                    "",
                    "请补充：家长态度、你如何回应、孩子后续观察安排、下一次跟进时间，以及是否需要再次同步家长。",
                    "信息齐了再回复“完成了”。",
                ]
            )
        return "\n".join(
            [
                "好，我们按家长沟通任务处理。",
                "",
                f"已有情况：{source}",
                "",
                "先核对事实：孩子最近真实表现是什么，哪些是已经确认的，哪些还需要观察。",
                "再拆家长关注点：家长是在担心成绩、习惯、情绪、服务体验，还是续费风险。",
                "沟通时先回应家长感受，再说明我们已关注到的问题和下一步安排。",
                "",
                "可参考话术：",
                f"{student}家长您好，您反馈的情况我们已经重点关注。今天我会先核对孩子在托管里的具体表现，确认问题点后给您一个清楚反馈，也会安排后续跟进。",
                "",
                "沟通后回复我：家长当前态度、你怎么回复的、下一步何时跟进。信息齐了再回复“完成了”。",
            ]
        )
    lines = ["好，我们开始。", "", "我先整理已有情况："]
    if task.get("source_text"):
        lines.append(f"- 原始记录：{task['source_text']}")
    if task.get("trigger_reason"):
        lines.append(f"- 判断原因：{task['trigger_reason']}")
    if task.get("tags"):
        lines.append(f"- 相关标签：{'、'.join(task['tags'][:6])}")
    lines.extend(["", _generic_task_prompt(task)])
    return "\n".join(lines)


def _help_script(task: dict[str, Any]) -> str:
    student = str(task.get("student_name") or "孩子")
    source = _parent_safe_text(str(task.get("source_text") or "孩子近期的情况"))
    if task.get("type") == "safety_incident":
        return (
            "可以，下面这段可直接发给家长：\n\n"
            f"{student}家长您好，我跟您同步一下孩子今天的安全情况。"
            f"目前记录到：{source}。我们已经先确认孩子当前状态，"
            "并会继续观察后续有没有不舒服或异常反应。\n\n"
            "发送后请补充：家长是否知情、孩子当前状态、处理措施和后续安排。"
        )
    if task.get("type") == "renewal_risk":
        return "\n".join(
            [
                "可以，按续费电话沟通来打，不要一上来催续费。",
                "",
                "电话顺序：",
                "1. 先确认家长现在方便说几分钟。",
                f"2. 先讲 {student} 最近真实表现：作业效率、学习习惯、需要提醒的地方。",
                "3. 再问家长顾虑：是效果、时间、费用，还是孩子意愿。",
                "4. 针对顾虑给下一步安排，不要只说“续费”。",
                "5. 最后约定明确时间：今天/明天给反馈，或约下次沟通。",
                "",
                "可参考话术：",
                f"{student}家长您好，我想跟您电话沟通一下孩子后续托管和续费的事。"
                "我不是单纯催您续费，主要是先把孩子近期表现、还需要我们继续盯的地方，"
                "以及接下来怎么帮孩子稳定下来跟您说清楚。您也可以直接跟我说，"
                "现在最担心的是效果、时间安排，还是费用，我好针对这个给您一个具体方案。",
                "",
                "电话后请记录：家长主要顾虑、你怎么回应、家长是否有续费意向、下一次何时跟进。",
            ]
        )
    if _is_parent_follow_up_task(task):
        return (
            "可以，下面是老师可参考的话术。Hermes 不会替你转发，沟通后请把结果记录回来：\n\n"
            f"{student}家长您好，您反馈的“{source}”我们已经重点关注。"
            "我会先核对孩子在托管里的实际表现，确认具体问题点后，再把观察结果和后续安排同步给您。\n\n"
            "沟通后请补充：家长态度、你怎么回复、下一步何时跟进。"
        )
    return (
        "可以，下面这段可直接发给家长：\n\n"
        f"{student}家长您好，您提到的“{source}”，我们已经重点关注。"
        "今天会继续观察孩子的具体表现，确认问题卡点后再把结果和建议反馈给您。\n\n"
        "发送后把家长态度和下一步安排回复给我。"
    )


def _fact_added_reply(task: dict[str, Any], missing: list[str]) -> str:
    if not missing:
        if task.get("type") != "safety_incident":
            return f"本次{task.get('level', 'C')}级任务处理情况已记录，闭环信息基本完整，可以回复“完成了”提交任务闭环。"
        return "已记录。可以继续补充，或回复“完成了”让我检查闭环信息。"
    labels = {
        "parent_attitude": "家长当前态度",
        "next_step": "下一步跟进安排",
        "parent_informed": "家长是否已知情",
        "child_status": "孩子当前状态",
        "action_taken": "已做处理",
        "follow_up_needed": "后续观察/跟进安排",
        "result": "处理结果和时间",
    }
    missing_text = "；".join(labels[item] for item in missing)
    if task.get("type") == "safety_incident":
        understood = _safety_evidence_summary(
            str(task.get("evidence_summary") or "")
        )
        understood_line = (
            "已记录。我已经记下：" + "、".join(understood) + "。\n"
            if understood
            else ""
        )
        if missing == ["child_status"]:
            return (
                f"{understood_line}只差最后一项：孩子当前状态。\n"
                "请直接回复一句，例如：孩子现在状态正常，没有红肿也没有疼；"
                "或：孩子还有点疼/红肿，我会继续观察并通知家长。"
            )
        return (
            f"{understood_line}还缺：{missing_text}。\n"
            "安全任务先确认孩子当前状态，必要时拍照留档；如果家长还不知情，建议现在通知家长。"
            "后续继续观察/跟进安排也要说清楚。你继续自然描述即可，我会根据已记录内容接着判断；"
            "如果你认为已经齐了，也可以回复“完成了”让我检查。"
        )
    return f"已记录。还缺：{missing_text}。补齐后回复“完成了”让我检查闭环。"


def _safety_evidence_summary(evidence: str) -> list[str]:
    text = str(evidence or "").replace(" ", "")
    summary: list[str] = []
    if _contains_any(
        text,
        (
            "家长不知道",
            "家长不知情",
            "不知情",
            "还不知道",
            "还没给",
            "没有给",
            "暂未通知",
            "没通知",
        ),
    ):
        summary.append("家长暂未知情")
    elif _contains_any(
        text,
        (
            "家长已知情",
            "妈妈已知情",
            "爸爸已知情",
            "已告知家长",
            "通知家长",
            "已经给家长说",
            "已经跟家长说",
            "给家长说了",
            "跟家长说了",
            "家长已知晓",
            "家长知情",
            "家长也知情",
            "家长已经知道",
        ),
    ):
        summary.append("家长已知情")
    if _contains_any(
        text,
        ("已处理", "做了处理", "已经处理", "处理了", "安抚", "已安抚", "进行了安抚", "消毒", "冰敷", "送医", "检查", "简单清理", "清理", "擦药", "包扎"),
    ):
        summary.append("已做基础处理")
    if _contains_any(
        text,
        ("无需跟进", "不用跟进", "不用再跟进", "不需要跟进", "不需要了"),
    ):
        summary.append("后续无需继续跟进")
    elif _contains_any(
        text,
        ("继续观察", "观察", "需要跟进", "明天复查", "明天回访", "明天再回访", "明天再看", "后续", "马上通知"),
    ):
        summary.append("后续会继续观察")
    if _contains_any(
        text,
        ("孩子目前", "孩子现在", "当前状态", "状态正常", "无异常", "没有红肿", "没有疼", "已恢复", "没有大碍", "无大碍", "没事", "问题不大"),
    ):
        summary.append("孩子当前状态已说明")
    return summary


def _append_unique_evidence(previous: str, reply_text: str) -> str:
    existing = [line.strip() for line in str(previous or "").splitlines() if line.strip()]
    reply = str(reply_text or "").strip()
    if reply and not any(reply == line or reply in line for line in existing):
        existing.append(reply)
    return "\n".join(existing)


def _parent_safe_text(text: str) -> str:
    cleaned = str(text or "")
    replacements = {
        "安全记录：": "",
        "安全记录:": "",
        "安全记录": "今天的情况",
        "S级任务": "这件事",
        "A级任务": "这件事",
        "闭环": "后续跟进",
        "入档": "记录",
        "绩效": "工作记录",
        "必达项": "需要跟进的事项",
        "任务编号": "",
        "记录编号": "",
        "系统识别": "我们关注到",
        "老板端": "",
        "H5": "页面",
    }
    for old, new in replacements.items():
        cleaned = cleaned.replace(old, new)
    polish = {
        "看查看": "查看",
        "马上看查看": "马上查看",
        "进门进出门": "进出门",
        "进门出门": "进出门",
        "进出门进出门": "进出门",
    }
    for old, new in polish.items():
        cleaned = cleaned.replace(old, new)
    return cleaned.strip(" ，,。；;\n")


def _generic_task_prompt(task: dict[str, Any]) -> str:
    text = f"{task.get('title') or ''}{task.get('source_text') or ''}{task.get('type') or ''}"
    if _contains_any(text, ("午休", "睡觉", "休息", "坐不住", "行为", "情绪", "纪律")):
        return "请补充孩子今天的午休表现、老师采取了什么处理、结果变化如何、明天是否继续观察。信息齐了再回复“完成了”。"
    if _contains_any(text, ("学习", "作业", "数学", "计算", "订正", "阅读", "书写", "错题")):
        return "请补充孩子今天的学习表现、错因或问题点、老师处理动作、订正/完成结果、下一步跟进安排。信息齐了再回复“完成了”。"
    return "请补充实际处理动作、结果变化和下一步安排。信息齐了再回复“完成了”。"


def _append_closure_event(
    task: dict[str, Any],
    *,
    action: str,
    actor_userid: str,
    text: str,
    timestamp: datetime,
    missing: list[str] | None = None,
) -> None:
    events = task.get("closure_events")
    if not isinstance(events, list):
        events = []
    events.append(
        {
            "at": timestamp.isoformat(timespec="seconds"),
            "by": actor_userid,
            "action": action,
            "status": str(task.get("status") or ""),
            "coach_stage": str(task.get("coach_stage") or ""),
            "text": str(text or "").strip(),
            "missing_fields": list(missing or []),
        }
    )
    task["closure_events"] = events[-50:]


def _next_task_message(tasks: list[dict[str, Any]], userid: str) -> str:
    next_task = current_task_for_user(tasks, userid)
    if not next_task:
        return "当前没有其他待处理任务。"
    level = str(next_task.get("level") or "C")
    if level == "S":
        return (
            f"当前还有一个 S 级任务待处理："
            f"{next_task.get('title', '未命名任务')}。\n请优先处理，回复“继续”即可开始。"
        )
    return (
        f"当前还有 1 个 {level} 级任务待处理，可稍后处理："
        f"{next_task.get('title', '未命名任务')}。\n回复“继续”即可开始。"
    )


def _next_reminder(level: str, now: datetime) -> datetime:
    return now + {
        "S": timedelta(minutes=15),
        "A": timedelta(hours=1),
        "B": timedelta(hours=6),
        "C": timedelta(days=1),
    }.get(level, timedelta(days=1))


def cancel_task(
    task: dict[str, Any],
    operator_userid: str,
    operator_role: str,
    reason: str,
    *,
    now: datetime | None = None,
) -> TaskReplyResult:
    if task.get("level") == "S" and operator_role != "boss":
        return TaskReplyResult(
            action="forbidden",
            reply="S级安全任务不能直接取消，请补充说明，由负责人完成安全闭环或联系老板确认。",
            task_id=str(task.get("id") or ""),
        )
    if operator_role == "manager" and task.get("level") in {"S", "A"}:
        return TaskReplyResult(
            action="forbidden",
            reply=f"{task.get('level')}级任务不能由店长取消，请补充说明或升级。",
            task_id=str(task.get("id") or ""),
        )
    timestamp = now or datetime.now()
    task["status"] = "cancelled"
    task["coach_stage"] = "closed"
    task["cancelled_at"] = timestamp.isoformat(timespec="seconds")
    task["cancelled_by"] = operator_userid
    task["cancel_reason"] = reason
    task["updated_at"] = timestamp.isoformat(timespec="seconds")
    return TaskReplyResult(
        action="cancelled",
        reply="任务已取消，原因已经记录。",
        task_id=str(task.get("id") or ""),
    )


def apply_task_reply(
    tasks: list[dict[str, Any]],
    userid: str,
    reply_text: str,
    *,
    now: datetime | None = None,
    task_id: str | None = None,
) -> TaskReplyResult:
    task = None
    if task_id:
        task = next(
            (
                item
                for item in tasks
                if str(item.get("id") or "") == str(task_id)
                and item.get("assignee_userid") == userid
                and item.get("status") not in _CLOSED_STATUSES
            ),
            None,
        )
    if task is None:
        task = current_task_for_user(tasks, userid)
    if not task:
        return TaskReplyResult("no_task", "你当前暂无待处理任务。")
    timestamp = now or datetime.now()
    task["updated_at"] = timestamp.isoformat(timespec="seconds")
    intent = classify_task_reply(reply_text)["intent"]
    task_id = str(task.get("id") or "")

    def finish(action: str, reply: str, missing: list[str] | None = None) -> TaskReplyResult:
        _append_closure_event(
            task,
            action=action,
            actor_userid=userid,
            text=reply_text,
            timestamp=timestamp,
            missing=missing,
        )
        return TaskReplyResult(action, reply, task_id)

    if intent == "start_processing":
        task["status"] = "active"
        task["coach_stage"] = "active_analysis"
        return finish("started", _start_guidance(task))
    if intent == "defer":
        task["status"] = "deferred"
        task["coach_stage"] = "waiting_start"
        task["defer_count"] = int(task.get("defer_count") or 0) + 1
        task["next_remind_at"] = _next_reminder(
            str(task.get("level") or "C"),
            timestamp,
        ).isoformat(timespec="seconds")
        return finish("deferred", "好的，稍后我会再次提醒。")
    if intent == "cancel":
        result = cancel_task(task, userid, "teacher", reply_text, now=timestamp)
        _append_closure_event(
            task,
            action=result.action,
            actor_userid=userid,
            text=reply_text,
            timestamp=timestamp,
        )
        return result
    if intent == "request_help":
        task["status"] = "active"
        task["coach_stage"] = "helping_reply"
        return finish("helped", _help_script(task))
    if intent == "complete_pending_confirmation":
        combined = "\n".join(
            part for part in (str(task.get("evidence_summary") or ""), reply_text) if part
        )
        _merge_safety_closure_fields(task, combined)
        missing = closure_missing_fields(task, combined)
        if missing:
            task["evidence_summary"] = _append_unique_evidence(
                str(task.get("evidence_summary") or ""),
                reply_text,
            )
            task["status"] = "waiting_confirmation"
            task["coach_stage"] = "waiting_closure_evidence"
            return finish(
                "needs_closure_evidence",
                _closure_prompt(task, missing),
                missing,
            )
        task["status"] = "completed"
        task["coach_stage"] = "closed"
        task["closure_summary"] = combined
        task["completed_at"] = timestamp.isoformat(timespec="seconds")
        return finish(
            "completed",
            "任务已完成，闭环结果已经记录。\n" + _next_task_message(tasks, userid),
        )

    previous = str(task.get("evidence_summary") or "")
    task["evidence_summary"] = _append_unique_evidence(previous, reply_text)
    _merge_safety_closure_fields(task, task["evidence_summary"])
    _merge_general_closure_fields(task, task["evidence_summary"])
    if task.get("type") == "safety_incident":
        missing = closure_missing_fields(task, task["evidence_summary"])
        if not missing:
            task["status"] = "waiting_confirmation"
            task["coach_stage"] = "ready_for_completion"
            return finish(
                "closure_ready",
                "闭环信息已补齐，可以回复“完成了”提交任务闭环。",
            )
        task["status"] = "waiting_confirmation"
        task["coach_stage"] = "waiting_closure_evidence"
        return finish(
            "fact_added",
            _fact_added_reply(task, missing),
            missing,
        )
    if task.get("status") == "waiting_confirmation":
        missing = closure_missing_fields(task, task["evidence_summary"])
        if not missing:
            task["status"] = "completed"
            task["coach_stage"] = "closed"
            task["closure_summary"] = task["evidence_summary"]
            task["completed_at"] = timestamp.isoformat(timespec="seconds")
            return finish(
                "completed",
                "任务已完成，闭环结果已经记录。\n"
                + _next_task_message(tasks, userid),
            )
        task["coach_stage"] = "waiting_closure_evidence"
        return finish(
            "fact_added",
            _fact_added_reply(task, missing),
            missing,
        )
    task["status"] = "active"
    task["coach_stage"] = "collecting_evidence"
    return finish(
        "fact_added",
        _fact_added_reply(task, []),
    )
