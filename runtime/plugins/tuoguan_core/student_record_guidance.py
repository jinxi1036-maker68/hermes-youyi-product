"""Production-canary Student Record Coach with a non-business draft context."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import threading
from typing import Any, Callable

from .ai_coach_foundation import LLMUnderstandingAdapter, analyze_record_v2
from .models import UserIdentity
from .store import TuoguanStore
from .student_resolver import all_student_names, resolve_student_for_record
from .tenant_context import current_tenant_id


CONFIG_FILE = "ai_coach_feature_flags.json"
DRAFT_FILE = "student_record_drafts.json"
EVENT_FILE = "ai_coach_guidance_events.jsonl"
_LOCK = threading.RLock()
_ADAPTER: LLMUnderstandingAdapter | None = None


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="seconds")


def _read_config(store: TuoguanStore) -> dict[str, Any]:
    payload = store.read_json(CONFIG_FILE, {})
    section = (payload.get("ai_coach") or {}).get("student_record_guidance") if isinstance(payload, dict) else None
    return section if isinstance(section, dict) else {}


def canary_enabled(store: TuoguanStore, identity: UserIdentity) -> bool:
    config = _read_config(store)
    return bool(
        config.get("enabled") is True
        and config.get("rollout_mode") == "canary"
        and identity.role == "teacher"
        and identity.canonical_user_id in set(map(str, config.get("canary_actor_ids") or []))
    )


def _drafts(store: TuoguanStore) -> dict[str, Any]:
    payload = store.read_json(DRAFT_FILE, {})
    return payload if isinstance(payload, dict) else {}


def _key(identity: UserIdentity) -> str:
    return f"{current_tenant_id()}:{identity.canonical_user_id}"


def get_draft(store: TuoguanStore, identity: UserIdentity) -> tuple[dict[str, Any] | None, bool]:
    row = _drafts(store).get(_key(identity))
    if not isinstance(row, dict):
        return None, False
    try:
        expired = datetime.fromisoformat(str(row.get("expires_at") or "")) <= _now()
    except ValueError:
        expired = True
    return (None, True) if expired else (deepcopy(row), False)


def _write_draft(store: TuoguanStore, identity: UserIdentity, row: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        payload = _drafts(store)
        current = payload.get(_key(identity))
        version = int(current.get("version") or 0) + 1 if isinstance(current, dict) else 1
        row = {**deepcopy(row), "version": version}
        payload[_key(identity)] = row
        store.write_json(DRAFT_FILE, payload)
        return deepcopy(row)


def clear_draft(store: TuoguanStore, identity: UserIdentity) -> None:
    with _LOCK:
        payload = _drafts(store)
        if payload.pop(_key(identity), None) is not None:
            store.write_json(DRAFT_FILE, payload)


def _append_event(store: TuoguanStore, row: dict[str, Any]) -> None:
    path = store.data_dir / EVENT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({**row, "created_at": _iso()}, ensure_ascii=False, separators=(",", ":")) + "\n")


def _adapter() -> LLMUnderstandingAdapter:
    global _ADAPTER
    if _ADAPTER is None:
        _ADAPTER = LLMUnderstandingAdapter.from_hermes_config(Path("/opt/hermes-youyi/config/config.yaml"))
    return _ADAPTER


def _is_record(result: Any) -> bool:
    value = str(getattr(result, "capability_candidate", "") or "").lower()
    intent = str(getattr(result, "intent", "") or "").lower()
    return value in {"student_daily_record", "record_student_daily_behavior", "student_record"} or intent in {"student_daily_record", "record_student_daily_behavior", "record_student"}


def evaluate(
    *, store: TuoguanStore, identity: UserIdentity, message_id: str, raw_text: str,
    deterministic_candidate: bool, adapter_factory: Callable[[], Any] | None = None,
) -> dict[str, Any] | None:
    """Return clarification or a verified command proposal; never writes records."""
    if not canary_enabled(store, identity):
        return None
    draft, expired = get_draft(store, identity)
    if not deterministic_candidate and draft is None:
        return None
    config = _read_config(store)
    ttl_minutes = max(1, int(config.get("draft_ttl_minutes") or 20))
    payload = {
        "tenant_id": current_tenant_id(),
        "actor_user_id": identity.canonical_user_id,
        "actor_role": identity.role,
        "message": raw_text,
        "enabled_capability_contracts": ["student_daily_record"],
        "system_owned_facts": {"tenant_id": current_tenant_id(), "actor_role": identity.role, "channel": "wecom_callback"},
        "student_record_draft": draft or {},
        "context_expired": expired,
    }
    try:
        adapter = (adapter_factory or _adapter)()
        understood = adapter.understand(payload)
        trace = deepcopy(getattr(adapter, "last_trace", {}))
    except Exception as exc:
        trace = deepcopy(getattr(locals().get("adapter"), "last_trace", {}))
        _append_event(store, {"message_id": message_id, "actor_user_id": identity.canonical_user_id, "coach_entered": True, "coach_bypass_reason": type(exc).__name__, "schema_trace": trace, "production_record_created": False})
        return {"action": "bypass", "coach_bypass_reason": type(exc).__name__}
    if not _is_record(understood) and draft is None:
        return None

    root_text = str(draft.get("confirmed_user_facts", {}).get("root_text") or "") if draft else ""
    combined = "；".join(part for part in (root_text, raw_text.strip()) if part)
    raw_matches = [name for name in all_student_names(store) if name and name in raw_text]
    raw_matches = sorted(set(raw_matches), key=len, reverse=True)
    if len(raw_matches) > 1 and not all(name in raw_matches[0] for name in raw_matches[1:]):
        return {"action": "clarify", "reply": "这句话里有不止一位学生，请告诉我这条记录具体是说谁。", "reason_code": "student_name_ambiguous", "record_scene": "unknown", "command_ready": False, "trace": trace, "context_expired": expired}
    explicit_name = raw_matches[0] if raw_matches else ""
    llm_name = str(getattr(understood, "business_object_candidate", "") or "").strip()
    if llm_name.lower() in {"none", "null", "unknown", "未识别", "无"}:
        llm_name = ""
    requested_name = explicit_name or llm_name or (str(draft.get("student_candidate") or "") if draft else "")
    if not requested_name:
        for fact in understood.extracted_facts:
            if fact.key in {"student", "student_name"} and fact.source in {"user_statement", "verified_fact"}:
                requested_name = str(fact.value or "").strip()
                break
    resolved_name, profile = resolve_student_for_record(store, identity, requested_name)
    if not resolved_name:
        reason = str(profile.get("reason_code") or "student_not_found")
        reply = "这条记录是说哪位学生？请告诉我学生姓名。" if reason == "student_not_found" else "这个姓名目前不能唯一确认，请补充完整姓名。"
        return {"action": "clarify", "reply": reply, "reason_code": reason, "record_scene": "unknown", "command_ready": False, "trace": trace, "context_expired": expired}

    # A new explicit student always starts a new object instead of contaminating an old draft.
    if draft and requested_name and requested_name != str(draft.get("student_candidate") or ""):
        draft = None
        root_text = ""
        combined = raw_text.strip()
    quality = analyze_record_v2(combined, verified_facts={"student_name": resolved_name})
    common = {
        "coach_entered": True, "entered_model": True, "record_scene": quality.scene,
        "command_ready": quality.command_ready, "clarification_needed": quality.clarification_needed,
        "context_expired": expired, "trace": trace, "resolved_student_name": resolved_name,
    }
    if quality.command_ready:
        return {**common, "action": "command", "composed_original_text": combined, "draft_context_resumed": bool(draft), "context_version": int(draft.get("version") or 0) if draft else 0}

    source_ids = list(draft.get("source_message_ids") or []) if draft else []
    if draft and message_id in source_ids:
        return {**common, "action": "clarify", "reply": str(draft.get("pending_question") or quality.suggested_question), "reason_code": "idempotency_hit", "guidance_sent": False, "draft_context_created": False, "draft_context_resumed": True, "context_version": int(draft.get("version") or 0), "duplicate_prevented": True}
    if message_id not in source_ids:
        source_ids.append(message_id)
    row = _write_draft(store, identity, {
        "context_type": "student_record_draft", "tenant_id": current_tenant_id(),
        "actor_user_id": identity.canonical_user_id, "actor_role": identity.role,
        "student_candidate": resolved_name, "resolved_student_id": str(profile.get("student_id") or ""),
        "record_scene": quality.scene,
        "confirmed_user_facts": {"root_text": combined, "sources": ["user_statement"]},
        "system_owned_facts": {"tenant_id": current_tenant_id(), "actor_role": identity.role, "program_id": str(profile.get("program_id") or "")},
        "missing_observation_dimensions": list(quality.missing_dimensions),
        "pending_question": quality.suggested_question,
        "root_message_id": str(draft.get("root_message_id") or message_id) if draft else message_id,
        "source_message_ids": source_ids,
        "expires_at": _iso(_now() + timedelta(minutes=ttl_minutes)),
    })
    _append_event(store, {**common, "message_id": message_id, "guidance_sent": True, "draft_context_created": not bool(draft), "draft_context_resumed": bool(draft), "context_version": row["version"], "production_record_created": False})
    return {**common, "action": "clarify", "reply": quality.suggested_question, "reason_code": "record_quality_clarification", "guidance_sent": True, "draft_context_created": not bool(draft), "draft_context_resumed": bool(draft), "context_version": row["version"]}


def record_outcome(store: TuoguanStore, identity: UserIdentity, message_id: str, decision: dict[str, Any], *, created: bool, writeback_verified: bool, duplicate_prevented: bool = False) -> None:
    _append_event(store, {
        "message_id": message_id, "actor_user_id": identity.canonical_user_id,
        "coach_entered": True, "entered_model": True, "record_scene": decision.get("record_scene"),
        "command_ready": True, "clarification_needed": False, "guidance_sent": False,
        "draft_context_created": False, "draft_context_resumed": bool(decision.get("draft_context_resumed")),
        "context_version": int(decision.get("context_version") or 0), "context_expired": bool(decision.get("context_expired")),
        "commandbus_dispatch_count": 1, "writeback_verified": writeback_verified,
        "render_verified": writeback_verified, "coach_bypass_reason": "", "hallucination_flags": [],
        "production_record_created": created, "duplicate_prevented": duplicate_prevented,
    })
    if writeback_verified:
        clear_draft(store, identity)
