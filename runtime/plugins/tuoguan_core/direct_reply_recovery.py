"""Durable, reply-only recovery for trusted direct-channel turns.

This module is deliberately smaller than a channel and smaller than an Agent
runtime.  It observes an already-completed, writeback-verified receipt and
keeps enough *server-attested* direct-turn evidence to ask the existing Hermes
runtime to express that result again.  It has no Tool import, no business
operation import, no reply template and no untrusted ingress.

The actual recovery Agent turn is supplied by the public ``reply_recovery``
platform adapter.  The manager only hands it immutable facts and later asks
the normal Work Runtime outbox to deliver Hermes' own text.
"""

from __future__ import annotations

import builtins
from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any

from .work_runtime import (
    DurableReplyOutbox,
    HermesReplyTerminal,
    ReplyDestination,
    ReplyJob,
    TrustedPartition,
    VerifiedBusinessFact,
    WorkRuntimeRejected,
)
from .work_runtime_channels import AdapterReplyChannelPort, DurableReplyDeliveryDispatcher, TrustedReplyChannelPort
from .work_runtime_receipts import PublicToolReceiptEvent


logger = logging.getLogger(__name__)


RECOVERY_PLATFORM = "reply_recovery"
RECOVERY_SENDER = "reply-recovery-service"
# This is a transport protocol reference, not text for a person. It used to
# start with U+2063. Some public platform paths normalize format characters,
# which silently turned the reference into ordinary visible text. Keep the
# protocol ASCII and accept the legacy spelling only while reading old turns.
CONTROL_PREFIX = "xiaoyou-reply-outbox:"
_LEGACY_CONTROL_PREFIX = "\u2063" + CONTROL_PREFIX
_REPLY_ID_RE = re.compile(r"reply_[0-9a-f]{32}\Z")
_DIRECT_CHANNELS = frozenset({"robot_poc", "wecom_callback"})
_PROACTIVE_NOTICE_KINDS = frozenset({"task_delivery", "relationship_touch"})


def control_marker(reply_id: str) -> str:
    """Opaque adapter control text, never a user-facing business reply."""

    value = str(reply_id or "").strip()
    if not _REPLY_ID_RE.fullmatch(value):
        raise WorkRuntimeRejected("direct_reply_control_reference_invalid")
    return CONTROL_PREFIX + value


def parse_control_marker(value: object) -> str | None:
    # Exact-match parsing is intentional. A normal reply that merely mentions
    # an identifier can never become a control operation. The adapter still
    # must resolve the reference against the held job and its trusted
    # destination before releasing any delivery.
    text = str(value or "").strip()
    for prefix in (CONTROL_PREFIX, _LEGACY_CONTROL_PREFIX):
        if text.startswith(prefix):
            reply_id = text[len(prefix):].strip()
            return reply_id if _REPLY_ID_RE.fullmatch(reply_id) else None
    return None


