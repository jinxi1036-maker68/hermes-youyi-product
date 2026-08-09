"""Boss-visible staff conversation activity built from Xiaoyou reply ledgers."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
import json
from typing import Any

from .models import UserIdentity
from .store import TuoguanStore
from .tenant_context import current_tenant_id


REPLY_LEDGER_FILE = "reply_ledger.jsonl"
STAFF_ROLES = {"teacher", "manager"}


def query_staff_conversation_activity(
    store: TuoguanStore,
    *,
    identity: UserIdentity,
    period: str = "today",
    since_hours: int = 24,
    include_latest_excerpt: bool = True,
    now_at: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    """Return a read-only activity index for staff conversations with Xiaoyou.

    This is a management activity view, not a raw transcript export. It lets the
    boss verify whether staff spoke with Xiaoyou and when, while keeping the
    model from guessing across isolated personal sessions.
    """

    if identity.role != "boss" and identity.platform != "system":
        return {"ok": False, "error": "permission_denied", "message": "只有老板或系统巡检可以查看员工对话活动。"}

    safe_limit = max(1, min(int(limit or 20), 100))
    now_value = _aware_time(_parse_time(now_at) or datetime.now().astimezone())
    window = _time_window(now_value, period=period, since_hours=since_hours)
    staff_index = _staff_index(store)
    grouped: dict[str, dict[str, Any]] = {}

    for row in _read_jsonl(store, REPLY_LEDGER_FILE):
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or "").strip()
        if role not in STAFF_ROLES:
            continue
        user_id = str(row.get("user_id") or row.get("canonical_user_id") or "").strip()
        if not user_id:
            continue
        occurred_at = _aware_time(_parse_time(row.get("created_at") or row.get("completed_at")))
        if occurred_at is None or occurred_at < window["start"] or occurred_at > window["end"]:
            continue
        item = grouped.setdefault(
            user_id,
            {
                "user_id": user_id,
                "role": role,
                "role_label": _role_label(role),
                "name": _display_name(user_id, role, staff_index),
                "message_count": 0,
                "first_at": occurred_at.isoformat(timespec="seconds"),
                "last_at": occurred_at.isoformat(timespec="seconds"),
                "latest_text": "",
                "latest_reply": "",
                "latest_message_id": "",
            },
        )
        item["message_count"] = int(item.get("message_count") or 0) + 1
        if occurred_at.isoformat(timespec="seconds") < str(item.get("first_at") or ""):
            item["first_at"] = occurred_at.isoformat(timespec="seconds")
        if occurred_at.isoformat(timespec="seconds") >= str(item.get("last_at") or ""):
            item["last_at"] = occurred_at.isoformat(timespec="seconds")
            item["latest_text"] = _limit_text(row.get("raw_text"), 160) if include_latest_excerpt else ""
            item["latest_reply"] = _limit_text(row.get("final_reply"), 160) if include_latest_excerpt else ""
            item["latest_message_id"] = str(row.get("message_id") or row.get("source_message_id") or "")
            item["role"] = role
            item["role_label"] = _role_label(role)
            item["name"] = _display_name(user_id, role, staff_index)

    conversations = sorted(
        grouped.values(),
        key=lambda item: (str(item.get("last_at") or ""), str(item.get("name") or "")),
        reverse=True,
    )[:safe_limit]
    role_counts: dict[str, int] = {"teacher": 0, "manager": 0}
    total_message_count = 0
    for item in conversations:
        role = str(item.get("role") or "")
        role_counts[role] = role_counts.get(role, 0) + 1
        total_message_count += int(item.get("message_count") or 0)

    rendered_text = _render_activity(conversations, window=window, role_counts=role_counts, total_message_count=total_message_count)
    return {
        "ok": True,
        "report_type": "staff_conversation_activity_v1",
        "tenant_id": current_tenant_id(),
        "read_only": True,
        "period": window["period"],
        "start_at": window["start"].isoformat(timespec="seconds"),
        "end_at": window["end"].isoformat(timespec="seconds"),
        "staff_contact_count": len(conversations),
        "total_message_count": total_message_count,
        "role_counts": role_counts,
        "conversations": conversations,
        "rendered_text": rendered_text,
        "render_verified": True,
        "writeback_verified": None,
        "boundary": {
            "boss_only_query": True,
            "raw_transcript_export": False,
            "does_not_change_performance": True,
            "does_not_change_salary": True,
            "does_not_create_tasks": True,
            "does_not_contact_parents": True,
            "model_decides_next_action": True,
        },
    }


def _read_jsonl(store: TuoguanStore, filename: str) -> list[dict[str, Any]]:
    path = store.path_for(filename)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19] if "%H" in fmt else text[:10], fmt)
        except ValueError:
            continue
    return None


def _aware_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.astimezone()
    return value.astimezone()


def _time_window(now_value: datetime, *, period: str, since_hours: int) -> dict[str, Any]:
    normalized_period = str(period or "today").strip().lower()
    if normalized_period in {"last_24h", "24h", "recent"}:
        hours = max(1, min(int(since_hours or 24), 24 * 90))
        return {"period": "last_24h", "start": now_value - timedelta(hours=hours), "end": now_value}
    if normalized_period == "yesterday":
        today_start = now_value.replace(hour=0, minute=0, second=0, microsecond=0)
        return {"period": "yesterday", "start": today_start - timedelta(days=1), "end": today_start}
    return {"period": "today", "start": now_value.replace(hour=0, minute=0, second=0, microsecond=0), "end": now_value}


def _staff_index(store: TuoguanStore) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = defaultdict(dict)
    staff = _dict(store.read_json("staff.json", {}))
    for user_id, profile in staff.items():
        if not isinstance(profile, dict):
            continue
        index[str(user_id)].update(
            {
                "staff_name": str(profile.get("name") or ""),
                "staff_role": str(profile.get("role") or ""),
            }
        )
    mapping = _dict(store.read_json("teacher_wecom_map.json", {}))
    for alias, user_id in mapping.items():
        bucket = index[str(user_id)]
        aliases = list(bucket.get("aliases") or [])
        if str(alias) and str(alias) not in aliases:
            aliases.append(str(alias))
        bucket["aliases"] = aliases
    directory = _dict(store.read_json("wecom_directory_cache.json", {}))
    for member in directory.get("members") or []:
        if not isinstance(member, dict):
            continue
        user_id = str(member.get("user_id") or member.get("userid") or "").strip()
        if not user_id:
            continue
        index[user_id].update({"directory_name": str(member.get("name") or ""), "directory_member": deepcopy(member)})
    whitelist = _dict(store.read_json("wecom_whitelist.json", {}))
    roles = _dict(whitelist.get("user_roles"))
    for user_id, role in roles.items():
        index[str(user_id)].setdefault("staff_role", str(role or ""))
    return dict(index)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _display_name(user_id: str, role: str, staff_index: dict[str, dict[str, Any]]) -> str:
    item = staff_index.get(str(user_id)) or {}
    aliases = [str(alias) for alias in item.get("aliases") or [] if str(alias).strip()]
    return (
        str(item.get("staff_name") or "").strip()
        or (aliases[0] if aliases else "")
        or str(item.get("directory_name") or "").strip()
        or _role_label(role)
        or str(user_id)
    )


def _role_label(role: str) -> str:
    return {"teacher": "老师", "manager": "店长", "boss": "老板"}.get(str(role or ""), str(role or "员工"))


def _limit_text(value: Any, max_len: int = 160) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - 1)] + "…"


def _render_activity(
    conversations: list[dict[str, Any]],
    *,
    window: dict[str, Any],
    role_counts: dict[str, int],
    total_message_count: int,
) -> str:
    label = "今天" if window.get("period") == "today" else ("昨天" if window.get("period") == "yesterday" else "最近")
    if not conversations:
        return f"{label}没有查到老师或店长和小优的对话活动记录。"
    lines = [
        f"{label}有 {len(conversations)} 位老师/店长和小优对话，共 {total_message_count} 轮；老师 {role_counts.get('teacher', 0)} 位，店长 {role_counts.get('manager', 0)} 位。",
    ]
    for item in conversations[:5]:
        who = str(item.get("name") or item.get("user_id") or "员工")
        latest = str(item.get("latest_text") or "").strip()
        suffix = f"；最近一句：{latest}" if latest else ""
        lines.append(f"- {who}（{item.get('role_label')}）：{item.get('message_count')} 轮，最近 {item.get('last_at')}{suffix}")
    lines.append("这是老板侧活动摘要，不等于绩效结论，也不会自动改任务、工资、制度或触达家长。")
    return "\n".join(lines)
