"""Boss-facing daily rhythm reports for Hermes autonomous work.

The reports summarize already-recorded autonomous work material. They do not
decide business actions, contact teachers or parents, assign tasks, or change
institution data.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import argparse
import asyncio
import json
import os
from typing import Any

from .digital_employee_state import (
    hermes_work_item_is_semantically_retired,
    query_attention_threads,
    query_autonomous_work_brief,
    query_hermes_work_items,
    query_staff_voice_radar,
)
from .employee_identity import owner_user_id as _owner_user_id
from .employee_identity import system_identity as _system_identity
from .self_evolution import build_self_evolution_brief
from .store import JSON_NO_CHANGE, TuoguanStore
from .tenant_context import current_tenant_id
from .workstyle_profiles import daily_report_style_for_owner
from .write_guard import assert_business_write_allowed, authorized_system_write

DAILY_REPORT_RUNS_FILE = "daily_report_runs.jsonl"
NOTIFICATION_OUTBOX_FILE = "notification_outbox.json"
VALID_REPORT_KINDS = {"morning", "evening"}
REPORT_FRESHNESS_HOURS = 36
REPORT_DELIVERY_WINDOWS = {
    "morning": (7 * 60 + 30, 10 * 60 + 30),
    "evening": (19 * 60 + 30, 23 * 60),
}


def queue_daily_boss_report(
    kind: str,
    *,
    store: TuoguanStore | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Queue one idempotent boss-only daily report in the existing outbox."""

    report_kind = _normalize_kind(kind)
    actual_store = store or TuoguanStore()
    timestamp = now or datetime.now().astimezone()
    tenant_id = current_tenant_id()
    day = timestamp.strftime("%Y%m%d")
    notification_id = f"autonomous_daily_report:{day}:{report_kind}"
    owner_id = _owner_user_id(actual_store)
    if not owner_id:
        return {
            "ok": False,
            "error": "boss_user_not_found",
            "message": "没有找到老板企业微信 user id，日报没有入队。",
            "dry_run": dry_run,
        }
    if not dry_run and not _in_delivery_window(report_kind, timestamp):
        return {
            "ok": False,
            "queued": False,
            "error": "daily_report_outside_delivery_window",
            "report_kind": report_kind,
            "observed_at": timestamp.isoformat(timespec="seconds"),
            "message": "固定日报只能在已批准的早报或晚报时间窗入队。",
        }

    report = build_daily_boss_report(report_kind, store=actual_store, now=timestamp)
    row = _outbox_row(
        notification_id=notification_id,
        kind=report_kind,
        owner_id=owner_id,
        timestamp=timestamp,
        content=str(report.get("content") or ""),
        summary=str(report.get("summary") or ""),
    )
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "queued": False,
            "notification_id": notification_id,
            "report": report,
            "outbox_item": row,
        }

    with authorized_system_write(
        actual_store.data_dir,
        job_name="daily_boss_report",
        allowed_files={NOTIFICATION_OUTBOX_FILE, DAILY_REPORT_RUNS_FILE},
    ) as auth:
        outbox_state: dict[str, Any] = {"queued": False, "existing_status": ""}

        def upsert_daily_report(outbox: Any) -> Any:
            outbox = outbox if isinstance(outbox, list) else []
            existing = _find_outbox_item(outbox, notification_id)
            if (
                existing
                and str(existing.get("status") or "") == "sent"
                and not _in_delivery_window(report_kind, _parse_report_timestamp(existing.get("sent_at") or existing.get("created_at"), timestamp))
            ):
                existing.update(row)
                existing["requeued_after_off_schedule_delivery"] = True
                outbox_state["queued"] = True
                return outbox[-2000:]
            if existing and str(existing.get("status") or "") in {"pending", "retry_pending", "sending", "sent"}:
                outbox_state["existing_status"] = str(existing.get("status") or "")
                return JSON_NO_CHANGE
            if existing:
                existing.update(row)
            else:
                outbox.append(row)
            outbox_state["queued"] = True
            return outbox[-2000:]

        actual_store.update_json(NOTIFICATION_OUTBOX_FILE, [], upsert_daily_report)
        if not outbox_state["queued"]:
            return {
                "ok": True,
                "queued": False,
                "idempotent_replay": True,
                "notification_id": notification_id,
                "existing_status": str(outbox_state.get("existing_status") or ""),
                "report": report,
            }
        _append_jsonl(
            actual_store,
            DAILY_REPORT_RUNS_FILE,
            {
                "run_id": f"daily_report_run:{day}:{report_kind}",
                "tenant_id": tenant_id,
                "report_kind": report_kind,
                "notification_id": notification_id,
                "target_user_id": owner_id,
                "queued_at": timestamp.isoformat(timespec="seconds"),
                "operation_id": auth.operation_id,
                "ledger_id": auth.ledger_id,
                "audit_id": auth.audit_id,
                "source_counts": deepcopy(report.get("source_counts") or {}),
                "auto_effects": _safe_auto_effects(),
            },
        )

    return {
        "ok": True,
        "queued": True,
        "notification_id": notification_id,
        "target_user_id": owner_id,
        "report": report,
        "writeback_verified": True,
    }


def _in_delivery_window(kind: str, timestamp: datetime) -> bool:
    start, end = REPORT_DELIVERY_WINDOWS[_normalize_kind(kind)]
    minute = timestamp.hour * 60 + timestamp.minute
    return start <= minute <= end


def _parse_report_timestamp(value: Any, fallback: datetime) -> datetime:
    text = str(value or "").strip()
    if not text:
        return fallback
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None and fallback.tzinfo is not None:
        parsed = parsed.replace(tzinfo=fallback.tzinfo)
    if fallback.tzinfo is not None:
        parsed = parsed.astimezone(fallback.tzinfo)
    return parsed


