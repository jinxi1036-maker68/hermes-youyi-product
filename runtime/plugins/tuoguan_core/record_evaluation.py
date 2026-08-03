"""Payroll-grade evaluation for teacher records."""

from __future__ import annotations

from datetime import datetime
from difflib import SequenceMatcher
import re
from typing import Any


_LOW_VALUE_PHRASES = (
    "表现不错",
    "表现很好",
    "挺好的",
    "还可以",
    "很棒",
    "不错",
    "很好",
    "正常",
    "已沟通",
    "沟通了",
)
_CONTEXT_WORDS = ("今天", "放学", "作业", "课堂", "写作业", "吃饭", "午休", "路上", "回家", "托管")
_BEHAVIOR_WORDS = (
    "完成",
    "订正",
    "主动",
    "提醒",
    "冲突",
    "哭",
    "拖拉",
    "专注",
    "错误",
    "正确率",
    "阅读",
    "计算",
    "书写",
    "背诵",
    "情绪",
)
_TEACHER_ACTION_WORDS = (
    "老师",
    "已经",
    "已",
    "沟通",
    "提醒",
    "引导",
    "安抚",
    "处理",
    "跟进",
    "建议",
    "反馈",
    "联系",
)
_NEXT_STEP_WORDS = ("明天", "后续", "继续", "建议", "需要", "计划", "跟进", "观察", "复盘")
_PARENT_WORDS = ("家长", "妈妈", "爸爸", "父母", "奶奶", "爷爷")
_PARENT_COMM_WORDS = ("沟通", "反馈", "联系", "微信", "电话", "说明", "告知", "建议", "同步")
_SAFETY_WORDS = ("安全", "摔", "撞", "磕", "碰", "受伤", "流血", "不舒服", "冲突", "打架")
_RISK_WORDS = ("续费", "退费", "转走", "投诉", "不满", "不认可", "焦虑", "担心", "风险")
_CLOSURE_WORDS = ("已处理", "已沟通", "已反馈", "已安抚", "已告知", "后续跟进", "继续观察", "闭环")
_BILLABLE_STUDENT_STATUSES = {"", "active", "enrolled", "normal", "unknown", "在读", "正常", "在托"}
_NON_BILLABLE_STUDENT_STATUSES = {
    "paused",
    "pause",
    "suspended",
    "churned",
    "inactive",
    "left",
    "trial",
    "test",
    "demo",
    "停课",
    "暂停",
    "流失",
    "退费",
    "测试",
    "试听",
}


