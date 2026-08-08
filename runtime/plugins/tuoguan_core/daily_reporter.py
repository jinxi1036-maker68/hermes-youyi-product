"""Boss-facing daily rhythm reports for Hermes autonomous work.

The reports summarize already-recorded autonomous work material. They do not
decide business actions, contact teachers or parents, assign tasks, or change
institution data.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import argparse
import asyncio
import json
import os
from typing import Any

from .digital_employee_state import (
    query_attention_threads,
    query_autonomous_work_brief,
    query_hermes_work_items,
)
from .employee_identity import owner_user_id as _owner_user_id
from .employee_identity import system_identity as _system_identity
from .store import TuoguanStore
from .tenant_context import current_tenant_id
from .write_guard import assert_business_write_allowed, authorized_system_write

DAILY_REPORT_RUNS_FILE = "daily_report_runs.jsonl"
NOTIFICATION_OUTBOX_FILE = "notification_outbox.json"
VALID_REPORT_KINDS = {"morning", "evening"}


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

    outbox = actual_store.read_json(NOTIFICATION_OUTBOX_FILE, [])
    if not isinstance(outbox, list):
        outbox = []
    existing = _find_outbox_item(outbox, notification_id)
    if existing and str(existing.get("status") or "") in {"pending", "retry_pending", "sent"}:
        return {
            "ok": True,
            "queued": False,
            "idempotent_replay": True,
            "notification_id": notification_id,
            "existing_status": str(existing.get("status") or ""),
            "report": report,
        }

    if existing:
        existing.update(row)
    else:
        outbox.append(row)

    with authorized_system_write(
        actual_store.data_dir,
        job_name="daily_boss_report",
        allowed_files={NOTIFICATION_OUTBOX_FILE, DAILY_REPORT_RUNS_FILE},
    ) as auth:
        actual_store.write_json(NOTIFICATION_OUTBOX_FILE, outbox[-2000:])
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
            "failed_at": observed_at if status == "failed" else "",
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
    attention = query_attention_threads(actual_store, identity=identity, include_closed=False, limit=5)
    items = work.get("items") if isinstance(work.get("items"), list) else []
    waiting_items = [item for item in items if str(item.get("status") or "") == "waiting"]
    open_attention = attention.get("attention_threads") if isinstance(attention.get("attention_threads"), list) else []
    source_counts = {
        "work_item_count": int(work.get("work_item_count") or 0),
        "waiting_count": int(work.get("waiting_count") or len(waiting_items)),
        "open_attention_count": int(attention.get("attention_count") or len(open_attention)),
        "recent_business_event_count": int(brief.get("recent_business_event_count") or 0),
        "recent_action_execution_count": int(brief.get("recent_action_execution_count") or 0),
        "result_unknown_action_count": int(brief.get("result_unknown_action_count") or 0),
    }
    proactivity_health = _proactivity_health(actual_store, timestamp)
    source_counts["proactivity_issue_count"] = len(proactivity_health)
    if report_kind == "morning":
        content = _render_morning_report(timestamp, items, waiting_items, open_attention, brief, source_counts, proactivity_health)
        summary = "小优每日早间工作安排"
    else:
        content = _render_evening_report(timestamp, items, waiting_items, open_attention, brief, source_counts, proactivity_health)
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
    source_counts: dict[str, int],
    proactivity_health: list[str],
) -> str:
    scorecard = brief.get("employee_scorecard") if isinstance(brief.get("employee_scorecard"), dict) else {}
    latest_review = scorecard.get("latest_review") if isinstance(scorecard.get("latest_review"), dict) else {}
    lines = [
        f"金总，早上好。我是小优，给你报一下今天的自主工作安排。时间：{timestamp.strftime('%Y-%m-%d %H:%M')}",
        "",
        "老板今天先看这句话：",
        *_numbered(_owner_digest_lines(brief, latest_review, source_counts, purpose="morning"), empty="1. 目标进展：当前没有新的目标证据；我会先做事实巡检，有缺口再说明需要谁确认。"),
        "",
        "今天我会重点盯这几件事：",
        *_numbered(_work_item_lines(items, purpose="morning"), empty="1. 暂无新的活跃工作事项；我会继续做只读巡检和事实缺口检查。"),
        "",
        "现在仍在等待或需要留意的地方：",
        *_numbered(_waiting_lines(waiting_items, open_attention), empty="1. 当前没有未解决提醒；如果发现需要你确认的关键事实，我会按白天低频规则单独问你。"),
        "",
        "主动性健康：",
        *_numbered(proactivity_health, empty="1. 过去24小时暂未发现主动汇报或主动外发异常；今天继续按日报和事实归属人提问规则推进。"),
        "",
        "今天的推进边界：我不会主动联系家长、不会批量派任务、不会改工资绩效权限；需要老师或店长补事实时，只问明确任务、记录、执行结果或运营事实。",
        _source_line(source_counts),
    ]
    return _limit_message("\n".join(lines), 2000)


def _render_evening_report(
    timestamp: datetime,
    items: list[dict[str, Any]],
    waiting_items: list[dict[str, Any]],
    open_attention: list[dict[str, Any]],
    brief: dict[str, Any],
    source_counts: dict[str, int],
    proactivity_health: list[str],
) -> str:
    scorecard = brief.get("employee_scorecard") if isinstance(brief.get("employee_scorecard"), dict) else {}
    latest_review = scorecard.get("latest_review") if isinstance(scorecard.get("latest_review"), dict) else {}
    value = brief.get("value_progress_ledger") if isinstance(brief.get("value_progress_ledger"), dict) else {}
    value_entries = value.get("entries") if isinstance(value.get("entries"), list) else []
    lines = [
        f"金总，今晚给你交一下今天的工作日报。时间：{timestamp.strftime('%Y-%m-%d %H:%M')}",
        "",
        "老板先看结论：",
        *_numbered(_owner_digest_lines(brief, latest_review, source_counts, purpose="evening"), empty="1. 目标进展：今天没有新的可确认目标结果；我不会把等待状态写成完成。"),
        "",
        "今天我确认看到的工作状态：",
        *_numbered(_work_item_lines(items, purpose="evening"), empty="1. 今天没有新的可确认业务推进记录；我没有把等待状态写成完成。"),
        "",
        "今天的价值和证据：",
        *_numbered(_value_lines(value_entries, latest_review), empty="1. 暂无新的可确认价值结果；我会继续按真实证据记录，不夸大归因。"),
        "",
        "仍然卡住或明天要继续看的事：",
        *_numbered(_waiting_lines(waiting_items, open_attention), empty="1. 当前没有未解决提醒；明天我会继续检查活跃目标、事实缺口和结果未知动作。"),
        "",
        "明天优先安排：",
        *_numbered(_tomorrow_lines(items, latest_review), empty="1. 继续巡检活跃目标、机构认知缺口、记录覆盖和老板待确认事项。"),
        "",
        "主动性健康：",
        *_numbered(proactivity_health, empty="1. 过去24小时暂未发现主动汇报或主动外发异常；如果外发失败，我会留下失败状态而不是沉默。"),
        "",
        "边界确认：今晚不主动联系家长、不派任务、不改业务数据；老师/店长只在工作事实归属明确且频率允许时被低频询问。",
        _source_line(source_counts),
    ]
    return _limit_message("\n".join(lines), 2200)


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
        status = _pick_text(item, "status") or "active"
        phase = _text_from_any(item.get("current_phase"))
        next_action = _first_text(item.get("next_actions"))
        waiting = _text_from_any(item.get("current_waiting"))
        detail = next_action or waiting or phase or _pick_text(item, "focus_summary")
        if not title:
            continue
        if purpose == "morning":
            lines.append(f"{title}：状态 {status}；今天先看 {detail or '是否有新事实和下一关注时间'}。")
        else:
            lines.append(f"{title}：状态 {status}；当前记录为 {detail or '暂无新的可确认变化'}。")
    return lines


def _waiting_lines(waiting_items: list[dict[str, Any]], open_attention: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in waiting_items[:3]:
        title = _pick_text(item, "title", "focus_key")
        waiting = _text_from_any(item.get("current_waiting")) or _first_text(item.get("blocked_by"))
        next_at = _pick_text(item, "next_attention_at", "next_contact_after")
        lines.append(f"{title or '未命名工作项'}：等待 {waiting or '新事实'}；下一关注时间 {next_at or '待状态更新'}。")
    for thread in open_attention[:2]:
        question = _pick_text(thread, "question_text")
        status = _pick_text(thread, "status") or "open"
        lines.append(f"未解决老板提醒：{question or '问题内容未记录'}；当前状态 {status}。")
    return lines


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
    assert_business_write_allowed(store.data_dir, filename)
    path = store.path_for(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


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
        return _limit_text(_public_text(value.strip()), 180)
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        for key in ("text", "summary", "name", "title", "reason", "status", "phase", "value", "description"):
            if str(value.get(key) or "").strip():
                return _limit_text(str(value[key]).strip(), 180)
        return _limit_text(_public_text(json.dumps(value, ensure_ascii=False, sort_keys=True)), 180)
    if isinstance(value, list):
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
