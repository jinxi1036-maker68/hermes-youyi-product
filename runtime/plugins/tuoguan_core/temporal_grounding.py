"""Time grounding helpers for Xiaoyou's model-led work."""

from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Any

from .tasks import current_task_for_user


_CLOSED_STATUSES = {"completed", "cancelled", "closed", "done", "closed_by_admin", "completed_by_admin"}
_WEEKDAYS = "一二三四五六日"
_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_NUMBER = r"\d{1,2}|[零〇一二两三四五六七八九十]{1,3}"


def local_now(now: datetime | None = None) -> datetime:
    if now is not None:
        return now
    return datetime.now().astimezone()


def _compact(text: str) -> str:
    return "".join(str(text or "").split())


def _cn_to_int(value: str) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 0
    if raw.isdigit():
        return int(raw)
    if raw == "十":
        return 10
    if "十" in raw:
        left, _, right = raw.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    total = 0
    for char in raw:
        total = total * 10 + _CN_DIGITS.get(char, 0)
    return total


def _parse_iso_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        try:
            return datetime.fromisoformat(raw.replace(" ", "T"))
        except ValueError:
            return None


def _same_zone_datetime(base: datetime, *, year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=base.tzinfo)


def _default_hour_for_text(text: str, *, level: str = "B") -> int:
    compact = _compact(text)
    if any(term in compact for term in ("凌晨",)):
        return 6
    if any(term in compact for term in ("早上", "早晨", "上午", "一早")):
        return 8
    if "中午" in compact:
        return 12
    if "下午" in compact:
        return 18
    if any(term in compact for term in ("今晚", "晚上", "晚间")):
        return 20
    return 18 if str(level or "B").upper() in {"S", "A"} else 20


def _extract_time(text: str, *, level: str = "B") -> tuple[int, int, bool]:
    compact = _compact(text)
    match = re.search(r"(\d{1,2})[:：](\d{1,2})", compact)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        explicit = True
    else:
        match = re.search(fr"({_NUMBER})(?:点|時|时)(半|({_NUMBER})分?)?", compact)
        if match:
            hour = _cn_to_int(match.group(1))
            minute = 30 if match.group(2) == "半" else _cn_to_int(match.group(3) or "")
            explicit = True
        else:
            hour = _default_hour_for_text(compact, level=level)
            minute = 0
            explicit = False
    afternoonish = any(term in compact for term in ("下午", "晚上", "今晚", "晚间"))
    morningish = any(term in compact for term in ("早上", "早晨", "上午", "凌晨"))
    if explicit and afternoonish and not morningish and 1 <= hour < 12:
        hour += 12
    hour = max(0, min(hour, 23))
    minute = max(0, min(minute, 59))
    return hour, minute, explicit


def parse_business_due_at(
    text: str,
    *,
    now: datetime | None = None,
    level: str = "B",
    allow_default: bool = False,
) -> str:
    """Infer an ISO due time from common Chinese task wording.

    The parser is intentionally conservative: without a time expression it
    returns an empty string unless the caller asks for a default task deadline.
    """

    base = local_now(now)
    compact = _compact(text)
    if not compact:
        return ""

    relative = re.search(fr"({_NUMBER})(分钟|小時|小时|天)后", compact)
    if relative:
        amount = _cn_to_int(relative.group(1))
        unit = relative.group(2)
        if amount > 0:
            if unit == "分钟":
                due = base + timedelta(minutes=amount)
            elif unit in {"小时", "小時"}:
                due = base + timedelta(hours=amount)
            else:
                due = base + timedelta(days=amount)
            return due.isoformat(timespec="seconds")

    has_temporal_word = any(
        term in compact
        for term in ("今天", "今日", "今晚", "明天", "明日", "后天", "後天", "早上", "上午", "中午", "下午", "晚上", "晚间")
    )
    month_day = re.search(fr"({_NUMBER})月({_NUMBER})(?:日|号|號)?", compact)
    explicit_date = bool(month_day)
    hour, minute, explicit_time = _extract_time(compact, level=level)
    if month_day:
        month = _cn_to_int(month_day.group(1))
        day = _cn_to_int(month_day.group(2))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return ""
        due = _same_zone_datetime(base, year=base.year, month=month, day=day, hour=hour, minute=minute)
    elif "后天" in compact or "後天" in compact:
        target = base + timedelta(days=2)
        due = _same_zone_datetime(base, year=target.year, month=target.month, day=target.day, hour=hour, minute=minute)
    elif "明天" in compact or "明日" in compact:
        target = base + timedelta(days=1)
        due = _same_zone_datetime(base, year=target.year, month=target.month, day=target.day, hour=hour, minute=minute)
    elif any(term in compact for term in ("今天", "今日", "今晚")) or explicit_time or has_temporal_word:
        due = _same_zone_datetime(base, year=base.year, month=base.month, day=base.day, hour=hour, minute=minute)
    elif allow_default:
        days = 1 if str(level or "B").upper() in {"S", "A"} else 2
        target = base + timedelta(days=days)
        due = _same_zone_datetime(base, year=target.year, month=target.month, day=target.day, hour=hour, minute=minute)
    else:
        return ""

    if due <= base and not explicit_date:
        due = base + timedelta(hours=2)
    return due.isoformat(timespec="seconds")


