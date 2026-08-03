"""Summer weekly-feedback and graduation-report draft coordination."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from typing import Any

from .growth_reports import build_growth_report_draft, save_growth_report_draft
from .programs import SUMMER_PROGRAM_ID, program_status
from .store import TuoguanStore


SUMMER_REPORT_BATCHES_FILE = "summer_report_batches.json"
SUMMER_LAUNCH_DATE = date(2026, 7, 4)
SUMMER_KICKOFF_END = date(2026, 7, 5)


def _summer_manager_id(store: TuoguanStore) -> str:
    whitelist = store.read_json("wecom_whitelist.json", {})
    if isinstance(whitelist, dict):
        ids = [str(item) for item in whitelist.get("summer_manager_ids") or [] if str(item)]
        if ids:
            return ids[0]
    return ""


def _summer_students(store: TuoguanStore) -> list[str]:
    students = store.read_json("students.json", {})
    output: list[str] = []
    if not isinstance(students, dict):
        return output
    for name, profile in students.items():
        if not isinstance(profile, dict):
            continue
        enrollments = profile.get("program_enrollments") or []
        active = str(profile.get("summer_status") or "") in {"active", "phone_pending"} or any(
            isinstance(item, dict)
            and str(item.get("program_id") or "") in {SUMMER_PROGRAM_ID, "2026_summer"}
            and str(item.get("status") or "active") == "active"
            for item in enrollments
        )
        if active:
            output.append(str(name))
    return sorted(set(output))


def _existing_report_keys(store: TuoguanStore, period_type: str, period_end: str) -> set[tuple[str, str, str]]:
    reports = store.read_json("growth_reports.json", [])
    if not isinstance(reports, list):
        return set()
    return {
        (str(item.get("student_name") or ""), str(item.get("period_type") or ""), str(item.get("period_end") or ""))
        for item in reports
        if isinstance(item, dict)
        and str(item.get("program_id") or "") == SUMMER_PROGRAM_ID
        and str(item.get("period_type") or "") == period_type
        and str(item.get("period_end") or "") == period_end
    }


def summer_weekly_period(now: datetime | None = None) -> dict[str, Any]:
    timestamp = now or datetime.now().astimezone()
    today = timestamp.date()
    if today < SUMMER_LAUNCH_DATE:
        return {"status": "not_started", "can_generate": False}
    if today <= SUMMER_KICKOFF_END or today == date(2026, 7, 6):
        return {
            "status": "kickoff_adaptation",
            "period_kind": "adaptation_summary",
            "period_label": "开班适应小结",
            "start": SUMMER_LAUNCH_DATE,
            "end": SUMMER_KICKOFF_END,
            "can_generate": today in {SUMMER_KICKOFF_END, date(2026, 7, 6)},
            "review_window": "2026-07-06至2026-07-07",
        }
    reference = today
    if today.weekday() in {0, 1}:
        reference = today - timedelta(days=today.weekday() + 1)
    days_since_wednesday = (reference.weekday() - 2) % 7
    start = reference - timedelta(days=days_since_wednesday)
    end = start + timedelta(days=4)
    return {
        "status": "formal_week",
        "period_kind": "weekly",
        "period_label": "暑假班周反馈",
        "start": start,
        "end": end,
        "can_generate": today in {end, end + timedelta(days=1)},
        "review_window": f"{(end + timedelta(days=1)).isoformat()}至{(end + timedelta(days=2)).isoformat()}",
    }


def prepare_summer_reports(
    store: TuoguanStore,
    *,
    report_type: str,
    actor_userid: str,
    now: datetime | None = None,
    include_regular_history: bool = False,
    require_friday: bool = False,
    require_cycle_window: bool = False,
) -> dict[str, Any]:
    timestamp = now or datetime.now().astimezone()
    if program_status(store, SUMMER_PROGRAM_ID) != "active":
        return {"ok": False, "error": "summer_program_not_active", "created": [], "needs_observation": []}
    weekly_period = summer_weekly_period(timestamp) if report_type == "weekly" else None
    enforce_window = require_cycle_window or require_friday
    if report_type == "weekly" and weekly_period and weekly_period.get("status") == "not_started":
        return {"ok": True, "skipped": "summer_not_started", "created": [], "needs_observation": [], "period": weekly_period}
    if report_type == "weekly" and enforce_window and not bool((weekly_period or {}).get("can_generate")):
        return {"ok": True, "skipped": "outside_weekly_generation_window", "created": [], "needs_observation": [], "period": weekly_period}
    period_type = "weekly" if report_type == "weekly" else "graduation"
    period_days = 7 if report_type == "weekly" else 60
    period_end = (weekly_period or {}).get("end") if report_type == "weekly" else timestamp.date()
    period_start = (weekly_period or {}).get("start") if report_type == "weekly" else None
    existing = _existing_report_keys(store, period_type, period_end.isoformat())
    created: list[dict[str, Any]] = []
    needs_observation: list[dict[str, Any]] = []
    for student_name in _summer_students(store):
        draft = build_growth_report_draft(
            store,
            student_name=student_name,
            period_days=period_days,
            period_type=period_type,
            now=timestamp,
            program_id=SUMMER_PROGRAM_ID,
            include_regular_history=include_regular_history,
            period_start=(datetime.combine(period_start, time.min, tzinfo=timestamp.tzinfo) if period_start else None),
            period_end=(datetime.combine(period_end, time.max, tzinfo=timestamp.tzinfo) if period_end else None),
        )
        coverage = draft.get("course_coverage") if isinstance(draft.get("course_coverage"), dict) else {}
        if int(draft.get("record_count") or 0) < 1 and int(coverage.get("overall_count") or 0) < 1:
            needs_observation.append({"student_name": student_name, "reason": "暑假班记录较少，请先补充观察或选择简短生成"})
            continue
        if int(draft.get("record_count") or 0) < 1:
            draft["needs_individual_observation"] = True
            draft["parent_summary"] = "本期只有课程整体覆盖，尚缺少孩子个人表现证据，家长版需保持保守表达。"
        if report_type == "weekly" and weekly_period and weekly_period.get("period_kind") == "adaptation_summary":
            draft["period_label"] = "开班适应小结"
            draft["share_title"] = f"{student_name}开班适应小结"
            draft["is_formal_weekly_report"] = False
        key = (student_name, period_type, str(draft.get("period_end") or ""))
        if key in existing:
            continue
        draft["summer_review_required"] = True
        draft["auto_send"] = False
        draft["include_regular_history"] = bool(include_regular_history)
        created.append(save_growth_report_draft(store, draft, actor=actor_userid))
    batch = {
        "id": f"summer_report_batch_{uuid.uuid4().hex[:10]}",
        "program_id": SUMMER_PROGRAM_ID,
        "report_type": report_type,
        "status": "pending_manager_review",
        "created_by": actor_userid,
        "created_at": timestamp.isoformat(timespec="seconds"),
        "report_ids": [str(item.get("id") or "") for item in created],
        "needs_observation": needs_observation,
        "include_regular_history": bool(include_regular_history),
        "auto_send": False,
        "period": {
            "kind": (weekly_period or {}).get("period_kind") if weekly_period else "graduation",
            "label": (weekly_period or {}).get("period_label") if weekly_period else "结业汇报",
            "start": period_start.isoformat() if period_start else "",
            "end": period_end.isoformat() if period_end else timestamp.date().isoformat(),
            "review_window": (weekly_period or {}).get("review_window", "") if weekly_period else "",
        },
    }
    batches = store.read_json(SUMMER_REPORT_BATCHES_FILE, [])
    batches = batches if isinstance(batches, list) else []
    batches.append(batch)
    store.write_json(SUMMER_REPORT_BATCHES_FILE, batches[-500:])
    return {"ok": True, "batch": batch, "created": created, "needs_observation": needs_observation, "period": weekly_period}


def ensure_summer_weekly_feedbacks(store: TuoguanStore, now: datetime | None = None) -> dict[str, Any]:
    return prepare_summer_reports(
        store,
        report_type="weekly",
        actor_userid=_summer_manager_id(store),
        now=now,
        require_cycle_window=True,
    )


def ensure_friday_summer_weekly_feedbacks(store: TuoguanStore, now: datetime | None = None) -> dict[str, Any]:
    """Compatibility alias; weekly generation now follows the Wed-Sun cycle."""
    return ensure_summer_weekly_feedbacks(store, now=now)


def summer_report_dashboard(store: TuoguanStore) -> dict[str, Any]:
    reports = store.read_json("growth_reports.json", [])
    reports = [item for item in reports if isinstance(item, dict) and str(item.get("program_id") or "") == SUMMER_PROGRAM_ID] if isinstance(reports, list) else []
    batches = store.read_json(SUMMER_REPORT_BATCHES_FILE, [])
    batches = [item for item in batches if isinstance(item, dict)] if isinstance(batches, list) else []
    return {
        "program_id": SUMMER_PROGRAM_ID,
        "draft_count": len([item for item in reports if item.get("status") == "draft"]),
        "approved_count": len([item for item in reports if item.get("status") == "approved"]),
        "shared_count": len([item for item in reports if item.get("approved_share_token_created_at")]),
        "needs_observation_count": sum(len(item.get("needs_observation") or []) for item in batches[-10:]),
        "recent_batches": batches[-10:][::-1],
    }