def sync_daily_report_delivery_status(
    store: TuoguanStore,
    *,
    outbox_item: dict[str, Any],
    operation_id: str,
    ledger_id: str = "",
    audit_id: str = "",
) -> dict[str, Any]:
    """Append delivery evidence for an already queued daily report."""

    if str(outbox_item.get("notification_type") or "") != "autonomous_daily_report":
        return {"ok": True, "skipped": True, "reason": "not_daily_report"}
    notification_id = str(outbox_item.get("id") or "").strip()
    if not notification_id:
        return {"ok": False, "error": "notification_id_required"}
    report_kind = _kind_from_notification(outbox_item)
    status = str(outbox_item.get("status") or "unknown")
    observed_at = datetime.now().astimezone().isoformat(timespec="seconds")
    existing = _read_jsonl(store, DAILY_REPORT_RUNS_FILE)
    fingerprint = {
        "notification_id": notification_id,
        "delivery_status": status,
        "sent_at": str(outbox_item.get("sent_at") or ""),
        "message_id": str(outbox_item.get("message_id") or ""),
        "last_error": str(outbox_item.get("last_error") or ""),
    }
    for row in reversed(existing[-50:]):
        if str(row.get("record_type") or "") != "daily_report_delivery_status":
            continue
        same = (
            str(row.get("notification_id") or "") == notification_id
            and str(row.get("delivery_status") or "") == fingerprint["delivery_status"]
            and str(row.get("sent_at") or "") == fingerprint["sent_at"]
            and str(row.get("message_id") or "") == fingerprint["message_id"]
            and str(row.get("last_error") or "") == fingerprint["last_error"]
        )
        if same:
            return {"ok": True, "state_changed": False, "idempotent_replay": True}
    _append_jsonl(
        store,
        DAILY_REPORT_RUNS_FILE,
        {
            "record_type": "daily_report_delivery_status",
            "run_id": f"daily_report_run:{_day_from_notification(outbox_item)}:{report_kind}",
            "tenant_id": current_tenant_id(),
            "report_kind": report_kind,
            "notification_id": notification_id,
            "target_user_id": str(outbox_item.get("target_user_id") or outbox_item.get("touser") or ""),
            "delivery_status": status,
            "status": status,
            "sent_at": fingerprint["sent_at"],
            "failed_at": observed_at if status in {"failed", "result_unknown"} else "",
            "message_id": fingerprint["message_id"],
            "last_error": fingerprint["last_error"],
            "observed_at": observed_at,
            "operation_id": operation_id,
            "ledger_id": ledger_id,
            "audit_id": audit_id,
            "auto_effects": _safe_auto_effects(),
        },
    )
    return {"ok": True, "state_changed": True, "writeback_verified": True}


def build_daily_boss_report(kind: str, *, store: TuoguanStore | None = None, now: datetime | None = None) -> dict[str, Any]:
    report_kind = _normalize_kind(kind)
    actual_store = store or TuoguanStore()
    timestamp = now or datetime.now().astimezone()
    identity = _system_identity()
    work = query_hermes_work_items(actual_store, identity=identity, include_closed=False, limit=10)
    brief = query_autonomous_work_brief(actual_store, identity=identity, limit=10)
    self_evolution = build_self_evolution_brief(actual_store, identity=identity, limit=6)
    staff_voice = query_staff_voice_radar(actual_store, identity=identity, now_at=timestamp.isoformat(timespec="seconds"), since_hours=24, limit=10)
    attention = query_attention_threads(actual_store, identity=identity, include_closed=False, limit=5)
    items = work.get("items") if isinstance(work.get("items"), list) else []
    open_attention = attention.get("attention_threads") if isinstance(attention.get("attention_threads"), list) else []
    items = _fresh_report_work_items(items, timestamp)
    open_attention = _fresh_attention_threads(open_attention, timestamp)
    waiting_items = [item for item in items if str(item.get("status") or "") == "waiting"]
    source_counts = {
        "work_item_count": len(items),
        "waiting_count": len(waiting_items),
        "open_attention_count": len(open_attention),
        "recent_business_event_count": int(brief.get("recent_business_event_count") or 0),
        "recent_action_execution_count": int(brief.get("recent_action_execution_count") or 0),
        "result_unknown_action_count": int(brief.get("result_unknown_action_count") or 0),
    }
    proactivity_health = _proactivity_health(actual_store, timestamp)
    source_counts["proactivity_issue_count"] = len(proactivity_health)
    source_counts["self_evolution_next_day_count"] = len(self_evolution.get("next_day_context") or [])
    source_counts["self_evolution_review_queue_count"] = int(self_evolution.get("review_queue_count") or 0)
    source_counts["staff_voice_signal_count"] = int(staff_voice.get("signal_count") or 0) if isinstance(staff_voice, dict) else 0
    source_counts["staff_voice_high_or_urgent_open_count"] = int(staff_voice.get("high_or_urgent_open_count") or 0) if isinstance(staff_voice, dict) else 0
    owner_id = _owner_user_id(actual_store)
    workstyle = daily_report_style_for_owner(actual_store, owner_id)
    source_counts["workstyle_preference_count"] = len(workstyle.get("active_preferences") or [])
    if report_kind == "morning":
        content = _render_morning_report(timestamp, items, waiting_items, open_attention, brief, self_evolution, source_counts, proactivity_health, workstyle, staff_voice)
        summary = "小优每日早间工作安排"
    else:
        content = _render_evening_report(timestamp, items, waiting_items, open_attention, brief, self_evolution, source_counts, proactivity_health, workstyle, staff_voice)
        summary = "小优每日晚间工作日报"
    return {
        "ok": True,
        "report_kind": report_kind,
        "generated_at": timestamp.isoformat(timespec="seconds"),
        "content": content,
        "summary": summary,
        "source_counts": source_counts,
        "model_led": False,
        "limits_model": False,
        "auto_effects": _safe_auto_effects(),
        "render_verified": True,
    }