def _workspace_root() -> Path:
    workspace = str(os.getenv("XIAOYOU_INSTITUTION_WORKSPACE") or "").strip()
    if workspace:
        # Product Workspace and its data root are deliberately distinct.  A
        # process may also carry the legacy data-root environment variable;
        # prefer the product-level setting, but never turn it into a second
        # outbox beside ``<workspace>/data``.
        candidate = Path(workspace).expanduser().resolve()
        return candidate / "data" if (candidate / "data").is_dir() else candidate
    raw = str(os.getenv("HERMES_TUOGUAN_DATA_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    # The server and certification launcher set an external Institution
    # Workspace.  This fallback is only for isolated local tests and never
    # uses Hermes' installation directory as an authority boundary.
    return (Path.cwd() / ".xiaoyou-institution-workspace").resolve()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class DirectReplyRecoveryManager:
    """One durable bridge from trusted direct turns to the Work Runtime.

    The bridge is intentionally only enabled for channels that have a
    server-authenticated direct turn.  It does not infer recipients from
    model text: channel, recipient, tenant, actor and device all come from
    the bound direct turn held by the Runtime Contract.
    """

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else _workspace_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database_path = self.root / "direct_reply_recovery.sqlite"
        self.outbox = DurableReplyOutbox(self.root / "durable_reply_outbox.sqlite")
        self._lock = threading.RLock()
        self._ports: dict[str, object] = {}
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._lock, closing(self._connect()) as connection:
            # v1 keyed direct-reply evidence by the Hermes agent *session*.
            # A session contains many user turns, so a verified receipt from
            # one turn could be observed while completing a later one. Do not
            # migrate those ambiguous rows into the new authoritative table:
            # preserve them for audit under a legacy name and start each new
            # bridge from the public hook's exact turn id.
            existing = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='direct_reply_turns'"
            ).fetchone()
            if existing is not None:
                columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(direct_reply_turns)")
                }
                if "direct_turn_id" not in columns:
                    connection.execute(
                        "ALTER TABLE direct_reply_turns RENAME TO direct_reply_turns_legacy_session_v1"
                    )
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS direct_reply_turns (
                    direct_turn_id TEXT PRIMARY KEY,
                    agent_session_id TEXT NOT NULL,
                    agent_turn_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    operation_hint TEXT NOT NULL,
                    partition_json TEXT NOT NULL,
                    destination_json TEXT NOT NULL,
                    original_text TEXT NOT NULL,
                    original_turn_id TEXT NOT NULL,
                    receipt_json TEXT NOT NULL DEFAULT '',
                    operation_id TEXT NOT NULL DEFAULT '',
                    reply_id TEXT NOT NULL DEFAULT '',
                    recovery_session_id TEXT NOT NULL DEFAULT '',
                    recovery_state TEXT NOT NULL DEFAULT '',
                    tool_attempted INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS direct_reply_session_turn_idx
                    ON direct_reply_turns(agent_session_id, agent_turn_id);
                CREATE UNIQUE INDEX IF NOT EXISTS direct_reply_operation_idx
                    ON direct_reply_turns(operation_id) WHERE operation_id <> '';
                CREATE UNIQUE INDEX IF NOT EXISTS direct_reply_recovery_session_idx
                    ON direct_reply_turns(recovery_session_id) WHERE recovery_session_id <> '';
                CREATE TABLE IF NOT EXISTS agenda_reply_ticket_links (
                    ticket_id TEXT PRIMARY KEY,
                    direct_turn_id TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(direct_turn_id) REFERENCES direct_reply_turns(direct_turn_id)
                );
                """
            )

    @staticmethod
    def _identity_value(turn: Any, name: str) -> str:
        identity = getattr(turn, "identity", None)
        return str(getattr(identity, name, "") or "").strip()

    @staticmethod
    def _channel(turn: Any) -> str:
        channel = str(getattr(turn, "platform", "") or "").strip().lower()
        if channel not in _DIRECT_CHANNELS:
            raise WorkRuntimeRejected("direct_reply_channel_not_trusted")
        return channel

    @staticmethod
    def _partition(turn: Any) -> TrustedPartition:
        channel = DirectReplyRecoveryManager._channel(turn)
        tenant = str(getattr(turn, "tenant_id", "") or "").strip()
        actor = DirectReplyRecoveryManager._identity_value(turn, "canonical_user_id")
        chat = str(getattr(turn, "chat_id", "") or "").strip()
        continuity = str(getattr(turn, "continuity_key", "") or "").strip() or None
        if not tenant or not actor or not chat:
            raise WorkRuntimeRejected("direct_reply_trusted_turn_fields_missing")
        principal_kind = "device" if channel == "robot_poc" else "actor"
        material = "".join((tenant, actor, principal_kind, continuity or chat, channel))
        return TrustedPartition(
            partition_id="direct_" + sha256(material.encode("utf-8")).hexdigest()[:32],
            tenant_id=tenant, canonical_actor_id=actor, principal_kind=principal_kind,
            trusted_session_id=chat, continuity_id=continuity, source_identity=channel + ":" + actor,
        )

    @staticmethod
    def _destination(turn: Any) -> ReplyDestination:
        channel = DirectReplyRecoveryManager._channel(turn)
        tenant = str(getattr(turn, "tenant_id", "") or "").strip()
        actor = DirectReplyRecoveryManager._identity_value(turn, "canonical_user_id")
        chat = str(getattr(turn, "chat_id", "") or "").strip()
        if not tenant or not actor or not chat:
            raise WorkRuntimeRejected("direct_reply_destination_fields_missing")
        # Hermes owns ``chat_id`` and, on the public WeCom callback path, it
        # may be an opaque Agent session rather than a WeCom recipient.  The
        # server-resolved canonical actor is the authenticated WeCom user and
        # is the only safe proactive-delivery destination.  Robot is
        # intentionally different: its device/session scope remains the
        # recipient binding validated by its adapter.
        recipient = actor if channel == "wecom_callback" else chat
        device_id = chat.split(":", 1)[0] if channel == "robot_poc" and ":" in chat else ""
        return ReplyDestination(tenant, channel, recipient, channel + ":" + actor, device_id)

    def capture_direct_turn(self, *, agent_session_id: str, raw_text: str, trusted_turn: Any) -> None:
        """Persist a server-attested direct turn before its Tool result exists."""

        platform = str(getattr(trusted_turn, "platform", "") or "").lower()
        if platform not in _DIRECT_CHANNELS:
            return
        session = str(agent_session_id or "").strip()
        text = str(raw_text or "").strip()
        if not session or not text:
            raise WorkRuntimeRejected("direct_reply_capture_fields_missing")
        partition = self._partition(trusted_turn)
        destination = self._destination(trusted_turn)
        actor = partition.canonical_actor_id
        operation_hint = str(getattr(trusted_turn, "message_id", "") or "").strip()
        original_turn_id = str(getattr(trusted_turn, "turn_id", "") or operation_hint).strip()
        if not original_turn_id:
            # There is no safe recovery correlation when a public lifecycle
            # turn id is unavailable. A normal direct reply may still happen;
            # reply-only recovery deliberately fails closed.
            raise WorkRuntimeRejected("direct_reply_turn_id_missing")
        direct_turn_id = sha256((session + "\x1f" + original_turn_id).encode("utf-8")).hexdigest()
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM direct_reply_turns WHERE direct_turn_id=?", (direct_turn_id,)
                ).fetchone()
                stable = (platform, partition.tenant_id, actor, _json(asdict(partition)), _json(asdict(destination)), text, original_turn_id)
                if row is not None:
                    existing = (
                        str(row["channel"]), str(row["tenant_id"]), str(row["actor_id"]), str(row["partition_json"]),
                        str(row["destination_json"]), str(row["original_text"]), str(row["original_turn_id"]),
                    )
                    if existing != stable:
                        raise WorkRuntimeRejected("direct_reply_turn_scope_conflict")
                else:
                    connection.execute(
                        """INSERT INTO direct_reply_turns(direct_turn_id,agent_session_id,agent_turn_id,channel,tenant_id,actor_id,
                            operation_hint,partition_json,destination_json,original_text,original_turn_id,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (direct_turn_id, session, original_turn_id, platform, partition.tenant_id, actor, operation_hint,
                         _json(asdict(partition)), _json(asdict(destination)), text, original_turn_id, now, now),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def capture_agenda_turn(
        self,
        *,
        ticket_id: str,
        agent_session_id: str,
        agent_turn_id: str,
        tenant_id: str,
        service_identity: str,
        partition: TrustedPartition,
        destination: ReplyDestination,
        raw_text: str,
        operation_hint: str,
    ) -> None:
        """Persist a server-attested Agenda turn for reply/delivery recovery.

        The caller is the public Agenda ingress after it has consumed a signed
        ticket. All identity, partition and destination data are supplied by
        that ticket's durable binding; model text cannot select a recipient or
        rewrite the scope.
        """

        ticket = str(ticket_id or "").strip()
        session = str(agent_session_id or "").strip()
        turn = str(agent_turn_id or "").strip()
        tenant = str(tenant_id or "").strip()
        actor = str(service_identity or "").strip()
        text = str(raw_text or "").strip()
        operation = str(operation_hint or "").strip()
        if not all((ticket, session, turn, tenant, actor, text, operation)):
            raise WorkRuntimeRejected("agenda_reply_capture_fields_missing")
        if actor != "service:agenda:" + tenant:
            raise WorkRuntimeRejected("agenda_reply_service_identity_invalid")
        if (
            partition.tenant_id != tenant
            or partition.canonical_actor_id != actor
            or partition.principal_kind != "service"
            or partition.source_identity != "agenda:" + tenant
        ):
            raise WorkRuntimeRejected("agenda_reply_partition_invalid")
        if destination.tenant_id != tenant or destination.channel != "wecom_callback":
            raise WorkRuntimeRejected("agenda_reply_destination_invalid")
        direct_turn_id = sha256((session + "\x1f" + turn).encode("utf-8")).hexdigest()
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM direct_reply_turns WHERE direct_turn_id=?", (direct_turn_id,)
                ).fetchone()
                stable = (
                    "agenda_service_work", tenant, actor, _json(asdict(partition)),
                    _json(asdict(destination)), text, turn, operation,
                )
                if row is not None:
                    existing = (
                        str(row["channel"]), str(row["tenant_id"]), str(row["actor_id"]),
                        str(row["partition_json"]), str(row["destination_json"]),
                        str(row["original_text"]), str(row["original_turn_id"]), str(row["operation_hint"]),
                    )
                    if existing != stable:
                        raise WorkRuntimeRejected("agenda_reply_turn_scope_conflict")
                else:
                    connection.execute(
                        """INSERT INTO direct_reply_turns(direct_turn_id,agent_session_id,agent_turn_id,channel,tenant_id,actor_id,
                            operation_hint,partition_json,destination_json,original_text,original_turn_id,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            direct_turn_id, session, turn, "agenda_service_work", tenant, actor,
                            operation, _json(asdict(partition)), _json(asdict(destination)), text, turn, now, now,
                        ),
                    )
                link = connection.execute(
                    "SELECT direct_turn_id FROM agenda_reply_ticket_links WHERE ticket_id=?", (ticket,)
                ).fetchone()
                if link is not None and str(link["direct_turn_id"]) != direct_turn_id:
                    raise WorkRuntimeRejected("agenda_reply_ticket_turn_conflict")
                if link is None:
                    connection.execute(
                        "INSERT INTO agenda_reply_ticket_links(ticket_id,direct_turn_id,created_at,updated_at) VALUES(?,?,?,?)",
                        (ticket, direct_turn_id, now, now),
                    )
                else:
                    connection.execute(
                        "UPDATE agenda_reply_ticket_links SET updated_at=? WHERE ticket_id=?", (now, ticket)
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _receipt_operation(receipt: dict[str, object]) -> str:
        return str(receipt.get("operation_id") or receipt.get("id") or "").strip()

    def observe_verified_receipt(self, event: PublicToolReceiptEvent) -> None:
        """Passively retain only completed, verified Tool receipt evidence."""

        if event.result_ok is not True or not event.result_writeback_verified:
            return
        receipt = dict(event.execution_receipt)
        operation_id = self._receipt_operation(receipt)
        if str(receipt.get("status") or "") != "completed" or not operation_id:
            return
        with closing(self._connect()) as connection:
            connection.execute(
                """UPDATE direct_reply_turns SET receipt_json=?, operation_id=?, updated_at=?
                   WHERE agent_session_id=? AND agent_turn_id=? AND tenant_id<>'' AND operation_id IN ('', ?)""",
                (_json(receipt), operation_id, time.time(), str(event.session_id or ""), str(event.turn_id or ""), operation_id),
            )

    def _row_for_turn(self, agent_session_id: str, agent_turn_id: str) -> sqlite3.Row | None:
        with closing(self._connect()) as connection:
            return connection.execute(
                "SELECT * FROM direct_reply_turns WHERE agent_session_id=? AND agent_turn_id=?",
                (agent_session_id, agent_turn_id),
            ).fetchone()

    def _row_for_turn_aliases(
        self,
        *,
        agent_session_id: str,
        agent_turn_id: str,
        trusted_turn_id: str = "",
    ) -> sqlite3.Row | None:
        """Resolve one direct turn from public-hook and Contract turn identities.

        Hermes may expose different opaque turn identifiers at different public
        lifecycle points.  ``trusted_turn_id`` is never supplied by the model
        or a client: it is the Runtime Contract's server-attested identifier
        for this same live turn.  We deliberately accept no session-only
        fallback here; ambiguity or a missing exact identity fails closed.
        """

        session = str(agent_session_id or "").strip()
        aliases = tuple(dict.fromkeys(
            value for value in (
                str(agent_turn_id or "").strip(),
                str(trusted_turn_id or "").strip(),
            ) if value
        ))
        if not session or not aliases:
            return None
        placeholders = ",".join("?" for _ in aliases)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM direct_reply_turns WHERE agent_session_id=? "
                f"AND agent_turn_id IN ({placeholders})",
                (session, *aliases),
            ).fetchall()
        # Multiple rows means two trusted turns would compete for one public
        # hook.  Do not choose a reply based on timing or message content.
        return rows[0] if len(rows) == 1 else None

    @staticmethod
    def _fact(row: sqlite3.Row) -> VerifiedBusinessFact | None:
        receipt_text = str(row["receipt_json"] or "")
        operation = str(row["operation_id"] or "")
        if not receipt_text or not operation:
            return None
        try:
            receipt = json.loads(receipt_text)
        except (TypeError, ValueError):
            return None
        if not isinstance(receipt, dict) or str(receipt.get("status") or "") != "completed":
            return None
        return VerifiedBusinessFact(
            operation_id=operation,
            tenant_id=str(row["tenant_id"]),
            receipt=receipt,
            writeback_verified=True,
            original_request_ref="direct-reply:" + sha256(str(row["direct_turn_id"]).encode("utf-8")).hexdigest()[:24],
            original_turn_id=str(row["original_turn_id"]),
        )

    def stage_terminal(
        self,
        *,
        agent_session_id: str,
        agent_turn_id: str,
        terminal_state: str,
        provider_succeeded: bool | None,
        final_reply_text: str,
        raw_trace_ref: str,
    ) -> ReplyJob | None:
        """Write agent truth and a held reply job after verified business truth."""

        session = str(agent_session_id or "")
        turn = str(agent_turn_id or "")
        if not session or not turn:
            return None
        row = self._row_for_turn(session, turn)
        if row is None:
            return None
        fact = self._fact(row)
        if fact is None:
            return None
        partition = TrustedPartition(**json.loads(str(row["partition_json"])))
        destination = ReplyDestination(**json.loads(str(row["destination_json"])))
        job = self.outbox.observe_agent_terminal(
            partition=partition,
            destination=destination,
            agent_attempt_id="direct-terminal:" + str(row["direct_turn_id"]),
            terminal_state=str(terminal_state),
            provider_succeeded=provider_succeeded,
            raw_trace_ref=str(raw_trace_ref or "direct-terminal:" + str(row["direct_turn_id"])),
            business=fact,
            final_reply_text=str(final_reply_text or ""),
            delivery_hold=True,
        )
        if job is not None:
            with closing(self._connect()) as connection:
                connection.execute(
                    "UPDATE direct_reply_turns SET reply_id=?, updated_at=? WHERE direct_turn_id=?",
                    (job.reply_id, time.time(), str(row["direct_turn_id"])),
                )
        return job

    def stage_tool_delivery_artifact(
        self,
        *,
        agent_session_id: str,
        agent_turn_id: str,
        trusted_turn_id: str = "",
        tool_name: str,
        result: object,
    ) -> ReplyJob | None:
        """Hold a Tool-owned external delivery artifact for the same direct turn.

        This bridge is intentionally content-blind.  It neither reads the
        user's message nor selects a Tool: it accepts only a successful,
        versioned artifact that the Tool itself returned through Hermes'
        public post-tool hook.  Unlike a business Receipt it cannot create
        Business Truth or become eligible for reply-only generation.  The
        hold prevents the delivery worker racing ahead of the gateway marker.
        """

        session = str(agent_session_id or "").strip()
        turn = str(agent_turn_id or "").strip()
        trusted_turn = str(trusted_turn_id or "").strip()
        name = str(tool_name or "").strip()
        if not session or not (turn or trusted_turn) or not name:
            return None
        row = self._row_for_turn_aliases(
            agent_session_id=session,
            agent_turn_id=turn,
            trusted_turn_id=trusted_turn,
        )
        if row is None:
            return None
        try:
            parsed = json.loads(result) if isinstance(result, str) else dict(result or {})
        except (TypeError, ValueError):
            return None
        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            return None
        data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
        artifact = data.get("delivery_artifact") if isinstance(data.get("delivery_artifact"), dict) else {}
        if str(artifact.get("version") or "") != "xiaoyou.delivery-artifact.v1":
            return None
        if str(artifact.get("kind") or "") != "reply_text":
            return None
        text = str(artifact.get("text") or "").strip()
        # The artifact is not an alternate answer.  It must be byte-for-byte
        # the Tool's normal verified render before it can cross a channel.
        if not text or text != str(data.get("rendered_text") or "").strip():
            raise WorkRuntimeRejected("tool_delivery_artifact_text_mismatch")
        partition = TrustedPartition(**json.loads(str(row["partition_json"])))
        destination = ReplyDestination(**json.loads(str(row["destination_json"])))
        digest = sha256(text.encode("utf-8")).hexdigest()[:32]
        operation_id = "tool-delivery-artifact:" + str(row["direct_turn_id"]) + ":" + name + ":" + digest
        job = self.outbox.observe_agent_notice(
            notice_id=operation_id,
            partition=partition,
            destination=destination,
            agent_attempt_id="direct-tool-artifact:" + str(row["direct_turn_id"]) + ":" + name,
            terminal_state="completed",
            provider_succeeded=True,
            raw_trace_ref="public-post-tool-artifact:" + session + ":" + name,
            final_reply_text=text,
            delivery_hold=True,
        )
        if job is not None:
            with closing(self._connect()) as connection:
                connection.execute(
                    "UPDATE direct_reply_turns SET reply_id=?, updated_at=? WHERE direct_turn_id=?",
                    (job.reply_id, time.time(), str(row["direct_turn_id"])),
                )
        return job

    def stage_proactive_notice(
        self,
        *,
        tenant_id: str,
        recipient_id: str,
        source_kind: str,
        notice_id: str,
        notice_text: str,
        trace_ref: str,
    ) -> ReplyJob:
        """Durably stage one server-attested proactive notice.

        This is not a second Agent or a reply generator.  It accepts only a
        notice already produced by a selected, permission-checked XiaoYou
        Tool contract and binds it to the Tool's pre-resolved recipient.  The
        normal Work Runtime delivery worker still owns the only channel send
        and records whether WeCom actually accepted it.
        """

        tenant = str(tenant_id or "").strip()
        recipient = str(recipient_id or "").strip()
        kind = str(source_kind or "").strip()
        key = str(notice_id or "").strip()
        text = str(notice_text or "").strip()
        if kind not in _PROACTIVE_NOTICE_KINDS:
            raise WorkRuntimeRejected("proactive_notice_source_kind_denied")
        if not all((tenant, recipient, key, text, str(trace_ref or "").strip())):
            raise WorkRuntimeRejected("proactive_notice_required_fields_missing")
        source_identity = kind + ":" + tenant
        material = "\x1f".join((tenant, kind, recipient, key))
        partition = TrustedPartition(
            partition_id="proactive_" + sha256(material.encode("utf-8")).hexdigest()[:32],
            tenant_id=tenant,
            canonical_actor_id="service:" + kind + ":" + tenant,
            principal_kind="service",
            trusted_session_id=kind + ":" + key,
            continuity_id=None,
            source_identity=source_identity,
        )
        destination = ReplyDestination(
            tenant_id=tenant,
            channel="wecom_callback",
            recipient_id=recipient,
            source_identity=source_identity,
        )
        operation_id = "proactive-notice:" + kind + ":" + tenant + ":" + key
        return self.outbox.observe_agent_notice(
            notice_id=operation_id,
            partition=partition,
            destination=destination,
            agent_attempt_id="proactive-notice:" + sha256(material.encode("utf-8")).hexdigest()[:32],
            terminal_state="completed",
            provider_succeeded=None,
            raw_trace_ref=str(trace_ref),
            final_reply_text=text,
            delivery_hold=False,
        )

    def tool_delivery_artifact_handoff(
        self,
        *,
        agent_session_id: str,
        agent_turn_id: str,
        trusted_turn_id: str = "",
    ) -> ReplyJob | None:
        """Return only a held Tool-delivery job for one exact trusted turn."""

        row = self._row_for_turn_aliases(
            agent_session_id=str(agent_session_id or ""),
            agent_turn_id=str(agent_turn_id or ""),
            trusted_turn_id=str(trusted_turn_id or ""),
        )
        if row is None:
            return None
        reply_id = str(row["reply_id"] or "")
        job = self.outbox.get_by_reply_id(reply_id)
        if job is None or not job.delivery_hold:
            return None
        if not str(job.operation_id).startswith("tool-delivery-artifact:"):
            return None
        return job

    def stage_agenda_ticket_terminal(
        self,
        *,
        ticket_id: str,
        terminal_state: str,
        provider_succeeded: bool | None,
        final_reply_text: str,
        raw_trace_ref: str,
    ) -> ReplyJob | None:
        """Stage a completed Agenda service turn into the shared outbox.

        A verified Tool receipt becomes normal Business Truth and may use the
        established same-Hermes, zero-Tool recovery path. A reply without a
        receipt is only an Agent notice: it is deliverable if Hermes produced
        it, but it cannot claim business success and cannot be regenerated.
        """

        ticket = str(ticket_id or "").strip()
        if not ticket:
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT t.* FROM direct_reply_turns t
                   JOIN agenda_reply_ticket_links l ON l.direct_turn_id=t.direct_turn_id
                   WHERE l.ticket_id=?""",
                (ticket,),
            ).fetchone()
        if row is None:
            return None
        partition = TrustedPartition(**json.loads(str(row["partition_json"])))
        destination = ReplyDestination(**json.loads(str(row["destination_json"])))
        trace_ref = str(raw_trace_ref or "agenda-terminal:" + str(row["direct_turn_id"]))
        fact = self._fact(row)
        if fact is not None:
            job = self.outbox.observe_agent_terminal(
                partition=partition,
                destination=destination,
                agent_attempt_id="agenda-terminal:" + str(row["direct_turn_id"]),
                terminal_state=str(terminal_state),
                provider_succeeded=provider_succeeded,
                raw_trace_ref=trace_ref,
                business=fact,
                final_reply_text=str(final_reply_text or ""),
                delivery_hold=True,
            )
            if job is None:
                return None
            # A service turn has no direct channel callback that could consume
            # a control marker. The ticket and destination are already
            # server-attested, so release only this exact held reply job into
            # the existing durable delivery worker.
            # Gateway adapters can surface more than one terminal callback
            # for the same turn. Releasing is an outbox transition, so repeat
            # callbacks must observe the already-released job rather than
            # turn a harmless duplicate callback into a delivery failure.
            if job.delivery_hold:
                job = self.outbox.release_delivery_hold(
                    reply_id=job.reply_id,
                    operation_id=job.operation_id,
                    destination=job.destination,
                    now=time.time(),
                )
        else:
            job = self.outbox.observe_agent_notice(
                notice_id="agenda-notice:" + ticket,
                partition=partition,
                destination=destination,
                agent_attempt_id="agenda-terminal:" + str(row["direct_turn_id"]),
                terminal_state=str(terminal_state),
                provider_succeeded=provider_succeeded,
                raw_trace_ref=trace_ref,
                final_reply_text=str(final_reply_text or ""),
            )
        if job is not None:
            with closing(self._connect()) as connection:
                connection.execute(
                    "UPDATE direct_reply_turns SET reply_id=?, updated_at=? WHERE direct_turn_id=?",
                    (job.reply_id, time.time(), str(row["direct_turn_id"])),
                )
        return job

    def release_handoff(self, *, reply_id: str, channel: str, chat_id: str) -> ReplyJob:
        job = self.outbox.get_by_reply_id(reply_id)
        if job is None or job.destination.channel != str(channel) or job.destination.recipient_id != str(chat_id):
            raise WorkRuntimeRejected("direct_reply_handoff_mismatch")
        # Gateway callbacks may be replayed after the original handoff was
        # accepted.  The durable state, not a second remote send, is the
        # idempotency authority: an already-released or delivered job is a
        # successful observation of the same handoff.
        if not job.delivery_hold:
            return job
        return self.outbox.release_delivery_hold(
            reply_id=reply_id, operation_id=job.operation_id, destination=job.destination, now=time.time(),
        )

    def release_wecom_callback_handoff(self, *, reply_id: str) -> ReplyJob:
        """Release one marker produced by the authenticated WeCom turn bridge.

        Hermes' public outbound callback exposes an opaque Agent session, not
        the original WeCom userid.  The actual recipient was already fixed
        from the server-attested canonical actor when the held job was
        created.  This narrow method is therefore intentionally distinct from
        ``release_handoff``: it accepts no model/client recipient value and
        only permits a held WeCom job whose trusted source is itself WeCom.
        The marker can only be produced by the exact-turn transform bridge;
        arbitrary content never calls this method.
        """

        job = self.outbox.get_by_reply_id(str(reply_id))
        if job is None:
            raise WorkRuntimeRejected("direct_reply_handoff_unknown")
        if (
            job.destination.channel != "wecom_callback"
            or not job.destination.recipient_id
            or not job.destination.source_identity.startswith("wecom_callback:")
        ):
            raise WorkRuntimeRejected("direct_reply_wecom_handoff_mismatch")
        if not job.delivery_hold:
            return job
        return self.outbox.release_delivery_hold(
            reply_id=job.reply_id,
            operation_id=job.operation_id,
            destination=job.destination,
            now=time.time(),
        )

    def release_pending_operation(self, *, operation_id: str, channel: str, chat_id: str) -> ReplyJob | None:
        job = self.outbox.get(str(operation_id))
        if job is None or job.destination.channel != str(channel) or job.destination.recipient_id != str(chat_id):
            return None
        return job if not job.delivery_hold else self.release_handoff(
            reply_id=job.reply_id, channel=channel, chat_id=chat_id,
        )

    def release_robot_handoff(self, *, reply_id: str, operation_id: str, chat_id: str) -> ReplyJob:
        job = self.outbox.get_by_reply_id(reply_id)
        if job is None or job.operation_id != str(operation_id):
            raise WorkRuntimeRejected("direct_reply_robot_handoff_mismatch")
        return self.release_handoff(reply_id=reply_id, channel="robot_poc", chat_id=chat_id)

    def release_pending_robot_operation(self, *, operation_id: str, chat_id: str) -> ReplyJob | None:
        return self.release_pending_operation(operation_id=operation_id, channel="robot_poc", chat_id=chat_id)

    def claim_recovery(self, *, worker_id: str) -> tuple[ReplyJob, str, str] | None:
        """Lease one pending reply and return a factual same-Hermes prompt."""

        job = self.outbox.claim_generation(worker_id=worker_id, now=time.time())
        if job is None:
            return None
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM direct_reply_turns WHERE reply_id=?", (job.reply_id,)).fetchone()
            if row is None:
                self.outbox.complete_generation(
                    job, worker_id=worker_id,
                    terminal=HermesReplyTerminal("failed", False, ""), now=time.time(),
                )
                return None
            recovery_session = "reply-recovery:" + job.reply_id
            request = self.outbox.reply_recovery_request(job)
            prompt = request.render_for_same_hermes(str(row["original_text"]))
            connection.execute(
                """UPDATE direct_reply_turns SET recovery_session_id=?, recovery_state='leased', tool_attempted=0, updated_at=?
                   WHERE reply_id=?""",
                (recovery_session, time.time(), job.reply_id),
            )
        return job, recovery_session, prompt

    def recovery_prompt(self, *, recovery_session_id: str, sender_id: str) -> str | None:
        if str(sender_id) != RECOVERY_SENDER:
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM direct_reply_turns WHERE recovery_session_id=? AND recovery_state='leased'",
                (str(recovery_session_id),),
            ).fetchone()
        if row is None:
            return None
        job = self.outbox.get_by_reply_id(str(row["reply_id"]))
        if job is None:
            return None
        return self.outbox.reply_recovery_request(job).render_for_same_hermes(str(row["original_text"]))

    def is_recovery_session(self, session_id: str) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT 1 FROM direct_reply_turns WHERE recovery_session_id=? AND recovery_state='leased'", (str(session_id),)).fetchone()
            return row is not None

    def note_tool_attempt(self, *, recovery_session_id: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute("UPDATE direct_reply_turns SET tool_attempted=1, updated_at=? WHERE recovery_session_id=?", (time.time(), str(recovery_session_id)))

    def finish_recovery(self, *, recovery_session_id: str, reply_text: str, worker_id: str) -> ReplyJob:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM direct_reply_turns WHERE recovery_session_id=?", (str(recovery_session_id),)).fetchone()
        if row is None:
            raise WorkRuntimeRejected("reply_recovery_session_unknown")
        job = self.outbox.get_by_reply_id(str(row["reply_id"]))
        if job is None:
            raise WorkRuntimeRejected("reply_recovery_job_unknown")
        selected = ("blocked_tool_attempt",) if bool(row["tool_attempted"]) else ()
        completed = self.outbox.complete_generation(
            job,
            worker_id=worker_id,
            terminal=HermesReplyTerminal("completed", True, str(reply_text or ""), selected_tools=selected),
            now=time.time(),
        )
        with closing(self._connect()) as connection:
            connection.execute("UPDATE direct_reply_turns SET recovery_state='generated', updated_at=? WHERE recovery_session_id=?", (time.time(), str(recovery_session_id)))
        return completed

    def fail_recovery(self, *, recovery_session_id: str, worker_id: str, reason: str) -> None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM direct_reply_turns WHERE recovery_session_id=?", (str(recovery_session_id),)).fetchone()
        if row is None:
            return
        job = self.outbox.get_by_reply_id(str(row["reply_id"]))
        if job is not None:
            self.outbox.complete_generation(
                job, worker_id=worker_id,
                terminal=HermesReplyTerminal("failed", False, ""), now=time.time(),
            )
        with closing(self._connect()) as connection:
            connection.execute("UPDATE direct_reply_turns SET recovery_state='generation_failed', updated_at=? WHERE recovery_session_id=?", (time.time(), str(recovery_session_id)))

    def register_delivery_adapter(self, *, channel: str, adapter: object) -> None:
        self.register_delivery_port(
            AdapterReplyChannelPort(channel=str(channel), adapter=adapter),
        )

    def register_delivery_port(self, port: TrustedReplyChannelPort) -> None:
        """Register one server-configured outbox transport port.

        The port does not originate ingress and the model cannot select it:
        the durable destination's fixed channel chooses the matching port.
        """

        normalized_channel = str(getattr(port, "channel", "") or "")
        if normalized_channel not in _DIRECT_CHANNELS:
            raise WorkRuntimeRejected("direct_reply_channel_not_supported")
        self._ports[normalized_channel] = port
        # A channel port is a server-owned transport fact.  Once it becomes
        # available, resume only replies that were locally parked because no
        # such port existed; do not touch remote failures or unknown delivery.
        requeued = self.outbox.requeue_blocked_channel(channel=normalized_channel, now=time.time())
        logger.info(
            "xiaoyou_durable_reply_port_registered channel=%s workspace=%s requeued_blocked=%s",
            normalized_channel,
            self.outbox.database_path,
            requeued,
        )

    async def deliver_next(self, *, worker_id: str) -> object | None:
        dispatcher = DurableReplyDeliveryDispatcher(outbox=self.outbox, ports=self._ports)
        return await dispatcher.deliver_next(worker_id=worker_id, now=time.time())

    def verify_delivery(self, *, delivery_id: str, channel: str, chat_id: str, reply_text: str) -> ReplyJob:
        job = self.outbox.get_by_delivery_id(str(delivery_id))
        if job is None or job.destination.channel != str(channel) or job.destination.recipient_id != str(chat_id):
            raise WorkRuntimeRejected("reply_delivery_destination_mismatch")
        if job.generation_state != "generated" or not job.reply_text.strip() or job.reply_text != str(reply_text):
            raise WorkRuntimeRejected("reply_delivery_truth_mismatch")
        return job

    def verify_robot_delivery(self, *, delivery_id: str, chat_id: str, reply_text: str) -> ReplyJob:
        return self.verify_delivery(delivery_id=delivery_id, channel="robot_poc", chat_id=chat_id, reply_text=reply_text)


def get_direct_reply_recovery_manager() -> DirectReplyRecoveryManager:
    """Return the process-wide XiaoYou manager for this Institution Workspace.

    Hermes may load independently versioned public plugins under different
    module names.  A module-local singleton would then split the outbox's
    durable facts from its in-memory channel ports.  The registry below is
    Python-process infrastructure owned by XiaoYou, keyed by the product
    Workspace; it neither reads Hermes internals nor changes Agent behaviour.
    """

    root = _workspace_root().resolve()
    key = str(root)
    lock = getattr(builtins, "_xiaoyou_direct_reply_manager_lock", None)
    if lock is None:
        lock = threading.RLock()
        setattr(builtins, "_xiaoyou_direct_reply_manager_lock", lock)
    with lock:
        managers = getattr(builtins, "_xiaoyou_direct_reply_managers", None)
        if not isinstance(managers, dict):
            managers = {}
            setattr(builtins, "_xiaoyou_direct_reply_managers", managers)
        manager = managers.get(key)
        # Do not use ``isinstance`` here: the same public capability source
        # may legitimately have two Python module identities when Hermes loads
        # plugins.  The workspace key, rather than a module-private class
        # identity, is the authority boundary for this process-local bridge.
        if manager is None:
            manager = DirectReplyRecoveryManager(root=root)
            managers[key] = manager
        return manager
