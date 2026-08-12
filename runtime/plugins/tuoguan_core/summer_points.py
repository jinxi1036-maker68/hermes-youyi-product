"""Deterministic summer-program point ledger and balance operations."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import re
from typing import Any
import uuid

from .models import UserIdentity
from .store import TuoguanStore
from .student_resolver import active_summer_students, resolve_active_summer_student
from .tenant_context import current_tenant_id


PROGRAM_ID = "summer_2026"
CHANNEL = "wecom_callback"
_VALID_REASON_TYPES = {"reward", "penalty", "exchange", "auction", "correction"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def resolve_student(
    store: TuoguanStore,
    identity: UserIdentity,
    requested_name: str,
) -> tuple[str, dict[str, Any]] | tuple[None, dict[str, Any]]:
    return resolve_active_summer_student(store, identity, requested_name)


def _legacy_totals(balance: dict[str, Any]) -> tuple[int, int]:
    added = int(balance.get("total_added") or 0)
    deducted = int(balance.get("total_deducted") or 0)
    if added or deducted:
        return added, deducted
    for item in balance.get("history") or []:
        if not isinstance(item, dict):
            continue
        delta = int(item.get("delta") or item.get("delta_points") or 0)
        added += max(delta, 0)
        deducted += abs(min(delta, 0))
    return added, deducted


def _compatible_balance_rows(store: TuoguanStore, balances: Any) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    roster = active_summer_students(store)
    rows: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    if not isinstance(balances, dict):
        return rows, [{"reason_code": "invalid_balance_root", "record_key": ""}]
    for record_key, value in balances.items():
        if not isinstance(value, dict):
            warnings.append({"reason_code": "invalid_balance_row", "record_key": str(record_key)})
            continue
        name = str(value.get("student_name") or record_key or "").strip()
        if not name or name not in roster:
            warnings.append({"reason_code": "unknown_legacy_student", "record_key": str(record_key)})
            continue
        profile = roster[name]
        if profile.get("resolution_ambiguous"):
            warnings.append({"reason_code": "ambiguous_legacy_student", "record_key": str(record_key)})
            continue
        current = int(value.get("current_points") if value.get("current_points") is not None else value.get("points") or 0)
        added, deducted = _legacy_totals(value)
        rows.append({
            "student_name": name,
            "student_id": str(value.get("student_id") or profile.get("student_id") or ""),
            "current_points": current,
            "total_added": added,
            "total_deducted": deducted,
            "last_event_id": str(value.get("last_event_id") or ""),
            "updated_at": str(value.get("updated_at") or ""),
            "program_id": PROGRAM_ID,
            "compatibility_source": "standard" if "current_points" in value else "legacy_points_history",
        })
    return rows, warnings


def infer_reason_type(raw_text: str, delta_points: int, provided: str = "") -> str:
    text = str(raw_text or "")
    if "拍卖" in text:
        return "auction"
    if "兑换" in text:
        return "exchange"
    if provided in _VALID_REASON_TYPES:
        return provided
    return "reward" if delta_points > 0 else "penalty"


def extract_reason(raw_text: str, student_name: str, fallback: str = "") -> str:
    text = str(raw_text or "").strip().rstrip("。！？!?；;")
    parts = [part.strip() for part in re.split(r"[，,]", text) if part.strip()]
    action_pattern = re.compile(r"(?:加|奖励|奖|扣|减)\s*\d+\s*分")
    candidates = [part for part in parts if not action_pattern.search(part)]
    if not candidates:
        candidates = [action_pattern.sub("", part).strip() for part in parts]
    reason = max(candidates, key=len, default=str(fallback or "").strip())
    reason = reason.replace(student_name, "", 1).strip()
    reason = re.sub(r"^(?:给)?小?[\u4e00-\u9fff]{1,4}", "", reason).strip() if reason.startswith("给") else reason
    reason = re.sub(r"^今天", "", reason).strip()
    return reason or str(fallback or "积分调整").strip() or "积分调整"


def change_points(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    requested_student_name: str,
    delta_points: int | None,
    raw_text: str,
    reason_text: str = "",
    reason_type: str = "",
    source_message_id: str = "",
    operation_id: str = "",
) -> dict[str, Any]:
    if identity.approval_state != "approved" or identity.role not in {"teacher", "boss"}:
        return {"ok": False, "reason_code": "permission_denied", "message": "当前账号不能操作暑假班积分。"}
    if delta_points is None or int(delta_points) == 0:
        return {
            "ok": False,
            "reason_code": "points_required",
            "message": f"要给{requested_student_name or '这个学生'}加或扣几分？请补充具体分数。",
            "writeback_verified": False,
        }
    delta = int(delta_points)
    text = str(raw_text or "")
    if any(word in text for word in ("扣", "减", "兑换", "拍卖")):
        delta = -abs(delta)
    elif any(word in text for word in ("加", "奖励", "奖")):
        delta = abs(delta)

    resolved_name, profile = resolve_student(store, identity, requested_student_name)
    if not resolved_name:
        code = str(profile.get("reason_code") or "student_not_found")
        if code == "student_name_ambiguous":
            names = "、".join(profile.get("matches") or [])
            message = f"“{requested_student_name}”对应多名学生：{names}。请说完整姓名。"
        else:
            message = f"没有查到{requested_student_name}，可能姓名写错，或者还没有登记到暑假班名单里。"
        return {"ok": False, "reason_code": code, "message": message, "writeback_verified": False}

    balances = store.read_json("summer_points.json", {})
    events = store.read_json("point_events.json", [])
    if not isinstance(balances, dict):
        balances = {}
    if not isinstance(events, list):
        events = []
    old = balances.get(resolved_name, {}) if isinstance(balances.get(resolved_name), dict) else {}
    current = int(old.get("current_points") if old.get("current_points") is not None else old.get("points") or 0)
    old_added, old_deducted = _legacy_totals(old)
    if current + delta < 0 and identity.role != "boss":
        return {
            "ok": False,
            "reason_code": "insufficient_points",
            "message": f"{resolved_name}当前只有{current}分，不能扣成负数。",
            "current_points": current,
            "writeback_verified": False,
        }
    stamp = _now()
    event_id = f"point_{uuid.uuid4().hex[:12]}"
    final_reason_type = infer_reason_type(text, delta, reason_type)
    final_reason = extract_reason(text, requested_student_name, reason_text)
    event = {
        "event_id": event_id,
        "student_name": resolved_name,
        "student_id": str(profile.get("student_id") or ""),
        "delta_points": delta,
        "reason_text": final_reason,
        "reason_type": final_reason_type,
        "operator_user_id": identity.canonical_user_id,
        "operator_name": identity.person_name,
        "operator_role": identity.role,
        "channel": CHANNEL,
        "source_message_id": str(source_message_id or operation_id),
        "operation_id": str(operation_id),
        "created_at": stamp,
        "tenant": current_tenant_id(),
        "tenant_id": current_tenant_id(),
        "program_id": PROGRAM_ID,
        "writeback_verified": True,
    }
    updated = {
        "student_name": resolved_name,
        "student_id": str(profile.get("student_id") or ""),
        "current_points": current + delta,
        "total_added": old_added + max(delta, 0),
        "total_deducted": old_deducted + abs(min(delta, 0)),
        "last_event_id": event_id,
        "updated_at": stamp,
        "program_id": PROGRAM_ID,
    }
    events.append(event)
    balances[resolved_name] = updated
    store.write_json("point_events.json", events)
    store.write_json("summer_points.json", balances)

    persisted_events = store.read_json("point_events.json", [])
    persisted_balances = store.read_json("summer_points.json", {})
    event_verified = any(
        isinstance(item, dict) and str(item.get("event_id") or "") == event_id
        for item in (persisted_events if isinstance(persisted_events, list) else [])
    )
    balance = persisted_balances.get(resolved_name, {}) if isinstance(persisted_balances, dict) else {}
    balance_verified = (
        isinstance(balance, dict)
        and int(balance.get("current_points") or 0) == current + delta
        and str(balance.get("last_event_id") or "") == event_id
    )
    verified = event_verified and balance_verified
    action = "加" if delta > 0 else "扣"
    rendered = (
        f"已给{resolved_name}{action}{abs(delta)}分，原因：{final_reason}。"
        f"当前积分：{current + delta}分。"
    )
    return {
        "ok": verified,
        "action": "change_summer_points",
        "reason_code": "success" if verified else "writeback_consistency_failed",
        "event_id": event_id,
        "event": event,
        "balance": balance,
        "writeback_verified": verified,
        "rendered_text": rendered if verified else "积分写入反查未通过，暂时不能确认成功。",
    }


def query_student_points(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    requested_student_name: str,
    detail_limit: int = 3,
) -> dict[str, Any]:
    if identity.approval_state != "approved" or identity.role not in {"teacher", "boss"}:
        return {"ok": False, "reason_code": "permission_denied", "message": "当前账号不能查询暑假班积分。"}
    resolved_name, profile = resolve_student(store, identity, requested_student_name)
    if not resolved_name:
        return {"ok": False, "reason_code": str(profile.get("reason_code") or "student_not_found"), "message": f"没有查到{requested_student_name}，可能姓名写错，或者还没有登记到暑假班名单里。"}
    balances = store.read_json("summer_points.json", {})
    events = store.read_json("point_events.json", [])
    compatible_rows, compatibility_warning = _compatible_balance_rows(store, balances)
    balance = next((item for item in compatible_rows if item["student_name"] == resolved_name), {})
    current = int(balance.get("current_points") or 0)
    recent = [
        deepcopy(item) for item in (events if isinstance(events, list) else [])
        if isinstance(item, dict) and str(item.get("student_name") or "") == resolved_name
    ][-max(0, min(int(detail_limit or 3), 10)):]
    lines = [f"{resolved_name}当前积分：{current}分。"]
    if recent:
        lines.append(f"最近{len(recent)}次变动：")
        for item in reversed(recent):
            delta = int(item.get("delta_points") or 0)
            lines.append(f"- {'+' if delta > 0 else ''}{delta}分：{item.get('reason_text') or '积分调整'}")
    else:
        lines.append("当前还没有积分变动记录。")
    return {
        "ok": True,
        "action": "query_summer_points",
        "result_count": 1,
        "student_name": resolved_name,
        "current_points": current,
        "recent_events": recent,
        "rendered_text": "\n".join(lines),
        "render_verified": True,
        "compatibility_warning": compatibility_warning,
    }


def query_points_ranking(store: TuoguanStore, *, identity: UserIdentity, limit: int = 10) -> dict[str, Any]:
    if identity.approval_state != "approved" or identity.role not in {"teacher", "boss"}:
        return {"ok": False, "reason_code": "permission_denied", "message": "当前账号不能查询暑假班积分排行榜。"}
    balances = store.read_json("summer_points.json", {})
    rows, compatibility_warning = _compatible_balance_rows(store, balances)
    rows.sort(key=lambda item: (-int(item.get("current_points") or 0), str(item.get("student_name") or "")))
    total = len(rows)
    shown = rows[:max(1, min(int(limit or 10), 50))]
    lines = [f"暑假班积分排行榜前{min(len(shown), int(limit or 10))}名（当前有积分学生共{total}人）："]
    if not shown:
        lines.append("当前还没有积分记录。")
    else:
        lines.extend(
            f"{index}. {item.get('student_name')} {int(item.get('current_points') or 0)}分"
            for index, item in enumerate(shown, 1)
        )
    return {
        "ok": True,
        "action": "query_summer_points_ranking",
        "result_count": total,
        "rendered_count": len(shown),
        "ranking": shown,
        "rendered_text": "\n".join(lines),
        "render_verified": True,
        "compatibility_warning": compatibility_warning,
    }