def _render_morning_report(
    timestamp: datetime,
    items: list[dict[str, Any]],
    waiting_items: list[dict[str, Any]],
    open_attention: list[dict[str, Any]],
    brief: dict[str, Any],
    self_evolution: dict[str, Any],
    source_counts: dict[str, int],
    proactivity_health: list[str],
    workstyle: dict[str, Any],
    staff_voice: dict[str, Any],
) -> str:
    scorecard = brief.get("employee_scorecard") if isinstance(brief.get("employee_scorecard"), dict) else {}
    latest_review = scorecard.get("latest_review") if isinstance(scorecard.get("latest_review"), dict) else {}
    candidate_lines = [
        _first_or_default(
            _owner_digest_lines(brief, latest_review, source_counts, purpose="morning"),
            "目标进展：暂无新的目标证据；我先做事实巡检，有缺口再说明需要谁补事实。",
        ),
        _first_or_default(
            _work_item_lines(items, purpose="morning"),
            "今天先做：继续巡检活跃目标、机构认知缺口和记录覆盖。",
        ),
        _first_or_default(
            _waiting_lines(waiting_items, open_attention),
            "卡点：当前没有未解决提醒；发现关键缺口时，我会问事实归属人。",
        ),
        _first_or_default(
            _tomorrow_lines(items, latest_review),
            "下一步：把等待、证据和需要确认的人拆清楚，不把建议当结果。",
        ),
    ]
    evolution_lines = _self_evolution_lines(self_evolution, purpose="morning")
    if evolution_lines:
        candidate_lines.insert(1, evolution_lines[0])
    staff_voice_line = _staff_voice_report_line(staff_voice, purpose="morning")
    if staff_voice_line:
        candidate_lines.insert(2, staff_voice_line)
    if _style_is_ultra_concise(workstyle):
        return _render_ultra_morning_report(
            items=items,
            waiting_items=waiting_items,
            open_attention=open_attention,
            candidate_lines=candidate_lines,
            source_counts=source_counts,
            proactivity_health=proactivity_health,
            workstyle=workstyle,
        )
    report_items = _concise_report_items(
        candidate_lines,
        proactivity_health=proactivity_health,
        max_items=_style_max_items(workstyle),
    )
    lines = [
        f"金总，早上好，我是小优。今天重点：",
        *_numbered(report_items, empty="1. 今天暂无新增材料，我会继续做事实巡检和卡点跟进。"),
        _style_closing_line(workstyle),
    ]
    return _limit_message("\n".join(lines), _style_limit(workstyle))


def _render_evening_report(
    timestamp: datetime,
    items: list[dict[str, Any]],
    waiting_items: list[dict[str, Any]],
    open_attention: list[dict[str, Any]],
    brief: dict[str, Any],
    self_evolution: dict[str, Any],
    source_counts: dict[str, int],
    proactivity_health: list[str],
    workstyle: dict[str, Any],
    staff_voice: dict[str, Any],
) -> str:
    scorecard = brief.get("employee_scorecard") if isinstance(brief.get("employee_scorecard"), dict) else {}
    latest_review = scorecard.get("latest_review") if isinstance(scorecard.get("latest_review"), dict) else {}
    value = brief.get("value_progress_ledger") if isinstance(brief.get("value_progress_ledger"), dict) else {}
    value_entries = value.get("entries") if isinstance(value.get("entries"), list) else []
    candidate_lines = [
        _first_or_default(
            _owner_digest_lines(brief, latest_review, source_counts, purpose="evening"),
            "目标进展：今天没有新的可确认目标结果；我不会把等待状态写成完成。",
        ),
        _first_or_default(
            _work_item_lines(items, purpose="evening"),
            "工作状态：今天没有新的可确认业务推进记录。",
        ),
        _first_or_default(
            _value_lines(value_entries, latest_review),
            "价值证据：暂无新的可确认价值结果；继续按真实证据记录。",
        ),
        _first_or_default(
            _waiting_lines(waiting_items, open_attention) or _tomorrow_lines(items, latest_review),
            "明天先看：活跃目标、事实缺口、记录覆盖和老板待确认事项。",
        ),
    ]
    evolution_lines = _self_evolution_lines(self_evolution, purpose="evening")
    if evolution_lines:
        candidate_lines.insert(1, evolution_lines[0])
    staff_voice_line = _staff_voice_report_line(staff_voice, purpose="evening")
    if staff_voice_line:
        candidate_lines.insert(2, staff_voice_line)
    if _style_is_ultra_concise(workstyle):
        return _render_ultra_evening_report(
            items=items,
            waiting_items=waiting_items,
            open_attention=open_attention,
            value_entries=value_entries,
            candidate_lines=candidate_lines,
            proactivity_health=proactivity_health,
            workstyle=workstyle,
        )
    report_items = _concise_report_items(
        candidate_lines,
        proactivity_health=proactivity_health,
        max_items=_style_max_items(workstyle),
    )
    lines = [
        "金总，今晚工作重点：",
        *_numbered(report_items, empty="1. 今天暂无新增材料，我没有把等待状态写成完成。"),
        _style_closing_line(workstyle),
    ]
    return _limit_message("\n".join(lines), _style_limit(workstyle))


