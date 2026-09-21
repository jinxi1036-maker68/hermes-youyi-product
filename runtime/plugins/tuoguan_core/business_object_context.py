"""Trusted, session-scoped continuity evidence for resolved business objects.

This module deliberately stores *tool evidence*, not language interpretations.
The model remains responsible for deciding whether a new utterance continues an
older object and which Tool to call.  The evidence only lets a later Tool call
prove that a concrete object was previously resolved by an authorised Tool in
the same tenant, actor and Hermes session.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any
import uuid

from .models import UserIdentity
from .store import TuoguanStore
from .tenant_context import current_tenant_id


BUSINESS_OBJECT_CONTEXT_FILE = "business_object_context.json"
_DEFAULT_TTL_MINUTES = 30
_MAX_CONFIRMED = 8
_MAX_PENDING = 4


def remember_candidate_set(
    store: TuoguanStore,
    identity: UserIdentity,
    *,
    session_key: str,
    object_type: str,
    display_name: str,
    candidates: list[dict[str, Any]],
    source_tool: str,
    source_subject_explicit: bool = False,
) -> None:
    """Keep authorised, unresolved candidates as evidence for a later turn.

    The candidates are never usable as write authority.  They give the model
    the exact fact returned by the previous trusted read so it can decide to
    ask for, and then query with, a distinguishing attribute.
    """

    key = _binding_key(identity, session_key)
    clean_candidates = [_clean_candidate(item) for item in candidates if _clean_candidate(item)]
    if not key or not clean_candidates or not str(object_type or "").strip() or not str(display_name or "").strip():
        return
    now = _now()
    expires_at = _expiry()

    def mutate(value: Any) -> dict[str, Any]:
        data = _prune(value)
        item = _session_item(data, key, now=now, expires_at=expires_at)
        pending = item.get("pending") if isinstance(item.get("pending"), list) else []
        existing = next(
            (
                row for row in pending
                if str(row.get("object_type") or "") == str(object_type)
                and str(row.get("display_name") or "") == str(display_name)
            ),
            None,
        )
        # A later model-led re-query must not fabricate or extend a user
        # reference.  It may preserve the original, short-lived declaration
        # from the same trusted session, but never create one by itself.
        inherited_subject_evidence = bool(
            isinstance(existing, dict)
            and existing.get("source_subject_explicit")
            and not _source_subject_evidence_expired(existing)
        )
        subject_explicit = bool(source_subject_explicit) or inherited_subject_evidence
        subject_recorded_at = (
            str((existing or {}).get("source_subject_recorded_at") or now)
            if inherited_subject_evidence else now
        )
        subject_expires_at = (
            str((existing or {}).get("source_subject_expires_at") or expires_at)
            if inherited_subject_evidence else expires_at
        )
        pending = [
            row for row in pending
            if not (
                str(row.get("object_type") or "") == str(object_type)
                and str(row.get("display_name") or "") == str(display_name)
            )
        ]
        pending.append({
            "object_type": str(object_type),
            "display_name": str(display_name),
            "candidates": clean_candidates[:8],
            "source_tool": str(source_tool),
            # This is a server-side fact about the authenticated raw user
            # turn that created the unresolved candidate set.  It is not a
            # model interpretation and cannot be promoted by a search hit.
            "source_subject_explicit": subject_explicit,
            "source_subject_recorded_at": subject_recorded_at if subject_explicit else "",
            "source_subject_expires_at": subject_expires_at if subject_explicit else "",
            "recorded_at": now,
            "expires_at": expires_at,
        })
        item["pending"] = pending[-_MAX_PENDING:]
        item["updated_at"] = now
        data["sessions"][key] = item
        return data

    store.update_json(BUSINESS_OBJECT_CONTEXT_FILE, {"schema_version": 1, "sessions": {}}, mutate)


def resolve_pending_candidate_confirmation(
    store: TuoguanStore,
    identity: UserIdentity,
    *,
    session_key: str,
    object_type: str,
    display_name: str,
    object_id: str,
    trusted_confirmation_text: str,
) -> dict[str, Any] | None:
    """Validate a two-turn, user-grounded confirmation of an ambiguous object.

    A pending set becomes write authority only when the authenticated user
    explicitly named the subject in the source turn, the current authenticated
    turn contains an attribute that uniquely distinguishes one original
    candidate, and the repository result is exactly that candidate.  Model
    Tool arguments and ordinary directory search results never satisfy this
    gate.
    """

    key = _binding_key(identity, session_key)
    expected_type = str(object_type or "").strip()
    expected_name = str(display_name or "").strip()
    expected_id = str(object_id or "").strip()
    compact_confirmation = "".join(str(trusted_confirmation_text or "").split())
    if not key or not expected_type or not expected_name or not compact_confirmation:
        return None
    data = _prune(store.read_json(BUSINESS_OBJECT_CONTEXT_FILE, {"sessions": {}}))
    sessions = data.get("sessions") if isinstance(data.get("sessions"), dict) else {}
    item = sessions.get(key) if isinstance(sessions.get(key), dict) else {}
    pending = item.get("pending") if isinstance(item.get("pending"), list) else []
    matches: list[dict[str, Any]] = []
    for row in pending:
        if (
            not isinstance(row, dict)
            or str(row.get("object_type") or "") != expected_type
            or str(row.get("display_name") or "") != expected_name
            or not bool(row.get("source_subject_explicit"))
            or _source_subject_evidence_expired(row)
            or _expired(row)
        ):
            continue
        candidates = [_clean_candidate(candidate) for candidate in (row.get("candidates") or [])]
        candidates = [candidate for candidate in candidates if candidate]
        target = [
            candidate for candidate in candidates
            if (not expected_id or str(candidate.get("student_id") or "") == expected_id)
            and str(candidate.get("student_name") or "") == expected_name
        ]
        if len(target) != 1:
            continue
        # The confirmation must distinguish this candidate from every other
        # candidate in the original server-returned set.  A shared attribute
        # such as merely naming a grade is insufficient.
        for attribute in ("class_name", "class", "grade", "campus_id"):
            value = str(target[0].get(attribute) or "").strip()
            if not value or "".join(value.split()) not in compact_confirmation:
                continue
            same_value = [
                candidate for candidate in candidates
                if str(candidate.get(attribute) or "").strip() == value
            ]
            if len(same_value) == 1:
                matches.append({
                    "object_type": expected_type,
                    "object_id": str(target[0].get("student_id") or ""),
                    "display_name": expected_name,
                    "authority_basis": "trusted_pending_candidate_confirmation",
                    "confirmation_attribute": attribute,
                    "confirmation_value": value,
                    "source_subject_recorded_at": str(row.get("source_subject_recorded_at") or ""),
                })
                break
    if len(matches) != 1:
        return None
    return deepcopy(matches[0])


def remember_resolved_object(
    store: TuoguanStore,
    identity: UserIdentity,
    *,
    session_key: str,
    object_type: str,
    object_id: str,
    display_name: str,
    attributes: dict[str, Any] | None,
    source_tool: str,
    write_authorized: bool = False,
    authority_basis: str = "",
) -> dict[str, Any] | None:
    """Issue a session-bound reference after an authorised Tool resolves one object."""

    key = _binding_key(identity, session_key)
    object_type = str(object_type or "").strip()
    object_id = str(object_id or "").strip()
    display_name = str(display_name or "").strip()
    if not key or not object_type or not object_id or not display_name:
        return None
    now = _now()
    expires_at = _expiry()
    holder: dict[str, Any] = {}

    def mutate(value: Any) -> dict[str, Any]:
        data = _prune(value)
        item = _session_item(data, key, now=now, expires_at=expires_at)
        confirmed = item.get("confirmed") if isinstance(item.get("confirmed"), list) else []
        existing = next(
            (
                row for row in confirmed
                if str(row.get("object_type") or "") == object_type
                and str(row.get("object_id") or "") == object_id
            ),
            None,
        )
        reference = str((existing or {}).get("object_ref") or f"objctx_{uuid.uuid4().hex}")
        evidence = {
            "object_type": object_type,
            "object_id": object_id,
            "display_name": display_name,
            "attributes": _clean_attributes(attributes),
            "object_ref": reference,
            "source_tool": str(source_tool),
            # A read Tool may identify an object without becoming authority
            # to write it.  Only a server-verifiable user reference, an
            # already-bound session object, or a completed protected write
            # may issue a write-capable continuity reference.
            "write_authorized": bool(write_authorized),
            "authority_basis": str(authority_basis or "").strip(),
            "confirmed_at": now,
            "expires_at": expires_at,
        }
        confirmed = [
            row for row in confirmed
            if not (
                str(row.get("object_type") or "") == object_type
                and str(row.get("object_id") or "") == object_id
            )
        ]
        confirmed.append(evidence)
        item["confirmed"] = confirmed[-_MAX_CONFIRMED:]
        pending = item.get("pending") if isinstance(item.get("pending"), list) else []
        # A read result whose scope was narrowed only by model arguments is
        # not proof that the authenticated user made the same distinction.
        # Keep the original pending set in that case: a later trusted user
        # confirmation still needs to be checked against the complete,
        # server-returned candidate set.  Only genuine write authority may
        # close that pending ambiguity.
        if write_authorized:
            pending = [
                row for row in pending
                if not (
                    str(row.get("object_type") or "") == object_type
                    and str(row.get("display_name") or "") == display_name
                )
            ]
        item["pending"] = pending
        item["updated_at"] = now
        data["sessions"][key] = item
        holder.update(deepcopy(evidence))
        return data

    store.update_json(BUSINESS_OBJECT_CONTEXT_FILE, {"schema_version": 1, "sessions": {}}, mutate)
    return holder or None


def resolve_confirmed_object(
    store: TuoguanStore,
    identity: UserIdentity,
    *,
    session_key: str,
    object_type: str,
    object_ref: str = "",
    object_id: str = "",
    require_write_authorization: bool = False,
) -> dict[str, Any] | None:
    """Return only an unexpired object reference bound to this actor/session."""

    key = _binding_key(identity, session_key)
    if not key:
        return None
    data = _prune(store.read_json(BUSINESS_OBJECT_CONTEXT_FILE, {"sessions": {}}))
    sessions = data.get("sessions") if isinstance(data.get("sessions"), dict) else {}
    item = sessions.get(key) if isinstance(sessions.get(key), dict) else {}
    confirmed = item.get("confirmed") if isinstance(item.get("confirmed"), list) else []
    expected_type = str(object_type or "").strip()
    expected_ref = str(object_ref or "").strip()
    expected_id = str(object_id or "").strip()
    matches = [
        row for row in confirmed
        if isinstance(row, dict)
        and str(row.get("object_type") or "") == expected_type
        and (not expected_ref or str(row.get("object_ref") or "") == expected_ref)
        and (not expected_id or str(row.get("object_id") or "") == expected_id)
        and (not require_write_authorization or bool(row.get("write_authorized")))
        and not _expired(row)
    ]
    if len(matches) != 1:
        return None
    return deepcopy(matches[0])


def render_session_object_context(
    store: TuoguanStore,
    identity: UserIdentity,
    *,
    session_key: str,
) -> str:
    """Render bounded factual evidence for the model; never a next-action rule."""

    key = _binding_key(identity, session_key)
    if not key:
        return ""
    data = _prune(store.read_json(BUSINESS_OBJECT_CONTEXT_FILE, {"sessions": {}}))
    sessions = data.get("sessions") if isinstance(data.get("sessions"), dict) else {}
    item = sessions.get(key) if isinstance(sessions.get(key), dict) else {}
    confirmed = [row for row in (item.get("confirmed") or []) if isinstance(row, dict) and not _expired(row)]
    pending = [row for row in (item.get("pending") or []) if isinstance(row, dict) and not _expired(row)]
    if not confirmed and not pending:
        return ""
    lines = [
        "【本会话可信业务对象证据】以下均来自当前身份、当前租户、当前 Hermes 会话内已有可信 Tool 的授权结果；"
        "它们只是事实证据，不表示本轮应操作任何对象，也不能代替模型理解用户。"
        "模型须自行判断用户是否在继续同一对象；任何读写仍必须调用相应可信 Tool，工具会重新校验权限、实体和回执。",
    ]
    if confirmed:
        lines.append("已确认对象：")
        for row in confirmed[-_MAX_CONFIRMED:]:
            attributes = _render_attributes(row.get("attributes"))
            lines.append(
                f"- type={row.get('object_type')} name={row.get('display_name')} id={row.get('object_id')} "
                f"object_ref={row.get('object_ref')}{attributes}"
            )
    if pending:
        lines.append("尚待消歧的授权候选（不能直接写入；如用户给出区分信息，模型可自主调用查询 Tool 做唯一解析）：")
        for row in pending[-_MAX_PENDING:]:
            candidate_text = "；".join(_render_candidate(candidate) for candidate in (row.get("candidates") or [])[:5])
            lines.append(f"- type={row.get('object_type')} name={row.get('display_name')} candidates={candidate_text}")
    return "\n".join(lines)


def _binding_key(identity: UserIdentity, session_key: str) -> str:
    session = str(session_key or "").strip()
    actor = str(identity.canonical_user_id or "").strip()
    if not session or not actor:
        return ""
    material = "\n".join((current_tenant_id(), actor, session))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _session_item(data: dict[str, Any], key: str, *, now: str, expires_at: str) -> dict[str, Any]:
    sessions = data.setdefault("sessions", {})
    item = sessions.get(key)
    if not isinstance(item, dict):
        item = {"confirmed": [], "pending": [], "created_at": now}
    item["expires_at"] = expires_at
    return item


def _prune(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    sessions = data.get("sessions") if isinstance(data.get("sessions"), dict) else {}
    kept: dict[str, Any] = {}
    for key, item in sessions.items():
        if not isinstance(item, dict) or _expired(item):
            continue
        confirmed = [row for row in (item.get("confirmed") or []) if isinstance(row, dict) and not _expired(row)]
        pending = [row for row in (item.get("pending") or []) if isinstance(row, dict) and not _expired(row)]
        if confirmed or pending:
            kept[str(key)] = {**item, "confirmed": confirmed[-_MAX_CONFIRMED:], "pending": pending[-_MAX_PENDING:]}
    return {"schema_version": 1, "sessions": kept}


def _clean_attributes(attributes: dict[str, Any] | None) -> dict[str, str]:
    source = attributes if isinstance(attributes, dict) else {}
    return {
        str(key): str(value).strip()
        for key, value in source.items()
        if str(key).strip() and str(value).strip()
    }


def _clean_candidate(value: Any) -> dict[str, str]:
    row = value if isinstance(value, dict) else {}
    allowed = ("student_id", "student_name", "class_name", "class", "grade", "campus_id")
    clean = {key: str(row.get(key) or "").strip() for key in allowed if str(row.get(key) or "").strip()}
    return clean if clean.get("student_id") and clean.get("student_name") else {}


def _render_attributes(value: Any) -> str:
    attributes = _clean_attributes(value if isinstance(value, dict) else {})
    return "" if not attributes else " " + " ".join(f"{key}={item}" for key, item in attributes.items())


def _render_candidate(value: Any) -> str:
    row = _clean_candidate(value)
    if not row:
        return ""
    return ",".join(f"{key}={item}" for key, item in row.items())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=_DEFAULT_TTL_MINUTES)).isoformat(timespec="seconds")


def _expired(value: dict[str, Any]) -> bool:
    raw = str(value.get("expires_at") or "")
    try:
        return datetime.fromisoformat(raw) <= datetime.now(timezone.utc)
    except ValueError:
        return True


def _source_subject_evidence_expired(value: dict[str, Any]) -> bool:
    raw = str(value.get("source_subject_expires_at") or "")
    try:
        return datetime.fromisoformat(raw) <= datetime.now(timezone.utc)
    except ValueError:
        return True
