"""Daily dashboard push helpers for the tutoring-center module."""

from __future__ import annotations

from datetime import datetime
import os
from typing import Any
from urllib.parse import quote

from .dashboard_auth import DashboardAuthError, sign_dashboard_token, token_expiry_datetime
from .dashboard_builder import refresh_dashboard_cache
from .models import UserIdentity
from .store import TuoguanStore
from .summer_course_coverage import teacher_recording_reminder


STATE_FILE = "daily_push_state.json"
CONFIG_FILE = "daily_push_config.json"
DEFAULT_PUSH_TIME = "10:00"
SUMMER_REMINDER_STATE_FILE = "summer_recording_reminder_state.json"


def _push_time() -> tuple[int, int]:
    raw = str(os.getenv("HERMES_TUOGUAN_DAILY_PUSH_TIME") or DEFAULT_PUSH_TIME).strip()
    try:
        hour, minute = raw.split(":", 1)
        return max(0, min(23, int(hour))), max(0, min(59, int(minute)))
    except Exception:
        return 10, 0


def _push_enabled() -> bool:
    raw = str(os.getenv("HERMES_TUOGUAN_DAILY_PUSH_ENABLED") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off", "disabled"}


def _summer_reminder_enabled() -> bool:
    raw = str(os.getenv("HERMES_SUMMER_RECORDING_REMINDER_ENABLED") or "0").strip().lower()
    return raw not in {"0", "false", "no", "off", "disabled"}


def _summer_reminder_time() -> tuple[int, int]:
    raw = str(os.getenv("HERMES_SUMMER_RECORDING_REMINDER_TIME") or "08:00").strip()
    try:
        hour, minute = raw.split(":", 1)
        return max(0, min(23, int(hour))), max(0, min(59, int(minute)))
    except Exception:
        return 8, 0


def _base_url() -> str:
    return str(os.getenv("HERMES_TUOGUAN_DASHBOARD_BASE_URL") or "").strip()


def _display_names(store: TuoguanStore) -> dict[str, str]:
    mapping = store.read_json("teacher_wecom_map.json", {})
    if not isinstance(mapping, dict):
        return {}
    result: dict[str, str] = {}
    for name, user_id in mapping.items():
        uid = str(user_id).strip()
        label = str(name).strip()
        if uid and label and uid not in result:
            result[uid] = label
    return result


def _staff_profiles(store: TuoguanStore) -> dict[str, dict[str, Any]]:
    staff = store.read_json("staff.json", {})
    return staff if isinstance(staff, dict) else {}


def _push_config(store: TuoguanStore) -> dict[str, Any]:
    config = store.read_json(CONFIG_FILE, {})
    return config if isinstance(config, dict) else {}


def _is_test_user(user_id: str, name: str, profile: dict[str, Any]) -> bool:
    text = " ".join(
        str(item or "")
        for item in (
            user_id,
            name,
            profile.get("name"),
            profile.get("role"),
            profile.get("status"),
            profile.get("staff_status"),
            profile.get("tag"),
        )
    ).lower()
    if bool(profile.get("is_test") or profile.get("test_account")):
        return True
    return any(marker in text for marker in ("测试", "test", "ceshi", "demo", "sandbox"))


def dashboard_push_identities(store: TuoguanStore) -> list[UserIdentity]:
    whitelist = store.read_json("wecom_whitelist.json", {})
    if not isinstance(whitelist, dict):
        return []
    roles = {
        str(user_id): str(role)
        for user_id, role in (whitelist.get("user_roles") or {}).items()
        if str(user_id).strip()
    }
    for user_id in whitelist.get("super_users") or []:
        roles.setdefault(str(user_id), "boss")
    for user_id in whitelist.get("allowed_users") or []:
        roles.setdefault(str(user_id), "teacher")
    names = _display_names(store)
    staff = _staff_profiles(store)
    config = _push_config(store)
    include_user_ids = {str(item).strip() for item in config.get("include_user_ids") or [] if str(item).strip()}
    exclude_user_ids = {str(item).strip() for item in config.get("exclude_user_ids") or [] if str(item).strip()}
    identities: list[UserIdentity] = []
    for user_id in include_user_ids:
        roles.setdefault(user_id, str(config.get("include_default_role") or "teacher"))
    for user_id, role in sorted(roles.items()):
        if role not in {"teacher", "manager", "boss"}:
            continue
        if user_id in exclude_user_ids:
            continue
        name = names.get(user_id, user_id)
        profile = staff.get(user_id) if isinstance(staff.get(user_id), dict) else {}
        if user_id not in include_user_ids and _is_test_user(user_id, name, profile):
            continue
        identities.append(
            UserIdentity(
                platform="wecom_callback",
                platform_user_id=user_id,
                canonical_user_id=user_id,
                person_name=name,
                role=role,
                approval_state="approved",
            )
        )
    return identities


def _already_sent_today(store: TuoguanStore, now: datetime) -> bool:
    state = store.read_json(STATE_FILE, {})
    if not isinstance(state, dict):
        return False
    return str(state.get("last_sent_date") or "") == now.date().isoformat()


def _mark_sent(store: TuoguanStore, now: datetime, *, sent: int, failed: int) -> None:
    store.write_json(
        STATE_FILE,
        {
            "last_sent_date": now.date().isoformat(),
            "last_sent_at": now.isoformat(timespec="seconds"),
            "sent_count": sent,
            "failed_count": failed,
        },
    )


def daily_push_due(store: TuoguanStore, now: datetime | None = None) -> bool:
    if not _push_enabled() or not _base_url():
        return False
    timestamp = now or datetime.now()
    hour, minute = _push_time()
    if (timestamp.hour, timestamp.minute) < (hour, minute):
        return False
    return not _already_sent_today(store, timestamp)


def dashboard_push_message(identity: UserIdentity, store: TuoguanStore) -> str:
    token = sign_dashboard_token(identity, store)
    url = f"{_base_url().rstrip('/')}/tuoguan/dashboard?token={quote(token, safe='')}"
    role_label = {"teacher": "老师", "manager": "店长", "boss": "老板"}.get(identity.role, "用户")
    expiry = token_expiry_datetime().strftime("%Y-%m-%d %H:%M")
    if identity.role == "teacher":
        profile = _staff_profiles(store).get(identity.canonical_user_id) or {}
        if "summer_2026" in {str(item) for item in profile.get("program_ids") or []}:
            reminder = teacher_recording_reminder(
                store,
                teacher_userid=identity.canonical_user_id,
                teacher_name=identity.person_name,
            )
            lead = reminder.get("message") or "今天按课程表完成整体记录，并点名2-3名有明显表现的孩子。"
        else:
            lead = "今天上班先看这3件事：缺记录、待闭环、家长沟通。"
    elif identity.role == "manager":
        lead = "今天先看团队要推动的事：风险闭环、老师缺项、续费沟通。"
    else:
        lead = "今天先看经营重点：风险、工资、续费和数据问题。"
    return (
        f"【Hermes {role_label}今日任务】\n"
        f"{lead}\n"
        f"{url}\n\n"
        f"链接有效期至 {expiry}。看板只读，处理记录和任务仍回企业微信直接说。"
    )


def summer_recording_reminder_due(store: TuoguanStore, now: datetime | None = None) -> bool:
    if not _summer_reminder_enabled():
        return False
    timestamp = now or datetime.now()
    if timestamp.weekday() not in {2, 3, 4, 5, 6}:
        return False
    hour, minute = _summer_reminder_time()
    if (timestamp.hour, timestamp.minute) < (hour, minute):
        return False
    state = store.read_json(SUMMER_REMINDER_STATE_FILE, {})
    return not isinstance(state, dict) or str(state.get("last_sent_date") or "") != timestamp.date().isoformat()


async def run_due_summer_recording_reminders(
    *,
    adapter: Any,
    store: TuoguanStore | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    actual_store = store or TuoguanStore()
    timestamp = now or datetime.now()
    if not summer_recording_reminder_due(actual_store, timestamp):
        return {"sent": 0, "failed": 0, "skipped": True}
    sent = 0
    failed = 0
    for identity in dashboard_push_identities(actual_store):
        if identity.role != "teacher":
            continue
        profile = _staff_profiles(actual_store).get(identity.canonical_user_id) or {}
        if "summer_2026" not in {str(item) for item in profile.get("program_ids") or []}:
            continue
        reminder = teacher_recording_reminder(
            actual_store,
            teacher_userid=identity.canonical_user_id,
            teacher_name=identity.person_name,
            now=timestamp,
        )
        if not reminder.get("due"):
            continue
        try:
            result = await adapter.send(
                identity.platform_user_id,
                str(reminder.get("message") or ""),
                metadata={
                    "handled_by": "tuoguan_core",
                    "outbound_source": "system_push",
                    "summer_recording_reminder": True,
                    "program_id": "summer_2026",
                    "idempotency_key": f"summer-recording-reminder:{identity.platform_user_id}:{timestamp.date().isoformat()}",
                },
            )
        except Exception:
            failed += 1
            continue
        if bool(getattr(result, "success", False)):
            sent += 1
        else:
            failed += 1
    actual_store.write_json(
        SUMMER_REMINDER_STATE_FILE,
        {
            "last_sent_date": timestamp.date().isoformat(),
            "last_sent_at": timestamp.isoformat(timespec="seconds"),
            "sent_count": sent,
            "failed_count": failed,
        },
    )
    return {"sent": sent, "failed": failed, "skipped": False}


async def run_due_daily_dashboard_push(
    *,
    adapter: Any,
    store: TuoguanStore | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    actual_store = store or TuoguanStore()
    timestamp = now or datetime.now()
    if not daily_push_due(actual_store, timestamp):
        return {"sent": 0, "failed": 0, "skipped": True}
    try:
        refresh_dashboard_cache(actual_store, now=timestamp)
    except Exception:
        pass
    sent = 0
    failed = 0
    for identity in dashboard_push_identities(actual_store):
        try:
            content = dashboard_push_message(identity, actual_store)
        except DashboardAuthError:
            failed += 1
            continue
        try:
            result = await adapter.send(
                identity.platform_user_id,
                content,
                metadata={
                    "handled_by": "tuoguan_core",
                    "daily_dashboard_push": True,
                    "outbound_source": "system_push",
                    "idempotency_key": f"daily-dashboard:{identity.platform_user_id}:{timestamp.date().isoformat()}",
                },
            )
        except Exception:
            failed += 1
            continue
        if bool(getattr(result, "success", False)):
            sent += 1
        else:
            failed += 1
    _mark_sent(actual_store, timestamp, sent=sent, failed=failed)
    return {"sent": sent, "failed": failed, "skipped": False}
