"""Versioned XiaoYou Work Runtime capability candidate.

This module is intentionally *not activated* by plugin startup yet.  It is a
portable capability contract for a future Work Inbox integration and contains
no Hermes-private imports, no model call, no business Tool, no business text
analysis, and no production channel adapter.  It preserves the P0-A/A2/A3
boundaries in one versioned, packageable place:

    authenticated work fact -> trusted partition -> durable inbox -> public
    Hermes port -> existing business truth chain -> durable reply outbox

The Work Runtime decides only fact durability, trusted partition and delivery
state.  Hermes remains the sole business interpreter and natural-language
speaker.  Permission, CommandBus, Repository, ExecutionReceipt and writeback
verification remain outside this module and are never imported here.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from typing import Any, Protocol


CAPABILITY_ID = "xiaoyou.work_runtime"
CAPABILITY_VERSION = "1.2.20-full-autonomous-work"
WORK_RUNTIME_SCHEMA_VERSION = 2


class WorkRuntimeRejected(ValueError):
    """Trusted ingress or a truth-boundary invariant was not satisfied."""


class WorkLeaseLost(RuntimeError):
    """A consumer attempted to finish work without its current lease."""


@dataclass(frozen=True)
class TrustedPrincipal:
    canonical_actor_id: str
    role: str
    kind: str  # human | device | service
    active: bool = True


@dataclass(frozen=True)
class ReplyDestination:
    """Server-attested destination.  It is never supplied by the model."""

    tenant_id: str
    channel: str
    recipient_id: str
    source_identity: str
    device_id: str = ""

    def canonical(self) -> str:
        if not all(str(value).strip() for value in (self.tenant_id, self.channel, self.recipient_id, self.source_identity)):
            raise WorkRuntimeRejected("reply_destination_required_fields_missing")
        return "\x1f".join((self.tenant_id, self.channel, self.recipient_id, self.source_identity, self.device_id))


@dataclass(frozen=True)
class WorkFact:
    """Opaque work fact.  ``payload_ref`` is not opened by this module."""

    tenant_id: str
    source_kind: str
    source_event_id: str
    source_version: str
    created_at: float
    payload_ref: str
    trusted_actor_ref: str
    trace_id: str
    correlation_id: str
    source_attested: bool = True

    @property
    def work_id(self) -> str:
        material = "\x1f".join((self.tenant_id, self.source_kind, self.source_event_id, self.source_version))
        return "work_" + sha256(material.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class TrustedWorkBinding:
    """Server-owned identity/session proof corresponding to one WorkFact."""

    work_id: str
    tenant_id: str
    source_kind: str
    source_event_id: str
    source_version: str
    payload_ref: str
    trusted_actor_ref: str
    source_identity: str
    principal: TrustedPrincipal
    authenticated_session_id: str
    continuity_id: str | None
    destination: ReplyDestination
    status: str = "active"


@dataclass(frozen=True)
class TrustedPartition:
    partition_id: str
    tenant_id: str
    canonical_actor_id: str
    principal_kind: str
    trusted_session_id: str
    continuity_id: str | None
    source_identity: str


@dataclass(frozen=True)
class WorkEnvelope:
    batch_id: str
    partition: TrustedPartition
    worker_id: str
    lease_until: float
    facts: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class WorkDeliveryAck:
    batch_id: str
    delivered_count: int
    returned_to_pending_count: int
    pending_rerun_batch_id: str | None


class PublicHermesWorkPort(Protocol):
    """Future bridge dependency: public platform ingress, never Agent internals."""

    async def submit_work_envelope(self, envelope: WorkEnvelope) -> object:
        ...


class SameHermesReplyPort(Protocol):
    """Reply recovery must use the existing Hermes brain with no Tool surface."""

    async def generate_reply_only(self, request: "ReplyOnlyRecoveryRequest") -> "HermesReplyTerminal":
        ...


class TrustedReplyDeliveryPort(Protocol):
    """A channel adapter must accept a stable delivery id or report unknown delivery."""

    async def send_reply(self, *, destination: ReplyDestination, reply_text: str, delivery_id: str) -> object:
        ...


class TrustedWorkAuthority:
    """Server-side fact/binding registry; it deliberately stores no payload text.

    A process-local registry is useful for a short probe, but is not sufficient
    for a durable Inbox: after a crash a replayed fact must be able to prove the
    same canonical subject and reply destination again.  Supplying
    ``database_path`` makes this adapter-authenticated evidence durable in the
    Institution Workspace.  It stores only the immutable binding metadata, not
    the referenced payload or model-generated content.
    """

    def __init__(self, database_path: Path | str | None = None) -> None:
        self.database_path = str(database_path) if database_path is not None else None
        self._bindings: dict[str, TrustedWorkBinding] = {}
        self._lock = threading.RLock()
        if self.database_path:
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
            self._init_schema()

    @property
    def durable(self) -> bool:
        """Whether trusted evidence survives a process restart."""

        return self.database_path is not None

    def _connect(self) -> sqlite3.Connection:
        if not self.database_path:
            raise RuntimeError("trusted_work_authority_not_durable")
        connection = sqlite3.connect(self.database_path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS trusted_work_bindings (
                    work_id TEXT PRIMARY KEY,
                    binding_json TEXT NOT NULL,
                    attested_at REAL NOT NULL
                );
                """
            )

    @staticmethod
    def _binding_json(binding: TrustedWorkBinding) -> str:
        return json.dumps(asdict(binding), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _binding_from_json(value: str) -> TrustedWorkBinding:
        data = json.loads(value)
        return TrustedWorkBinding(
            work_id=str(data["work_id"]), tenant_id=str(data["tenant_id"]), source_kind=str(data["source_kind"]),
            source_event_id=str(data["source_event_id"]), source_version=str(data["source_version"]),
            payload_ref=str(data["payload_ref"]), trusted_actor_ref=str(data["trusted_actor_ref"]),
            source_identity=str(data["source_identity"]),
            principal=TrustedPrincipal(**dict(data["principal"])),
            authenticated_session_id=str(data["authenticated_session_id"]), continuity_id=data.get("continuity_id"),
            destination=ReplyDestination(**dict(data["destination"])), status=str(data.get("status", "active")),
        )

    def _stored_binding(self, work_id: str) -> TrustedWorkBinding | None:
        cached = self._bindings.get(work_id)
        if cached is not None:
            return cached
        if not self.database_path:
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT binding_json FROM trusted_work_bindings WHERE work_id = ?", (work_id,)
            ).fetchone()
        if row is None:
            return None
        binding = self._binding_from_json(str(row["binding_json"]))
        self._bindings[work_id] = binding
        return binding

    @staticmethod
    def _validate_fact(fact: WorkFact) -> None:
        if not fact.source_attested:
            raise WorkRuntimeRejected("source_not_attested")
        if not all(
            str(value).strip()
            for value in (
                fact.tenant_id, fact.source_kind, fact.source_event_id, fact.source_version,
                fact.payload_ref, fact.trusted_actor_ref, fact.trace_id, fact.correlation_id,
            )
        ):
            raise WorkRuntimeRejected("work_fact_required_fields_missing")

    def attest(self, fact: WorkFact, binding: TrustedWorkBinding) -> None:
        """Record one adapter-authenticated mapping and reject mismatches."""

        self._validate_fact(fact)
        if binding.status != "active" or not binding.principal.active:
            raise WorkRuntimeRejected("source_or_principal_not_active")
        comparisons = {
            "work_id": fact.work_id,
            "tenant_id": fact.tenant_id,
            "source_kind": fact.source_kind,
            "source_event_id": fact.source_event_id,
            "source_version": fact.source_version,
            "payload_ref": fact.payload_ref,
            "trusted_actor_ref": fact.trusted_actor_ref,
        }
        for field, actual in comparisons.items():
            if str(getattr(binding, field)) != str(actual):
                raise WorkRuntimeRejected(f"binding_mismatch:{field}")
        if not all(
            str(value).strip()
            for value in (
                binding.source_identity, binding.authenticated_session_id,
                binding.principal.canonical_actor_id, binding.principal.role,
            )
        ):
            raise WorkRuntimeRejected("trusted_binding_required_fields_missing")
        if binding.destination.tenant_id != fact.tenant_id or binding.destination.source_identity != binding.source_identity:
            raise WorkRuntimeRejected("trusted_destination_mismatch")
        with self._lock:
            existing = self._stored_binding(fact.work_id)
            if existing is not None and existing != binding:
                # A source-level duplicate may be delivered again, but it cannot
                # be rebound to another subject/session/destination after its
                # canonical WorkFact id has entered the trusted runtime.
                raise WorkRuntimeRejected("work_fact_trusted_binding_conflict")
            if existing is None and self.database_path:
                with closing(self._connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute(
                        "SELECT binding_json FROM trusted_work_bindings WHERE work_id = ?", (fact.work_id,)
                    ).fetchone()
                    if row is not None:
                        stored = self._binding_from_json(str(row["binding_json"]))
                        if stored != binding:
                            connection.execute("ROLLBACK")
                            raise WorkRuntimeRejected("work_fact_trusted_binding_conflict")
                        connection.execute("COMMIT")
                    else:
                        connection.execute(
                            "INSERT INTO trusted_work_bindings(work_id, binding_json, attested_at) VALUES (?, ?, ?)",
                            (fact.work_id, self._binding_json(binding), time.time()),
                        )
                        connection.execute("COMMIT")
            self._bindings[fact.work_id] = binding

    def binding_for(self, fact: WorkFact) -> TrustedWorkBinding:
        self._validate_fact(fact)
        with self._lock:
            binding = self._stored_binding(fact.work_id)
        if binding is None:
            raise WorkRuntimeRejected("work_fact_has_no_trusted_binding")
        # Re-run validation against the immutable fact rather than trusting an
        # id lookup alone; a source cannot reuse a work id with altered scope.
        self.attest(fact, binding)
        return binding

    def partition_for(self, fact: WorkFact) -> TrustedPartition:
        binding = self.binding_for(fact)
        continuity = str(binding.continuity_id or "").strip()
        if continuity and binding.principal.kind != "service":
            session_component = "continuity:" + continuity
            source_component = "attested-cross-channel"
            principal_component = "attested-subject"
        else:
            session_component = "session:" + binding.authenticated_session_id
            source_component = binding.source_identity
            principal_component = binding.principal.kind
        material = "\x1f".join(
            (binding.tenant_id, binding.principal.canonical_actor_id, principal_component, session_component, source_component)
        )
        return TrustedPartition(
            partition_id="twp_" + sha256(material.encode("utf-8")).hexdigest()[:32],
            tenant_id=binding.tenant_id,
            canonical_actor_id=binding.principal.canonical_actor_id,
            principal_kind=binding.principal.kind,
            trusted_session_id=binding.authenticated_session_id,
            continuity_id=continuity or None,
            source_identity=source_component,
        )


class DurableWorkInbox:
    """Content-blind SQLite Work Inbox, independent of Hermes storage/home."""

    def __init__(self, database_path: Path | str, *, debounce_seconds: float = 2.5, lease_seconds: float = 30.0) -> None:
        self.database_path = str(database_path)
        self.debounce_seconds = float(debounce_seconds)
        self.lease_seconds = float(lease_seconds)
        self._schema_lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._schema_lock, closing(self._connect()) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS work_facts (
                    work_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, partition_id TEXT NOT NULL,
                    source_kind TEXT NOT NULL, source_event_id TEXT NOT NULL, source_version TEXT NOT NULL,
                    created_at REAL NOT NULL, payload_ref TEXT NOT NULL, trusted_actor_ref TEXT NOT NULL,
                    trace_id TEXT NOT NULL, correlation_id TEXT NOT NULL, state TEXT NOT NULL,
                    lease_owner TEXT, lease_until REAL, delivered_batch_id TEXT, delivered_at REAL,
                    UNIQUE(tenant_id, source_kind, source_event_id, source_version)
                );
                CREATE TABLE IF NOT EXISTS work_partitions (
                    partition_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, partition_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wake_batches (
                    batch_id TEXT PRIMARY KEY, partition_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                    status TEXT NOT NULL, created_at REAL NOT NULL, not_before REAL NOT NULL,
                    lease_owner TEXT, lease_until REAL, pending_rerun INTEGER NOT NULL DEFAULT 0,
                    completed_at REAL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS work_one_active_batch
                    ON wake_batches(partition_id) WHERE status IN ('scheduled', 'leased');
                CREATE INDEX IF NOT EXISTS work_ready_idx ON work_facts(partition_id, state, created_at);
                """
            )

    @staticmethod
    def _partition_json(partition: TrustedPartition) -> str:
        return json.dumps(asdict(partition), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _partition_from_json(payload: str) -> TrustedPartition:
        return TrustedPartition(**json.loads(payload))

    def ingest(self, fact: WorkFact, *, authority: TrustedWorkAuthority, now: float) -> tuple[bool, str, TrustedPartition]:
        """Persist one fact and coalesce only its authenticated partition."""

        partition = authority.partition_for(fact)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute("SELECT work_id FROM work_facts WHERE work_id=?", (fact.work_id,)).fetchone()
                if existing is not None:
                    connection.execute("COMMIT")
                    return True, str(existing["work_id"]), partition
                connection.execute(
                    "INSERT OR IGNORE INTO work_partitions(partition_id, tenant_id, partition_json) VALUES (?, ?, ?)",
                    (partition.partition_id, partition.tenant_id, self._partition_json(partition)),
                )
                connection.execute(
                    """INSERT INTO work_facts(work_id, tenant_id, partition_id, source_kind, source_event_id, source_version,
                        created_at, payload_ref, trusted_actor_ref, trace_id, correlation_id, state)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                    (
                        fact.work_id, fact.tenant_id, partition.partition_id, fact.source_kind, fact.source_event_id,
                        fact.source_version, fact.created_at, fact.payload_ref, fact.trusted_actor_ref,
                        fact.trace_id, fact.correlation_id,
                    ),
                )
                self._ensure_batch(connection, partition=partition, now=now)
                connection.execute("COMMIT")
                return False, fact.work_id, partition
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _ensure_batch(self, connection: sqlite3.Connection, *, partition: TrustedPartition, now: float, immediate: bool = False) -> str:
        active = connection.execute(
            "SELECT batch_id, status FROM wake_batches WHERE partition_id=? AND status IN ('scheduled', 'leased')",
            (partition.partition_id,),
        ).fetchone()
        if active is not None:
            if str(active["status"]) == "leased":
                connection.execute("UPDATE wake_batches SET pending_rerun=1 WHERE batch_id=?", (active["batch_id"],))
            return str(active["batch_id"])
        batch_id = "wb_" + uuid.uuid4().hex[:24]
        connection.execute(
            """INSERT INTO wake_batches(batch_id, partition_id, tenant_id, status, created_at, not_before)
               VALUES (?, ?, ?, 'scheduled', ?, ?)""",
            (batch_id, partition.partition_id, partition.tenant_id, now, now if immediate else now + self.debounce_seconds),
        )
        return batch_id

    def recover(self, *, now: float) -> dict[str, int]:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                facts = connection.execute(
                    "UPDATE work_facts SET state='pending', lease_owner=NULL, lease_until=NULL WHERE state='leased' AND lease_until < ?",
                    (now,),
                ).rowcount
                batches = connection.execute(
                    """UPDATE wake_batches SET status='scheduled', lease_owner=NULL, lease_until=NULL, not_before=?
                       WHERE status='leased' AND lease_until < ?""", (now, now),
                ).rowcount
                missing = connection.execute(
                    """SELECT DISTINCT f.partition_id, p.partition_json FROM work_facts f JOIN work_partitions p USING(partition_id)
                       WHERE f.state='pending' AND NOT EXISTS (SELECT 1 FROM wake_batches b
                           WHERE b.partition_id=f.partition_id AND b.status IN ('scheduled','leased'))"""
                ).fetchall()
                for row in missing:
                    self._ensure_batch(connection, partition=self._partition_from_json(str(row["partition_json"])), now=now, immediate=True)
                connection.execute("COMMIT")
                return {"facts_released": int(facts), "batches_released": int(batches), "batches_created": len(missing)}
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def claim(self, *, worker_id: str, now: float, max_facts: int = 100) -> WorkEnvelope | None:
        if not worker_id.strip() or max_facts < 1:
            raise ValueError("worker_id_and_positive_max_facts_required")
        self.recover(now=now)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                batch = connection.execute(
                    "SELECT * FROM wake_batches WHERE status='scheduled' AND not_before <= ? ORDER BY created_at, batch_id LIMIT 1", (now,)
                ).fetchone()
                if batch is None:
                    connection.execute("COMMIT")
                    return None
                lease_until = now + self.lease_seconds
                if connection.execute(
                    "UPDATE wake_batches SET status='leased', lease_owner=?, lease_until=? WHERE batch_id=? AND status='scheduled'",
                    (worker_id, lease_until, batch["batch_id"]),
                ).rowcount != 1:
                    connection.execute("ROLLBACK")
                    return None
                rows = connection.execute(
                    "SELECT * FROM work_facts WHERE partition_id=? AND state='pending' ORDER BY created_at, work_id LIMIT ?",
                    (batch["partition_id"], max_facts),
                ).fetchall()
                ids = [str(row["work_id"]) for row in rows]
                if ids:
                    marks = ",".join("?" for _ in ids)
                    connection.execute(
                        f"UPDATE work_facts SET state='leased', lease_owner=?, lease_until=? WHERE work_id IN ({marks}) AND state='pending'",
                        (worker_id, lease_until, *ids),
                    )
                partition_row = connection.execute("SELECT partition_json FROM work_partitions WHERE partition_id=?", (batch["partition_id"],)).fetchone()
                connection.execute("COMMIT")
                return WorkEnvelope(
                    batch_id=str(batch["batch_id"]), partition=self._partition_from_json(str(partition_row["partition_json"])),
                    worker_id=worker_id, lease_until=lease_until,
                    facts=tuple({
                        "work_id": str(row["work_id"]), "tenant_id": str(row["tenant_id"]), "partition_id": str(row["partition_id"]),
                        "source_kind": str(row["source_kind"]), "source_event_id": str(row["source_event_id"]),
                        "source_version": str(row["source_version"]), "created_at": float(row["created_at"]),
                        "payload_ref": str(row["payload_ref"]), "trusted_actor_ref": str(row["trusted_actor_ref"]),
                        "trace_id": str(row["trace_id"]), "correlation_id": str(row["correlation_id"]),
                    } for row in rows),
                )
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def acknowledge(self, envelope: WorkEnvelope, *, delivered_work_ids: tuple[str, ...], now: float) -> WorkDeliveryAck:
        expected = {str(fact["work_id"]) for fact in envelope.facts}
        delivered = set(delivered_work_ids)
        if not delivered.issubset(expected):
            raise WorkRuntimeRejected("ack_contains_fact_not_in_envelope")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                batch = connection.execute("SELECT * FROM wake_batches WHERE batch_id=?", (envelope.batch_id,)).fetchone()
                if batch is None or str(batch["status"]) != "leased" or str(batch["lease_owner"]) != envelope.worker_id:
                    raise WorkLeaseLost("batch_not_owned")
                if delivered:
                    marks = ",".join("?" for _ in delivered)
                    connection.execute(
                        f"UPDATE work_facts SET state='delivered', lease_owner=NULL, lease_until=NULL, delivered_batch_id=?, delivered_at=? WHERE work_id IN ({marks}) AND lease_owner=? AND state='leased'",
                        (envelope.batch_id, now, *sorted(delivered), envelope.worker_id),
                    )
                remaining = expected - delivered
                if remaining:
                    marks = ",".join("?" for _ in remaining)
                    connection.execute(
                        f"UPDATE work_facts SET state='pending', lease_owner=NULL, lease_until=NULL WHERE work_id IN ({marks}) AND lease_owner=? AND state='leased'",
                        (*sorted(remaining), envelope.worker_id),
                    )
                connection.execute("UPDATE wake_batches SET status='completed', completed_at=?, lease_owner=NULL, lease_until=NULL WHERE batch_id=?", (now, envelope.batch_id))
                pending = connection.execute("SELECT count(*) AS n FROM work_facts WHERE partition_id=? AND state='pending'", (envelope.partition.partition_id,)).fetchone()
                rerun = None
                if int(pending["n"]) > 0:
                    rerun = self._ensure_batch(connection, partition=envelope.partition, now=now, immediate=True)
                connection.execute("COMMIT")
                return WorkDeliveryAck(envelope.batch_id, len(delivered), len(remaining), rerun)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def renew_lease(self, envelope: WorkEnvelope, *, now: float, lease_seconds: float | None = None) -> WorkEnvelope:
        """Extend an active trusted-envelope lease without changing its facts.

        A real Hermes turn may legitimately outlive the short lease that is
        suitable for ordinary inbox consumers.  The coordinator may renew
        only the exact batch it already owns; this method cannot add facts,
        change a partition, or acknowledge business work.  It is therefore a
        scheduling/recovery primitive, not a business retry mechanism.
        """

        duration = self.lease_seconds if lease_seconds is None else float(lease_seconds)
        if duration <= 0:
            raise ValueError("work_lease_duration_must_be_positive")
        expected = {str(fact["work_id"]) for fact in envelope.facts}
        until = float(now) + duration
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                batch = connection.execute(
                    "SELECT * FROM wake_batches WHERE batch_id=?", (envelope.batch_id,)
                ).fetchone()
                if (
                    batch is None
                    or str(batch["status"]) != "leased"
                    or str(batch["lease_owner"] or "") != envelope.worker_id
                    or str(batch["partition_id"] or "") != envelope.partition.partition_id
                ):
                    raise WorkLeaseLost("batch_not_owned")
                if expected:
                    marks = ",".join("?" for _ in expected)
                    owned = connection.execute(
                        f"SELECT count(*) AS n FROM work_facts WHERE work_id IN ({marks}) "
                        "AND state='leased' AND lease_owner=?",
                        (*sorted(expected), envelope.worker_id),
                    ).fetchone()
                    if int(owned["n"]) != len(expected):
                        raise WorkLeaseLost("work_fact_not_owned")
                    connection.execute(
                        f"UPDATE work_facts SET lease_until=? WHERE work_id IN ({marks}) "
                        "AND state='leased' AND lease_owner=?",
                        (until, *sorted(expected), envelope.worker_id),
                    )
                changed = connection.execute(
                    "UPDATE wake_batches SET lease_until=? WHERE batch_id=? AND status='leased' AND lease_owner=?",
                    (until, envelope.batch_id, envelope.worker_id),
                ).rowcount
                if changed != 1:
                    raise WorkLeaseLost("batch_lease_not_renewed")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return WorkEnvelope(
            batch_id=envelope.batch_id,
            partition=envelope.partition,
            worker_id=envelope.worker_id,
            lease_until=until,
            facts=envelope.facts,
        )


@dataclass(frozen=True)
class VerifiedBusinessFact:
    """Read-only completed receipt supplied by the existing business chain."""

    operation_id: str
    tenant_id: str
    receipt: dict[str, object]
    writeback_verified: bool
    original_request_ref: str
    original_turn_id: str

    def validate(self) -> None:
        if not all(str(value).strip() for value in (self.operation_id, self.tenant_id, self.original_request_ref, self.original_turn_id)):
            raise WorkRuntimeRejected("verified_business_fact_fields_missing")
        if str(self.receipt.get("status") or "") != "completed" or not self.writeback_verified:
            raise WorkRuntimeRejected("reply_outbox_requires_completed_verified_receipt")


@dataclass(frozen=True)
class ReplyOnlyRecoveryRequest:
    recovery_id: str
    tenant_id: str
    original_request_ref: str
    original_turn_id: str
    verified_receipt: dict[str, object]
    destination: ReplyDestination

    def render_for_same_hermes(self, original_request_text: str) -> str:
        """A neutral factual prompt; it does not contain a generated reply."""

        return (
            "【已验证结果的回复恢复】\n"
            "上一轮业务操作已经由服务端回执确认完成。现在只需依据以下已验证事实，用自然中文回复用户；"
            "本轮没有任何工具，不得重新执行或尝试业务操作。\n"
            + json.dumps({
                "recovery_id": self.recovery_id, "original_request": original_request_text,
                "verified_execution_receipt": self.verified_receipt, "writeback_verified": True,
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )


@dataclass(frozen=True)
class HermesReplyTerminal:
    terminal_state: str
    provider_succeeded: bool | None
    reply_text: str
    selected_tools: tuple[str, ...] = ()
    raw_trace_ref: str = ""


@dataclass(frozen=True)
class ReplyJob:
    reply_id: str
    operation_id: str
    tenant_id: str
    partition_id: str
    destination: ReplyDestination
    generation_state: str
    delivery_state: str
    reply_text: str
    delivery_id: str
    delivery_hold: bool
    lease_owner: str | None
    lease_until: float | None
    delivery_disposition: str
    channel_message_id: str
    delivery_trace_ref: str


class DurableReplyOutbox:
    """Durable reply state; it has no business capability or success authority."""

    def __init__(self, database_path: Path | str, *, lease_seconds: float = 30.0) -> None:
        self.database_path = str(database_path)
        self.lease_seconds = float(lease_seconds)
        self._schema_lock = threading.Lock()
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._schema_lock, closing(self._connect()) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS business_truth (
                    operation_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, partition_id TEXT NOT NULL,
                    receipt_json TEXT NOT NULL, writeback_verified INTEGER NOT NULL,
                    original_request_ref TEXT NOT NULL, original_turn_id TEXT NOT NULL, recorded_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_truth (
                    agent_attempt_id TEXT PRIMARY KEY, operation_id TEXT, terminal_state TEXT NOT NULL,
                    provider_succeeded INTEGER, raw_trace_ref TEXT NOT NULL, final_reply_text TEXT NOT NULL, recorded_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reply_jobs (
                    reply_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE, tenant_id TEXT NOT NULL, partition_id TEXT NOT NULL,
                    destination_json TEXT NOT NULL, generation_state TEXT NOT NULL, delivery_state TEXT NOT NULL,
                    reply_text TEXT NOT NULL, generation_attempts INTEGER NOT NULL DEFAULT 0, delivery_attempts INTEGER NOT NULL DEFAULT 0,
                    delivery_id TEXT NOT NULL UNIQUE, delivery_hold INTEGER NOT NULL DEFAULT 0, lease_owner TEXT, lease_until REAL, last_error TEXT NOT NULL DEFAULT '',
                    delivery_disposition TEXT NOT NULL DEFAULT '', channel_message_id TEXT NOT NULL DEFAULT '',
                    delivery_trace_ref TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS reply_jobs_ready_idx ON reply_jobs(generation_state, delivery_state, created_at);
                """
            )
            # Additive migration: delivery evidence is intentionally separate
            # from verified business truth, so an existing isolated Workspace
            # can be resumed without touching receipts or prior replies.
            known_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(reply_jobs)").fetchall()
            }
            for name, definition in (
                ("delivery_disposition", "TEXT NOT NULL DEFAULT ''"),
                ("channel_message_id", "TEXT NOT NULL DEFAULT ''"),
                ("delivery_trace_ref", "TEXT NOT NULL DEFAULT ''"),
                # A normal direct channel hand-off must not deliver the first
                # reply until its adapter has atomically replaced the normal
                # terminal callback. This is transport ordering only; it does
                # not affect business truth or reply generation.
                ("delivery_hold", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in known_columns:
                    connection.execute(f"ALTER TABLE reply_jobs ADD COLUMN {name} {definition}")

    @staticmethod
    def _reply_id(operation_id: str) -> str:
        return "reply_" + sha256(operation_id.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _destination_json(destination: ReplyDestination) -> str:
        destination.canonical()
        return json.dumps(asdict(destination), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _destination_from_json(payload: str) -> ReplyDestination:
        return ReplyDestination(**json.loads(payload))

    def observe_agent_terminal(
        self, *, partition: TrustedPartition, destination: ReplyDestination, agent_attempt_id: str,
        terminal_state: str, provider_succeeded: bool | None, raw_trace_ref: str,
        business: VerifiedBusinessFact | None = None, final_reply_text: str = "", delivery_hold: bool = False,
        now: float | None = None,
    ) -> ReplyJob | None:
        """Copy truth observations; never run/re-run a business Tool."""

        if not all(str(value).strip() for value in (agent_attempt_id, terminal_state, raw_trace_ref)):
            raise WorkRuntimeRejected("agent_terminal_fields_missing")
        if destination.tenant_id != partition.tenant_id:
            raise WorkRuntimeRejected("reply_destination_partition_cross_tenant")
        timestamp = time.time() if now is None else float(now)
        if business is not None:
            business.validate()
            if business.tenant_id != partition.tenant_id:
                raise WorkRuntimeRejected("business_partition_cross_tenant")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                operation_id = business.operation_id if business else None
                if business:
                    receipt_json = json.dumps(business.receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    existing = connection.execute("SELECT * FROM business_truth WHERE operation_id=?", (business.operation_id,)).fetchone()
                    if existing is None:
                        connection.execute(
                            """INSERT INTO business_truth(operation_id, tenant_id, partition_id, receipt_json, writeback_verified,
                                original_request_ref, original_turn_id, recorded_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)""",
                            (business.operation_id, business.tenant_id, partition.partition_id, receipt_json,
                             business.original_request_ref, business.original_turn_id, timestamp),
                        )
                    elif (str(existing["tenant_id"]) != business.tenant_id or str(existing["partition_id"]) != partition.partition_id
                          or str(existing["receipt_json"]) != receipt_json or int(existing["writeback_verified"]) != 1):
                        raise WorkRuntimeRejected("operation_business_truth_conflict")
                connection.execute(
                    """INSERT OR IGNORE INTO agent_truth(agent_attempt_id, operation_id, terminal_state, provider_succeeded,
                        raw_trace_ref, final_reply_text, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (agent_attempt_id, operation_id, terminal_state, None if provider_succeeded is None else int(provider_succeeded),
                     raw_trace_ref, final_reply_text.strip(), timestamp),
                )
                if not business:
                    connection.execute("COMMIT")
                    return None
                current = connection.execute("SELECT * FROM reply_jobs WHERE operation_id=?", (business.operation_id,)).fetchone()
                if current is None:
                    generated = bool(final_reply_text.strip())
                    destination_json = self._destination_json(destination)
                    delivery_id = "delivery_" + sha256((business.operation_id + "\x1f" + destination.canonical()).encode("utf-8")).hexdigest()[:32]
                    connection.execute(
                        """INSERT INTO reply_jobs(reply_id, operation_id, tenant_id, partition_id, destination_json,
                            generation_state, delivery_state, reply_text, delivery_id, delivery_hold, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (self._reply_id(business.operation_id), business.operation_id, business.tenant_id, partition.partition_id,
                         destination_json, "generated" if generated else "pending_generation",
                         "not_ready" if delivery_hold else ("pending" if generated else "not_ready"),
                         final_reply_text.strip(), delivery_id, int(delivery_hold), timestamp, timestamp),
                    )
                else:
                    if (str(current["tenant_id"]) != business.tenant_id or str(current["partition_id"]) != partition.partition_id
                        or str(current["destination_json"]) != self._destination_json(destination)):
                        raise WorkRuntimeRejected("operation_reply_destination_or_partition_conflict")
                    if final_reply_text.strip() and not str(current["reply_text"]).strip():
                        connection.execute(
                            """UPDATE reply_jobs SET reply_text=?, generation_state='generated',
                               delivery_state=CASE WHEN delivery_hold=1 THEN 'not_ready' ELSE 'pending' END,
                               updated_at=? WHERE operation_id=?""",
                            (final_reply_text.strip(), timestamp, business.operation_id),
                        )
                row = connection.execute("SELECT * FROM reply_jobs WHERE operation_id=?", (business.operation_id,)).fetchone()
                connection.execute("COMMIT")
                return self._job(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def observe_agent_notice(
        self,
        *,
        notice_id: str,
        partition: TrustedPartition,
        destination: ReplyDestination,
        agent_attempt_id: str,
        terminal_state: str,
        provider_succeeded: bool | None,
        raw_trace_ref: str,
        final_reply_text: str,
        delivery_hold: bool = False,
        now: float | None = None,
    ) -> ReplyJob | None:
        """Durably deliver a trusted Hermes notice that has no business receipt.

        This is deliberately narrower than a business reply: it can preserve a
        model-generated question, warning, or status report for a
        server-attested service turn, but it cannot create Business Truth,
        cannot claim a Tool succeeded, and is ineligible for reply-only
        regeneration.  A missing final reply therefore remains a truthful
        agent/provider failure rather than an opportunity to invent text.
        """

        operation_id = str(notice_id or "").strip()
        text = str(final_reply_text or "").strip()
        if not operation_id or not all(str(value).strip() for value in (agent_attempt_id, terminal_state, raw_trace_ref)):
            raise WorkRuntimeRejected("agent_notice_required_fields_missing")
        if destination.tenant_id != partition.tenant_id:
            raise WorkRuntimeRejected("reply_destination_partition_cross_tenant")
        timestamp = time.time() if now is None else float(now)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT OR IGNORE INTO agent_truth(agent_attempt_id, operation_id, terminal_state, provider_succeeded,
                        raw_trace_ref, final_reply_text, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        agent_attempt_id,
                        operation_id,
                        str(terminal_state),
                        None if provider_succeeded is None else int(provider_succeeded),
                        str(raw_trace_ref),
                        text,
                        timestamp,
                    ),
                )
                # No final language is not a deliverable and must not be
                # promoted to a synthetic notice or a reply-only recovery.
                if str(terminal_state) != "completed" or not text:
                    connection.execute("COMMIT")
                    return None
                destination_json = self._destination_json(destination)
                current = connection.execute(
                    "SELECT * FROM reply_jobs WHERE operation_id=?", (operation_id,)
                ).fetchone()
                if current is None:
                    delivery_id = "delivery_" + sha256((operation_id + "\x1f" + destination.canonical()).encode("utf-8")).hexdigest()[:32]
                    connection.execute(
                        """INSERT INTO reply_jobs(reply_id, operation_id, tenant_id, partition_id, destination_json,
                            generation_state, delivery_state, reply_text, delivery_id, delivery_hold, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, 'generated', ?, ?, ?, ?, ?, ?)""",
                        (
                            self._reply_id(operation_id),
                            operation_id,
                            partition.tenant_id,
                            partition.partition_id,
                            destination_json,
                            "not_ready" if delivery_hold else "pending",
                            text,
                            delivery_id,
                            1 if delivery_hold else 0,
                            timestamp,
                            timestamp,
                        ),
                    )
                else:
                    if (
                        str(current["tenant_id"]) != partition.tenant_id
                        or str(current["partition_id"]) != partition.partition_id
                        or str(current["destination_json"]) != destination_json
                        or str(current["reply_text"]) != text
                    ):
                        raise WorkRuntimeRejected("agent_notice_truth_conflict")
                row = connection.execute(
                    "SELECT * FROM reply_jobs WHERE operation_id=?", (operation_id,)
                ).fetchone()
                connection.execute("COMMIT")
                return self._job(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _claim(self, *, worker_id: str, now: float, generation: bool) -> ReplyJob | None:
        if not worker_id.strip():
            raise ValueError("reply_worker_id_required")
        self.recover(now=now)
        condition = "generation_state='pending_generation' AND delivery_state='not_ready'" if generation else "generation_state='generated' AND delivery_state='pending'"
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(f"SELECT * FROM reply_jobs WHERE {condition} ORDER BY created_at, reply_id LIMIT 1").fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return None
                until = now + self.lease_seconds
                if connection.execute(
                    "UPDATE reply_jobs SET delivery_state='leased', lease_owner=?, lease_until=?, updated_at=? WHERE reply_id=? AND delivery_state=?",
                    (worker_id, until, now, row["reply_id"], row["delivery_state"]),
                ).rowcount != 1:
                    connection.execute("ROLLBACK")
                    return None
                result = connection.execute("SELECT * FROM reply_jobs WHERE reply_id=?", (row["reply_id"],)).fetchone()
                connection.execute("COMMIT")
                return self._job(result)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def claim_generation(self, *, worker_id: str, now: float) -> ReplyJob | None:
        return self._claim(worker_id=worker_id, now=now, generation=True)

    def claim_delivery(self, *, worker_id: str, now: float) -> ReplyJob | None:
        return self._claim(worker_id=worker_id, now=now, generation=False)

    def complete_generation(self, job: ReplyJob, *, worker_id: str, terminal: HermesReplyTerminal, now: float) -> ReplyJob:
        """Reject a reply recovery that attempts any Tool, including read Tools."""

        if terminal.selected_tools:
            raise WorkRuntimeRejected("reply_only_recovery_may_not_select_tools")
        if terminal.terminal_state != "completed" or not terminal.reply_text.strip():
            return self._finish(job, worker_id=worker_id, state="generation_failed", delivery_state="not_ready", error=f"terminal:{terminal.terminal_state}", now=now)
        return self._finish(
            job,
            worker_id=worker_id,
            state="generated",
            delivery_state="not_ready" if job.delivery_hold else "pending",
            reply_text=terminal.reply_text.strip(),
            now=now,
        )

    def release_delivery_hold(
        self,
        *,
        reply_id: str,
        operation_id: str,
        destination: ReplyDestination,
        now: float,
    ) -> ReplyJob:
        """Atomically make one direct-channel reply eligible for delivery.

        The caller must already own the source transport operation.  Matching
        the immutable operation and destination prevents a response intended
        for one subject/channel from being released by another active turn.
        """

        destination_json = self._destination_json(destination)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                updated = connection.execute(
                    """UPDATE reply_jobs SET delivery_hold=0,
                       delivery_state=CASE WHEN generation_state='generated' THEN 'pending' ELSE 'not_ready' END,
                       updated_at=?
                       WHERE reply_id=? AND operation_id=? AND destination_json=? AND delivery_hold=1""",
                    (now, reply_id, operation_id, destination_json),
                ).rowcount
                if updated != 1:
                    raise WorkRuntimeRejected("reply_delivery_hold_not_releasable")
                row = connection.execute("SELECT * FROM reply_jobs WHERE reply_id=?", (reply_id,)).fetchone()
                connection.execute("COMMIT")
                return self._job(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def complete_delivery(
        self,
        job: ReplyJob,
        *,
        worker_id: str,
        delivered: bool,
        error: str = "",
        channel_message_id: str = "",
        delivery_trace_ref: str = "",
        now: float,
    ) -> ReplyJob:
        """Record a conclusive channel outcome.

        ``delivered=False`` means the adapter knows that no user-visible reply
        was accepted and it may be retried.  Ambiguous network outcomes must
        use :meth:`complete_delivery_unknown`; they are never silently folded
        into a retryable failure.
        """

        return self._finish(
            job, worker_id=worker_id, state="generated", delivery_state="delivered" if delivered else "pending",
            error="" if delivered else error or "channel_delivery_failed", now=now, increment_delivery=True,
            delivery_disposition="delivered" if delivered else "failed",
            channel_message_id=str(channel_message_id or "") or None,
            delivery_trace_ref=str(delivery_trace_ref or "") or None,
        )

    def complete_delivery_blocked(
        self,
        job: ReplyJob,
        *,
        worker_id: str,
        error: str,
        now: float,
    ) -> ReplyJob:
        """Park a reply that cannot be sent because its local port is absent.

        This is deliberately distinct from a remote delivery failure.  No
        channel request has occurred, so retrying on every worker tick cannot
        improve delivery and merely creates a hot loop.  A later, trusted port
        registration may explicitly requeue only this channel's parked jobs.
        It never touches Business Truth or invokes Hermes.
        """

        if not str(error).strip():
            raise WorkRuntimeRejected("blocked_delivery_requires_error")
        return self._finish(
            job,
            worker_id=worker_id,
            state="generated",
            delivery_state="blocked",
            error=str(error),
            now=now,
            increment_delivery=True,
            delivery_disposition="blocked",
        )

    def requeue_blocked_channel(self, *, channel: str, now: float) -> int:
        """Make only locally parked jobs eligible after a trusted port appears."""

        expected = str(channel or "").strip()
        if not expected:
            raise WorkRuntimeRejected("blocked_delivery_channel_required")
        changed = 0
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    "SELECT reply_id,destination_json FROM reply_jobs "
                    "WHERE generation_state='generated' AND delivery_state='blocked' "
                    "AND last_error='channel_unregistered'"
                ).fetchall()
                for row in rows:
                    destination = self._destination_from_json(str(row["destination_json"]))
                    if destination.channel != expected:
                        continue
                    changed += connection.execute(
                        "UPDATE reply_jobs SET delivery_state='pending', last_error='', "
                        "delivery_disposition='port_registered', updated_at=? "
                        "WHERE reply_id=? AND delivery_state='blocked'",
                        (now, str(row["reply_id"])),
                    ).rowcount
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return changed

    def suppress_pending_delivery(self, *, reply_id: str, reason: str, now: float) -> ReplyJob:
        """Record an operator-verified pre-delivery suppression without business mutation.

        This is intentionally unavailable for delivered, unknown or leased
        jobs: those states require channel evidence or lease recovery.  It is
        used only when a reply has been proven invalid before any delivery
        attempt, such as a response generated against a superseded Workspace
        authority pointer.
        """

        if not str(reply_id).strip() or not str(reason).strip():
            raise WorkRuntimeRejected("delivery_suppression_fields_required")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                updated = connection.execute(
                    "UPDATE reply_jobs SET delivery_state='suppressed', delivery_disposition='suppressed', "
                    "last_error=?, lease_owner=NULL, lease_until=NULL, updated_at=? "
                    "WHERE reply_id=? AND generation_state='generated' "
                    "AND delivery_state IN ('pending','blocked','not_ready')",
                    (str(reason), now, str(reply_id)),
                ).rowcount
                if updated != 1:
                    raise WorkRuntimeRejected("delivery_not_suppressible")
                row = connection.execute("SELECT * FROM reply_jobs WHERE reply_id=?", (str(reply_id),)).fetchone()
                connection.execute("COMMIT")
                return self._job(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def complete_delivery_unknown(
        self,
        job: ReplyJob,
        *,
        worker_id: str,
        error: str,
        delivery_trace_ref: str,
        channel_message_id: str = "",
        now: float,
    ) -> ReplyJob:
        """Quarantine an uncertain outbound attempt without touching business truth.

        A timeout or broken connection can happen after a remote channel
        accepts the reply.  Automatically re-sending would be at-least-once
        delivery, not evidence of a failed send.  Keep the job ``unknown``
        until a channel readback or explicit operator evidence reconciles it.
        This never wakes Hermes and never re-runs a business Tool.
        """

        if not str(error).strip() or not str(delivery_trace_ref).strip():
            raise WorkRuntimeRejected("unknown_delivery_requires_error_and_trace")
        return self._finish(
            job,
            worker_id=worker_id,
            state="generated",
            delivery_state="unknown",
            error=str(error),
            now=now,
            increment_delivery=True,
            delivery_disposition="unknown",
            channel_message_id=str(channel_message_id or ""),
            delivery_trace_ref=str(delivery_trace_ref),
        )

    def reconcile_unknown_delivery(
        self,
        *,
        reply_id: str,
        delivered: bool,
        reconciliation_ref: str,
        now: float,
    ) -> ReplyJob:
        """Resolve a quarantined delivery from external evidence only.

        A negative reconciliation makes the reply eligible for a delivery-only
        retry.  It does not reopen the Agent turn or replay its operation.
        """

        if not str(reconciliation_ref).strip():
            raise WorkRuntimeRejected("delivery_reconciliation_evidence_required")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = connection.execute(
                    """UPDATE reply_jobs SET delivery_state=?, delivery_disposition=?,
                        delivery_trace_ref=?, last_error='', updated_at=?
                       WHERE reply_id=? AND delivery_state='unknown'""",
                    (
                        "delivered" if delivered else "pending",
                        "reconciled_delivered" if delivered else "reconciled_not_delivered",
                        str(reconciliation_ref),
                        now,
                        reply_id,
                    ),
                ).rowcount
                if result != 1:
                    raise WorkRuntimeRejected("unknown_delivery_not_reconcilable")
                row = connection.execute("SELECT * FROM reply_jobs WHERE reply_id=?", (reply_id,)).fetchone()
                connection.execute("COMMIT")
                return self._job(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def requeue_generation(self, *, reply_id: str, now: float) -> ReplyJob:
        """Requeue only a failed reply generation; never a business operation."""

        with closing(self._connect()) as connection:
            connection.execute(
                """UPDATE reply_jobs SET generation_state='pending_generation', delivery_state='not_ready',
                    lease_owner=NULL, lease_until=NULL, updated_at=?
                   WHERE reply_id=? AND generation_state='generation_failed'""", (now, reply_id),
            )
            row = connection.execute("SELECT * FROM reply_jobs WHERE reply_id=?", (reply_id,)).fetchone()
            if row is None:
                raise WorkRuntimeRejected("reply_job_not_found")
            return self._job(row)

    def _finish(self, job: ReplyJob, *, worker_id: str, state: str, delivery_state: str, now: float,
                reply_text: str | None = None, error: str = "", increment_delivery: bool = False,
                delivery_disposition: str | None = None, channel_message_id: str | None = None,
                delivery_trace_ref: str | None = None) -> ReplyJob:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = connection.execute(
                    """UPDATE reply_jobs SET generation_state=?, delivery_state=?, reply_text=COALESCE(?, reply_text),
                        generation_attempts=generation_attempts+?, delivery_attempts=delivery_attempts+?,
                        lease_owner=NULL, lease_until=NULL, last_error=?,
                        delivery_disposition=COALESCE(?, delivery_disposition),
                        channel_message_id=COALESCE(?, channel_message_id),
                        delivery_trace_ref=COALESCE(?, delivery_trace_ref), updated_at=?
                       WHERE reply_id=? AND delivery_state='leased' AND lease_owner=?""",
                    (state, delivery_state, reply_text, 0 if increment_delivery else 1, 1 if increment_delivery else 0,
                     error, delivery_disposition, channel_message_id, delivery_trace_ref, now, job.reply_id, worker_id),
                ).rowcount
                if result != 1:
                    raise WorkLeaseLost("reply_job_not_owned")
                row = connection.execute("SELECT * FROM reply_jobs WHERE reply_id=?", (job.reply_id,)).fetchone()
                connection.execute("COMMIT")
                return self._job(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def recover(self, *, now: float) -> int:
        with closing(self._connect()) as connection:
            return int(connection.execute(
                """UPDATE reply_jobs SET delivery_state=CASE WHEN generation_state='generated' AND delivery_hold=0 THEN 'pending' ELSE 'not_ready' END,
                    lease_owner=NULL, lease_until=NULL, updated_at=? WHERE delivery_state='leased' AND lease_until < ?""", (now, now),
            ).rowcount)

    def get(self, operation_id: str) -> ReplyJob | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM reply_jobs WHERE operation_id=?", (operation_id,)).fetchone()
            return self._job(row) if row else None

    def get_by_reply_id(self, reply_id: str) -> ReplyJob | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM reply_jobs WHERE reply_id=?", (reply_id,)).fetchone()
            return self._job(row) if row else None

    def get_by_delivery_id(self, delivery_id: str) -> ReplyJob | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM reply_jobs WHERE delivery_id=?", (delivery_id,)).fetchone()
            return self._job(row) if row else None

    def reply_recovery_request(self, job: ReplyJob) -> ReplyOnlyRecoveryRequest:
        """Return immutable verified facts for a same-Hermes, zero-Tool reply.

        This is deliberately a read-only public outbox operation.  A channel
        adapter may resolve ``original_request_ref`` from its trusted payload
        store, but it never receives a business Tool or a capability to alter
        the stored receipt.
        """

        with closing(self._connect()) as connection:
            truth = connection.execute(
                "SELECT * FROM business_truth WHERE operation_id=?", (job.operation_id,)
            ).fetchone()
        if truth is None or int(truth["writeback_verified"]) != 1:
            raise WorkRuntimeRejected("reply_recovery_missing_verified_business_truth")
        if str(truth["tenant_id"]) != job.tenant_id or str(truth["partition_id"]) != job.partition_id:
            raise WorkRuntimeRejected("reply_recovery_business_truth_scope_conflict")
        return ReplyOnlyRecoveryRequest(
            recovery_id=job.reply_id,
            tenant_id=job.tenant_id,
            original_request_ref=str(truth["original_request_ref"]),
            original_turn_id=str(truth["original_turn_id"]),
            verified_receipt=json.loads(str(truth["receipt_json"])),
            destination=job.destination,
        )

    @staticmethod
    def _job(row: sqlite3.Row) -> ReplyJob:
        return ReplyJob(
            reply_id=str(row["reply_id"]), operation_id=str(row["operation_id"]), tenant_id=str(row["tenant_id"]),
            partition_id=str(row["partition_id"]), destination=DurableReplyOutbox._destination_from_json(str(row["destination_json"])),
            generation_state=str(row["generation_state"]), delivery_state=str(row["delivery_state"]),
            reply_text=str(row["reply_text"]), delivery_id=str(row["delivery_id"]),
            delivery_hold=bool(row["delivery_hold"]),
            lease_owner=str(row["lease_owner"]) if row["lease_owner"] else None,
            lease_until=float(row["lease_until"]) if row["lease_until"] is not None else None,
            delivery_disposition=str(row["delivery_disposition"] or ""),
            channel_message_id=str(row["channel_message_id"] or ""),
            delivery_trace_ref=str(row["delivery_trace_ref"] or ""),
        )


def capability_descriptor() -> dict[str, object]:
    """Versioned, JSON-safe contract for archive and cross-Core certification."""

    return {
        "id": CAPABILITY_ID,
        "version": CAPABILITY_VERSION,
        "schema_version": WORK_RUNTIME_SCHEMA_VERSION,
        "activation": "not_registered_by_default",
        "hermes_dependency": "public platform ingress and public lifecycle hooks only",
        "truth_boundaries": ["trusted_partition", "business_receipt", "agent_terminal", "reply", "delivery", "unknown_delivery"],
        "durable_replay": "requires externally supplied durable trusted-binding evidence",
        "public_receipt_observation": "requires an opt-in public post_tool_call listener plus a server-generated platform operation identity; traces are not Receipt truth",
        "prohibitions": [
            "payload_intent_or_keyword_routing", "business_tool_selection", "business_reply_template",
            "receipt_mutation", "business_write_replay", "automatic_unknown_delivery_resend", "second_model", "private_agent_dependency",
        ],
    }
