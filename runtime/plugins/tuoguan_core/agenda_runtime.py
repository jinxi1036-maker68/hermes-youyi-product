"""Current-Workspace Agenda bridge for XiaoYou's public Work Runtime.

This module is deliberately a scheduler and durable transport boundary.  It
reads only server-owned work-item state/time fields, coalesces attested facts
through :mod:`work_runtime`, and issues opaque tickets for the public
``agenda_service_work`` platform adapter.  It never reads natural-language
payloads to classify work, chooses a Tool, creates a business reply, or
declares a business operation successful.

The Agent loop, Permission, CommandBus, Repository, ExecutionReceipt and
writeback verification remain outside this module.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid
from typing import Any

from .digital_employee_state import query_hermes_work_items
from .models import UserIdentity
from .store import TuoguanStore
from .tenant_context import current_tenant_id
from .work_runtime import (
    DurableWorkInbox,
    ReplyDestination,
    TrustedPartition,
    TrustedPrincipal,
    TrustedWorkAuthority,
    TrustedWorkBinding,
    WorkEnvelope,
    WorkFact,
    WorkLeaseLost,
    WorkRuntimeRejected,
)


AGENDA_DATABASE_FILENAME = "agenda_work_runtime.sqlite"
SERVICE_PREFIX = "service:agenda:"
SOURCE_PREFIX = "agenda:"
WORKER_PREFIX = "agenda-wake"
DEFAULT_INTERVAL_SECONDS = 20.0
DEFAULT_LEASE_SECONDS = 300.0
TICKET_TTL_SECONDS = 1800.0
# Bump only when the public Agent ingress contract changes.  The transition to
# v4 establishes the trusted Agenda turn before Hermes can emit a tool-less
# terminal response.  v5 additionally waits for that public source-scoped
# Tool surface to be registered before consuming a durable ticket, so an
# already-open fact is entitled to one fresh,
# server-issued delivery attempt.  It is not a business-priority change and
# does not inspect task prose.
# v8 adds the non-terminal continuation contract. It never chooses a business
# priority, recipient, wording or cadence. It merely turns an open task that
# left an Agenda turn without a model-selected successor into a durable,
# content-free fact that Hermes must consider on a later turn.
TASK_AGENDA_CONTRACT_VERSION = "8"
GOVERNANCE_AGENDA_CONTRACT_VERSION = "1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_time(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _data_root(value: str | Path | None = None) -> Path:
    raw = str(value or os.getenv("HERMES_TUOGUAN_DATA_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    workspace = str(os.getenv("XIAOYOU_INSTITUTION_WORKSPACE") or "").strip()
    if workspace:
        candidate = Path(workspace).expanduser().resolve()
        return candidate / "data" if (candidate / "data").is_dir() else candidate
    raise RuntimeError("agenda_workspace_data_dir_missing")


def _candidate_reason(item: dict[str, object], *, now: float) -> str | None:
    """Return a structural wake reason without inspecting business text."""

    status = str(item.get("status") or "").strip()
    if status not in {"active", "waiting", "blocked"}:
        return None
    next_attention = _parse_time(item.get("next_attention_at"))
    if next_attention is not None and next_attention <= now:
        return "attention_due"
    next_contact = _parse_time(item.get("next_contact_after"))
    if status == "waiting" and next_contact is not None and next_contact <= now:
        return "waiting_recheck_due"
    waiting = item.get("current_waiting")
    if isinstance(waiting, dict):
        for key in ("next_attention_at", "next_check_at", "deadline_at"):
            candidate = _parse_time(waiting.get(key))
            if candidate is not None and candidate <= now:
                return "waiting_state_due"
    return None


def _task_candidate_reason(task: dict[str, object], *, now: float) -> str | None:
    """Observe open task lifecycle only; never classify its text or priority.

    An open assigned task that has never been represented in the current Work
    Runtime is a factual handoff gap.  Agenda may wake Hermes with that fact,
    but Hermes alone decides whether it warrants a question, a reminder, a
    state change, or silence.
    """

    status = str(task.get("status") or "pending").strip().lower()
    if status in {"completed", "closed", "cancelled", "superseded", "archived"}:
        return None
    assignee = str(task.get("assignee_userid") or "").strip()
    if not assignee:
        return None
    # A terminal Agent reply must not consume an open task by itself. When the
    # terminal bridge observed that no successor attention condition exists, it
    # records this content-free integrity fact. Agenda may wake Hermes to choose
    # a successor but never chooses the time, recipient, or business action.
    continuation = task.get("agenda_continuation")
    if isinstance(continuation, dict) and str(continuation.get("state") or "") == "required":
        return "task_continuation_required"
    # A prior Hermes service turn may have explicitly scheduled another
    # structural review. Until that time the fact is intentionally quiet;
    # after it, a new source version reaches Hermes so the model can choose to
    # continue, ask again, or remain silent. Agenda never derives a cadence
    # from task prose or decides that a reminder is worthwhile.
    followup = task.get("agenda_followup")
    if isinstance(followup, dict) and str(followup.get("state") or "") == "scheduled":
        next_attention = _parse_time(followup.get("next_attention_at"))
        if next_attention is not None:
            if next_attention <= now:
                return "task_followup_due"
            return None
    due = _parse_time(task.get("due_at"))
    if due is not None and due <= now:
        return "task_due"
    return "task_open_unobserved"


def _source_version(item: dict[str, object], *, reason: str) -> str:
    """Fingerprint only trusted lifecycle/time material, never payload prose."""

    waiting = item.get("current_waiting") if isinstance(item.get("current_waiting"), dict) else {}
    material = {
        "work_item_id": str(item.get("work_item_id") or ""),
        "status": str(item.get("status") or ""),
        "updated_at": str(item.get("updated_at") or ""),
        "next_attention_at": str(item.get("next_attention_at") or ""),
        "next_contact_after": str(item.get("next_contact_after") or ""),
        "waiting_times": {
            key: str(waiting.get(key) or "")
            for key in ("next_attention_at", "next_check_at", "deadline_at")
        },
        "reason": str(reason),
    }
    return "state_" + sha256(_json(material).encode("utf-8")).hexdigest()[:24]


def _task_source_version(task: dict[str, object], *, reason: str) -> str:
    followup = task.get("agenda_followup") if isinstance(task.get("agenda_followup"), dict) else {}
    continuation = task.get("agenda_continuation") if isinstance(task.get("agenda_continuation"), dict) else {}
    material = {
        "agenda_contract_version": TASK_AGENDA_CONTRACT_VERSION,
        "task_id": str(task.get("id") or ""),
        "status": str(task.get("status") or ""),
        "assignee_userid": str(task.get("assignee_userid") or ""),
        "due_at": str(task.get("due_at") or ""),
        "created_at": str(task.get("created_at") or ""),
        "agenda_followup_state": str(followup.get("state") or ""),
        "agenda_followup_at": str(followup.get("next_attention_at") or ""),
        "agenda_followup_operation": str(followup.get("operation_id") or ""),
        "agenda_followup_ticket": str(followup.get("ticket_id") or ""),
        "agenda_continuation_state": str(continuation.get("state") or ""),
        "agenda_continuation_ticket": str(continuation.get("ticket_id") or ""),
        "agenda_continuation_required_at": str(continuation.get("required_at") or ""),
        "reason": str(reason),
    }
    return "task_" + sha256(_json(material).encode("utf-8")).hexdigest()[:24]


def _governance_event_id(fact: dict[str, object]) -> str:
    """Return the stable identity of one already-authoritative governance fact."""

    for key in ("claim_id", "handover_id", "attention_id"):
        value = str(fact.get(key) or "").strip()
        if value:
            target = str(fact.get("target_relation_id") or "").strip()
            return value + (":" + target if target else "")
    return ""


def _governance_source_version(fact: dict[str, object]) -> str:
    """Fingerprint only lifecycle/identity fields; never payload prose."""

    material = {
        "agenda_contract_version": GOVERNANCE_AGENDA_CONTRACT_VERSION,
        "kind": str(fact.get("kind") or ""),
        "claim_id": str(fact.get("claim_id") or ""),
        "handover_id": str(fact.get("handover_id") or ""),
        "attention_id": str(fact.get("attention_id") or ""),
        "target_relation_id": str(fact.get("target_relation_id") or ""),
        "state": str(fact.get("state") or ""),
        "updated_at": str(fact.get("updated_at") or ""),
        "source_version": str(fact.get("source_version") or ""),
    }
    return "governance_" + sha256(_json(material).encode("utf-8")).hexdigest()[:24]


class CurrentWorkspaceAgenda:
    """One durable, content-blind Agenda/Wake coordinator for one tenant."""

    def __init__(
        self,
        *,
        data_dir: str | Path | None = None,
        tenant_id: str = "",
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self.data_dir = _data_root(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.tenant_id = str(tenant_id or current_tenant_id()).strip()
        if not self.tenant_id:
            raise ValueError("agenda_tenant_required")
        self.database_path = self.data_dir / AGENDA_DATABASE_FILENAME
        self.worker_id = WORKER_PREFIX + ":" + self.tenant_id
        self.lease_seconds = max(60.0, float(lease_seconds))
        self.authority = TrustedWorkAuthority(self.database_path)
        self.inbox = DurableWorkInbox(self.database_path, debounce_seconds=2.5, lease_seconds=self.lease_seconds)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS agenda_workspace_payloads (
                    payload_ref TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    work_item_id TEXT NOT NULL,
                    source_version TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    observed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agenda_service_tickets (
                    ticket_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    service_identity TEXT NOT NULL,
                    source_identity TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    message_id TEXT NOT NULL UNIQUE,
                    model_message TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    dispatched_at TEXT NOT NULL DEFAULT '',
                    agent_claimed_at TEXT NOT NULL DEFAULT '',
                    agent_session_id TEXT NOT NULL DEFAULT '',
                    agent_turn_id TEXT NOT NULL DEFAULT '',
                    reply_text TEXT NOT NULL DEFAULT '',
                    delivery_state TEXT NOT NULL DEFAULT '',
                    completed_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    partition_json TEXT NOT NULL,
                    destination_json TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    lease_until REAL NOT NULL,
                    work_ids_json TEXT NOT NULL,
                    terminal_kind TEXT NOT NULL DEFAULT '',
                    inbox_ack_state TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS agenda_service_ticket_state_idx
                    ON agenda_service_tickets(state, expires_at, updated_at);
                CREATE INDEX IF NOT EXISTS agenda_service_ticket_batch_idx
                    ON agenda_service_tickets(batch_id, inbox_ack_state);
                CREATE TABLE IF NOT EXISTS agenda_runtime_audit (
                    event_id TEXT PRIMARY KEY,
                    observed_at REAL NOT NULL,
                    event_type TEXT NOT NULL,
                    ticket_id TEXT NOT NULL DEFAULT '',
                    work_id TEXT NOT NULL DEFAULT '',
                    detail_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agenda_runtime_status (
                    tenant_id TEXT PRIMARY KEY,
                    started_at REAL NOT NULL,
                    last_cycle_at REAL NOT NULL,
                    cycle_count INTEGER NOT NULL,
                    last_observed INTEGER NOT NULL,
                    last_admitted INTEGER NOT NULL,
                    last_duplicates INTEGER NOT NULL,
                    last_issued_ticket_id TEXT NOT NULL DEFAULT '',
                    last_error_class TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS agenda_runtime_daily_status (
                    tenant_id TEXT NOT NULL,
                    local_date TEXT NOT NULL,
                    first_cycle_at REAL NOT NULL,
                    last_cycle_at REAL NOT NULL,
                    cycle_count INTEGER NOT NULL,
                    observed_facts INTEGER NOT NULL,
                    admitted_facts INTEGER NOT NULL,
                    duplicate_facts INTEGER NOT NULL,
                    issued_ticket_count INTEGER NOT NULL,
                    PRIMARY KEY(tenant_id, local_date)
                );
                """
            )

    def _audit(self, event_type: str, *, ticket_id: str = "", work_id: str = "", detail: dict[str, object] | None = None) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO agenda_runtime_audit(event_id,observed_at,event_type,ticket_id,work_id,detail_json) VALUES(?,?,?,?,?,?)",
                (
                    "agenda_audit_" + uuid.uuid4().hex,
                    time.time(),
                    str(event_type),
                    str(ticket_id),
                    str(work_id),
                    _json(detail or {}),
                ),
            )

    def _boss_destination(self) -> ReplyDestination:
        """Derive exactly one owner destination from the authoritative directory."""

        whitelist = TuoguanStore(self.data_dir).read_json("wecom_whitelist.json", {})
        if not isinstance(whitelist, dict):
            raise WorkRuntimeRejected("agenda_owner_directory_invalid")
        owners = [str(value).strip() for value in (whitelist.get("super_users") or []) if str(value).strip()]
        allowed = {str(value).strip() for value in (whitelist.get("allowed_users") or []) if str(value).strip()}
        if len(owners) != 1 or owners[0] not in allowed:
            raise WorkRuntimeRejected("agenda_owner_destination_not_unique")
        source = SOURCE_PREFIX + self.tenant_id
        return ReplyDestination(
            tenant_id=self.tenant_id,
            channel="wecom_callback",
            recipient_id=owners[0],
            source_identity=source,
        )

    def _trusted_task_destination(self, recipient_id: str) -> ReplyDestination:
        """Bind a task's already-attested assignee to a current directory entry.

        This is identity routing, not a model decision about who should work on
        a task.  An absent, pending, rejected or offboarded identity fails
        closed and the task remains observable for a later, trusted handoff.
        """

        recipient = str(recipient_id or "").strip()
        whitelist = TuoguanStore(self.data_dir).read_json("wecom_whitelist.json", {})
        if not isinstance(whitelist, dict):
            raise WorkRuntimeRejected("agenda_recipient_directory_invalid")
        allowed = {str(value).strip() for value in (whitelist.get("allowed_users") or []) if str(value).strip()}
        contacts = whitelist.get("wecom_contacts") if isinstance(whitelist.get("wecom_contacts"), dict) else {}
        pending = {str(value).strip() for value in (whitelist.get("pending_users") or []) if str(value).strip()}
        rejected = {str(value).strip() for value in (whitelist.get("rejected_users") or []) if str(value).strip()}
        offboarded: set[str] = set()
        for row in whitelist.get("offboarded_users") or []:
            if isinstance(row, str):
                offboarded.add(row.strip())
            elif isinstance(row, dict):
                offboarded.update(str(row.get(key) or "").strip() for key in ("user_id", "userid", "canonical_user_id", "id"))
        owners = {str(value).strip() for value in (whitelist.get("super_users") or []) if str(value).strip()}
        roles = whitelist.get("user_roles") if isinstance(whitelist.get("user_roles"), dict) else {}
        effective_role = "boss" if recipient in owners else str(roles.get(recipient) or "").strip().lower()
        if (
            not recipient or recipient not in allowed or recipient not in contacts
            or recipient in pending or recipient in rejected or recipient in offboarded
            or effective_role not in {"boss", "manager", "teacher"}
        ):
            raise WorkRuntimeRejected("agenda_task_recipient_not_current_trusted_contact")
        return ReplyDestination(
            tenant_id=self.tenant_id,
            channel="wecom_callback",
            recipient_id=recipient,
            source_identity=SOURCE_PREFIX + self.tenant_id,
        )

    def _destination_for_fact(self, fact: WorkFact | dict[str, object]) -> ReplyDestination:
        payload_ref = str(fact.payload_ref) if isinstance(fact, WorkFact) else str(fact.get("payload_ref") or "")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM agenda_workspace_payloads WHERE payload_ref=? AND tenant_id=?",
                (payload_ref, self.tenant_id),
            ).fetchone()
        if row is None:
            raise WorkRuntimeRejected("agenda_payload_reference_missing")
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise WorkRuntimeRejected("agenda_payload_invalid")
        if str(payload.get("source_kind") or "") == "workspace_task":
            return self._trusted_task_destination(str(payload.get("assignee_userid") or ""))
        if str(payload.get("source_kind") or "") == "personnel_service_governance":
            # Cross-service attention has an already-confirmed target service
            # relation. Binding its recipient to that relation's current
            # responsible identity is a trusted data-scope decision, not a
            # payload interpretation or model-selected recipient. Other
            # governance facts require an institution-level decision and go
            # only to the authoritative owner destination.
            recipient = str(payload.get("recipient_user_id") or "").strip()
            if recipient:
                return self._trusted_task_destination(recipient)
            return self._boss_destination()
        return self._boss_destination()

    def _governance_recipient(self, fact: dict[str, object]) -> str:
        """Resolve a declared attention target from authority state only.

        Missing/ambiguous relationships intentionally return an empty value;
        callers fail closed rather than guessing a teacher from a name or old
        ``teacher`` field.
        """

        if str(fact.get("kind") or "") != "cross_service_attention_open":
            return ""
        relation_id = str(fact.get("target_relation_id") or "").strip()
        if not relation_id:
            return ""
        try:
            from .personnel_service_governance_v1 import PersonnelServiceGovernance

            snapshot = PersonnelServiceGovernance(TuoguanStore(self.data_dir)).snapshot()
            matches = [
                row for row in (snapshot.get("service_relations") or [])
                if isinstance(row, dict)
                and str(row.get("tenant_id") or "") == self.tenant_id
                and str(row.get("relation_id") or "") == relation_id
                and str(row.get("state") or "") == "active"
                and str(row.get("assignee_user_id") or "").strip()
            ]
            return str(matches[0].get("assignee_user_id") or "") if len(matches) == 1 else ""
        except Exception:
            return ""

    def _governance_agenda_facts(self, store: TuoguanStore) -> list[dict[str, object]]:
        """Read only authoritative governance lifecycle facts for Agenda.

        Confirmed aggregate facts and claim states are lifecycle material, not
        message text. Agenda records them durably but does not rank them,
        decide their business meaning, or select a Tool.
        """

        facts: list[dict[str, object]] = []
        try:
            from .personnel_service_governance_v1 import PersonnelServiceGovernance
            from .governance_claims_v1 import GovernanceClaimService

            governance = PersonnelServiceGovernance(store)
            # This performs only durable follower cleanup born from an
            # already verified identity activation.  It neither ranks work nor
            # creates an Agenda fact; a temporary failure is intentionally
            # contained below so stale predecessor Claims cannot become a
            # false request for owner confirmation.
            claim_service = GovernanceClaimService(
                governance=governance,
                legacy_data_dir=store.data_dir,
            )
            claim_service.reconcile_pending_identity_claims(tenant_id=self.tenant_id)
            pending_reconciliations = governance.pending_identity_claim_reconciliations(tenant_id=self.tenant_id)

            for raw in governance.agenda_facts(tenant_id=self.tenant_id):
                if isinstance(raw, dict):
                    facts.append(dict(raw))
        except Exception:
            self._audit("governance_fact_source_unavailable", detail={})
            return facts
        claims = store.read_json("governance_claims_v1.json", {})
        rows = claims.get("claims") if isinstance(claims, dict) else {}
        if isinstance(rows, dict):
            for claim in rows.values():
                if not isinstance(claim, dict) or str(claim.get("tenant_id") or "") != self.tenant_id:
                    continue
                state = str(claim.get("state") or "")
                if state not in {"awaiting_confirmation", "awaiting_boss_identity_activation", "conflicted"}:
                    continue
                # A pending reconciliation task is itself proof that the
                # authoritative identity transition has completed.  While its
                # follower Claim write is retrying, never turn that already
                # superseded predecessor into a new Agenda ticket.  The
                # structural comparison is confined to userid/role/campus and
                # predecessor time; it does not inspect payload prose or make
                # a business decision.
                if any(
                    claim_service._is_replaced_by_pending_identity_activation(
                        claim,
                        tenant_id=self.tenant_id,
                        staff_user_id=str(task.get("staff_user_id") or ""),
                        role=str(task.get("role") or ""),
                        campus_id=str(task.get("campus_id") or ""),
                        source_cutoff_at=str(task.get("source_cutoff_at") or ""),
                    )
                    for task in pending_reconciliations
                ):
                    continue
                facts.append({
                    "kind": "governance_claim_" + state,
                    "claim_id": str(claim.get("claim_id") or ""),
                    "state": state,
                    "updated_at": str(claim.get("updated_at") or claim.get("created_at") or ""),
                    "source_version": sha256(_json({
                        "state": state,
                        "updated_at": str(claim.get("updated_at") or ""),
                        "conflicts": list(claim.get("conflicts") or []),
                    }).encode("utf-8")).hexdigest()[:24],
                })
        return facts

    def _service_identity(self) -> UserIdentity:
        actor = SERVICE_PREFIX + self.tenant_id
        return UserIdentity(
            platform="agenda_service_work",
            platform_user_id=actor,
            canonical_user_id=actor,
            person_name="小优 Agenda 服务",
            role="agenda_service",
            approval_state="approved",
        )

    def _binding(self, fact: WorkFact) -> TrustedWorkBinding:
        actor = SERVICE_PREFIX + self.tenant_id
        source = SOURCE_PREFIX + self.tenant_id
        # A service identity can own several unrelated durable work facts.
        # Partition each trusted Workspace *state version*, not merely the
        # whole institution or a long-lived object id.  A later state fact is
        # still tied to the same object in the Workspace, but it must not
        # inherit an Agent transcript generated before a verified update or a
        # repaired scheduler configuration.  This remains content-blind: the
        # version fingerprints only lifecycle/time fields above.
        work_session = (
            "agenda-work:" + self.tenant_id + ":" + str(fact.source_event_id)
            + ":" + str(fact.source_version)
        )
        return TrustedWorkBinding(
            work_id=fact.work_id,
            tenant_id=self.tenant_id,
            source_kind=fact.source_kind,
            source_event_id=fact.source_event_id,
            source_version=fact.source_version,
            payload_ref=fact.payload_ref,
            trusted_actor_ref=fact.trusted_actor_ref,
            source_identity=source,
            principal=TrustedPrincipal(canonical_actor_id=actor, role="agenda_service", kind="service"),
            authenticated_session_id=work_session,
            continuity_id=work_session,
            destination=self._destination_for_fact(fact),
        )

    def scan_workspace(self, *, now: float | None = None) -> dict[str, int]:
        """Observe eligible work-item state and persist attested WorkFacts."""

        timestamp = time.time() if now is None else float(now)
        store = TuoguanStore(self.data_dir)
        identity = self._service_identity()
        observed = 0
        admitted = 0
        duplicates = 0
        items = query_hermes_work_items(store, identity=identity, include_closed=False, limit=200)
        for raw in items.get("items") or []:
            if not isinstance(raw, dict):
                continue
            reason = _candidate_reason(raw, now=timestamp)
            if reason is None:
                continue
            item_id = str(raw.get("work_item_id") or "").strip()
            if not item_id:
                continue
            observed += 1
            version = _source_version(raw, reason=reason)
            payload_ref = "agenda-work:" + item_id + ":" + version
            fact = WorkFact(
                tenant_id=self.tenant_id,
                source_kind="workspace_work_item",
                source_event_id=item_id,
                source_version=version,
                created_at=timestamp,
                payload_ref=payload_ref,
                trusted_actor_ref=SERVICE_PREFIX + self.tenant_id,
                trace_id="agenda-scan:" + sha256((item_id + version).encode("utf-8")).hexdigest()[:24],
                correlation_id="agenda-work-item:" + item_id,
            )
            payload = {
                "work_item_id": item_id,
                "status": str(raw.get("status") or ""),
                "wake_reason": reason,
                "next_attention_at": str(raw.get("next_attention_at") or ""),
                "next_contact_after": str(raw.get("next_contact_after") or ""),
                "source_version": version,
            }
            with closing(self._connect()) as connection:
                connection.execute(
                    """INSERT OR IGNORE INTO agenda_workspace_payloads(payload_ref,tenant_id,work_item_id,source_version,payload_json,observed_at)
                       VALUES(?,?,?,?,?,?)""",
                    (payload_ref, self.tenant_id, item_id, version, _json(payload), timestamp),
                )
            self.authority.attest(fact, self._binding(fact))
            duplicate, work_id, _partition = self.inbox.ingest(fact, authority=self.authority, now=timestamp)
            if duplicate:
                duplicates += 1
            else:
                admitted += 1
                self._audit("work_fact_admitted", work_id=work_id, detail={"reason": reason, "item_id": item_id})
        # Current task state is a separate Workspace source from legacy
        # notification queues.  Its open/due lifecycle is enough to produce a
        # work fact; content is stored only as an attested payload for Hermes,
        # never inspected here to decide whether a task matters.
        for raw_task in store.load_tasks():
            if not isinstance(raw_task, dict):
                continue
            task_id = str(raw_task.get("id") or "").strip()
            reason = _task_candidate_reason(raw_task, now=timestamp)
            if not task_id or reason is None:
                continue
            observed += 1
            version = _task_source_version(raw_task, reason=reason)
            payload_ref = "agenda-task:" + task_id + ":" + version
            fact = WorkFact(
                tenant_id=self.tenant_id,
                source_kind="workspace_task",
                source_event_id=task_id,
                source_version=version,
                created_at=timestamp,
                payload_ref=payload_ref,
                trusted_actor_ref=SERVICE_PREFIX + self.tenant_id,
                trace_id="agenda-task-scan:" + sha256((task_id + version).encode("utf-8")).hexdigest()[:24],
                correlation_id="agenda-task:" + task_id,
            )
            payload = {
                "source_kind": "workspace_task",
                "task_id": task_id,
                "title": str(raw_task.get("title") or raw_task.get("task_name") or ""),
                "status": str(raw_task.get("status") or "pending"),
                "level": str(raw_task.get("level") or ""),
                "due_at": str(raw_task.get("due_at") or ""),
                "created_at": str(raw_task.get("created_at") or ""),
                "assignee_userid": str(raw_task.get("assignee_userid") or ""),
                "created_by": str(raw_task.get("created_by") or ""),
                "wake_reason": reason,
                "source_version": version,
                # Server-attested lifecycle observations only. They preserve
                # previous contact/continuation evidence without inspecting
                # task prose or deciding what the next business action is.
                "agenda_followup": (
                    dict(raw_task.get("agenda_followup"))
                    if isinstance(raw_task.get("agenda_followup"), dict)
                    else {}
                ),
                "agenda_continuation": (
                    dict(raw_task.get("agenda_continuation"))
                    if isinstance(raw_task.get("agenda_continuation"), dict)
                    else {}
                ),
                "agenda_contact_history": [
                    dict(value)
                    for value in (raw_task.get("agenda_contact_history") or [])
                    if isinstance(value, dict)
                ][-12:],
            }
            with closing(self._connect()) as connection:
                connection.execute(
                    """INSERT OR IGNORE INTO agenda_workspace_payloads(payload_ref,tenant_id,work_item_id,source_version,payload_json,observed_at)
                       VALUES(?,?,?,?,?,?)""",
                    (payload_ref, self.tenant_id, task_id, version, _json(payload), timestamp),
                )
            try:
                self.authority.attest(fact, self._binding(fact))
            except WorkRuntimeRejected as exc:
                self._audit("workspace_task_not_admitted", work_id=task_id, detail={"reason": type(exc).__name__})
                continue
            duplicate, work_id, _partition = self.inbox.ingest(fact, authority=self.authority, now=timestamp)
            if duplicate:
                duplicates += 1
            else:
                admitted += 1
                self._audit("workspace_task_fact_admitted", work_id=work_id, detail={"reason": reason, "task_id": task_id})
        # Personnel/service governance produces only durable lifecycle facts
        # here. It is not a second worker: every admitted fact enters this
        # same Inbox, trusted binding, public service ingress and Hermes Agent
        # loop as current tasks and work-items above.
        for raw_governance in self._governance_agenda_facts(store):
            event_id = _governance_event_id(raw_governance)
            if not event_id:
                continue
            observed += 1
            version = _governance_source_version(raw_governance)
            payload_ref = "agenda-governance:" + event_id + ":" + version
            fact = WorkFact(
                tenant_id=self.tenant_id,
                source_kind="personnel_service_governance",
                source_event_id=event_id,
                source_version=version,
                created_at=timestamp,
                payload_ref=payload_ref,
                trusted_actor_ref=SERVICE_PREFIX + self.tenant_id,
                trace_id="agenda-governance-scan:" + sha256((event_id + version).encode("utf-8")).hexdigest()[:24],
                correlation_id="agenda-governance:" + event_id,
            )
            payload = {
                "source_kind": "personnel_service_governance",
                "governance_fact_id": event_id,
                "kind": str(raw_governance.get("kind") or ""),
                "claim_id": str(raw_governance.get("claim_id") or ""),
                "handover_id": str(raw_governance.get("handover_id") or ""),
                "attention_id": str(raw_governance.get("attention_id") or ""),
                "target_relation_id": str(raw_governance.get("target_relation_id") or ""),
                "state": str(raw_governance.get("state") or ""),
                "recipient_user_id": self._governance_recipient(raw_governance),
                "source_version": version,
            }
            with closing(self._connect()) as connection:
                connection.execute(
                    """INSERT OR IGNORE INTO agenda_workspace_payloads(payload_ref,tenant_id,work_item_id,source_version,payload_json,observed_at)
                       VALUES(?,?,?,?,?,?)""",
                    (payload_ref, self.tenant_id, event_id, version, _json(payload), timestamp),
                )
            try:
                self.authority.attest(fact, self._binding(fact))
            except WorkRuntimeRejected as exc:
                self._audit("governance_fact_not_admitted", work_id=event_id, detail={"reason": type(exc).__name__})
                continue
            duplicate, work_id, _partition = self.inbox.ingest(fact, authority=self.authority, now=timestamp)
            if duplicate:
                duplicates += 1
            else:
                admitted += 1
                self._audit("governance_fact_admitted", work_id=work_id, detail={"kind": payload["kind"], "fact_id": event_id})
        return {"observed": observed, "admitted": admitted, "duplicates": duplicates}

    def _ticket_message(self, facts: list[dict[str, object]]) -> str:
        payloads: list[dict[str, object]] = []
        with closing(self._connect()) as connection:
            for fact in facts:
                row = connection.execute(
                    "SELECT payload_json FROM agenda_workspace_payloads WHERE payload_ref=? AND tenant_id=?",
                    (str(fact["payload_ref"]), self.tenant_id),
                ).fetchone()
                if row is None:
                    raise WorkRuntimeRejected("agenda_payload_reference_missing")
                payload = json.loads(str(row["payload_json"]))
                if not isinstance(payload, dict):
                    raise WorkRuntimeRejected("agenda_payload_invalid")
                payloads.append(payload)
        source_kinds = {str(item.get("source_kind") or "") for item in payloads}
        if source_kinds == {"workspace_task"}:
            capability_text = (
                "本任务服务回合提供 agenda_task_read_current_work_facts、agenda_task_read_runtime_context、"
                "agenda_task_contact_current_task_party、agenda_schedule_current_task_recheck 和 agenda_task_publish_user_message。先读取事实。"
                "若当前任务未终态，先判断是否存在现在可实际推进的下一步；例如当前责任人可信、允许联系，"
                "且现有投递或人回复事实显示仍需要核实，你可以自行决定是否调用联系 Tool。"
                "只有当前确实只能等待外部事实时，才调用复查 Tool 写入一个未来关注时间。"
                "如果既完成了实际推进又仍需等待，是否安排后续关注仍由你根据已有联系时间、回复、提醒次数、"
                "紧急程度与截止时间自行判断。不要机械催问或只把复查时间向后推；系统不替你决定频率、收件人或处置。"
                "这些 Tool 不完成任务、不替代人的决定。"
                "这些是模型可直接调用的可信 Tool，不是 tool_call 的参数；不要调用 tool_call 或 tuoguan_* 工具。"
            )
        elif source_kinds == {"personnel_service_governance"}:
            capability_text = (
                "本治理服务回合只提供 agenda_governance_read_current_work_facts、agenda_governance_read_runtime_context 和 agenda_governance_publish_user_message；"
                "它们是模型可直接调用的可信 Tool，不是 tool_call 的参数；不要调用 tool_call 或 tuoguan_* 工具。"
            )
        else:
            capability_text = (
                "本受限服务回合只提供 agenda_read_current_work_facts、agenda_query_current_work_items、"
                "agenda_update_current_work_item、agenda_read_runtime_context 和 agenda_service_publish_user_message；它们是模型可直接调用的可信 Tool，"
                "不是 tool_call 的参数；不要调用 tool_call 或 tuoguan_* 工具。"
            )
        return (
            "【可信 Agenda 工作事实】\n"
            "这是当前 Institution Workspace 基于已认证状态和时间字段产生的后台工作唤醒，不是用户消息，也不代表任何业务已经完成。\n"
            "请先审阅下列可信工作事实，并在需要时使用可用可信工具读取工作状态；再由你自行判断是否需要继续、更新事项、向可信责任人询问，或暂不行动。"
            + capability_text
            + "它们是既有可信 Tool 的最小能力面，不预设你必须执行任何动作。"
            + "工作事实本身不是业务完成依据。没有成功读取到可信 Tool 结果时，不得声称事项已完成，也不得更新具体事项。"
            + "若选择写入，只能以本轮 Tool 的真实回执和写后验证为准；不要把这条唤醒本身当作完成依据。\n"
            + "如果你已判断需要向本工单的可信接收人表达业务信息，必须调用本轮对应的 publish_user_message Tool 写入一段自然业务语言；"
            + "普通最终文本不会被投递。不要复制或转述 claim、ticket、service、runtime、工作事实原文或其它内部材料。"
            + _json({"facts": payloads})
        )

    def _issue_ticket(self, envelope: WorkEnvelope, *, now: float) -> str:
        facts = [dict(value) for value in envelope.facts]
        if not facts:
            raise WorkRuntimeRejected("agenda_envelope_empty")
        message = self._ticket_message(facts)
        ticket_id = "agenda_ticket_" + uuid.uuid4().hex
        message_id = "agenda-message:" + ticket_id
        service = SERVICE_PREFIX + self.tenant_id
        source = SOURCE_PREFIX + self.tenant_id
        destinations = [self._destination_for_fact(fact) for fact in facts]
        destination_jsons = {_json(asdict(destination)) for destination in destinations}
        if len(destination_jsons) != 1:
            # A trusted partition must never turn into a mixed-recipient
            # conversation.  This is a transport fail-closed guard, not a
            # payload/content classification.
            raise WorkRuntimeRejected("agenda_ticket_destination_partition_mismatch")
        destination = destinations[0]
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO agenda_service_tickets(ticket_id,tenant_id,service_identity,source_identity,session_id,turn_id,message_id,
                    model_message,payload_sha256,state,expires_at,updated_at,partition_json,destination_json,batch_id,worker_id,lease_until,work_ids_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ticket_id, self.tenant_id, service, source,
                    envelope.partition.trusted_session_id, "agenda-turn:" + ticket_id, message_id,
                    message, sha256(message.encode("utf-8")).hexdigest(), "issued", now + TICKET_TTL_SECONDS, _utc_now(),
                    _json(asdict(envelope.partition)),
                    _json(asdict(destination)),
                    envelope.batch_id, envelope.worker_id, envelope.lease_until,
                    _json([str(fact["work_id"]) for fact in facts]),
                ),
            )
        self._audit("ticket_issued", ticket_id=ticket_id, detail={"batch_id": envelope.batch_id, "fact_count": len(facts)})
        return ticket_id

    def _envelope_for_ticket(self, row: sqlite3.Row) -> WorkEnvelope:
        work_ids = [str(value) for value in json.loads(str(row["work_ids_json"]))]
        with closing(self._connect()) as connection:
            facts = []
            for work_id in work_ids:
                fact = connection.execute("SELECT * FROM work_facts WHERE work_id=?", (work_id,)).fetchone()
                if fact is None:
                    raise WorkRuntimeRejected("agenda_ticket_work_fact_missing")
                facts.append({
                    "work_id": str(fact["work_id"]), "tenant_id": str(fact["tenant_id"]), "partition_id": str(fact["partition_id"]),
                    "source_kind": str(fact["source_kind"]), "source_event_id": str(fact["source_event_id"]),
                    "source_version": str(fact["source_version"]), "created_at": float(fact["created_at"]),
                    "payload_ref": str(fact["payload_ref"]), "trusted_actor_ref": str(fact["trusted_actor_ref"]),
                    "trace_id": str(fact["trace_id"]), "correlation_id": str(fact["correlation_id"]),
                })
        return WorkEnvelope(
            batch_id=str(row["batch_id"]),
            partition=TrustedPartition(**json.loads(str(row["partition_json"]))),
            worker_id=str(row["worker_id"]),
            lease_until=float(row["lease_until"]),
            facts=tuple(facts),
        )

    def _renew_open_tickets(self, *, now: float) -> int:
        count = 0
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM agenda_service_tickets WHERE state IN ('issued','dispatched','agent_claimed')"
            ).fetchall()
        for row in rows:
            try:
                envelope = self._envelope_for_ticket(row)
                renewed = self.inbox.renew_lease(envelope, now=now, lease_seconds=self.lease_seconds)
                with closing(self._connect()) as connection:
                    connection.execute(
                        "UPDATE agenda_service_tickets SET lease_until=?,updated_at=? WHERE ticket_id=?",
                        (renewed.lease_until, _utc_now(), str(row["ticket_id"])),
                    )
                count += 1
            except (WorkLeaseLost, WorkRuntimeRejected):
                self._audit("ticket_lease_lost", ticket_id=str(row["ticket_id"]), detail={})
        return count

    def _expire_orphaned_agent_tickets(self, *, now: float) -> int:
        """End a hard-crashed claimed turn without replaying its business work.

        An ``agent_claimed`` ticket may have reached a protected write before a
        process dies.  Once its server-issued ticket TTL passes, it therefore
        becomes an explicit interrupted terminal, not a fresh Agent input. If
        the direct-turn bridge holds a verified Receipt, it can create only a
        same-Hermes reply-recovery job; otherwise no synthetic reply is made.
        """

        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT ticket_id FROM agenda_service_tickets "
                "WHERE state='agent_claimed' AND expires_at<?", (float(now),)
            ).fetchall()
        count = 0
        for row in rows:
            ticket_id = str(row["ticket_id"])
            try:
                from .direct_reply_recovery import get_direct_reply_recovery_manager

                get_direct_reply_recovery_manager().stage_agenda_ticket_terminal(
                    ticket_id=ticket_id,
                    terminal_state="failed",
                    provider_succeeded=None,
                    final_reply_text="",
                    raw_trace_ref="agenda-runtime:orphaned-agent-ticket",
                )
            except Exception:
                self._audit("orphaned_ticket_reply_stage_deferred", ticket_id=ticket_id, detail={})
                continue
            with closing(self._connect()) as connection:
                changed = connection.execute(
                    "UPDATE agenda_service_tickets SET state='expired', delivery_state='agent_interrupted', "
                    "updated_at=? WHERE ticket_id=? AND state='agent_claimed'",
                    (_utc_now(), ticket_id),
                ).rowcount
            if changed:
                self._audit("orphaned_ticket_expired", ticket_id=ticket_id, detail={})
                count += 1
        return count

    def _reconcile_terminal_tickets(self, *, now: float) -> int:
        count = 0
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT * FROM agenda_service_tickets
                   WHERE state IN ('completed','timeout','failed','expired') AND inbox_ack_state=''"""
            ).fetchall()
        for row in rows:
            ticket = str(row["ticket_id"])
            try:
                envelope = self._envelope_for_ticket(row)
                # This is an acknowledgement of delivery to Hermes, not a
                # claim that a Tool or the business action completed. Terminal
                # provider failures remain explicitly recorded on the ticket.
                self.inbox.acknowledge(
                    envelope,
                    delivered_work_ids=tuple(str(fact["work_id"]) for fact in envelope.facts),
                    now=now,
                )
                terminal = "agent_completed" if str(row["state"]) == "completed" else "agent_inconclusive"
                with closing(self._connect()) as connection:
                    connection.execute(
                        "UPDATE agenda_service_tickets SET inbox_ack_state='acknowledged',terminal_kind=?,updated_at=? WHERE ticket_id=?",
                        (terminal, _utc_now(), ticket),
                    )
                self._audit("ticket_reconciled", ticket_id=ticket, detail={"state": str(row["state"]), "terminal_kind": terminal})
                count += 1
            except (WorkLeaseLost, WorkRuntimeRejected):
                self._audit("ticket_reconcile_deferred", ticket_id=ticket, detail={})
        return count

    def run_once(self, *, now: float | None = None) -> dict[str, object]:
        timestamp = time.time() if now is None else float(now)
        scanned = self.scan_workspace(now=timestamp)
        expired = self._expire_orphaned_agent_tickets(now=timestamp)
        renewed = self._renew_open_tickets(now=timestamp)
        reconciled = self._reconcile_terminal_tickets(now=timestamp)
        issued = ""
        envelope = self.inbox.claim(worker_id=self.worker_id, now=timestamp, max_facts=20)
        if envelope is not None:
            issued = self._issue_ticket(envelope, now=timestamp)
        result = {"scanned": scanned, "expired": expired, "renewed": renewed, "reconciled": reconciled, "issued_ticket_id": issued}
        # Aggregate per-cycle observability instead of creating a verbose log
        # event every few seconds.  This lets operations distinguish a live
        # scheduler with no work from a scheduler that never woke, without
        # treating a successful cycle as a business success.
        with closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO agenda_runtime_status(tenant_id,started_at,last_cycle_at,cycle_count,last_observed,last_admitted,last_duplicates,last_issued_ticket_id,last_error_class)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(tenant_id) DO UPDATE SET
                       last_cycle_at=excluded.last_cycle_at,
                       cycle_count=agenda_runtime_status.cycle_count+1,
                       last_observed=excluded.last_observed,
                       last_admitted=excluded.last_admitted,
                       last_duplicates=excluded.last_duplicates,
                       last_issued_ticket_id=excluded.last_issued_ticket_id,
                       last_error_class=''""",
                (self.tenant_id, timestamp, timestamp, 1, int(scanned["observed"]), int(scanned["admitted"]), int(scanned["duplicates"]), str(issued), ""),
            )
            # A current scheduler with no eligible work is meaningful only if
            # operations can distinguish it from a scheduler that did not
            # wake. This is aggregate runtime evidence, not business truth or
            # a delivery ledger, and no payload text is recorded here.
            local_date = datetime.now().astimezone().date().isoformat()
            connection.execute(
                """INSERT INTO agenda_runtime_daily_status(
                       tenant_id,local_date,first_cycle_at,last_cycle_at,cycle_count,
                       observed_facts,admitted_facts,duplicate_facts,issued_ticket_count)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(tenant_id,local_date) DO UPDATE SET
                       last_cycle_at=excluded.last_cycle_at,
                       cycle_count=agenda_runtime_daily_status.cycle_count+1,
                       observed_facts=agenda_runtime_daily_status.observed_facts+excluded.observed_facts,
                       admitted_facts=agenda_runtime_daily_status.admitted_facts+excluded.admitted_facts,
                       duplicate_facts=agenda_runtime_daily_status.duplicate_facts+excluded.duplicate_facts,
                       issued_ticket_count=agenda_runtime_daily_status.issued_ticket_count+excluded.issued_ticket_count""",
                (
                    self.tenant_id, local_date, timestamp, timestamp, 1,
                    int(scanned["observed"]), int(scanned["admitted"]), int(scanned["duplicates"]),
                    1 if issued else 0,
                ),
            )
        return result