def _render_ultra_morning_report(
    *,
    items: list[dict[str, Any]],
    waiting_items: list[dict[str, Any]],
    open_attention: list[dict[str, Any]],
    candidate_lines: list[str],
    source_counts: dict[str, int],
    proactivity_health: list[str],
    workstyle: dict[str, Any],
) -> str:
    status = _ultra_morning_status(source_counts)
    confirmation = _first_safe_report_text(
        _waiting_lines(waiting_items, open_attention),
        "无新增老板确认点。",
        limit=58,
    )
    focus = _first_safe_report_text(
        _work_item_lines(items, purpose="morning") or candidate_lines[1:],
        "继续巡检活跃目标、事实缺口和记录覆盖。",
        limit=58,
    )
    rows = [
        "金总，早上好，小优今日重点：",
        f"状态：{_strip_report_prefix(status)}",
        f"需确认：{_strip_report_prefix(confirmation)}",
        f"今日重点：{_strip_report_prefix(focus)}",
    ]
    if proactivity_health:
        rows.append(f"异常：{_strip_report_prefix(_limit_text(proactivity_health[0], 54))}")
    rows.append(_style_closing_line(workstyle))
    return _limit_message(_join_style_lines(rows, workstyle), _style_limit(workstyle))


def _render_ultra_evening_report(
    *,
    items: list[dict[str, Any]],
    waiting_items: list[dict[str, Any]],
    open_attention: list[dict[str, Any]],
    value_entries: list[dict[str, Any]],
    candidate_lines: list[str],
    proactivity_health: list[str],
    workstyle: dict[str, Any],
) -> str:
    completed = _first_safe_report_text(
        _value_lines(value_entries, {}) or candidate_lines,
        "无新的可确认完成项；没有把等待状态写成完成。",
        limit=82,
    )
    issue = _first_safe_report_text(
        [proactivity_health[0]] if proactivity_health else _waiting_lines(waiting_items, open_attention),
        "无新增异常。",
        limit=78,
    )
    tomorrow = _first_safe_report_text(
        _tomorrow_lines(items, {}) or _work_item_lines(items, purpose="morning"),
        "继续巡检活跃目标、事实缺口和记录覆盖。",
        limit=82,
    )
    rows = [
        "金总，今晚小优汇报：",
        f"完成：{_strip_report_prefix(completed)}",
        f"异常：{_strip_report_prefix(issue)}",
        f"明日计划：{_strip_report_prefix(tomorrow)}",
        _style_closing_line(workstyle),
    ]
    return _limit_message(_join_style_lines(rows, workstyle), _style_limit(workstyle))


def _fresh_attention_threads(rows: list[dict[str, Any]], timestamp: datetime) -> list[dict[str, Any]]:
    cutoff = timestamp - timedelta(hours=REPORT_FRESHNESS_HOURS)
    fresh: list[dict[str, Any]] = []
    for row in rows:
        row_time = _material_timestamp(row, timestamp)
        if row_time is not None and row_time < cutoff:
            continue
        fresh.append(row)
    return fresh


def _fresh_report_work_items(rows: list[dict[str, Any]], timestamp: datetime) -> list[dict[str, Any]]:
    cutoff = timestamp - timedelta(hours=REPORT_FRESHNESS_HOURS)
    fresh: list[dict[str, Any]] = []
    for row in rows:
        row_time = _material_timestamp(row, timestamp)
        is_old = bool(row_time is not None and row_time < cutoff)
        status = str(row.get("status") or "").strip().lower()
        if status not in {"active", "waiting", "blocked", "pending"}:
            continue
        if hermes_work_item_is_semantically_retired(row):
            continue
        # Daily reports are about current work, not the entire open ledger. An
        # old `active` flag is insufficient evidence that an item is still a
        # useful today-focus.
        if is_old:
            continue
        fresh.append(row)
    return fresh
def _material_timestamp(row: dict[str, Any], now: datetime) -> datetime | None:
    for key in (
        "updated_at",
        "created_at",
        "queued_at",
        "sent_at",
        "last_attention_at",
        "next_attention_at",
        "next_contact_after",
    ):
        value = str(row.get(key) or "").strip()
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=now.tzinfo)
        return parsed
    return None


def _proactivity_health(store: TuoguanStore, timestamp: datetime) -> list[str]:
    issues: list[str] = []
    outbox = store.read_json(NOTIFICATION_OUTBOX_FILE, [])
    if not isinstance(outbox, list):
        outbox = []
    since = timestamp.timestamp() - 24 * 3600
    delivery_rows = [
        row for row in _read_jsonl(store, DAILY_REPORT_RUNS_FILE)
        if str(row.get("record_type") or "") == "daily_report_delivery_status"
        and str(row.get("status") or row.get("delivery_status") or "") == "sent"
        and _row_ts(row.get("observed_at") or row.get("sent_at")) >= since
    ]
    if not delivery_rows:
        issues.append("过去24小时没有可验证的老板日报送达记录；本次日报会立即外发并写回投递状态。")
    failed_daily = [
        item for item in outbox
        if isinstance(item, dict)
        and str(item.get("notification_type") or "") == "autonomous_daily_report"
        and str(item.get("status") or "") in {"failed", "result_unknown"}
        and _row_ts(item.get("failed_at") or item.get("last_attempt_at") or item.get("created_at")) >= since
    ]
    if failed_daily:
        issues.append(f"过去24小时有 {len(failed_daily)} 条日报投递异常，已保留失败证据，不再静默压掉。")
    failed_proactive = [
        item for item in outbox
        if isinstance(item, dict)
        and str(item.get("notification_type") or "") in {"autonomous_owner_attention", "relationship_touch"}
        and str(item.get("status") or "") in {"failed", "result_unknown"}
        and _row_ts(item.get("failed_at") or item.get("last_attempt_at") or item.get("created_at")) >= since
    ]
    if failed_proactive:
        issues.append(f"过去24小时有 {len(failed_proactive)} 条主动提问/关系触达异常，需要继续核验发送链路。")
    reminder_keys: dict[str, int] = {}
    for item in outbox:
        if not isinstance(item, dict):
            continue
        if str(item.get("action") or "") not in {"task_created", "task_due", "manual_assignment"}:
            continue
        if _row_ts(item.get("created_at")) < since:
            continue
        key = "|".join(str(item.get(part) or "") for part in ("task_id", "action", "touser"))
        reminder_keys[key] = reminder_keys.get(key, 0) + 1
    repeated = [key for key, count in reminder_keys.items() if count > 1]
    if repeated:
        issues.append(f"过去24小时发现 {len(repeated)} 组任务提醒重复候选，需要按幂等键核验。")
    reply_rows = [
        row for row in _read_jsonl(store, "reply_ledger.jsonl")
        if _row_ts(row.get("completed_at") or row.get("created_at")) >= since
    ]
    unverified_claims = [
        row for row in reply_rows
        if not (row.get("tool_calls") or row.get("used_tool_registry_entry"))
        and _looks_like_unverified_capability_claim(row)
    ]
    if unverified_claims:
        issues.append(f"过去24小时有 {len(unverified_claims)} 条回复疑似未查工具却声称已查/已保存；需要按先查证再答复规则复盘。")
    missed_staff_directory = [
        row for row in reply_rows
        if not (row.get("tool_calls") or row.get("used_tool_registry_entry"))
        and _looks_like_staff_directory_need(row.get("raw_text"))
        and _looks_like_staff_directory_deflection(row.get("final_reply"))
    ]
    if missed_staff_directory:
        issues.append(f"过去24小时有 {len(missed_staff_directory)} 条人员/企业微信问题疑似未先查目录就转人工；需要优先使用人员目录工具。")
    return issues[:3]


