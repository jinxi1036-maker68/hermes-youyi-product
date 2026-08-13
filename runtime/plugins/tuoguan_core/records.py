"""Student recognition, record classification, and task triggering."""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from .dashboard_builder import refresh_dashboard_cache
from .record_evaluation import evaluate_record
from .store import TuoguanStore
from .tasks import build_task_contract


class StudentRecognitionError(ValueError):
    pass


class UnknownStudentError(StudentRecognitionError):
    pass


class AmbiguousStudentError(StudentRecognitionError):
    pass


def _refresh_dashboard_cache_best_effort(store: TuoguanStore) -> None:
    try:
        refresh_dashboard_cache(store, create_operation_tasks=False)
    except Exception:
        pass


def _add_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _contains_any(text: str, words: tuple[str, ...] | list[str]) -> bool:
    return any(word in text for word in words)


def _evidence_span(
    *,
    record_type: str,
    text: str,
    reason: str,
    confidence: float,
) -> dict[str, Any]:
    return {
        "record_type": record_type,
        "text": text,
        "reason": reason,
        "confidence": round(float(confidence), 2),
    }


def recognize_student(text: str, store: TuoguanStore) -> str:
    students = store.read_json("students.json", {})
    if not isinstance(students, dict):
        raise UnknownStudentError("Student registry is unavailable")
    matches: list[str] = []
    for name, payload in students.items():
        terms = [str(name)]
        if isinstance(payload, dict):
            terms.extend(str(alias) for alias in payload.get("aliases") or [])
        if any(term and term in text for term in terms):
            matches.append(str(name))
    matches = list(dict.fromkeys(matches))
    if not matches:
        raise UnknownStudentError("No student could be identified")
    if len(matches) > 1:
        raise AmbiguousStudentError("Student identity is ambiguous")
    return matches[0]