def claimed_ticket_context(
    *,
    database_path: str | Path,
    tenant_id: str,
    service_identity: str,
    message_id: str,
    raw_text: str,
    agent_session_id: str,
    agent_turn_id: str,
) -> dict[str, object] | None:
    """Return one public-ingress ticket context after the Runtime Contract claims it."""

    database = Path(database_path)
    if not database.is_file():
        return None
    candidates = [str(raw_text or "")]
    prefix = "[小优 Agenda 服务] "
    if candidates[0].startswith(prefix):
        candidates.append(candidates[0][len(prefix):])
    digests = {sha256(value.encode("utf-8")).hexdigest() for value in candidates}
    with closing(sqlite3.connect(database, timeout=5.0, isolation_level=None)) as connection:
        connection.row_factory = sqlite3.Row
        marks = ",".join("?" for _ in digests)
        rows = connection.execute(
            "SELECT * FROM agenda_service_tickets WHERE state='agent_claimed' AND tenant_id=? AND service_identity=? "
            "AND message_id=? AND payload_sha256 IN (" + marks + ")",
            (str(tenant_id), str(service_identity), str(message_id), *sorted(digests)),
        ).fetchall()
        if len(rows) != 1:
            return None
        row = rows[0]
        if str(row["source_identity"] or "") != SOURCE_PREFIX + str(tenant_id):
            return None
        if float(row["expires_at"]) < time.time():
            return None
        existing_session = str(row["agent_session_id"] or "")
        existing_turn = str(row["agent_turn_id"] or "")
        if (existing_session and existing_session != str(agent_session_id)) or (existing_turn and existing_turn != str(agent_turn_id)):
            return None
        connection.execute(
            "UPDATE agenda_service_tickets SET agent_session_id=?,agent_turn_id=?,updated_at=? WHERE ticket_id=? AND state='agent_claimed'",
            (str(agent_session_id), str(agent_turn_id), _utc_now(), str(row["ticket_id"])),
        )
        return {
            "ticket_id": str(row["ticket_id"]),
            "partition": TrustedPartition(**json.loads(str(row["partition_json"]))),
            "destination": ReplyDestination(**json.loads(str(row["destination_json"]))),
            "operation_hint": str(row["message_id"]),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="XiaoYou current-Workspace Agenda/Wake coordinator")
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--interval-seconds", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    runtime = CurrentWorkspaceAgenda(data_dir=args.data_dir or None)
    if args.once:
        print(_json(runtime.run_once()))
        return 0
    interval = max(2.0, float(args.interval_seconds))
    while True:
        try:
            runtime.run_once()
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            # The process-level failure ledger is intentionally non-business;
            # it preserves a truthful scheduler failure without inventing a
            # Tool result or waking Hermes through an alternate path.
            runtime._audit("runtime_iteration_failed", detail={"error_class": type(exc).__name__})
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