def _row_ts(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed.timestamp()
    except (TypeError, ValueError):
        return 0.0


def _looks_like_unverified_capability_claim(row: dict[str, Any]) -> bool:
    text = str(row.get("final_reply") or "")
    return any(
        term in text
        for term in (
            "我查了",
            "小优查了",
            "查到",
            "系统里显示",
            "系统显示",
            "已保存",
            "已经保存",
            "确认并保存",
            "已经记住",
            "记住了",
            "以后按这个来理解",
            "以后就按这个来理解",
        )
    )


def _looks_like_staff_directory_need(value: Any) -> bool:
    text = str(value or "")
    return any(term in text for term in ("企业微信", "通讯录", "老师名单", "店长", "老师都有谁", "乱码", "人员", "名字"))


def _looks_like_staff_directory_deflection(value: Any) -> bool:
    text = str(value or "")
    return any(term in text for term in ("查不到", "需要您告诉", "需要你告诉", "技术", "处理一下", "没法", "无法从系统"))


def _owner_digest_lines(
    brief: dict[str, Any],
    latest_review: dict[str, Any],
    source_counts: dict[str, int],
    *,
    purpose: str,
) -> list[str]:
    lines: list[str] = []
    goal_progress = _pick_text(latest_review, "goal_progress")
    blocked_by = _pick_text(latest_review, "blocked_by")
    tomorrow_focus = _pick_text(latest_review, "tomorrow_focus")
    if goal_progress:
        lines.append(f"目标进展：{goal_progress}")
    elif source_counts.get("work_item_count", 0) or source_counts.get("waiting_count", 0):
        lines.append(
            "目标进展："
            f"当前有 {source_counts.get('work_item_count', 0)} 个活跃事项、{source_counts.get('waiting_count', 0)} 个等待确认；"
            "我会先把等待和证据拆清楚，不把建议当结果。"
        )
    if blocked_by:
        lines.append(f"当前卡点：{blocked_by}")
    if purpose == "morning" and tomorrow_focus:
        lines.append(f"今天先盯：{tomorrow_focus}")
    if purpose == "evening" and source_counts.get("result_unknown_action_count", 0):
        lines.append(f"结果未知：还有 {source_counts.get('result_unknown_action_count', 0)} 个动作需要继续核验，暂不写成完成。")
    return lines[:3]


def _work_item_lines(items: list[dict[str, Any]], *, purpose: str) -> list[str]:
    lines: list[str] = []
    for item in items[:4]:
        title = _pick_text(item, "title", "focus_summary", "focus_key")
        status = _public_status_label(_pick_text(item, "status") or "active")
        phase = _public_phase_text(item.get("current_phase"))
        next_action = _first_text(item.get("next_actions"))
        waiting = _text_from_any(item.get("current_waiting"))
        detail = next_action or waiting or phase or _pick_text(item, "focus_summary")
        if not title:
            continue
        if purpose == "morning":
            lines.append(f"{title}：{status}；今天先看 {detail or '是否有新事实和下一关注时间'}。")
        else:
            lines.append(f"{title}：{status}；当前记录为 {detail or '暂无新的可确认变化'}。")
    return lines


def _public_phase_text(value: Any) -> str:
    if isinstance(value, str):
        return "" if _looks_like_internal_structured_text(value) else _text_from_any(value)
    if not isinstance(value, dict):
        return ""
    # `phase_key` is an internal state key, not boss-facing prose. Only fields
    # explicitly written as human-readable labels may enter a report.
    for key in ("phase_label", "label", "summary", "name", "description"):
        text = _text_from_any(value.get(key))
        if text and not _looks_like_internal_structured_text(text):
            return text
    return ""


def _waiting_lines(waiting_items: list[dict[str, Any]], open_attention: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in waiting_items[:3]:
        title = _pick_text(item, "title", "focus_key")
        waiting = _clean_waiting_detail(_text_from_any(item.get("current_waiting")) or _first_text(item.get("blocked_by")))
        next_at = _public_time_label(_pick_text(item, "next_attention_at", "next_contact_after"))
        lines.append(f"{title or '未命名工作项'}：等待{waiting or '新事实'}；下一关注时间 {next_at or '待状态更新'}。")
    for thread in open_attention[:2]:
        question = _pick_text(thread, "question_text")
        status = _public_status_label(_pick_text(thread, "status") or "open")
        lines.append(f"未解决老板提醒：{question or '问题内容未记录'}；当前状态 {status}。")
    return lines


def _public_status_label(status: str) -> str:
    value = str(status or "").strip().lower()
    mapping = {
        "active": "进行中",
        "open": "待处理",
        "waiting": "等待确认",
        "pending": "待处理",
        "retry_pending": "待重试",
        "sending": "发送中",
        "sent": "已发送",
        "done": "已完成",
        "completed": "已完成",
        "closed": "已关闭",
        "failed": "异常",
        "result_unknown": "结果待核验",
        "suppressed": "已抑制",
        "superseded": "已更新",
    }
    return mapping.get(value, str(status or "").strip() or "进行中")


def _clean_waiting_detail(value: str) -> str:
    text = str(value or "").strip()
    for prefix in ("等待", "待", "等"):
        if text.startswith(prefix):
            cleaned = text[len(prefix):].strip(" ：:，,；;")
            return cleaned or text
    return text


def _public_time_label(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone(timedelta(hours=8)))
    return f"{parsed.month}月{parsed.day}日{parsed.hour:02d}:{parsed.minute:02d}"


def _value_lines(value_entries: list[dict[str, Any]], latest_review: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for entry in value_entries[-3:]:
        subject = _pick_text(entry, "subject") or "未命名事项"
        discovered = _pick_text(entry, "discovered")
        action = _pick_text(entry, "hermes_action")
        outcome = _pick_text(entry, "outcome")
        lines.append(f"{subject}：发现 {discovered or '已记录观察'}；小优动作 {action or '整理证据'}；结果 {outcome or '等待真实结果'}。")
    if not lines and latest_review:
        parts = [
            _pick_text(latest_review, "goal_progress"),
            _pick_text(latest_review, "risk_detection"),
            _pick_text(latest_review, "business_opportunity"),
        ]
        note = "；".join(part for part in parts if part)
        if note:
            lines.append(note)
    return lines


def _tomorrow_lines(items: list[dict[str, Any]], latest_review: dict[str, Any]) -> list[str]:
    focus = _pick_text(latest_review, "tomorrow_focus")
    lines = [focus] if focus else []
    for item in items[:3]:
        next_action = _first_text(item.get("next_actions"))
        if next_action:
            title = _pick_text(item, "title", "focus_key") or "工作项"
            lines.append(f"{title}：{next_action}")
    return lines[:4]


def _self_evolution_lines(brief: dict[str, Any], *, purpose: str) -> list[str]:
    if not isinstance(brief, dict):
        return []
    next_context = [
        _limit_text(str(item or ""), 130)
        for item in (brief.get("next_day_context") or [])
        if str(item or "").strip()
    ]
    review_count = int(brief.get("review_queue_count") or 0)
    lines: list[str] = []
    if next_context:
        prefix = "今天带入" if purpose == "morning" else "复盘进化"
        lines.append(f"{prefix}：{next_context[0]}")
    if purpose == "evening" and review_count > 0:
        lines.append(f"待确认进化：{review_count} 条中高风险候选只留档，未自动生效。")
    return lines[:2]


def _staff_voice_report_line(radar: dict[str, Any], *, purpose: str) -> str:
    if not isinstance(radar, dict) or not radar.get("ok"):
        return ""
    named = radar.get("named_signals") if isinstance(radar.get("named_signals"), list) else []
    high_count = int(radar.get("high_or_urgent_open_count") or 0)
    trends = radar.get("low_risk_trends") if isinstance(radar.get("low_risk_trends"), list) else []
    if high_count and named:
        latest = named[-1]
        who = _limit_text(latest.get("source_name") or latest.get("source_user_id") or latest.get("source_role") or "员工", 40)
        summary = _limit_text(latest.get("signal_summary"), 120)
        return f"员工声音：{high_count} 条高风险开放，最近 {who} 提到 {summary or '现场问题'}。"
    medium = [item for item in named if str(item.get("risk_level") or "") == "medium"]
    if medium:
        latest = medium[-1]
        who = _limit_text(latest.get("source_name") or latest.get("source_user_id") or latest.get("source_role") or "员工", 40)
        summary = _limit_text(latest.get("signal_summary"), 120)
        label = "今天关注" if purpose == "morning" else "团队反馈"
        return f"员工声音：{label} {who} 的中风险反馈：{summary or '需进一步看事实'}。"
    if trends:
        trend = trends[0]
        return f"员工声音：低风险趋势 {trend.get('category')} {trend.get('count')} 条，先观察趋势，不点名打扰。"
    return ""


def _first_or_default(lines: list[str], fallback: str) -> str:
    for line in lines:
        cleaned = _limit_text(str(line or ""), 160)
        if cleaned:
            return cleaned
    return fallback


def _concise_report_items(
    candidates: list[str],
    *,
    proactivity_health: list[str],
    max_items: int,
) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    candidate_limit = max_items - 1 if proactivity_health else max_items
    for candidate in candidates:
        cleaned = _limit_text(candidate, 170)
        key = "".join(cleaned.split())
        if cleaned and key not in seen:
            items.append(cleaned)
            seen.add(key)
        if len(items) >= max(2, candidate_limit):
            break
    if proactivity_health and len(items) < max_items:
        items.append("异常：" + _limit_text(proactivity_health[0], 150))
    return items[:max(3, max_items)]


def _style_max_items(workstyle: dict[str, Any]) -> int:
    try:
        value = int(workstyle.get("max_items") or 5)
    except (TypeError, ValueError):
        value = 5
    return max(3, min(value, 5))


def _style_limit(workstyle: dict[str, Any]) -> int:
    try:
        value = int(workstyle.get("max_chars") or 700)
    except (TypeError, ValueError):
        value = 700
    return max(420, min(value, 1200))


def _style_closing_line(workstyle: dict[str, Any]) -> str:
    closing = str(workstyle.get("closing_line") or "").strip()
    return closing or "细节我已留档，需要我展开哪一项你直接说。"


def _style_is_ultra_concise(workstyle: dict[str, Any]) -> bool:
    try:
        max_items = int(workstyle.get("max_items") or 5)
    except (TypeError, ValueError):
        max_items = 5
    return str(workstyle.get("report_length") or "") == "ultra_concise" or max_items <= 3


def _ultra_morning_status(source_counts: dict[str, int]) -> str:
    work_count = int(source_counts.get("work_item_count") or 0)
    waiting_count = int(source_counts.get("waiting_count") or 0)
    attention_count = int(source_counts.get("open_attention_count") or 0)
    parts: list[str] = []
    if work_count:
        parts.append(f"{work_count}个事项在跟进")
    if waiting_count:
        parts.append(f"{waiting_count}个事项待确认")
    if attention_count:
        parts.append(f"{attention_count}个老板提醒待处理")
    if not parts:
        return "正常巡检中，暂无新增确认点。"
    return "，".join(parts) + "。"


def _join_style_lines(lines: list[str], workstyle: dict[str, Any]) -> str:
    separator = "\n\n" if str(workstyle.get("layout") or "") == "spaced_sections" else "\n"
    return separator.join(line for line in lines if str(line or "").strip())


def _first_safe_report_text(lines: list[str], fallback: str, *, limit: int = 110) -> str:
    for line in lines:
        text = _limit_text(str(line or ""), limit)
        if text and not _looks_like_internal_structured_text(text):
            return text
    return fallback


def _strip_report_prefix(text: str) -> str:
    value = str(text or "").strip()
    prefixes = (
        "目标进展：", "当前卡点：", "今天先盯：", "目标推进：", "工作状态：",
        "价值证据：", "明天先看：", "下一步：", "异常：", "完成：", "需确认：", "今日重点：",
        "今天带入：", "复盘进化：", "未解决老板提醒：",
    )
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            if value.startswith(prefix):
                value = value[len(prefix):].strip()
                changed = True
    return value


def _looks_like_internal_structured_text(text: str) -> bool:
    compact = "".join(str(text or "").split())
    if compact.startswith(("{", "[")) or "{\"" in compact or "{'" in compact:
        return True
    return any(
        term in compact
        for term in (
            "'goal_id'", "\"goal_id\"", "'current_phase'", "\"current_phase\"",
            "'phase_key'", "\"phase_key\"", "'focus_key'", "\"focus_key\"",
            "'work_item_id'", "\"work_item_id\"",
        )
    )


def _outbox_row(
    *,
    notification_id: str,
    kind: str,
    owner_id: str,
    timestamp: datetime,
    content: str,
    summary: str,
) -> dict[str, Any]:
    return {
        "id": notification_id,
        "status": "pending",
        "delivery_mode": "direct_wecom",
        "notification_type": "autonomous_daily_report",
        "task_id": f"autonomous_daily_report:{kind}:{timestamp.strftime('%Y%m%d')}",
        "role": "boss",
        "action": f"daily_{kind}_report",
        "target_user_id": owner_id,
        "recipient_user_id": owner_id,
        "to_user_id": owner_id,
        "touser": owner_id,
        "content": content,
        "summary": summary,
        "created_at": timestamp.isoformat(timespec="seconds"),
        "attempt_count": 0,
        "auto_effects": _safe_auto_effects(),
    }


def _safe_auto_effects() -> dict[str, bool]:
    return {
        "sends_parent_messages": False,
        "sends_teacher_messages": False,
        "creates_teacher_tasks": False,
        "changes_salary": False,
        "changes_permissions": False,
        "changes_responsibility_binding": False,
        "deletes_data": False,
        "changes_router": False,
        "forces_next_action": False,
    }


def _append_jsonl(store: TuoguanStore, filename: str, row: dict[str, Any]) -> None:
    store.append_jsonl_verified(filename, row)


def _find_outbox_item(outbox: list[Any], notification_id: str) -> dict[str, Any] | None:
    for item in outbox:
        if isinstance(item, dict) and str(item.get("id") or "") == notification_id:
            return item
    return None


def _read_jsonl(store: TuoguanStore, filename: str) -> list[dict[str, Any]]:
    path = store.path_for(filename)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _kind_from_notification(item: dict[str, Any]) -> str:
    text = " ".join(str(item.get(key) or "") for key in ("id", "task_id", "action"))
    if "evening" in text:
        return "evening"
    return "morning"


def _day_from_notification(item: dict[str, Any]) -> str:
    for key in ("id", "task_id"):
        text = str(item.get(key) or "")
        for part in text.replace(":", " ").split():
            if len(part) == 8 and part.isdigit():
                return part
    created = str(item.get("created_at") or "")[:10].replace("-", "")
    return created if len(created) == 8 and created.isdigit() else datetime.now().strftime("%Y%m%d")


def _numbered(lines: list[str], *, empty: str) -> list[str]:
    if not lines:
        return [empty]
    return [f"{idx}. {line}" for idx, line in enumerate(lines, start=1)]


def _pick_text(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        text = _text_from_any(data.get(key))
        if text:
            return text
    return ""


def _first_text(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            text = _text_from_any(item)
            if text:
                return text
        return ""
    return _text_from_any(value)


def _text_from_any(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        cleaned = _public_text(value.strip())
        return "" if cleaned in {"", "{}", "[]", "null", "None"} else _limit_text(cleaned, 180)
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        if not value:
            return ""
        for key in ("text", "summary", "name", "title", "reason", "status", "phase", "value", "description"):
            if str(value.get(key) or "").strip():
                return _limit_text(str(value[key]).strip(), 180)
        return ""
    if isinstance(value, list):
        if not value:
            return ""
        parts = [_text_from_any(item) for item in value[:3]]
        return _limit_text("；".join(part for part in parts if part), 180)
    return _limit_text(_public_text(str(value).strip()), 180)


def _source_line(source_counts: dict[str, int]) -> str:
    return (
        "材料来源："
        f"活跃事项 {source_counts.get('work_item_count', 0)}，"
        f"等待 {source_counts.get('waiting_count', 0)}，"
        f"未解决提醒 {source_counts.get('open_attention_count', 0)}，"
        f"近期业务事件 {source_counts.get('recent_business_event_count', 0)}，"
        f"动作账本 {source_counts.get('recent_action_execution_count', 0)}。"
    )


def _limit_text(text: str, limit: int) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "…"


def _limit_message(text: str, limit: int) -> str:
    lines = [" ".join(line.split()) for line in str(text or "").splitlines()]
    normalized = "\n".join(lines).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "…"


def _public_text(text: str) -> str:
    replacements = {
        "boss_attention_candidate": "老板提醒候选",
        "boss_attention_candidates": "老板提醒候选",
        "notification_outbox": "发送队列",
        "attention_thread": "提醒线程",
        "action_execution": "动作账本",
        "writeback": "写后核验",
    }
    result = str(text or "")
    for source, target in replacements.items():
        result = result.replace(source, target)
    return result


def _normalize_kind(kind: str) -> str:
    normalized = str(kind or "").strip().lower()
    if normalized not in VALID_REPORT_KINDS:
        raise ValueError("report kind must be morning or evening")
    return normalized


def _wecom_callback_extra_from_env() -> dict[str, Any]:
    return {
        "name": os.getenv("WECOM_CALLBACK_APP_NAME") or "default",
        "corp_id": os.getenv("WECOM_CALLBACK_CORP_ID") or "",
        "corp_secret": os.getenv("WECOM_CALLBACK_CORP_SECRET") or "",
        "agent_id": os.getenv("WECOM_CALLBACK_AGENT_ID") or "",
        "token": os.getenv("WECOM_CALLBACK_TOKEN") or "",
        "encoding_aes_key": os.getenv("WECOM_CALLBACK_ENCODING_AES_KEY") or "",
        "host": os.getenv("WECOM_CALLBACK_HOST") or "0.0.0.0",
        "port": os.getenv("WECOM_CALLBACK_PORT") or "8645",
        "path": os.getenv("WECOM_CALLBACK_PATH") or "/wecom/callback",
    }


def _wecom_config_from_env() -> Any:
    from gateway.config import Platform, PlatformConfig

    extra = _wecom_callback_extra_from_env()
    missing = [key for key in ("corp_id", "corp_secret", "agent_id") if not str(extra.get(key) or "").strip()]
    if missing:
        raise RuntimeError("wecom_callback_config_missing:" + ",".join(missing))
    try:
        return PlatformConfig(enabled=True, extra=extra)
    except TypeError:
        config = PlatformConfig(platform=Platform.WECOM_CALLBACK, enabled=True, options=extra)
        setattr(config, "extra", extra)
        return config


async def drain_notification_outbox_once(notification_id: str = "") -> dict[str, Any]:
    """Best-effort oneshot drain for systemd timers in Hermes versions without startup hooks."""

    from plugins.platforms.wecom.callback_adapter import WecomCallbackAdapter
    from . import _drain_notification_outbox

    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - production dependency check
        raise RuntimeError("httpx_missing_for_wecom_outbox_drain") from exc

    adapter = WecomCallbackAdapter(_wecom_config_from_env())
    try:
        try:
            from gateway.platforms._http_client_limits import platform_httpx_limits

            adapter._http_client = httpx.AsyncClient(timeout=20.0, limits=platform_httpx_limits())
        except Exception:
            adapter._http_client = httpx.AsyncClient(timeout=20.0)
        await _drain_notification_outbox(adapter)
    finally:
        cleanup = getattr(adapter, "_cleanup", None)
        if callable(cleanup):
            await cleanup()
        elif getattr(adapter, "_http_client", None) is not None:
            await adapter._http_client.aclose()
    normalized_id = str(notification_id or "").strip()
    if not normalized_id:
        return {"ok": True, "drained": True}
    store = TuoguanStore()
    outbox = store.read_json(NOTIFICATION_OUTBOX_FILE, [])
    matched = _find_outbox_item(outbox if isinstance(outbox, list) else [], normalized_id)
    if not matched:
        return {"ok": False, "drained": True, "error": "notification_missing_after_drain", "notification_id": normalized_id}
    status = str(matched.get("status") or "")
    payload = {
        "ok": status == "sent",
        "drained": True,
        "notification_id": normalized_id,
        "delivery_status": status,
        "sent_at": str(matched.get("sent_at") or ""),
        "message_id": str(matched.get("message_id") or ""),
        "last_error": str(matched.get("last_error") or ""),
    }
    if status != "sent":
        payload["error"] = "daily_report_not_sent_after_drain"
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Queue Hermes boss daily report.")
    parser.add_argument("kind", choices=sorted(VALID_REPORT_KINDS))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--drain-outbox", action="store_true", help="After queueing, safely drain direct WeCom outbox once.")
    args = parser.parse_args(argv)
    result = queue_daily_boss_report(args.kind, dry_run=bool(args.dry_run))
    if args.drain_outbox and not args.dry_run and result.get("ok"):
        try:
            result["outbox_drain"] = asyncio.run(drain_notification_outbox_once(str(result.get("notification_id") or "")))
        except Exception as exc:
            result["outbox_drain"] = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not result.get("ok"):
        return 1
    drain = result.get("outbox_drain")
    if isinstance(drain, dict) and drain.get("ok") is False:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