def analyze_teacher_record(text: str, store: TuoguanStore) -> dict[str, Any]:
    raw = str(text or "").strip()
    student_name = recognize_student(raw, store)
    students = store.read_json("students.json", {})
    student_profile = (
        students.get(student_name, {}) if isinstance(students, dict) else {}
    )
    campus_id = (
        str(student_profile.get("campus_id") or "")
        if isinstance(student_profile, dict)
        else ""
    )
    compact = raw.replace(" ", "")
    record_types: list[str] = []
    tags: list[str] = []
    reasons: list[str] = []
    evidence_spans: list[dict[str, Any]] = []

    # 否定词前置：命中这些词且安全关键词紧邻否定词时，跳过误触发
    negative_words = ["没有", "没", "不", "未", "无", "不曾"]
    safety_words = [
        "摔倒", "摔伤", "摔跤",
        "受伤", "磕碰", "磕破", "碰到头", "碰了头", "碰头",
        "撞到头", "撞了头", "磕到头", "磕了头", "摔到头", "摔了头",
        "夹手", "夹了一下", "门缝夹", "被门夹",
        "呛到", "呛水", "呛饭", "流血", "流鼻血", "出血",
        "肚子疼", "头疼", "身体不舒服", "不舒服", "发烧", "接送异常", "未接到", "接错",
        "冲突", "打架", "互殴",
    ]

    groups = {
        "safety_incident": [
            "摔倒", "摔伤", "摔跤", "受伤", "磕碰", "磕破",
            "碰到头", "碰了头", "碰头", "撞到头", "撞了头",
            "磕到头", "磕了头", "摔到头", "摔了头",
            "夹手", "夹了一下", "门缝夹", "被门夹",
            "呛到", "呛水", "呛饭", "流血", "流鼻血", "出血",
            "肚子疼", "头疼", "身体不舒服", "不舒服", "发烧", "接送异常", "未接到", "接错",
            "冲突", "打架", "互殴",
        ],
        "parent_complaint": ["不满意", "不满", "投诉", "生气", "要说法", "质疑", "曝光"],
        "new_trial": ["体验生", "新生", "新来的", "第一天", "试听", "试托"],
        "renewal_risk": ["续费", "不续", "退费", "转走", "再看看", "考虑一下"],
        "academic_issue": ["数学", "计算", "语文", "英语", "阅读", "书写", "作文", "错题", "不会", "正确率"],
        "learning_habit": ["拖拉", "粗心", "注意力", "走神", "依赖", "磨蹭", "反复提醒", "效率低"],
        "positive_progress": ["进步", "主动", "认真", "变好", "提升", "不错", "很好", "独立", "表扬"],
        "meal_care": ["午餐", "吃饭", "米饭", "青菜", "挑食", "进餐", "加饭", "饭量", "汤"],
        "nap_care": ["午休", "睡觉", "休息", "睡着", "躺下"],
        "life_care": ["排队", "进教室", "出教室", "洗手", "喝水", "如厕", "整理书包"],
        "activity_care": ["体育", "活动", "运动", "跑步", "科学实验", "实验", "户外", "跳绳"],
        "behavior_observation": ["情绪", "纪律", "坐不住", "哭闹", "吵闹", "互动", "同学", "提醒后"],
    }
    reason_map = {
        "safety_incident": "识别到安全异常信号",
        "parent_complaint": "识别到家长不满或投诉信号",
        "new_trial": "识别到新生或体验生信号",
        "renewal_risk": "识别到续费或退费风险",
        "academic_issue": "识别到学科问题",
    }

    def _has_negative_before(keyword: str, text: str, window: int = 8) -> bool:
        """判断 keyword 前面 window 个字符内是否有否定词。"""
        idx = text.find(keyword)
        if idx <= 0:
            return False
        start = max(0, idx - window)
        prefix = text[start:idx]
        # “不小心夹到/磕到”是在描述意外发生，不是否定安全事件。
        prefix = prefix.replace("不小心", "")
        return any(neg in prefix for neg in negative_words)

    def _is_safety_negated(keyword: str, text: str) -> bool:
        """安全类关键词被否定词否定（如'没有磕碰''没有不舒服'），返回 True。"""
        if keyword not in safety_words:
            return False
        return _has_negative_before(keyword, text)

    def _has_unnegated_any(words: tuple[str, ...] | list[str]) -> bool:
        return any(word in compact and not _has_negative_before(word, compact) for word in words)

    for record_type, words in groups.items():
        matched = False
        for word in words:
            if record_type == "safety_incident" and _is_safety_negated(word, compact):
                continue
            if word in compact:
                matched = True
                break
        if matched:
            _add_unique(record_types, record_type)
            if record_type in reason_map:
                reasons.append(reason_map[record_type])
                evidence_spans.append(
                    _evidence_span(
                        record_type=record_type,
                        text=word,
                        reason=reason_map[record_type],
                        confidence=0.9 if record_type == "safety_incident" else 0.82,
                    )
                )

    body_parts = (
        "头", "额头", "脸", "眼睛", "鼻子", "嘴", "牙", "手", "胳膊", "腿", "膝盖", "肚子", "脚", "腰",
    )
    impact_words = ("碰", "撞", "磕", "摔", "砸", "划", "扭", "崴", "夹", "烫")
    symptom_words = (
        "疼", "痛", "肿", "红", "青", "紫", "包", "破", "哭", "吐", "晕", "流血", "出血", "严重", "不舒服",
    )
    if (
        "safety_incident" not in record_types
        and _has_unnegated_any(impact_words)
        and _contains_any(compact, body_parts)
    ):
        _add_unique(record_types, "safety_incident")
        _add_unique(reasons, "识别到身体碰撞或受伤风险")
        evidence_spans.append(
            _evidence_span(
                record_type="safety_incident",
                text="身体部位+碰撞动作",
                reason="口语表达中出现身体部位和碰撞动作组合",
                confidence=0.86,
            )
        )
    if (
        "safety_incident" not in record_types
        and _contains_any(compact, body_parts)
        and _has_unnegated_any(symptom_words)
    ):
        _add_unique(record_types, "safety_incident")
        _add_unique(reasons, "识别到身体异常或疼痛信号")
        evidence_spans.append(
            _evidence_span(
                record_type="safety_incident",
                text="身体部位+疼痛/异常",
                reason="口语表达中出现身体部位和疼痛异常组合",
                confidence=0.84,
            )
        )

    parent_words = ["妈妈", "爸爸", "家长", "父母", "奶奶", "爷爷"]
    anxiety_words = ["担心", "焦虑", "问", "咨询", "怎么办", "怎么", "提不上去", "没进步", "效果"]
    if any(word in compact for word in parent_words) and any(
        word in compact for word in anxiety_words
    ):
        _add_unique(record_types, "parent_anxiety")
        _add_unique(tags, "家长关注学习效果")
        reasons.append("识别到家长焦虑或咨询")
        evidence_spans.append(
            _evidence_span(
                record_type="parent_anxiety",
                text="家长+焦虑/咨询",
                reason="识别到家长正在询问或担心学习效果",
                confidence=0.74,
            )
        )
    complaint_words = (
        "不高兴", "不开心", "有意见", "意见很大", "不认可", "不接受", "要退", "要说法", "质问", "吵",
    )
    if _contains_any(compact, parent_words) and _contains_any(compact, complaint_words):
        _add_unique(record_types, "parent_complaint")
        _add_unique(reasons, "识别到家长不满或投诉信号")
        evidence_spans.append(
            _evidence_span(
                record_type="parent_complaint",
                text="家长+不满表达",
                reason="识别到家长不认可、有意见或退费倾向",
                confidence=0.84,
            )
        )
    renewal_words = (
        "到期", "快到期", "该交费", "该缴费", "交费", "缴费", "报名", "报班", "下期", "下个月还来",
    )
    renewal_risk_words = (
        "犹豫", "没定", "再考虑", "再想想", "观望", "嫌贵", "价格", "效果不好", "不想来了", "可能不来",
    )
    if _contains_any(compact, renewal_words) and _contains_any(compact, renewal_risk_words):
        _add_unique(record_types, "renewal_risk")
        _add_unique(reasons, "识别到续费或报名犹豫信号")
        evidence_spans.append(
            _evidence_span(
                record_type="renewal_risk",
                text="续费/到期+犹豫/嫌贵",
                reason="识别到续费节点和家长犹豫信号同时出现",
                confidence=0.8,
            )
        )

    if "safety_incident" in record_types:
        _add_unique(tags, "安全观察")
    if "parent_complaint" in record_types:
        _add_unique(tags, "家长不满")
    if "new_trial" in record_types:
        _add_unique(tags, "新生体验")
    if "renewal_risk" in record_types:
        _add_unique(tags, "续费风险")
    if "academic_issue" in record_types:
        if "数学" in compact or "计算" in compact:
            _add_unique(tags, "数学计算弱")
        if "阅读" in compact:
            _add_unique(tags, "阅读理解弱")
        if "书写" in compact or "写字" in compact:
            _add_unique(tags, "语文书写慢")
    if "learning_habit" in record_types:
        if any(word in compact for word in ["慢", "效率低", "拖拉", "磨蹭"]):
            _add_unique(tags, "作业效率低")
        if "粗心" in compact:
            _add_unique(tags, "粗心")
        if "注意力" in compact or "走神" in compact:
            _add_unique(tags, "注意力不集中")
    if "positive_progress" in record_types:
        _add_unique(tags, "正向成长")
    if "meal_care" in record_types:
        _add_unique(tags, "午餐照护")
    if "nap_care" in record_types:
        _add_unique(tags, "午休照护")
    if "life_care" in record_types:
        _add_unique(tags, "生活照护")
    if "behavior_observation" in record_types:
        _add_unique(tags, "行为表现")

    if not record_types:
        record_types.append("student_daily")
    elif set(record_types) <= {"academic_issue", "learning_habit"}:
        record_types.append("student_daily")

    if {"safety_incident", "parent_complaint", "new_trial"} & set(record_types):
        level, create_task = "S", True
    elif {"parent_anxiety", "renewal_risk"} & set(record_types):
        level, create_task = "A", True
    else:
        level, create_task = "C", False
    if "safety_incident" in record_types:
        confidence = max([0.9] + [float(item.get("confidence") or 0) for item in evidence_spans])
    elif evidence_spans:
        confidence = max(float(item.get("confidence") or 0) for item in evidence_spans)
    elif record_types == ["student_daily"]:
        confidence = 0.56
    else:
        confidence = 0.68

    return {
        "student_name": student_name,
        "campus_id": campus_id,
        "content": raw,
        "source_text": raw,
        "record_types": record_types,
        "tags": tags,
        "level": level,
        "trigger_reason": "；".join(reasons) or "普通记录沉淀",
        "analysis_engine": "hybrid_rules_v2",
        "confidence": round(confidence, 2),
        "evidence_spans": evidence_spans,
        "missing_fields": [],
        "should_create_task": create_task,
    }


