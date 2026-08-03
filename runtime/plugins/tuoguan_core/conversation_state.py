"""Per-user pending conversation state for short follow-up replies."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .models import UserIdentity
from .store import TuoguanStore


CONVERSATION_STATE_FILE = "conversation_state.json"

_DEFAULT_TTL_MINUTES = {
    "pending_config_change_confirm": 24 * 60,
    "pending_teacher_handoff_confirm": 24 * 60,
    "pending_student_duplicate_confirm": 24 * 60,
    "pending_student_add_confirm": 24 * 60,
    "pending_unregistered_student_confirm": 24 * 60,
    "pending_weekly_feedback_review": 24 * 60,
    "pending_weekly_feedback_child_review": 24 * 60,
    "pending_graduation_report_review": 24 * 60,
    "pending_task_next_choice": 30,
    "pending_task_close_confirm": 30,
    "pending_safety_closure": 60,
    "pending_general_confirmation": 30,
    "pending_program_record_confirm": 30,
    "pending_staff_config_confirm": 24 * 60,
    "pending_p4_8_account_userid": 30,
    "pending_p4_8_account_config_confirm": 30,
}

_SHORT_FOLLOWUP_REPLIES = {
    "可以",
    "确认",
    "确认执行",
    "关联",
    "新建",
    "创建",
    "跳过",
    "暂不导入",
    "继续",
    "下一个",
    "开始",
    "处理",
    "简短生成",
    "补充",
    "重新生成",
    "修改",
    "取消执行",
    "确认配置",
    "取消配置",
}


def compact_text(text: str) -> str:
    return str(text or "").replace(" ", "").strip()


def remember_conversation_state(
    store: TuoguanStore,
    identity: UserIdentity,
    *,
    state_type: str,
    last_system_prompt: str,
    expected_replies: list[str] | tuple[str, ...],
    payload: dict[str, Any] | None = None,
    source_handler: str = "",
    ttl_minutes: int | None = None,
) -> dict[str, Any]:
    """Write or replace one pending state for this canonical user id."""

    now = datetime.now()
    ttl = ttl_minutes if ttl_minutes is not None else _DEFAULT_TTL_MINUTES.get(state_type, 30)
    state = {
        "user_id": identity.canonical_user_id,
        "role": identity.role,
        "state_type": state_type,
        "last_system_prompt": str(last_system_prompt or ""),
        "expected_replies": [str(item) for item in expected_replies if str(item)],
        "payload": payload or {},
        "created_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(minutes=ttl)).isoformat(timespec="seconds"),
        "source_handler": source_handler,
    }
    states = _load_states(store)
    states[identity.canonical_user_id] = state
    store.write_json(CONVERSATION_STATE_FILE, states)
    return state


def remember_conversation_state_for_user(
    store: TuoguanStore,
    *,
    user_id: str,
    role: str = "",
    state_type: str,
    last_system_prompt: str,
    expected_replies: list[str] | tuple[str, ...],
    payload: dict[str, Any] | None = None,
    source_handler: str = "",
    ttl_minutes: int | None = None,
) -> dict[str, Any]:
    identity = UserIdentity(
        platform="wecom_callback",
        platform_user_id=user_id,
        canonical_user_id=user_id,
        person_name="",
        role=role,
        approval_state="approved",
    )
    return remember_conversation_state(
        store,
        identity,
        state_type=state_type,
        last_system_prompt=last_system_prompt,
        expected_replies=expected_replies,
        payload=payload,
        source_handler=source_handler,
        ttl_minutes=ttl_minutes,
    )


def load_conversation_state(
    store: TuoguanStore,
    identity: UserIdentity,
    *,
    include_expired: bool = False,
) -> dict[str, Any] | None:
    states = _load_states(store)
    state = states.get(identity.canonical_user_id)
    if not isinstance(state, dict):
        return None
    if is_state_expired(state):
        if include_expired:
            return state
        states.pop(identity.canonical_user_id, None)
        store.write_json(CONVERSATION_STATE_FILE, states)
        return None
    return state


def clear_conversation_state(store: TuoguanStore, identity: UserIdentity) -> None:
    states = _load_states(store)
    if identity.canonical_user_id in states:
        states.pop(identity.canonical_user_id, None)
        store.write_json(CONVERSATION_STATE_FILE, states)


def is_state_expired(state: dict[str, Any]) -> bool:
    expires_at = _parse_datetime(state.get("expires_at"))
    return bool(expires_at and expires_at < datetime.now())


def is_short_followup_reply(text: str) -> bool:
    return compact_text(text) in _SHORT_FOLLOWUP_REPLIES


def matches_expected_reply(text: str, state: dict[str, Any]) -> bool:
    compact = compact_text(text)
    expected = {compact_text(item) for item in state.get("expected_replies") or [] if str(item)}
    if compact in expected:
        return True
    aliases = _aliases_for_state(str(state.get("state_type") or ""))
    return compact in aliases


def conversation_state_summary(state: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {}
    payload = state.get("payload") if isinstance(state.get("payload"), dict) else {}
    return {
        "state_type": str(state.get("state_type") or ""),
        "source_handler": str(state.get("source_handler") or ""),
        "expected_replies": [str(item) for item in (state.get("expected_replies") or [])][:8],
        "payload_keys": sorted(str(key) for key in payload.keys())[:12],
        "student_name": str(payload.get("student_name") or payload.get("import_student_name") or ""),
        "task_id": str(payload.get("task_id") or ""),
        "change_id": str(payload.get("change_id") or ""),
        "issue_id": str(payload.get("issue_id") or ""),
        "proposal_id": str(payload.get("proposal_id") or ""),
        "expires_at": str(state.get("expires_at") or ""),
    }


def _aliases_for_state(state_type: str) -> set[str]:
    if state_type == "pending_student_duplicate_confirm":
        return {"关联", "关联为同一个学生", "1", "新建", "创建为新学生", "2", "跳过", "暂不导入", "3"}
    if state_type in {"pending_config_change_confirm", "pending_teacher_handoff_confirm"}:
        return {"确认执行", "确认", "取消执行", "取消"}
    if state_type == "pending_staff_config_confirm":
        return {"确认配置", "确认执行", "确认", "取消配置", "取消执行", "取消"}
    if state_type == "pending_unregistered_student_confirm":
        return {"补录这个孩子", "确认补录", "补录", "暂不补录", "跳过"}
    if state_type in {"pending_weekly_feedback_review", "pending_weekly_feedback_child_review"}:
        return {"确认", "可以", "修改", "跳过", "下一个", "简短生成", "重新生成", "补充"}
    if state_type == "pending_task_next_choice":
        return {"继续", "开始", "处理", "1", "开始下一个", "处理下一个"}
    if state_type == "pending_program_record_confirm":
        return {"暑假班", "记录到暑假班", "托管班", "记录到托管班", "取消"}
    return set()


def _load_states(store: TuoguanStore) -> dict[str, Any]:
    data = store.read_json(CONVERSATION_STATE_FILE, {})
    return data if isinstance(data, dict) else {}


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
