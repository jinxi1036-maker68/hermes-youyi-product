"""Evidence-backed student growth report drafts and approvals."""

from __future__ import annotations

import uuid
import secrets
import string
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from .analytics import _parse_datetime
from .store import TuoguanStore
from .programs import canonical_program_id, item_program_id
from .summer_course_coverage import student_course_evidence


_POSITIVE_TAGS = {"正向成长"}
_PERIODS = {
    "weekly": {"days": 7, "label": "周报告", "title": "本周成长反馈"},
    "monthly": {"days": 30, "label": "月报告", "title": "本月成长反馈"},
    "semester": {"days": 120, "label": "学期报告", "title": "本学期成长反馈"},
    "graduation": {"days": 60, "label": "结业汇报", "title": "暑假班结业成长汇报"},
}
_CONCERN_TAG_LABELS = {
    "数学计算弱": "数学计算",
    "阅读理解弱": "阅读理解",
    "语文书写慢": "书写",
    "作业效率低": "作业效率",
    "粗心": "细心程度",
    "注意力不集中": "专注力",
}
_REPORT_CONTEXT_FILE = "growth_report_context.json"
_REPORT_LINKS_FILE = "growth_report_links.json"
_REPORT_CONTEXT_TTL_SECONDS = 2 * 60 * 60


def _record_time(record: dict[str, Any]) -> datetime | None:
    return _parse_datetime(record.get("created_at") or record.get("timestamp"))