def _primary_type(record_types: list[str]) -> str:
    for item in (
        "safety_incident",
        "parent_complaint",
        "new_trial",
        "renewal_risk",
        "parent_anxiety",
        "academic_issue",
        "learning_habit",
        "positive_progress",
        "student_daily",
    ):
        if item in record_types:
            return item
    return "student_daily"


def _task_title(student: str, task_type: str) -> str:
    labels = {
        "safety_incident": "安全风险任务",
        "parent_complaint": "家长投诉处理任务",
        "new_trial": "新生体验跟进任务",
        "renewal_risk": "续费风险任务",
        "parent_anxiety": "家长沟通任务",
        "academic_issue": "观察整理任务",
        "learning_habit": "观察整理任务",
        "positive_progress": "正向反馈任务",
        "student_daily": "记录补充任务",
    }
    return f"{student}{labels[task_type]}"


def build_task_draft(
    analysis: dict[str, Any],
    assignee_userid: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    created = now or datetime.now()
    level = str(analysis.get("level") or "C")
    due_delta, remind_delta = {
        "S": (timedelta(hours=1), timedelta(minutes=15)),
        "A": (timedelta(days=1), timedelta(hours=2)),
        "B": (timedelta(days=2), timedelta(hours=6)),
        "C": (timedelta(days=7), timedelta(days=1)),
    }[level]
    record_types = list(analysis.get("record_types") or [])
    task_type = _primary_type(record_types)
    timestamp = created.isoformat(timespec="seconds")
    title = _task_title(str(analysis["student_name"]), task_type)
    due_at = (created + due_delta).isoformat(timespec="seconds")
    task = {
        "id": f"task_{uuid.uuid4().hex[:12]}",
        "title": title,
        "type": task_type,
        "level": level,
        "status": "pending",
        "student_name": analysis["student_name"],
        "campus_id": analysis.get("campus_id") or "",
        "program_id": analysis.get("program_id") or "regular_tuoguan",
        "assignee_userid": assignee_userid,
        "assignee_role": "teacher",
        "source_type": "verified_student_record",
        "source_authority": "verified_business_record",
        "source_text": analysis.get("source_text") or "",
        "trigger_reason": analysis.get("trigger_reason") or "",
        "analysis_engine": analysis.get("analysis_engine") or "",
        "analysis_confidence": analysis.get("confidence"),
        "evidence_spans": list(analysis.get("evidence_spans") or []),
        "missing_fields": list(analysis.get("missing_fields") or []),
        "record_types": record_types,
        "tags": list(analysis.get("tags") or []),
        "due_at": due_at,
        "next_remind_at": (created + remind_delta).isoformat(timespec="seconds"),
        "defer_count": 0,
        "escalation_count": 0,
        "coach_stage": "waiting_start",
        "evidence_summary": "",
        "closure_summary": "",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    task["task_contract"] = build_task_contract(
        title=title,
        source_text=str(analysis.get("source_text") or ""),
        student_name=str(analysis["student_name"]),
        due_at=due_at,
        business_goal=str(analysis.get("trigger_reason") or title),
        assignee_user_id=assignee_userid,
        assigned_by_role="system",
        known_facts=[str(value) for value in analysis.get("evidence_spans") or [] if str(value)],
    )
    return task


def save_analysis(
    analysis: dict[str, Any],
    assignee_userid: str,
    store: TuoguanStore,
    *,
    source_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    records = store.read_json("records.json", [])
    if not isinstance(records, list):
        records = []
    record = {
        **analysis,
        "id": str(analysis.get("id") or f"record_{uuid.uuid4().hex[:12]}"),
        "teacher": assignee_userid,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "source_meta": source_meta or {},
    }
    record["program_id"] = str(
        analysis.get("program_id")
        or (source_meta or {}).get("program_id")
        or "regular_tuoguan"
    )
    record["record_evaluation"] = evaluate_record(record, previous_records=records)
    if record["program_id"] == "summer_2026":
        evaluation = record["record_evaluation"]
        evaluation["payroll_eligible"] = False
        evaluation["score"] = 0.0
        evaluation["required_bucket"] = ""
        evaluation["reason_codes"] = list(dict.fromkeys([*(evaluation.get("reason_codes") or []), "summer_program_non_payroll"]))
        evaluation["reason_texts"] = list(dict.fromkeys([*(evaluation.get("reason_texts") or []), "暑假班记录不参与托管班工资绩效"]))
    records.append(record)
    store.write_json("records.json", records)

    if not analysis.get("should_create_task"):
        _refresh_dashboard_cache_best_effort(store)
        return {"record": records[-1], "task": None, "created": False}

    draft = build_task_draft(analysis, assignee_userid)
    persisted_task: dict[str, Any] = {}
    created = False

    def upsert_record_task(tasks: list[dict[str, Any]]) -> None:
        nonlocal persisted_task, created
        existing = next(
            (
                task for task in tasks
                if task.get("status") not in {"completed", "cancelled", "closed", "done", "closed_by_admin", "completed_by_admin"}
                and task.get("student_name") == draft["student_name"]
                and task.get("type") == draft["type"]
            ),
            None,
        )
        if existing is not None:
            source = str(draft.get("source_text") or "")
            previous = str(existing.get("evidence_summary") or "")
            evidence_lines = [line for line in previous.splitlines() if line.strip()]
            if source and source not in evidence_lines:
                existing["evidence_summary"] = "\n".join([*evidence_lines, source]).strip()
            existing["duplicate_record_count"] = int(existing.get("duplicate_record_count") or 0) + 1
            existing["updated_at"] = datetime.now().isoformat(timespec="seconds")
            persisted_task = deepcopy(existing)
            return
        tasks.append(deepcopy(draft))
        persisted_task = deepcopy(draft)
        created = True

    store.update_tasks(upsert_record_task)
    _refresh_dashboard_cache_best_effort(store)
    return {"record": records[-1], "task": persisted_task, "created": created}


def remove_record_by_id(
    store: TuoguanStore,
    *,
    record_id: str,
    actor_userid: str,
) -> dict[str, Any] | None:
    records = store.read_json("records.json", [])
    if not isinstance(records, list):
        return None
    kept: list[dict[str, Any]] = []
    removed: dict[str, Any] | None = None
    for record in records:
        if not isinstance(record, dict):
            kept.append(record)
            continue
        if (
            removed is None
            and str(record.get("id") or "") == record_id
            and str(record.get("teacher") or "") == actor_userid
        ):
            removed = record
            continue
        kept.append(record)
    if removed is None:
        return None
    store.write_json("records.json", kept)
    _refresh_dashboard_cache_best_effort(store)
    return removed
