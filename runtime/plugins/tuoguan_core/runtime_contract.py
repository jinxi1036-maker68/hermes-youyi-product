"""XiaoYou's public-host Runtime Contract implementation.

This module is deliberately independent of Core-private identity storage and
agent internals.  A platform adapter or a public Hermes lifecycle hook
creates a trusted turn from server-owned transport fields.  Tools consume the
immutable record; model tool arguments and client payload fields are never an
identity source.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
from typing import Any

from .identity import IdentityService
from .models import UserIdentity
from .store import TuoguanStore
from .tenant_context import current_tenant_id


_ACTIVE_TURN: ContextVar["TrustedTurn | None"] = ContextVar("xiaoyou_trusted_turn", default=None)
_LOCK = threading.RLock()
_BY_SESSION: dict[str, dict[str, "TrustedTurn"]] = {}
# Hermes deliberately owns its opaque agent-session IDs.  A Robot adapter
# therefore cannot know that ID before it dispatches a verified device turn.
# Keep a one-shot, server-only ingress binding until the *public* pre-LLM hook
# observes it.  This is identity/correlation plumbing, never intent
# classification or tool routing.
_PENDING_ROBOT_INGRESS: dict[tuple[str, str, str], list["TrustedTurn"]] = {}
_TTL = timedelta(minutes=20)
_TRUSTED_CHANNELS = frozenset({"wecom_callback", "robot_poc", "feishu", "agenda_service_work"})


@dataclass(frozen=True)
class TrustedTurn:
    """Server-resolved facts that a XiaoYou Tool may trust for one turn."""

    platform: str
    tenant_id: str
    identity: UserIdentity
    chat_id: str
    session_id: str
    turn_id: str
    message_id: str
    issued_at: datetime
    source: str
    # A server-attested, channel-scoped continuity identity for XiaoYou-owned
    # business facts.  This is intentionally distinct from ``session_id``:
    # Hermes owns the latter and may replace its opaque value between releases
    # or turns.  It is never derived from model Tool arguments or user text.
    continuity_key: str
    # A public platform capability may attach a static server Skill by
    # transforming the model-facing user string.  This field is set only by
    # the authenticated adapter so the Runtime Contract can keep the original
    # transport text and identity record separate from that host-owned context.
    host_transforms_model_user_message: bool = False

    @property
    def actor_user_id(self) -> str:
        return self.identity.canonical_user_id


def _normalise_platform(value: Any) -> str:
    platform = getattr(value, "value", value)
    return str(platform or "").strip().lower()


def _normalise_actor(value: Any) -> str:
    actor = str(value or "").strip()
    return actor.split(":", 1)[1].strip() if ":" in actor else actor


def _message_digest(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _trusted_continuity_key(
    *, platform: str, tenant_id: str, actor_user_id: str, chat_id: str,
) -> str:
    """Return an opaque XiaoYou continuity identity from trusted transport facts.

    ``chat_id`` is supplied only by an authenticated platform adapter/public
    hook.  In particular, Robot constructs it from the verified device and
    validated channel session before Hermes receives the message.  Hashing
    makes this a stable Workspace lookup identity without exposing the raw
    channel scope in ledgers or traces.
    """

    material = "\x1f".join((
        "xiaoyou-continuity-v1",
        str(platform or ""),
        str(tenant_id or ""),
        str(actor_user_id or ""),
        str(chat_id or ""),
    ))
    return "xyc1_" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _purge_expired_locked(now: datetime) -> None:
    expired_sessions: list[str] = []
    for session_id, turns in _BY_SESSION.items():
        stale = [turn_id for turn_id, turn in turns.items() if now - turn.issued_at > _TTL]
        for turn_id in stale:
            turns.pop(turn_id, None)
        if not turns:
            expired_sessions.append(session_id)
    for session_id in expired_sessions:
        _BY_SESSION.pop(session_id, None)
    stale_ingress: list[tuple[str, str, str]] = []
    for key, candidates in _PENDING_ROBOT_INGRESS.items():
        kept = [turn for turn in candidates if now - turn.issued_at <= _TTL]
        if kept:
            _PENDING_ROBOT_INGRESS[key] = kept
        else:
            stale_ingress.append(key)
    for key in stale_ingress:
        # The transformed-message path may have selected an ingress whose
        # original text lives under another digest key. Remove that exact
        # server operation, not merely the lifecycle hook's transformed key.
        for pending_key, pending_rows in list(_PENDING_ROBOT_INGRESS.items()):
            remaining = [item for item in pending_rows if item.message_id != ingress.message_id]
            if remaining:
                _PENDING_ROBOT_INGRESS[pending_key] = remaining
            else:
                _PENDING_ROBOT_INGRESS.pop(pending_key, None)


def _resolve_identity(*, platform: str, actor_user_id: str, user_name: str, chat_id: str) -> UserIdentity:
    # Robot identity is bound to the existing approved WeCom identity directory;
    # its transport platform must not create a parallel actor/role database.
    identity_platform = "wecom_callback" if platform == "robot_poc" else platform
    return IdentityService(TuoguanStore()).resolve(
        identity_platform,
        actor_user_id,
        user_name=user_name,
        chat_id=chat_id,
        message_text="",
    )


def bind_trusted_turn(
    *,
    platform: Any,
    actor_user_id: Any,
    session_id: Any,
    turn_id: Any,
    message_id: Any = "",
    chat_id: Any = "",
    user_name: Any = "",
    tenant_id: Any = "",
    source: str,
    host_transforms_model_user_message: bool = False,
) -> TrustedTurn | None:
    """Bind only a server-originated channel identity to a turn.

    Callers are the verified Robot device adapter or the documented Hermes
    pre-LLM hook.  An empty/mismatched channel identity fails closed.  The
    return value contains no model-provided role or tenant field.
    """

    normalized_platform = _normalise_platform(platform)
    actor = _normalise_actor(actor_user_id)
    session = str(session_id or "").strip()
    turn = str(turn_id or "").strip() or str(message_id or "").strip()
    message = str(message_id or "").strip() or turn
    conversation = str(chat_id or "").strip() or session
    if normalized_platform not in _TRUSTED_CHANNELS or not actor or not session or not turn:
        return None
    identity = _resolve_identity(
        platform=normalized_platform,
        actor_user_id=actor,
        user_name=str(user_name or ""),
        chat_id=conversation,
    )
    record = TrustedTurn(
        platform=normalized_platform,
        tenant_id=str(tenant_id or current_tenant_id()).strip() or current_tenant_id(),
        identity=identity,
        chat_id=conversation,
        session_id=session,
        turn_id=turn,
        message_id=message,
        issued_at=datetime.now(timezone.utc),
        source=str(source or "public_lifecycle_hook"),
        continuity_key=_trusted_continuity_key(
            platform=normalized_platform,
            tenant_id=str(tenant_id or current_tenant_id()).strip() or current_tenant_id(),
            actor_user_id=identity.canonical_user_id,
            chat_id=conversation,
        ),
        host_transforms_model_user_message=bool(host_transforms_model_user_message),
    )
    with _LOCK:
        _purge_expired_locked(record.issued_at)
        turns = _BY_SESSION.setdefault(session, {})
        existing = turns.get(turn)
        # A different actor/tenant may never replace an already authenticated
        # turn.  This protects against channel/session cross-wiring.
        if existing is not None and (
            existing.actor_user_id != record.actor_user_id
            or existing.tenant_id != record.tenant_id
            or existing.platform != record.platform
        ):
            return None
        turns[turn] = existing or record
        record = turns[turn]
    _ACTIVE_TURN.set(record)
    return record


def bind_robot_device_turn(
    *,
    actor_user_id: str,
    tenant_id: str,
    session_id: str,
    turn_id: str,
    message_id: str,
    chat_id: str,
    message_text: str,
    host_transforms_model_user_message: bool = False,
) -> TrustedTurn | None:
    """Record a device-registry-authenticated Robot turn before dispatch.

    The resulting record is available immediately to the transport trace.  A
    single opaque ingress entry then lets the public Hermes pre-LLM lifecycle
    hook attach the same trusted record to the Core-owned agent session.
    """

    record = bind_trusted_turn(
        platform="robot_poc",
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        session_id=session_id,
        turn_id=turn_id,
        message_id=message_id,
        chat_id=chat_id,
        source="robot_device_registry",
        host_transforms_model_user_message=host_transforms_model_user_message,
    )
    if record is None:
        return None
    key = (record.platform, record.actor_user_id, _message_digest(message_text))
    with _LOCK:
        _purge_expired_locked(record.issued_at)
        pending = _PENDING_ROBOT_INGRESS.setdefault(key, [])
        # A duplicate dispatch of the same authenticated operation must not
        # create another candidate.  A distinct simultaneous turn with the
        # same words remains ambiguous and is intentionally not claimed.
        if not any(item.message_id == record.message_id for item in pending):
            pending.append(record)
    return record


def claim_robot_turn_for_agent(
    *,
    actor_user_id: Any,
    raw_text: Any,
    agent_session_id: Any,
    agent_turn_id: Any,
) -> TrustedTurn | None:
    """Attach one verified Robot ingress to a public Hermes lifecycle turn.

    This bridge is fail-closed. Normal Robot turns require one fresh
    device-registry ingress with the same canonical sender and byte-exact
    message digest. A public, server-configured Hermes Skill may prepend its
    static payload to the model-facing message; in that case the bridge accepts
    exactly one pending ingress for the actor, but only when the adapter
    recorded that host transformation before dispatch. Client fields, model
    arguments and Core-private session state never participate. Ambiguity
    always blocks the Tool-facing identity context.
    """

    actor = _normalise_actor(actor_user_id)
    session = str(agent_session_id or "").strip()
    turn = str(agent_turn_id or "").strip()
    if not actor or not session or not turn:
        return None
    now = datetime.now(timezone.utc)
    with _LOCK:
        _purge_expired_locked(now)
        key = ("robot_poc", actor, _message_digest(raw_text))
        candidates = list(_PENDING_ROBOT_INGRESS.get(key, []))
        if len(candidates) != 1:
            # Hermes' documented ``MessageEvent.auto_skill`` feature can
            # prepend a static, server-owned Skill to the agent's *model*
            # message.  The public lifecycle hook intentionally receives that
            # transformed text, so an exact client-text digest cannot match.
            # The adapter creates at most one live device turn per terminal;
            # still require exactly one pending actor ingress globally so two
            # devices/session races fail closed instead of being guessed.
            transformed = [
                item
                for (platform, candidate_actor, _digest), rows in _PENDING_ROBOT_INGRESS.items()
                if platform == "robot_poc" and candidate_actor == actor
                for item in rows
                if item.host_transforms_model_user_message
            ]
            candidates = transformed if len(transformed) == 1 else []
        if len(candidates) != 1:
            return None
        ingress = candidates[0]
        # Consume the actual source record before exposing the agent-bound
        # record so an identical later LLM call cannot replay this authority.
        # ``key`` is the model-facing digest and differs when the documented
        # host Skill transform is active, hence remove by the selected ingress
        # identity rather than assuming the exact-message key above.
        for pending_key, pending_rows in list(_PENDING_ROBOT_INGRESS.items()):
            remaining = [
                item for item in pending_rows
                if not (
                    item.message_id == ingress.message_id
                    and item.session_id == ingress.session_id
                    and item.turn_id == ingress.turn_id
                )
            ]
            if remaining:
                _PENDING_ROBOT_INGRESS[pending_key] = remaining
            else:
                _PENDING_ROBOT_INGRESS.pop(pending_key, None)
        source_rows = _BY_SESSION.get(ingress.session_id, {})
        source_rows.pop(ingress.turn_id, None)
        if not source_rows:
            _BY_SESSION.pop(ingress.session_id, None)
        record = TrustedTurn(
            platform=ingress.platform,
            tenant_id=ingress.tenant_id,
            identity=ingress.identity,
            chat_id=ingress.chat_id,
            session_id=session,
            turn_id=turn,
            message_id=ingress.message_id,
            issued_at=now,
            source="robot_device_registry_public_hook_bridge",
            # Preserve the adapter-attested XiaoYou continuity scope while
            # recording the Core-owned session strictly for this Agent turn.
            continuity_key=ingress.continuity_key,
            host_transforms_model_user_message=ingress.host_transforms_model_user_message,
        )
        rows = _BY_SESSION.setdefault(session, {})
        existing = rows.get(turn)
        if existing is not None and (
            existing.actor_user_id != record.actor_user_id
            or existing.tenant_id != record.tenant_id
            or existing.platform != record.platform
        ):
            return None
        rows[turn] = existing or record
        record = rows[turn]
    _ACTIVE_TURN.set(record)
    return record


def activate_trusted_turn(*, session_id: Any, turn_id: Any = "") -> TrustedTurn | None:
    """Activate a pre-LLM authenticated turn for a public pre-tool hook."""

    session = str(session_id or "").strip()
    turn = str(turn_id or "").strip()
    if not session:
        return None
    now = datetime.now(timezone.utc)
    with _LOCK:
        _purge_expired_locked(now)
        candidates = _BY_SESSION.get(session, {})
        selected = candidates.get(turn) if turn else None
        # Some supported Hermes releases expose an agent turn id to pre-LLM
        # but a gateway message id to pre-tool.  A fallback is safe only when
        # this session has exactly one live server-authenticated turn.
        if selected is None and len(candidates) == 1:
            selected = next(iter(candidates.values()))
    if selected is not None:
        _ACTIVE_TURN.set(selected)
    return selected


def current_trusted_turn() -> TrustedTurn | None:
    return _ACTIVE_TURN.get()


def clear_trusted_turn(*, session_id: Any = "", turn_id: Any = "") -> None:
    """Retire finished state without clearing an unrelated concurrent turn."""

    active = _ACTIVE_TURN.get()
    if active is not None and (not session_id or active.session_id == str(session_id)):
        _ACTIVE_TURN.set(None)
    session = str(session_id or "").strip()
    turn = str(turn_id or "").strip()
    if not session:
        return
    with _LOCK:
        rows = _BY_SESSION.get(session)
        if not rows:
            return
        if turn:
            rows.pop(turn, None)
        elif len(rows) == 1:
            rows.clear()
        if not rows:
            _BY_SESSION.pop(session, None)


def clear_all_trusted_turns() -> None:
    """Test/controlled-reset helper; never called by normal message handling."""

    with _LOCK:
        _BY_SESSION.clear()
        _PENDING_ROBOT_INGRESS.clear()
    _ACTIVE_TURN.set(None)


def trusted_turn_snapshot() -> dict[str, Any]:
    """Return public, content-free diagnostic facts for certification tests."""

    active = current_trusted_turn()
    if active is None:
        return {"active": False}
    return {
        "active": True,
        "platform": active.platform,
        "tenant_id": active.tenant_id,
        "actor_user_id": active.actor_user_id,
        "role": active.identity.role,
        "approval_state": active.identity.approval_state,
        "session_id": active.session_id,
        "continuity_key_hash": _message_digest(active.continuity_key)[:16],
        "turn_id": active.turn_id,
        "message_id": active.message_id,
        "source": active.source,
    }


# ---- Agenda service-work public ingress ----------------------------------
# The platform adapter deposits a server-issued ticket in the external
# Institution Workspace.  This bridge consumes one matching ticket through a
# public pre-LLM lifecycle hook.  It never reads Core-private request state or
# model/client identity fields.
def claim_agenda_service_turn_for_agent(*, actor_user_id: Any, raw_text: Any, agent_session_id: Any, agent_turn_id: Any) -> TrustedTurn | None:
    import os
    from pathlib import Path
    import sqlite3

    actor = str(actor_user_id or "").strip()
    session = str(agent_session_id or "").strip()
    turn = str(agent_turn_id or "").strip()
    database = str(os.environ.get("XIAOYOU_AGENDA_SERVICE_INGRESS_DB") or "").strip()
    if not actor.startswith("service:agenda:") or not session or not turn or not database:
        return None
    tenant_id = actor.removeprefix("service:agenda:")
    if not tenant_id:
        return None
    raw_candidates = [str(raw_text or "")]
    display_prefix = "[小优 Agenda 服务] "
    if raw_candidates[0].startswith(display_prefix):
        raw_candidates.append(raw_candidates[0][len(display_prefix):])
    expected_digests = tuple({_message_digest(value) for value in raw_candidates})
    now = datetime.now(timezone.utc)
    try:
        connection = sqlite3.connect(Path(database), timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
    except (OSError, sqlite3.Error):
        return None
    try:
        connection.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in expected_digests)
        rows = connection.execute(
            "SELECT * FROM agenda_service_tickets WHERE state='dispatched' AND service_identity=? "
            "AND payload_sha256 IN (" + placeholders + ") AND expires_at>=?",
            (actor, *expected_digests, now.timestamp()),
        ).fetchall()
        if len(rows) != 1:
            connection.execute("ROLLBACK")
            return None
        row = rows[0]
        if str(row["tenant_id"] or "") != tenant_id or str(row["source_identity"] or "") != "agenda:" + tenant_id:
            connection.execute("ROLLBACK")
            return None
        changed = connection.execute(
            "UPDATE agenda_service_tickets SET state='agent_claimed', agent_claimed_at=?, updated_at=? WHERE ticket_id=? AND state='dispatched'",
            (now.isoformat(timespec="milliseconds"), now.isoformat(timespec="milliseconds"), str(row["ticket_id"])),
        ).rowcount
        if changed != 1:
            connection.execute("ROLLBACK")
            return None
        connection.execute("COMMIT")
    except sqlite3.Error:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return None
    finally:
        connection.close()
    identity = UserIdentity(
        platform="agenda_service_work", platform_user_id=actor, canonical_user_id=actor,
        person_name="小优 Agenda 服务", role="agenda_service", approval_state="approved",
    )
    try:
        partition = json.loads(str(row["partition_json"] or "{}"))
    except (TypeError, ValueError):
        return None
    trusted_session = str(partition.get("trusted_session_id") or "").strip()
    continuity = str(partition.get("continuity_id") or trusted_session).strip()
    if (
        not trusted_session
        or str(partition.get("tenant_id") or "") != tenant_id
        or str(partition.get("canonical_actor_id") or "") != actor
        or str(partition.get("source_identity") or "") != "agenda:" + tenant_id
    ):
        return None
    chat_material = tenant_id + "\x1f" + trusted_session
    trusted_chat_id = "agenda-service:" + tenant_id + ":" + hashlib.sha256(
        chat_material.encode("utf-8")
    ).hexdigest()[:24]
    record = TrustedTurn(
        platform="agenda_service_work", tenant_id=tenant_id, identity=identity,
        chat_id=trusted_chat_id, session_id=session, turn_id=turn,
        message_id=str(row["message_id"]), issued_at=now,
        source="agenda_service_work_ticket_public_hook_bridge",
        continuity_key=continuity,
    )
    with _LOCK:
        _purge_expired_locked(now)
        rows_for_session = _BY_SESSION.setdefault(session, {})
        existing = rows_for_session.get(turn)
        if existing is not None and (
            existing.actor_user_id != record.actor_user_id
            or existing.tenant_id != record.tenant_id
            or existing.platform != record.platform
        ):
            return None
        rows_for_session[turn] = existing or record
        record = rows_for_session[turn]
    _ACTIVE_TURN.set(record)
    return record
