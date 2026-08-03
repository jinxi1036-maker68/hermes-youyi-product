"""Manual business focus overrides for periodic operations."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .models import UserIdentity
from .store import TuoguanStore


FOCUS_FILE = "operations_focus.json"
_SET_WORDS = ("经营重点", "运营重点", "本月重点", "本周重点", "重点抓", "先抓")
_DISABLE_WORDS = ("不要推", "先不要", "暂停", "不推")
_KEYWORD_ALIASES = {
    "暑假": "暑假招生",
    "暑假招生": "暑假招生",
    "招生": "招生",
    "续费": "续费",
    "暑假续费": "暑假续费",
    "家长满意": "家长满意度",
    "家长满意度": "家长满意度",
    "服务": "服务质量",
    "服务质量": "服务质量",
    "安全": "安全",
    "开学": "开学稳定",
    "新生": "新生稳定",
    "记录": "记录质量",
    "家长沟通": "家长沟通",
}


def classify_operations_focus(text: str, identity: UserIdentity) -> dict[str, Any] | None:
    compact = str(text or "").replace(" ", "")
    if identity.role != "boss":
        return None
    if not looks_like_operations_focus(text):
        return None
    scope = "week" if "本周" in compact or "这周" in compact or "这两周" in compact else "month"
    disable_keywords = _extract_keywords(compact) if any(word in compact for word in _DISABLE_WORDS) else []
    keywords = [] if disable_keywords else _extract_keywords(compact)
    if not keywords and not disable_keywords:
        return None
    return {
        "scope": scope,
        "keywords": keywords,
        "disabled_keywords": disable_keywords,
        "source_text": str(text or "").strip(),
    }


def looks_like_operations_focus(text: str) -> bool:
    compact = str(text or "").replace(" ", "")
    if not any(word in compact for word in _SET_WORDS + _DISABLE_WORDS):
        return False
    return any(word in compact for word in ("重点", "招生", "续费", "服务", "家长", "安全", "开学", "新生"))


def save_operations_focus(
    store: TuoguanStore,
    draft: dict[str, Any],
    identity: UserIdentity,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = now or datetime.now()
    scope = str(draft.get("scope") or "month")
    starts_at, ends_at = _scope_window(timestamp, scope)
    items = _load_items(store)
    item = {
        "id": f"focus_{timestamp.strftime('%Y%m%d%H%M%S')}",
        "scope": scope,
        "keywords": list(draft.get("keywords") or []),
        "disabled_keywords": list(draft.get("disabled_keywords") or []),
        "source_text": str(draft.get("source_text") or ""),
        "created_by": identity.canonical_user_id,
        "created_by_name": identity.person_name or identity.platform_user_id,
        "created_at": timestamp.isoformat(timespec="seconds"),
        "starts_at": starts_at.isoformat(timespec="seconds"),
        "ends_at": ends_at.isoformat(timespec="seconds"),
        "status": "active",
    }
    items.append(item)
    store.write_json(FOCUS_FILE, items[-100:])
    return item


def active_operations_focus(store: TuoguanStore, *, now: datetime | None = None) -> dict[str, Any]:
    timestamp = now or datetime.now()
    active = []
    for item in _load_items(store):
        if str(item.get("status") or "active") != "active":
            continue
        start = _parse_dt(item.get("starts_at")) or datetime.min
        end = _parse_dt(item.get("ends_at")) or datetime.max
        if start <= timestamp <= end:
            active.append(item)
    active.sort(key=lambda item: (1 if item.get("scope") == "week" else 0, str(item.get("created_at") or "")), reverse=True)
    keywords: list[str] = []
    disabled: list[str] = []
    latest = active[0] if active else {}
    for item in active:
        for keyword in item.get("keywords") or []:
            if keyword not in keywords:
                keywords.append(str(keyword))
        for keyword in item.get("disabled_keywords") or []:
            if keyword not in disabled:
                disabled.append(str(keyword))
    return {
        "active": bool(active),
        "keywords": keywords,
        "disabled_keywords": disabled,
        "latest": latest,
        "items": active[:10],
        "summary": _summary(keywords, disabled),
    }


def operations_focus_reply(item: dict[str, Any]) -> str:
    scope_label = "本周" if item.get("scope") == "week" else "本月"
    keywords = "、".join(item.get("keywords") or [])
    disabled = "、".join(item.get("disabled_keywords") or [])
    if disabled:
        main = f"已设置{scope_label}先不推：{disabled}。"
    else:
        main = f"已设置{scope_label}经营重点：{keywords}。"
    return (
        f"{main}\n"
        "它会先影响老板/店长看板的经营重点和今日事项排序；不会直接大量生成老师任务。"
    )


def _extract_keywords(text: str) -> list[str]:
    found: list[str] = []
    for raw, normalized in _KEYWORD_ALIASES.items():
        if raw in text and normalized not in found:
            found.append(normalized)
    return found[:6]


def _scope_window(now: datetime, scope: str) -> tuple[datetime, datetime]:
    if scope == "week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=6, hours=23, minutes=59, seconds=59)
        return start, end
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now.month == 12:
        next_month = now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        next_month = now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, next_month - timedelta(seconds=1)


def _load_items(store: TuoguanStore) -> list[dict[str, Any]]:
    raw = store.read_json(FOCUS_FILE, [])
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _summary(keywords: list[str], disabled: list[str]) -> str:
    if keywords and disabled:
        return f"当前重点：{'、'.join(keywords)}；暂不推：{'、'.join(disabled)}"
    if keywords:
        return f"当前重点：{'、'.join(keywords)}"
    if disabled:
        return f"当前暂不推：{'、'.join(disabled)}"
    return "当前使用系统默认经营节奏"
