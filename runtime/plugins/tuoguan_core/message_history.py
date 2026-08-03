"""User- and conversation-scoped message ledger visible to semantic routing."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from .conversation_state import conversation_state_summary
from .models import UserIdentity
from .store import TuoguanStore


MESSAGE_HISTORY_FILE = "message_history.jsonl"
MESSAGE_HISTORY_INDEX_FILE = "message_history_idempotency.json"
MAX_INDEX_ENTRIES = 5000


def conversation_id_for(
    platform: str,
    user_id: str,
    chat_id: str = "",
    chat_type: str = "dm",
) -> str:
    platform_key = str(platform or "wecom_callback").lower()
    kind = str(chat_type or "dm").lower()
    target = str(chat_id or user_id or "unknown").strip()
    return f"{platform_key}:{kind}:{target}"


def record_inbound_message(
    store: TuoguanStore,
    identity: UserIdentity,
    *,
    text: str,
    conversation_id: str,
    message_id: str = "",
) -> tuple[dict[str, Any], bool]:
    key = f"in:{conversation_id}:{message_id}" if message_id else ""
    return append_message(
        store,
        conversation_id=conversation_id,
        user_id=identity.platform_user_id,
        canonical_user_id=identity.canonical_user_id,
        role=identity.role,
        direction="inbound",
        message_role="user",
        source="user",
        message_text=text,
        message_id=message_id,
        idempotency_key=key,
    )


def record_outbound_message(
    store: TuoguanStore,
    *,
    platform: str,
    recipient_user_id: str,
    conversation_id: str,
    message_text: str,
    source: str,
    message_id: str = "",
    idempotency_key: str = "",
    related_state_type: str = "",
    related_task_id: str = "",
    related_config_id: str = "",
    related_program_id: str = "",
) -> tuple[dict[str, Any], bool]:
    identity = _resolve_outbound_identity(store, platform, recipient_user_id)
    state = _state_for_user(store, identity.canonical_user_id)
    state_summary = conversation_state_summary(state)
    payload = state.get("payload") if isinstance(state, dict) and isinstance(state.get("payload"), dict) else {}
    state_type = related_state_type or str(state_summary.get("state_type") or "")
    actual_source = _specific_source(source, state_type, related_task_id)
    return append_message(
        store,
        conversation_id=conversation_id,
        user_id=recipient_user_id,
        canonical_user_id=identity.canonical_user_id,
        role=identity.role,
        direction="outbound",
        message_role="assistant",
        source=actual_source,
        message_text=message_text,
        message_id=message_id,
        idempotency_key=idempotency_key,
        related_state_type=state_type,
        related_task_id=related_task_id or str(payload.get("task_id") or ""),
        related_config_id=related_config_id or str(payload.get("change_id") or payload.get("proposal_id") or ""),
        related_program_id=related_program_id or str(payload.get("program_id") or ""),
    )


def append_message(
    store: TuoguanStore,
    *,
    conversation_id: str,
    user_id: str,
    canonical_user_id: str,
    role: str,
    direction: str,
    message_role: str,
    source: str,
    message_text: str,
    message_id: str = "",
    idempotency_key: str = "",
    related_state_type: str = "",
    related_task_id: str = "",
    related_config_id: str = "",
    related_program_id: str = "",
) -> tuple[dict[str, Any], bool]:
    now = datetime.now().isoformat(timespec="seconds")
    key = str(idempotency_key or "").strip()
    if key:
        index = store.read_json(MESSAGE_HISTORY_INDEX_FILE, {})
        index = index if isinstance(index, dict) else {}
        existing_id = str(index.get(key) or "")
        if existing_id:
            return {"id": existing_id, "idempotency_key": key}, False
    else:
        index = {}
    entry = {
        "id": f"msg_{uuid.uuid4().hex}",
        "conversation_id": str(conversation_id),
        "user_id": str(user_id),
        "canonical_user_id": str(canonical_user_id),
        "role": str(role),
        "direction": str(direction),
        "message_role": str(message_role),
        "source": str(source),
        "message_text": str(message_text or ""),
        "message_id": str(message_id or ""),
        "idempotency_key": key,
        "related_state_type": str(related_state_type or ""),
        "related_task_id": str(related_task_id or ""),
        "related_config_id": str(related_config_id or ""),
        "related_program_id": str(related_program_id or ""),
        "created_at": now,
        "visible_to_model": not (str(direction) == "outbound" and str(source) in {"system_push", "deterministic_fallback"}),
    }
    path = store.path_for(MESSAGE_HISTORY_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with store._lock:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if key:
        index[key] = entry["id"]
        if len(index) > MAX_INDEX_ENTRIES:
            index = dict(list(index.items())[-MAX_INDEX_ENTRIES:])
        store.write_json(MESSAGE_HISTORY_INDEX_FILE, index)
    return entry, True


def recent_message_history(
    store: TuoguanStore,
    *,
    canonical_user_id: str,
    conversation_id: str,
    limit: int = 12,
) -> list[dict[str, Any]]:
    path = store.path_for(MESSAGE_HISTORY_FILE)
    if not path.exists():
        return []
    matched: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict) or not item.get("visible_to_model"):
                    continue
                if str(item.get("canonical_user_id") or "") != str(canonical_user_id):
                    continue
                if str(item.get("conversation_id") or "") != str(conversation_id):
                    continue
                matched.append(item)
    except OSError:
        return []
    return matched[-max(1, int(limit)) :]


def history_summary(items: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    return [
        {
            "message_role": str(item.get("message_role") or ""),
            "direction": str(item.get("direction") or ""),
            "source": str(item.get("source") or ""),
            "message_text": str(item.get("message_text") or "")[:500],
            "related_state_type": str(item.get("related_state_type") or ""),
            "related_task_id": str(item.get("related_task_id") or ""),
            "related_config_id": str(item.get("related_config_id") or ""),
            "related_program_id": str(item.get("related_program_id") or ""),
            "created_at": str(item.get("created_at") or ""),
        }
        for item in items[-max(1, int(limit)) :]
    ]


def content_fingerprint(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:20]


def _state_for_user(store: TuoguanStore, user_id: str) -> dict[str, Any] | None:
    states = store.read_json("conversation_state.json", {})
    if not isinstance(states, dict):
        return None
    state = states.get(user_id)
    return state if isinstance(state, dict) else None


def _resolve_outbound_identity(store: TuoguanStore, platform: str, user_id: str) -> UserIdentity:
    whitelist = store.read_json("wecom_whitelist.json", {})
    whitelist = whitelist if isinstance(whitelist, dict) else {}
    roles = whitelist.get("user_roles") if isinstance(whitelist.get("user_roles"), dict) else {}
    role = str(roles.get(user_id) or "")
    if user_id in {str(item) for item in whitelist.get("super_users") or []}:
        role = "boss"
    elif user_id in {str(item) for item in whitelist.get("summer_manager_ids") or []}:
        role = "manager"
    elif not role:
        role = "unknown"
    mapping = store.read_json("teacher_wecom_map.json", {})
    mapping = mapping if isinstance(mapping, dict) else {}
    name = next((str(key) for key, value in mapping.items() if str(value) == str(user_id)), "")
    return UserIdentity(
        platform=str(platform or "wecom_callback"),
        platform_user_id=str(user_id),
        canonical_user_id=str(user_id),
        person_name=name,
        role=role,
        approval_state="approved" if role != "unknown" else "pending",
    )


def _specific_source(source: str, state_type: str, task_id: str) -> str:
    if source not in {"deterministic_fallback", "system_push"}:
        return source
    if state_type == "pending_staff_config_confirm":
        return "staff_config"
    if state_type in {"pending_student_duplicate_confirm", "pending_unregistered_student_confirm"}:
        return "summer_import"
    if state_type in {"pending_safety_closure", "pending_task_close_confirm"}:
        return "safety_handler"
    if task_id:
        return "task_handler" if source == "deterministic_fallback" else "system_push"
    return source