def evaluate_record(
    record: dict[str, Any],
    *,
    previous_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return explainable payroll evaluation for a single record."""
    student = _record_student(record)
    content = _record_content(record)
    tags = _record_tags(record)
    record_types = _record_types(record)
    accepted = bool(student and content)
    reason_codes: list[str] = []
    if not student:
        reason_codes.append("missing_student")
    if not content:
        reason_codes.append("missing_content")
    category = _category(content, tags, record_types)
    required_bucket = _required_bucket(category)
    duplicate = _is_duplicate(record, previous_records or [])
    if duplicate:
        reason_codes.append("duplicate_same_student_day")

    detail_score = _detail_score(content)
    has_action = _has_any(content, _TEACHER_ACTION_WORDS)
    has_next = _has_any(content, _NEXT_STEP_WORDS)
    if accepted and detail_score <= 1:
        reason_codes.append("too_generic")
    if accepted and not (has_action or has_next):
        reason_codes.append("missing_teacher_action")
    if accepted and category in {"parent_communication", "safety_closure", "risk_closure"} and not _has_any(content, _CLOSURE_WORDS + _NEXT_STEP_WORDS):
        reason_codes.append("missing_closure")

    payroll_eligible = accepted and not duplicate and detail_score >= 2 and (has_action or has_next)
    quality_level = "low"
    score = 0.0
    if payroll_eligible:
        if detail_score >= 4 and has_action and has_next:
            quality_level = "excellent"
            score = 3.0
        elif detail_score >= 3 and has_action:
            quality_level = "quality"
            score = 2.0
        else:
            quality_level = "valid"
            score = 1.0
    return {
        "schema_version": 1,
        "accepted": accepted,
        "payroll_eligible": payroll_eligible,
        "quality_level": quality_level,
        "score": score,
        "category": category,
        "required_bucket": required_bucket,
        "reason_codes": reason_codes,
        "reason_texts": [_reason_text(code) for code in reason_codes],
    }


def evaluate_monthly_record_performance(
    *,
    records: list[dict[str, Any]],
    students: dict[str, dict[str, Any]],
    teacher_id: str,
    now: datetime,
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    own_students = {
        name: profile
        for name, profile in students.items()
        if _student_teacher(profile) == teacher_id and is_billable_student(profile)
    }
    own_student_names = set(own_students)
    month_records = [
        record
        for record in records
        if _record_teacher(record, students) == teacher_id
        and _record_student(record) in own_student_names
        and (_timestamp(record) is not None and _timestamp(record) >= month_start)
    ]
    sorted_records = sorted(month_records, key=lambda item: str(item.get("timestamp") or item.get("created_at") or ""))
    previous: list[dict[str, Any]] = []
    evaluated_records: list[dict[str, Any]] = []
    for record in sorted_records:
        evaluation = dict(record.get("record_evaluation") or {})
        if not evaluation:
            evaluation = evaluate_record(record, previous_records=previous)
        evaluated_records.append({"record": record, "evaluation": evaluation})
        previous.append(record)

    required_cfg = _record_rules(rules)
    parent_target = max(1, int(required_cfg.get("parent_communication_monthly_required_count") or 1))
    growth_target = max(1, int(required_cfg.get("growth_observation_monthly_required_count") or 4))
    parent_students = _student_requirement_items(
        own_students,
        evaluated_records,
        bucket="parent_communication",
        target_count=parent_target,
    )
    growth_students = _student_requirement_items(
        own_students,
        evaluated_records,
        bucket="growth_observation",
        target_count=growth_target,
    )
    risk_students = _risk_requirement_items(own_students, evaluated_records)
    required_items = parent_students + growth_students + risk_students
    required_total = sum(int(item.get("target_count") or 1) for item in required_items)
    required_done = sum(
        min(
            int(item.get("evidence_count") or 0),
            int(item.get("target_count") or 1),
        )
        for item in required_items
    )
    required_rate = required_done / required_total if required_total else 1.0

    quality_target = max(1.0, float(required_cfg.get("quality_score_target") or max(1, len(own_students) * 2)))
    quality_score = sum(float(item["evaluation"].get("score") or 0) for item in evaluated_records)
    quality_rate = min(quality_score / quality_target, 1.0)
    required_weight = float(required_cfg.get("required_weight") or 0.75)
    required_weight = min(max(required_weight, 0.0), 1.0)
    quality_weight = 1.0 - required_weight
    completion_rate = (required_rate * required_weight) + (quality_rate * quality_weight)
    low_or_excluded = [
        _record_evidence_card(item["record"], item["evaluation"])
        for item in evaluated_records
        if not item["evaluation"].get("payroll_eligible")
    ][:20]
    evidence = [
        _record_evidence_card(item["record"], item["evaluation"])
        for item in evaluated_records
        if item["evaluation"].get("payroll_eligible")
    ][:20]
    return {
        "schema_version": 1,
        "mode": "required_first",
        "required_weight": round(required_weight, 2),
        "quality_weight": round(quality_weight, 2),
        "required_total": required_total,
        "required_done": required_done,
        "required_rate": round(required_rate * 100),
        "quality_score": round(quality_score, 2),
        "quality_score_target": quality_target,
        "quality_rate": round(quality_rate * 100),
        "completion_rate": min(max(completion_rate, 0.0), 1.0),
        "completion_percent": round(min(max(completion_rate, 0.0), 1.0) * 100),
        "record_count": len(month_records),
        "accepted_count": sum(1 for item in evaluated_records if item["evaluation"].get("accepted")),
        "payroll_eligible_count": sum(1 for item in evaluated_records if item["evaluation"].get("payroll_eligible")),
        "quality_count": sum(1 for item in evaluated_records if item["evaluation"].get("quality_level") in {"quality", "excellent"}),
        "requirements": {
            "parent_communication": parent_students,
            "growth_observation": growth_students,
            "risk_closure": risk_students,
        },
        "missing_required": [item for item in required_items if not item["done"]][:30],
        "excluded_records": low_or_excluded,
        "evidence_records": evidence,
        "billable_student_count": len(own_students),
        "billable_student_policy": "active_or_unknown_only",
    }


def is_billable_student(profile: dict[str, Any]) -> bool:
    """Whether monthly payroll requirements should include this student."""
    if not isinstance(profile, dict):
        return False
    raw_values = [
        profile.get("billing_status"),
        profile.get("service_status"),
        profile.get("student_status"),
        profile.get("status"),
        profile.get("enrollment_status"),
    ]
    statuses = {str(value or "").strip().lower() for value in raw_values if str(value or "").strip()}
    if not statuses:
        return True
    if statuses & _NON_BILLABLE_STUDENT_STATUSES:
        return False
    return bool(statuses & _BILLABLE_STUDENT_STATUSES) or "active" in statuses


def evaluation_reason_labels() -> dict[str, str]:
    return {
        "missing_student": "没有识别到学生",
        "missing_content": "缺少记录内容",
        "too_generic": "内容过泛，缺少具体场景或行为",
        "missing_teacher_action": "缺少老师处理动作或跟进动作",
        "missing_closure": "风险/沟通类记录缺少闭环或下一步",
        "duplicate_same_student_day": "同一学生同一天相似记录，防刷分不重复计绩效",
    }


def _record_rules(rules: dict[str, Any] | None) -> dict[str, Any]:
    default = {
        "required_weight": 0.75,
        "quality_score_target": 0,
        "parent_communication_monthly_required_count": 1,
        "growth_observation_monthly_required_count": 4,
    }
    if not isinstance(rules, dict):
        return default
    cfg = rules.get("record_performance")
    if isinstance(cfg, dict):
        return {**default, **cfg}
    return default


def _student_requirement_items(
    students: dict[str, dict[str, Any]],
    evaluated_records: list[dict[str, Any]],
    *,
    bucket: str,
    target_count: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for student in sorted(students):
        matched = [
            item
            for item in evaluated_records
            if _record_student(item["record"]) == student
            and item["evaluation"].get("required_bucket") == bucket
            and item["evaluation"].get("payroll_eligible")
        ]
        evidence_count = len(matched)
        items.append(
            {
                "student_name": student,
                "bucket": bucket,
                "done": evidence_count >= target_count,
                "evidence_count": evidence_count,
                "target_count": target_count,
                "reason": "" if evidence_count >= target_count else _missing_reason(bucket, target_count, evidence_count),
            }
        )
    return items


def _risk_requirement_items(
    students: dict[str, dict[str, Any]],
    evaluated_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    risk_students = {
        name
        for name, profile in students.items()
        if str(profile.get("relationship_temperature") or "").lower() in {"at_risk", "risk", "高风险"}
        or str(profile.get("renewal_status") or "").lower() in {"declined", "risk", "at_risk"}
    }
    for item in evaluated_records:
        if item["evaluation"].get("required_bucket") == "risk_closure":
            risk_students.add(_record_student(item["record"]))
    result: list[dict[str, Any]] = []
    for student in sorted(name for name in risk_students if name):
        matched = [
            item
            for item in evaluated_records
            if _record_student(item["record"]) == student
            and item["evaluation"].get("required_bucket") == "risk_closure"
            and item["evaluation"].get("payroll_eligible")
        ]
        result.append(
            {
                "student_name": student,
                "bucket": "risk_closure",
                "done": bool(matched),
                "evidence_count": len(matched),
                "target_count": 1,
                "reason": "" if matched else "本月缺少重点/风险学生闭环记录",
            }
        )
    return result


def _record_evidence_card(record: dict[str, Any], evaluation: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": str(record.get("id") or ""),
        "student_name": _record_student(record),
        "time": str(record.get("timestamp") or record.get("created_at") or ""),
        "summary": _record_content(record)[:80],
        "quality_level": str(evaluation.get("quality_level") or "low"),
        "score": evaluation.get("score", 0),
        "required_bucket": str(evaluation.get("required_bucket") or ""),
        "reason_texts": list(evaluation.get("reason_texts") or []),
    }


def _missing_reason(bucket: str, target_count: int = 1, evidence_count: int = 0) -> str:
    if evidence_count > 0:
        left = max(0, target_count - evidence_count)
        progress = f"已完成 {evidence_count}/{target_count}，还差 {left} 条"
        if bucket == "growth_observation":
            return f"本月成长观察{progress}"
        if bucket == "parent_communication":
            return f"本月家长沟通{progress}"
    return {
        "parent_communication": "本月缺少该学生的有效家长沟通记录",
        "growth_observation": f"本月有效成长观察不足 {target_count} 条",
        "risk_closure": "本月缺少重点/风险学生闭环记录",
    }.get(bucket, "本月必达项未完成")


def _category(content: str, tags: list[str], record_types: list[str]) -> str:
    corpus = " ".join(tags + record_types) + " " + content
    if _has_any(corpus, _SAFETY_WORDS) or "safety_incident" in record_types:
        return "safety_closure"
    if _has_any(corpus, _RISK_WORDS) or {"renewal_risk", "parent_complaint"} & set(record_types):
        return "risk_closure"
    if (_has_any(corpus, _PARENT_WORDS) and _has_any(corpus, _PARENT_COMM_WORDS)) or "parent_anxiety" in record_types:
        return "parent_communication"
    return "growth_observation"


def _required_bucket(category: str) -> str:
    if category in {"parent_communication", "safety_closure", "risk_closure"}:
        return category
    return "growth_observation"


def _detail_score(content: str) -> int:
    compact = content.replace(" ", "")
    if len(compact) < 8:
        return 0
    score = 0
    if len(compact) >= 18:
        score += 1
    if len(compact) >= 35:
        score += 1
    if _has_any(compact, _CONTEXT_WORDS):
        score += 1
    if _has_any(compact, _BEHAVIOR_WORDS):
        score += 1
    if _has_any(compact, _TEACHER_ACTION_WORDS):
        score += 1
    if _has_any(compact, _NEXT_STEP_WORDS):
        score += 1
    if compact in _LOW_VALUE_PHRASES or any(compact.endswith(phrase) and len(compact) <= len(phrase) + 6 for phrase in _LOW_VALUE_PHRASES):
        score = min(score, 1)
    return score


def _is_duplicate(record: dict[str, Any], previous_records: list[dict[str, Any]]) -> bool:
    student = _record_student(record)
    day = _record_day(record)
    content = _fingerprint(_record_content(record))
    if not student or not day or not content:
        return False
    for previous in previous_records:
        if _record_student(previous) != student or _record_day(previous) != day:
            continue
        previous_content = _fingerprint(_record_content(previous))
        if not previous_content:
            continue
        if content == previous_content:
            return True
        if SequenceMatcher(None, content, previous_content).ratio() >= 0.86:
            return True
    return False


def _fingerprint(value: str) -> str:
    return re.sub(r"\s+", "", value).strip()


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _record_student(record: dict[str, Any]) -> str:
    return str(record.get("student_name") or record.get("student") or "").strip()


def _record_content(record: dict[str, Any]) -> str:
    return str(record.get("content") or record.get("source_text") or record.get("text") or "").strip()


def _record_tags(record: dict[str, Any]) -> list[str]:
    value = record.get("tags")
    return [str(item) for item in value if item] if isinstance(value, list) else []


def _record_types(record: dict[str, Any]) -> list[str]:
    value = record.get("record_types") or record.get("types")
    return [str(item) for item in value if item] if isinstance(value, list) else []


def _student_teacher(profile: dict[str, Any]) -> str:
    return str(profile.get("teacher") or profile.get("teacher_id") or "").strip()


def _record_teacher(record: dict[str, Any], students: dict[str, dict[str, Any]]) -> str:
    direct = str(record.get("teacher") or record.get("teacher_id") or record.get("created_by") or "").strip()
    if direct:
        return direct
    profile = students.get(_record_student(record), {})
    return _student_teacher(profile)


def _record_day(record: dict[str, Any]) -> str:
    timestamp = str(record.get("timestamp") or record.get("created_at") or "")
    return timestamp[:10]


def _timestamp(record: dict[str, Any]) -> datetime | None:
    value = str(record.get("timestamp") or record.get("created_at") or "").replace("Z", "+00:00")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _reason_text(code: str) -> str:
    return evaluation_reason_labels().get(code, code)