def _excerpt(record: dict[str, Any], limit: int = 160) -> str:
    text = " ".join(str(record.get("content") or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_growth_report_draft(
    store: TuoguanStore,
    *,
    student_name: str,
    period_days: int = 30,
    period_type: str = "monthly",
    now: datetime | None = None,
    program_id: str | None = None,
    include_regular_history: bool = False,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> dict[str, Any]:
    timestamp = now or datetime.now().astimezone()
    normalized_period = period_type if period_type in _PERIODS else "monthly"
    period_meta = _PERIODS[normalized_period]
    days = max(7, min(int(period_days or period_meta["days"]), 180))
    students = store.read_json("students.json", {})
    records = store.read_json("records.json", [])
    if not isinstance(students, dict) or student_name not in students:
        raise ValueError(f"没有找到学生“{student_name}”。")
    if not isinstance(records, list):
        records = []
    profile = students.get(student_name)
    profile = profile if isinstance(profile, dict) else {}
    window_end = period_end or timestamp
    start = period_start or (window_end - timedelta(days=days))
    selected_program = canonical_program_id(program_id) if program_id else None
    evidence = []
    for record in records:
        if (
            not isinstance(record, dict)
            or str(record.get("student_name") or "") != student_name
        ):
            continue
        record_program = item_program_id(record)
        if selected_program and record_program != selected_program:
            if not (include_regular_history and record_program == "regular_tuoguan"):
                continue
        created = _record_time(record)
        if created is None:
            continue
        if created.tzinfo is None and window_end.tzinfo is not None:
            created = created.replace(tzinfo=window_end.tzinfo)
        if start.tzinfo is None and created.tzinfo is not None:
            start = start.replace(tzinfo=created.tzinfo)
        if created < start or created > window_end:
            continue
        evidence.append((created, record))
    evidence.sort(key=lambda item: item[0])

    tag_counts: Counter[str] = Counter()
    positive_evidence = []
    concern_evidence = []
    daily_evidence = []
    for created, record in evidence:
        tags = {str(tag) for tag in record.get("tags") or []}
        types = {str(item) for item in record.get("record_types") or []}
        tag_counts.update(tags)
        item = {
            "date": created.date().isoformat(),
            "content": _excerpt(record),
            "tags": sorted(tags),
            "record_types": sorted(types),
        }
        if tags & _POSITIVE_TAGS or "positive_progress" in types:
            positive_evidence.append(item)
        if tags & set(_CONCERN_TAG_LABELS) or types & {
            "academic_issue",
            "learning_habit",
        }:
            concern_evidence.append(item)
        if "student_daily" in types or not types:
            daily_evidence.append(item)

    strengths = []
    if positive_evidence:
        strengths.append("近期记录中出现明确的正向表现。")
    for tag, count in tag_counts.most_common():
        if tag in _POSITIVE_TAGS:
            strengths.append(f"{tag}相关记录{count}次。")
    concerns = [
        f"{label}相关记录{tag_counts[tag]}次。"
        for tag, label in _CONCERN_TAG_LABELS.items()
        if tag_counts[tag]
    ]
    goals = [
        str(goal)
        for goal in profile.get("learning_goals") or []
        if str(goal).strip()
    ]
    next_steps = goals[:3]
    if not next_steps and concerns:
        next_steps.append("请老师结合上述记录确认下一阶段最优先的改进目标。")
    if not next_steps:
        next_steps.append("当前证据不足，请老师补充近期目标和观察重点。")

    report_id = f"growth_{uuid.uuid4().hex[:12]}"
    evidence_items = [
        {
            "date": created.date().isoformat(),
            "content": _excerpt(record),
            "tags": [str(tag) for tag in record.get("tags") or []],
            "record_types": [
                str(item) for item in record.get("record_types") or []
            ],
        }
        for created, record in evidence
    ]
    teacher_followups = _teacher_followups(evidence_items, concerns)
    home_suggestions = _home_cooperation_suggestions(concerns, next_steps)
    parent_summary = _parent_summary(
        student_name=student_name,
        record_count=len(evidence_items),
        strengths=strengths,
        concerns=concerns,
    )
    course_coverage = {
        "overall_count": 0,
        "individual_count": 0,
        "by_course": {},
        "missing_courses": [],
    }
    if selected_program == "summer_2026":
        course_coverage = student_course_evidence(
            store,
            student_name=student_name,
            start=start.date(),
            end=window_end.date(),
        )
        if not evidence_items and int(course_coverage.get("overall_count") or 0) > 0:
            parent_summary = "本期有课程整体参与记录，但针对孩子个人表现的证据还不充分，暂不作具体能力判断。"
    share_title = f"{student_name}{period_meta['title']}"
    parent_draft_lines = [
        f"【{share_title}｜待老师确认】",
        f"统计周期：{start.date().isoformat()} 至 {window_end.date().isoformat()}",
        f"本期有效记录：{len(evidence_items)}条",
        "",
        "【本期表现】",
        f"- {parent_summary}",
        "【成长亮点】",
    ]
    parent_draft_lines.extend(
        f"- {item}" for item in (strengths or ["暂无足够的正向证据，待老师补充。"])
    )
    parent_draft_lines.append("【需要继续关注】")
    parent_draft_lines.extend(
        f"- {item}" for item in (concerns or ["当前记录未形成明确问题结论。"])
    )
    parent_draft_lines.append("【老师已做跟进】")
    parent_draft_lines.extend(f"- {item}" for item in teacher_followups)
    parent_draft_lines.append("【下一步】")
    parent_draft_lines.extend(f"- {item}" for item in next_steps)
    parent_draft_lines.append("【家校配合建议】")
    parent_draft_lines.extend(f"- {item}" for item in home_suggestions)
    if selected_program == "summer_2026" and int(course_coverage.get("overall_count") or 0) > 0:
        parent_draft_lines.extend([
            "【课程记录说明】",
            (
                f"- 本期有{int(course_coverage.get('overall_count') or 0)}次课程整体覆盖。"
                "整体覆盖只说明孩子参加了对应课程和课堂整体情况，不作为孩子个人具体表现的依据。"
            ),
        ])
    source_label = "2026暑假班记录" if selected_program == "summer_2026" else "托管记录"
    if selected_program == "summer_2026" and include_regular_history:
        source_label = "2026暑假班记录，并单独参考以往托管表现"
    parent_draft_lines.extend(["", f"说明：本草稿仅依据{source_label}生成，需老师核对后才能作为正式沟通依据。"])

    return {
        "id": report_id,
        "student_name": student_name,
        "program_id": selected_program or "regular_tuoguan",
        "includes_regular_history": bool(include_regular_history),
        "status": "draft",
        "period_type": normalized_period,
        "period_label": period_meta["label"],
        "period_start": start.date().isoformat(),
        "period_end": window_end.date().isoformat(),
        "period_days": days,
        "record_count": len(evidence_items),
        "share_title": share_title,
        "parent_summary": parent_summary,
        "strengths": strengths,
        "concerns": concerns,
        "next_steps": next_steps,
        "teacher_followups": teacher_followups,
        "home_cooperation_suggestions": home_suggestions,
        "evidence": evidence_items,
        "course_coverage": course_coverage,
        "evidence_level": (
            "individual"
            if evidence_items or int(course_coverage.get("individual_count") or 0) > 0
            else ("overall_only" if int(course_coverage.get("overall_count") or 0) > 0 else "insufficient")
        ),
        "parent_draft": "\n".join(parent_draft_lines),
        "created_at": timestamp.isoformat(timespec="seconds"),
        "created_by": "",
        "approved_at": "",
        "approved_by": "",
        "approved_share_token_created_at": "",
    }


def save_growth_report_draft(
    store: TuoguanStore,
    report: dict[str, Any],
    *,
    actor: str,
) -> dict[str, Any]:
    reports = store.read_json("growth_reports.json", [])
    if not isinstance(reports, list):
        reports = []
    item = deepcopy(report)
    item["created_by"] = actor
    reports.append(item)
    store.write_json("growth_reports.json", reports[-2000:])
    return item


def remember_growth_report_context(
    store: TuoguanStore,
    *,
    user_id: str,
    report: dict[str, Any],
    now: datetime | None = None,
) -> None:
    context = store.read_json(_REPORT_CONTEXT_FILE, {})
    if not isinstance(context, dict):
        context = {}
    timestamp = now or datetime.now().astimezone()
    context[str(user_id)] = {
        "report_id": str(report.get("id") or ""),
        "student_name": str(report.get("student_name") or ""),
        "period_type": str(report.get("period_type") or ""),
        "created_at": timestamp.isoformat(timespec="seconds"),
    }
    store.write_json(_REPORT_CONTEXT_FILE, context)


def latest_growth_report_context(
    store: TuoguanStore,
    *,
    user_id: str,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    context = store.read_json(_REPORT_CONTEXT_FILE, {})
    if not isinstance(context, dict):
        return None
    item = context.get(str(user_id))
    if not isinstance(item, dict):
        return None
    created_at = _parse_datetime(item.get("created_at"))
    timestamp = now or datetime.now().astimezone()
    if created_at is None:
        return None
    if created_at.tzinfo is None and timestamp.tzinfo is not None:
        created_at = created_at.replace(tzinfo=timestamp.tzinfo)
    if (timestamp - created_at).total_seconds() > _REPORT_CONTEXT_TTL_SECONDS:
        return None
    report = growth_report_by_id(store, str(item.get("report_id") or ""))
    if not report or report.get("status") != "draft":
        return None
    return report


def pending_growth_reports_for_actor(
    store: TuoguanStore,
    *,
    actor: str,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    reports = store.read_json("growth_reports.json", [])
    if not isinstance(reports, list):
        return []
    timestamp = now or datetime.now().astimezone()
    candidates: list[dict[str, Any]] = []
    for report in reports:
        if not isinstance(report, dict):
            continue
        if report.get("status") != "draft" or str(report.get("created_by") or "") != actor:
            continue
        created = _parse_datetime(report.get("created_at"))
        if created is None:
            continue
        if created.tzinfo is None and timestamp.tzinfo is not None:
            created = created.replace(tzinfo=timestamp.tzinfo)
        if (timestamp - created).total_seconds() <= _REPORT_CONTEXT_TTL_SECONDS:
            candidates.append(deepcopy(report))
    return sorted(candidates, key=lambda item: str(item.get("created_at") or ""), reverse=True)


def approve_growth_report(
    store: TuoguanStore,
    *,
    report_id: str,
    actor: str,
    note: str = "",
    now: datetime | None = None,
) -> dict[str, Any] | None:
    reports = store.read_json("growth_reports.json", [])
    if not isinstance(reports, list):
        return None
    timestamp = now or datetime.now().astimezone()
    target = None
    for report in reports:
        if isinstance(report, dict) and str(report.get("id") or "") == report_id:
            target = report
            break
    if target is None:
        return None
    target["status"] = "approved"
    target["approved_at"] = timestamp.isoformat(timespec="seconds")
    target["approved_by"] = actor
    target["approval_note"] = str(note or "").strip()
    store.write_json("growth_reports.json", reports)
    return deepcopy(target)


def latest_growth_report(
    store: TuoguanStore,
    *,
    student_name: str,
    approved_only: bool = False,
    period_type: str = "",
) -> dict[str, Any] | None:
    reports = store.read_json("growth_reports.json", [])
    if not isinstance(reports, list):
        return None
    matching = [
        report
        for report in reports
        if isinstance(report, dict)
        and str(report.get("student_name") or "") == student_name
        and (not approved_only or report.get("status") == "approved")
        and (not period_type or str(report.get("period_type") or "") == period_type)
    ]
    if not matching:
        return None
    return deepcopy(
        max(
            matching,
            key=lambda report: str(
                report.get("approved_at") or report.get("created_at") or ""
            ),
        )
    )


def growth_report_by_id(store: TuoguanStore, report_id: str) -> dict[str, Any] | None:
    reports = store.read_json("growth_reports.json", [])
    if not isinstance(reports, list):
        return None
    for report in reports:
        if isinstance(report, dict) and str(report.get("id") or "") == str(report_id):
            return deepcopy(report)
    return None


def mark_growth_report_shared(
    store: TuoguanStore,
    *,
    report_id: str,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    reports = store.read_json("growth_reports.json", [])
    if not isinstance(reports, list):
        return None
    timestamp = now or datetime.now().astimezone()
    target = None
    for report in reports:
        if isinstance(report, dict) and str(report.get("id") or "") == report_id:
            target = report
            break
    if target is None:
        return None
    target["approved_share_token_created_at"] = timestamp.isoformat(timespec="seconds")
    store.write_json("growth_reports.json", reports)
    return deepcopy(target)


def create_parent_report_short_link(
    store: TuoguanStore,
    *,
    report_id: str,
    token: str,
    expires_at: datetime,
    created_by: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    links = store.read_json(_REPORT_LINKS_FILE, {})
    if not isinstance(links, dict):
        links = {}
    timestamp = now or datetime.now().astimezone()
    alphabet = string.ascii_lowercase + string.digits
    code = ""
    for _ in range(20):
        candidate = "".join(secrets.choice(alphabet) for _ in range(8))
        if candidate not in links:
            code = candidate
            break
    if not code:
        code = uuid.uuid4().hex[:10]
    item = {
        "code": code,
        "report_id": str(report_id),
        "token": str(token),
        "expires_at": _aware_iso(expires_at),
        "created_by": str(created_by),
        "created_at": timestamp.isoformat(timespec="seconds"),
        "access_count": 0,
    }
    links[code] = item
    store.write_json(_REPORT_LINKS_FILE, links)
    return deepcopy(item)


def resolve_parent_report_short_link(
    store: TuoguanStore,
    *,
    code: str,
    now: datetime | None = None,
    mark_access: bool = False,
) -> dict[str, Any] | None:
    links = store.read_json(_REPORT_LINKS_FILE, {})
    if not isinstance(links, dict):
        return None
    key = str(code or "").strip()
    item = links.get(key)
    if not isinstance(item, dict):
        return None
    expires_at = _parse_datetime(item.get("expires_at"))
    timestamp = now or datetime.now().astimezone()
    if expires_at is None:
        return None
    if expires_at.tzinfo is None and timestamp.tzinfo is not None:
        expires_at = expires_at.replace(tzinfo=timestamp.tzinfo)
    if expires_at <= timestamp:
        return None
    report = growth_report_by_id(store, str(item.get("report_id") or ""))
    if not report or report.get("status") != "approved":
        return None
    if mark_access:
        item["access_count"] = int(item.get("access_count") or 0) + 1
        links[key] = item
        store.write_json(_REPORT_LINKS_FILE, links)
    return deepcopy(item)


def parent_report_payload(report: dict[str, Any]) -> dict[str, Any]:
    strengths = _clean_list(report.get("strengths"))
    concerns = _clean_list(report.get("concerns"))
    followups = _clean_list(report.get("teacher_followups"))
    next_steps = _clean_list(report.get("next_steps"))
    home_suggestions = _clean_list(report.get("home_cooperation_suggestions"))
    evidence_items = _clean_evidence(report.get("evidence"))
    raw_record_count = int(report.get("record_count") or 0)
    evidence_record_count = len(evidence_items)
    scored_record_count = evidence_record_count
    total_sections = 4
    filled_sections = sum(bool(items) for items in (strengths, concerns, followups, next_steps + home_suggestions))
    completeness = max(35, min(100, int((filled_sections / total_sections) * 100)))
    growth_score = _parent_growth_score(
        record_count=scored_record_count,
        strengths=strengths,
        concerns=concerns,
        followups=followups,
        home_suggestions=home_suggestions,
    )
    coupon = _growth_coupon(
        growth_score,
        record_count=scored_record_count,
        strengths=strengths,
        concerns=concerns,
        followups=followups,
        evidence_items=evidence_items,
    )
    recent_changes = _recent_growth_changes(evidence_items)
    stored_parent_summary = str(report.get("parent_summary") or "")
    parent_summary = stored_parent_summary
    if not parent_summary or _is_generic_parent_summary(parent_summary):
        parent_summary = _parent_summary_from_evidence(
            student_name=str(report.get("student_name") or ""),
            record_count=scored_record_count,
            evidence_items=evidence_items,
            strengths=strengths,
            concerns=concerns,
        )
    trust_account = _growth_trust_account(
        report=report,
        evidence_items=evidence_items,
        raw_record_count=raw_record_count,
        scored_record_count=scored_record_count,
        strengths=strengths,
        concerns=concerns,
        followups=followups,
        next_steps=next_steps,
        home_suggestions=home_suggestions,
    )
    return {
        "id": str(report.get("id") or ""),
        "student_name": str(report.get("student_name") or ""),
        "share_title": str(report.get("share_title") or ""),
        "period_type": str(report.get("period_type") or ""),
        "period_label": str(report.get("period_label") or "成长报告"),
        "period_start": str(report.get("period_start") or ""),
        "period_end": str(report.get("period_end") or ""),
        "record_count": raw_record_count,
        "scored_record_count": scored_record_count,
        "visual": {
            "completion_rate": completeness,
            "growth_score": growth_score,
            "growth_level": max(1, min(5, (growth_score + 19) // 20)),
            "tree_scale": round(min(1.0, 0.54 + growth_score / 220), 2),
            "leaf_opacity": round(min(0.92, 0.42 + growth_score / 190), 2),
            "followup_count": len(followups),
        },
        "trust_account": trust_account,
        "evidence_ledger": trust_account["evidence_source"],
        "renewal_review": _renewal_review(report, trust_account),
        "semester_coupon": coupon,
        "generated_at": str(report.get("approved_at") or report.get("created_at") or ""),
        "parent_summary": parent_summary or str(report.get("parent_summary") or ""),
        "recent_changes": recent_changes,
        "strengths": strengths,
        "concerns": concerns,
        "teacher_followups": followups,
        "next_steps": next_steps,
        "home_cooperation_suggestions": home_suggestions,
        "footer_note": "本报告由托管记录整理生成，并经老师审核后分享。",
    }


def _growth_trust_account(
    *,
    report: dict[str, Any],
    evidence_items: list[dict[str, Any]],
    raw_record_count: int,
    scored_record_count: int,
    strengths: list[str],
    concerns: list[str],
    followups: list[str],
    next_steps: list[str],
    home_suggestions: list[str],
) -> dict[str, Any]:
    parent_comm_count = sum(1 for item in evidence_items if _is_parent_communication_evidence(item))
    quality_count = sum(1 for item in evidence_items if _is_quality_growth_evidence(item))
    evidence_source = {
        "raw_record_count": raw_record_count,
        "scored_record_count": scored_record_count,
        "quality_evidence_count": quality_count,
        "parent_communication_count": parent_comm_count,
        "teacher_followup_count": len(followups),
        "next_step_count": len(next_steps),
        "home_suggestion_count": len(home_suggestions),
        "concern_count": len(concerns),
        "rule_version": "growth_trust_account_v1",
        "generated_at": str(report.get("approved_at") or report.get("created_at") or ""),
    }
    checkpoints = [
        {"label": "成长记录", "done": scored_record_count > 0, "value": scored_record_count},
        {"label": "优质证据", "done": quality_count > 0, "value": quality_count},
        {"label": "家校沟通", "done": parent_comm_count > 0, "value": parent_comm_count},
        {"label": "老师跟进", "done": len(followups) > 0, "value": len(followups)},
        {"label": "下一步建议", "done": bool(next_steps or home_suggestions), "value": len(next_steps) + len(home_suggestions)},
    ]
    return {
        "title": "孩子成长账户",
        "subtitle": "成长树、阶段反馈和激励券都来自同一条已审核证据链。",
        "evidence_source": evidence_source,
        "checkpoints": checkpoints,
        "explain": [
            f"本期用于计算的已审核证据为{scored_record_count}条。",
            f"其中优质证据{quality_count}条，家校沟通证据{parent_comm_count}条。",
            "单条记录不会高额抵扣，必须持续积累优质记录、沟通和老师跟进。",
        ],
    }


def _renewal_review(report: dict[str, Any], trust_account: dict[str, Any]) -> dict[str, Any]:
    due_date = str(report.get("renewal_due_date") or "").strip()
    evidence = trust_account.get("evidence_source", {})
    ready = (
        int(evidence.get("scored_record_count") or 0) >= 3
        and int(evidence.get("teacher_followup_count") or 0) >= 1
        and int(evidence.get("parent_communication_count") or 0) >= 1
    )
    return {
        "status": "ready_for_review" if ready else "needs_more_evidence",
        "renewal_due_date": due_date,
        "teacher_or_boss_confirmation_required": True,
        "message": (
            "证据较完整，可作为续费前成长回顾素材，发送前仍需老师或老板确认。"
            if ready
            else "当前仍需继续积累成长证据、家校沟通或老师跟进，暂不建议自动发送续费话术。"
        ),
    }


def _parent_growth_score(
    *,
    record_count: int,
    strengths: list[str],
    concerns: list[str],
    followups: list[str],
    home_suggestions: list[str],
) -> int:
    score = (
        record_count * 14
        + len(strengths) * 16
        + len(followups) * 10
        + len(home_suggestions) * 8
        - len(concerns) * 8
    )
    if record_count <= 0:
        evidence_cap = 20
    elif record_count == 1:
        evidence_cap = 30
    elif record_count <= 3:
        evidence_cap = 45
    elif record_count <= 5:
        evidence_cap = 60
    elif record_count <= 8:
        evidence_cap = 78
    elif record_count <= 11:
        evidence_cap = 90
    else:
        evidence_cap = 100
    return max(20, min(100, evidence_cap, score))


def _growth_coupon(
    growth_score: int,
    *,
    record_count: int,
    strengths: list[str],
    concerns: list[str],
    followups: list[str],
    evidence_items: list[dict[str, Any]],
) -> dict[str, Any]:
    max_amount = 200
    parent_comm_count = sum(1 for item in evidence_items if _is_parent_communication_evidence(item))
    quality_count = sum(1 for item in evidence_items if _is_quality_growth_evidence(item))
    if record_count <= 0:
        base_amount = 0
        evidence_cap = 0
    elif record_count == 1:
        base_amount = 20
        evidence_cap = 20
    elif record_count <= 3:
        base_amount = 40
        evidence_cap = 50
    elif record_count <= 5:
        base_amount = 70
        evidence_cap = 80
    elif record_count <= 8:
        base_amount = 100
        evidence_cap = 120
    elif record_count <= 11:
        base_amount = 130
        evidence_cap = 160
    else:
        base_amount = 160
        evidence_cap = max_amount
    bonus = 0
    if quality_count >= 2:
        bonus += 20
    elif quality_count == 1 and record_count >= 2:
        bonus += 10
    if parent_comm_count >= 1 and record_count >= 3:
        bonus += 20
    if len(followups) >= 2 and record_count >= 4:
        bonus += 10
    if strengths and not concerns and record_count >= 4:
        bonus += 10
    penalty = min(40, len(concerns) * 10)
    amount = min(max_amount, evidence_cap, max(0, base_amount + bonus - penalty))
    amount = int(amount / 10) * 10
    if amount >= max_amount:
        status = "已达最高成长激励"
    elif amount >= 120:
        status = "成长激励持续增加"
    elif amount > 0:
        status = "继续保持可提升"
    else:
        status = "继续积累成长证据"
    return {
        "title": "下学期成长激励券",
        "amount": amount,
        "max_amount": max_amount,
        "progress_percent": round(amount / max_amount * 100) if max_amount else 0,
        "status": status,
        "display_status": f"预计成长券 ¥{amount}，以校区最终审核为准",
        "review_required": True,
        "rule_version": "semester_growth_coupon_v1",
        "evidence": {
            "record_count": record_count,
            "quality_count": quality_count,
            "parent_communication_count": parent_comm_count,
            "concern_count": len(concerns),
            "evidence_cap": evidence_cap,
        },
        "basis": [
            f"本期已审核记录{record_count}条，当前证据上限为{evidence_cap}元",
            f"优质成长证据{quality_count}条，家校沟通证据{parent_comm_count}条",
            f"基础金额{base_amount}元，加分{bonus}元，关注项扣减{penalty}元",
            "本学期最高可抵扣200元，最终以校区确认和正式续费规则为准",
        ],
    }


def _clean_evidence(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _evidence_text(item: dict[str, Any]) -> str:
    parts = [
        str(item.get("content") or ""),
        " ".join(str(tag) for tag in item.get("tags") or []),
        " ".join(str(record_type) for record_type in item.get("record_types") or []),
    ]
    return " ".join(part for part in parts if part)


def _is_parent_communication_evidence(item: dict[str, Any]) -> bool:
    text = _evidence_text(item)
    return any(word in text for word in ("家长", "妈妈", "爸爸", "微信", "电话", "沟通", "同步", "反馈"))


def _is_quality_growth_evidence(item: dict[str, Any]) -> bool:
    text = _evidence_text(item)
    has_scene = any(word in text for word in ("今天", "托管", "写作业", "吃饭", "放学", "课堂"))
    has_action = any(word in text for word in ("老师", "提醒", "引导", "安抚", "订正", "沟通", "跟进", "建议"))
    has_next = any(word in text for word in ("继续", "后续", "明天", "建议", "观察", "跟进"))
    return len(text) >= 35 and has_scene and has_action and has_next


def _growth_topic_label(text: str) -> str:
    if any(word in text for word in ("午休", "睡觉", "休息")):
        return "午休"
    if any(word in text for word in ("午餐", "晚餐", "吃饭", "进餐", "青菜", "米饭", "挑食")):
        return "生活"
    if any(word in text for word in ("数学", "计算", "订正", "作业", "阅读", "书写", "口算")):
        return "学习"
    if any(word in text for word in ("家长", "妈妈", "爸爸", "沟通", "同步", "反馈")):
        return "沟通"
    if any(word in text for word in ("情绪", "哭", "坐不住", "纪律", "排队", "互动")):
        return "行为"
    if any(word in text for word in ("磕", "摔", "夹", "疼", "出血", "红肿", "不舒服")):
        return "安全"
    return "观察"


def _short_parent_sentence(text: str, limit: int = 42) -> str:
    cleaned = " ".join(text.split())
    for prefix in ("今天", "托管时", "托管"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].lstrip("，,。 ")
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def _recent_growth_changes(evidence_items: list[dict[str, Any]]) -> list[str]:
    changes: list[str] = []
    seen: set[str] = set()
    for item in reversed(evidence_items[-8:]):
        text = str(item.get("content") or "").strip()
        if not text:
            continue
        label = _growth_topic_label(_evidence_text(item))
        if label in seen and len(changes) >= 2:
            continue
        seen.add(label)
        changes.append(f"{label}：{_short_parent_sentence(text)}")
        if len(changes) >= 4:
            break
    return list(reversed(changes))


def _concern_focus(concerns: list[str]) -> str:
    if not concerns:
        return "稳定习惯和主动检查"
    text = "、".join(concerns)
    for label in ("数学计算", "阅读理解", "书写", "作业效率", "细心程度", "专注力"):
        if label in text:
            return label
    return concerns[0].replace("相关记录", "").replace("次。", "")


def _parent_summary_from_evidence(
    *,
    student_name: str,
    record_count: int,
    evidence_items: list[dict[str, Any]],
    strengths: list[str],
    concerns: list[str],
) -> str:
    if record_count <= 0:
        return f"{student_name}本期记录较少，暂时先从午休、作业完成和课堂配合几个方面继续观察。"
    topics = []
    for item in evidence_items[-6:]:
        label = _growth_topic_label(_evidence_text(item))
        if label not in topics and label != "观察":
            topics.append(label)
    if record_count < 3:
        scope = "、".join(topics[:2]) if topics else "午休、作业完成和课堂配合"
        return f"{student_name}本期记录还不多，先从{scope}继续观察，后续再形成更完整的成长判断。"
    main = "和".join(topics[:2]) if topics else "托管状态"
    focus = _concern_focus(concerns)
    if strengths and concerns:
        return f"{student_name}近期在{main}上能看到变化，老师提醒后能继续调整，接下来重点关注{focus}。"
    if strengths:
        return f"{student_name}近期{main}整体更稳定，老师会继续巩固好习惯，并观察是否能保持。"
    if concerns:
        return f"{student_name}近期主要需要关注{focus}，老师已结合日常托管继续提醒和跟进。"
    return f"{student_name}近期{main}整体平稳，老师会继续观察作业完成、生活习惯和课堂配合。"


def _is_generic_parent_summary(text: str) -> bool:
    generic_patterns = (
        "有值得肯定的",
        "需要继续陪伴和巩固",
        "整体状态稳定，记录中能看到积极变化",
        "托管状态整体平稳",
        "老师已根据日常观察整理阶段反馈",
    )
    return any(pattern in text for pattern in generic_patterns)


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _aware_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat(timespec="seconds")


def _parent_summary(
    *,
    student_name: str,
    record_count: int,
    strengths: list[str],
    concerns: list[str],
) -> str:
    if record_count <= 0:
        return f"{student_name}本期记录证据不足，建议老师继续补充观察后再分享给家长。"
    if strengths and not concerns:
        return f"{student_name}本期整体状态稳定，记录中能看到积极变化，建议继续保持当前节奏。"
    if strengths and concerns:
        return f"{student_name}本期有值得肯定的进步，同时也有需要继续陪伴和巩固的地方。"
    if concerns:
        return f"{student_name}本期需要在学习习惯和任务完成上继续关注，老师会结合日常托管持续跟进。"
    return f"{student_name}本期托管状态整体平稳，老师已根据日常观察整理阶段反馈。"


def _teacher_followups(
    evidence_items: list[dict[str, Any]],
    concerns: list[str],
) -> list[str]:
    followups = []
    if evidence_items:
        followups.append("老师已持续记录孩子在托管中的学习、作业和日常表现。")
    if concerns:
        followups.append("针对需要关注的方面，老师会在后续托管中继续观察并及时提醒。")
    if not followups:
        followups.append("老师会继续通过日常托管观察补充更完整的成长证据。")
    return followups


def _home_cooperation_suggestions(
    concerns: list[str],
    next_steps: list[str],
) -> list[str]:
    suggestions = []
    if concerns:
        suggestions.append("建议家长在家中给予稳定鼓励，不急于批评，先帮助孩子保持完成节奏。")
    suggestions.extend(next_steps[:2])
    if not suggestions:
        suggestions.append("建议家长继续保持日常沟通，有变化可以及时同步老师。")
    return suggestions[:3]