def _daypart(now: datetime) -> str:
    hour = now.hour
    if 5 <= hour < 9:
        return "早间"
    if 9 <= hour < 12:
        return "上午"
    if 12 <= hour < 14:
        return "中午"
    if 14 <= hour < 18:
        return "下午"
    if 18 <= hour < 21:
        return "傍晚/晚间工作时段"
    if 21 <= hour < 24:
        return "夜间收尾时段"
    return "凌晨"


def _due_state(due_at: Any, now: datetime) -> str:
    due = _parse_iso_datetime(due_at)
    if due is None:
        return "未设置明确截止时间"
    compare_now = now
    if due.tzinfo is not None and compare_now.tzinfo is None:
        compare_now = compare_now.replace(tzinfo=due.tzinfo)
    if due.tzinfo is None and compare_now.tzinfo is not None:
        due = due.replace(tzinfo=compare_now.tzinfo)
    delta = due - compare_now
    if delta.total_seconds() < 0:
        hours = int(abs(delta.total_seconds()) // 3600)
        return f"已逾期约{hours}小时"
    if delta <= timedelta(hours=2):
        return "2小时内到期"
    if delta <= timedelta(days=1):
        return "24小时内到期"
    return "未到期"


def _is_expired(value: Any, now: datetime) -> bool:
    expires_at = _parse_iso_datetime(value)
    if expires_at is None:
        return False
    compare_now = now
    if expires_at.tzinfo is not None and compare_now.tzinfo is None:
        compare_now = compare_now.replace(tzinfo=expires_at.tzinfo)
    if expires_at.tzinfo is None and compare_now.tzinfo is not None:
        compare_now = compare_now.replace(tzinfo=None)
    return expires_at < compare_now


def _load_tasks(store: Any) -> list[dict[str, Any]]:
    try:
        tasks = store.load_tasks()
    except Exception:
        try:
            tasks = store.read_json("tasks.json", [])
        except Exception:
            tasks = []
    return tasks if isinstance(tasks, list) else []


def _open_tasks_for_user(tasks: list[dict[str, Any]], user_id: str) -> list[dict[str, Any]]:
    return [
        task
        for task in tasks
        if isinstance(task, dict)
        and str(task.get("assignee_userid") or task.get("assignee_user_id") or "") == user_id
        and str(task.get("status") or "") not in _CLOSED_STATUSES
    ]


def _read_context(store: Any, name: str, user_id: str) -> dict[str, Any] | None:
    try:
        data = store.read_json(name, {})
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    item = data.get(user_id)
    return item if isinstance(item, dict) else None


def _task_by_id(tasks: list[dict[str, Any]], task_id: str) -> dict[str, Any] | None:
    return next((task for task in tasks if str(task.get("id") or "") == task_id), None)


def build_temporal_grounding_context(
    store: Any,
    *,
    identity: Any,
    raw_text: str,
    session_id: str = "",
    chat_id: str = "",
    platform: str = "wecom_callback",
    now: datetime | None = None,
) -> str:
    current = local_now(now)
    user_id = str(getattr(identity, "canonical_user_id", "") or "")
    role = str(getattr(identity, "role", "") or "")
    compact = _compact(raw_text)
    taskish = any(term in compact for term in ("任务", "完成", "处理", "进展", "截止", "几号", "几点", "今天", "明天", "今晚", "晚安", "早安"))
    lines = [
        "【小优当前时间锚点】",
        f"当前本地时间：{current.isoformat(timespec='seconds')}",
        f"当前日期：{current:%Y-%m-%d}（星期{_WEEKDAYS[current.weekday()]}），当前时段：{_daypart(current)}。",
        "凡涉及今天、明天、今晚、刚才、任务截止、早安/晚安，必须以本时间锚点为准；不要沿用旧聊天里的日期。",
    ]
    if current.hour < 21:
        lines.append("现在不是睡前收尾时段；不要说“晚安”“安心睡觉”。可用“辛苦了”“你先忙”“我继续跟进”。")
    else:
        lines.append("夜间问候也只能在自然收尾时使用，不能替代任务事实核验。")

    if not user_id:
        return "\n".join(lines)

    tasks = _load_tasks(store)
    open_tasks = _open_tasks_for_user(tasks, user_id)
    active_context = _read_context(store, "active_task_context.json", user_id)
    pending_context = _read_context(store, "pending_next_task_context.json", user_id)
    model_focus = _model_focus_for_turn(
        store,
        user_id=user_id,
        session_id=session_id,
        chat_id=chat_id,
        platform=platform,
    )
    stale_notes: list[str] = []
    context_task: dict[str, Any] | None = None
    if model_focus:
        focus_task_id = str(model_focus.get("task_id") or "")
        focus_task = _task_by_id(open_tasks, focus_task_id)
        expired = _is_expired(model_focus.get("focus_expires_at"), current)
        if focus_task is not None and not expired:
            context_task = focus_task
        elif focus_task_id:
            stale_notes.append(
                f"模型任务焦点已失效：{focus_task_id}，不能继续按它闭环或解释日期。"
            )
    if active_context:
        active_task_id = str(active_context.get("task_id") or "")
        active_task = _task_by_id(open_tasks, active_task_id)
        expired = _is_expired(active_context.get("expires_at"), current)
        if active_task is not None and not expired:
            context_task = active_task
        else:
            stale_notes.append(
                f"历史任务焦点已失效：{active_task_id or '无任务ID'}，不能继续按它闭环或解释日期。"
            )
    if pending_context:
        pending_task_id = str(pending_context.get("task_id") or "")
        pending_task = _task_by_id(open_tasks, pending_task_id)
        expired = _is_expired(pending_context.get("expires_at"), current)
        if pending_task is None or expired:
            stale_notes.append(
                f"历史下一个任务提示已失效：{pending_task_id or '无任务ID'}，不能当作当前任务。"
            )
    current_task = context_task or current_task_for_user(open_tasks, user_id)
    if taskish:
        if current_task:
            title = str(current_task.get("title") or "未命名任务")
            lines.append(
                "当前任务时间事实："
                f"id={current_task.get('id') or ''}；"
                f"状态={current_task.get('status') or ''}；"
                f"创建={current_task.get('created_at') or '未知'}；"
                f"截止={current_task.get('due_at') or '未设置'}（{_due_state(current_task.get('due_at'), current)}）；"
                f"标题={title[:80]}。"
            )
        else:
            lines.append("当前账号没有开放任务；用户说“这个任务完成了/处理了”时，必须先追问具体任务或调用任务查询，不能声称状态已更新、提醒已停止或任务已闭环。")
        if stale_notes:
            lines.append("；".join(stale_notes))
    elif role in {"boss", "manager", "teacher"} and stale_notes:
        lines.append("；".join(stale_notes))
    return "\n".join(lines)


def _model_focus_for_turn(
    store: Any,
    *,
    user_id: str,
    session_id: str,
    chat_id: str,
    platform: str,
) -> dict[str, Any] | None:
    try:
        data = store.read_json("model_focus.json", {})
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    keys = [
        str(session_id or ""),
        f"{platform}:{user_id}",
    ]
    if chat_id:
        keys.append(f"agent:main:{platform}:dm:{chat_id}:{user_id}")
    for key in keys:
        item = data.get(key)
        if isinstance(item, dict):
            return item
    suffix = f":{user_id}"
    for key, item in data.items():
        if isinstance(item, dict) and str(key).endswith(suffix):
            return item
    return None
