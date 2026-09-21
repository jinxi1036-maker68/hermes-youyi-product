"""Server-only public Platform Adapter for an authenticated Agenda work turn.

This is intentionally a transport adapter, not an Agenda planner or business
handler.  The only input it accepts is an opaque ticket previously recorded by
the server-side Work Runtime bridge.  It neither receives nor trusts client,
model, or Agenda supplied tenant / role / Tool values.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource


PLATFORM_NAME = "agenda_service_work"
SERVICE_PREFIX = "service:agenda:"
logger = logging.getLogger(__name__)


# One configured Agenda service adapter owns the server-only ingress in a
# Gateway process.  The public lifecycle bridge below deliberately exposes
# only terminal delivery settlement; it never accepts source prose, a Tool,
# an identity, or an Agenda decision from a caller.
_ACTIVE_ADAPTER: Any = None


class AgendaServiceIngressRejected(ValueError):
    """A server ticket was absent, malformed, expired, or already consumed."""


@dataclass(frozen=True)
class ServiceWorkTicket:
    ticket_id: str
    tenant_id: str
    service_identity: str
    source_identity: str
    session_id: str
    turn_id: str
    message_id: str
    model_message: str
    payload_sha256: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _platform_value() -> Any:
    try:
        return Platform(PLATFORM_NAME)
    except (TypeError, ValueError):
        # Hermes accepts a registered third-party platform by name.  Some
        # repository test stubs only know its built-in enum values.
        return PLATFORM_NAME


def _result(*, ok: bool, message_id: str = "", error: str = "") -> SendResult:
    try:
        return SendResult(success=ok, message_id=message_id, error=error)
    except TypeError:
        return SendResult(ok=ok, message_id=message_id, error=error)


def _config_extra(config: PlatformConfig) -> dict[str, Any]:
    extra = getattr(config, "extra", None)
    if isinstance(extra, dict):
        return extra
    options = getattr(config, "options", None)
    return options if isinstance(options, dict) else {}


def _ticket_db_from_config(config: PlatformConfig) -> Path:
    raw = str(_config_extra(config).get("ticket_db_path") or os.environ.get("XIAOYOU_AGENDA_SERVICE_INGRESS_DB") or "").strip()
    if not raw:
        raise AgendaServiceIngressRejected("agenda_service_ticket_db_missing")
    return Path(raw).expanduser().resolve()


def check_agenda_service_work_requirements() -> bool:
    return True


def validate_agenda_service_work_config(config: PlatformConfig) -> bool:
    try:
        return bool(_ticket_db_from_config(config))
    except AgendaServiceIngressRejected:
        return False


class AgendaServiceWorkAdapter(BasePlatformAdapter):
    """Consume an already-issued service ticket and enter Hermes publicly.

    There is deliberately no network listener and no generic ``submit_text``
    endpoint.  This removes any path for a caller to claim a tenant, role,
    recipient, source identity, or Tool by constructing a payload.
    """

    supports_async_delivery = False
    interactive_resume = False

    def __init__(self, config: PlatformConfig) -> None:
        platform = _platform_value()
        try:
            super().__init__(config, platform)
        except TypeError:
            self.config = config
            self.platform = platform
            self._message_handler = None
            self._running = False
        self.ticket_db_path = _ticket_db_from_config(config)
        self.response_timeout_seconds = max(5.0, min(float(_config_extra(config).get("response_timeout_seconds") or 90.0), 120.0))
        self._futures: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._active_ticket_by_chat: dict[str, str] = {}
        # A Gateway may ask the platform adapter to acknowledge the same
        # terminal output twice while it winds down a turn.  Remembering this
        # short-lived transport fact lets us acknowledge the duplicate without
        # staging a second reply or reopening a completed business turn.
        self._completed_ticket_by_chat: dict[str, str] = {}
        self._lock = asyncio.Lock()
        # ``transform_llm_output`` may run on Hermes' agent worker while
        # ``submit_ticket`` waits on the Gateway loop.  Ticket terminalisation
        # therefore needs a small process-local lock in addition to the
        # durable SQLite compare-and-set below.
        self._terminal_lock = threading.RLock()
        self._future_loops: dict[str, asyncio.AbstractEventLoop] = {}
        self._drain_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._toolsets_ready_notice_emitted = False
        self._capability_discovery_attempted = False
        global _ACTIVE_ADAPTER
        _ACTIVE_ADAPTER = self

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self.ticket_db_path.exists():
            return False
        # A Gateway restart can happen after this adapter atomically changes
        # an issued ticket to ``dispatched`` but before Hermes has claimed it
        # through the Runtime Contract.  There is no Agent turn in that state
        # to replay, so safely return only those *unclaimed* tickets to the
        # public adapter queue.  Tickets already agent_claimed are deliberately
        # left untouched: they may have reached a protected Tool and must be
        # resolved by Receipt/reply recovery, never blindly redelivered.
        self._requeue_unclaimed_dispatches()
        self._running = True
        self._stop.clear()
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain_issued_tickets())
        return True

    def _requeue_unclaimed_dispatches(self) -> int:
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE agenda_service_tickets SET state='issued', dispatched_at='', updated_at=? "
                "WHERE state='dispatched' AND agent_claimed_at=''",
                (_utc_now(),),
            ).rowcount
        return int(changed or 0)

    async def disconnect(self) -> None:
        self._running = False
        self._stop.set()
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None
        # On a graceful Gateway shutdown, a claimed service turn may already
        # have reached a protected Tool even though its final reply has not
        # returned. Preserve that business truth for the shared outbox and
        # mark the service ticket terminal; never re-dispatch the Agent turn.
        # If no Receipt exists, this remains an explicit inconclusive outcome.
        async with self._lock:
            active_tickets = tuple(self._active_ticket_by_chat.values())
        for ticket_id in active_tickets:
            self._stage_terminal(
                ticket_id,
                terminal_state="failed",
                provider_succeeded=None,
                reply_text="",
                trace_ref="agenda-service:gateway-disconnect",
            )
            await self._complete(ticket_id, {
                "ok": False,
                "state": "failed",
                "operation_id": self._ticket_message_id(ticket_id),
                "error": "agenda_service_gateway_disconnected",
                "message": "Gateway 在最终回复前停止；未声明任何新的业务成功。",
                "delivery_status": "agent_interrupted",
            })
        async with self._lock:
            self._active_ticket_by_chat.clear()
            self._completed_ticket_by_chat.clear()
        global _ACTIVE_ADAPTER
        if _ACTIVE_ADAPTER is self:
            _ACTIVE_ADAPTER = None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"id": str(chat_id), "type": PLATFORM_NAME, "is_group": False}

    def _source_toolsets_ready(self) -> bool:
        """Wait for XiaoYou's publicly registered Tool surface at cold start.

        Hermes loads plugins asynchronously.  A service ticket is durable, so
        dispatching it during that small window would create a tool-less Agent
        turn even though its source-scoped capability is moments away from
        registration.  This is lifecycle readiness only: it neither reads a
        Work payload nor chooses an action.  The registry query is the same
        public Toolset surface Hermes uses to resolve plugin capabilities.
        """

        # Platform adapters are connected before the first Agent is built.
        # Ask Hermes' public plugin discovery seam to load the already
        # configured XiaoYou capability package once, rather than waiting for
        # an Agent construction that itself depends on this ticket.  This is
        # package lifecycle only; it supplies no Work data or business choice.
        if not self._capability_discovery_attempted:
            self._capability_discovery_attempted = True
            try:
                from hermes_cli.plugins import discover_plugins

                discover_plugins()
            except Exception:
                logger.exception("agenda_service_xiaoyou_capability_discovery_failed")
        try:
            from tools.registry import registry

            required = {
                "agenda_service": {"agenda_read_current_work_facts"},
                "agenda_task": {
                    "agenda_task_read_current_work_facts",
                    "agenda_task_contact_current_task_party",
                    "agenda_schedule_current_task_recheck",
                },
                "agenda_governance": {"agenda_governance_read_current_work_facts"},
            }
            ready = all(
                names.issubset(set(registry.get_tool_names_for_toolset(toolset)))
                for toolset, names in required.items()
            )
        except Exception:
            ready = False
        if not ready and not self._toolsets_ready_notice_emitted:
            self._toolsets_ready_notice_emitted = True
            logger.info("agenda_service_waiting_for_xiaoyou_toolsets")
        return ready

    def toolsets_for_source(self, source: SessionSource) -> list[str] | None:
        """Use Hermes' public source-scoped Tool resolver for service work.

        This is a static capability of the authenticated platform, not a
        content classification or a business routing decision.  The Gateway
        validates these keys against normal configuration after the plugin's
        explicit ``tools.override`` consent grant.
        """

        if str(getattr(source, "platform", "")) != PLATFORM_NAME:
            return None
        # A Workspace task and a Hermes work-item have different trusted
        # write contracts.  Determine only the *server-attested source kind*
        # from the opaque ticket partition; never inspect payload prose,
        # decide task priority, or select a business operation.  A task gets
        # a read-only fact surface so a model cannot mistake it for a
        # work-item lifecycle writer.
        session_id = str(getattr(source, "thread_id", "") or "").strip()
        if session_id:
            try:
                with self._connect() as connection:
                    row = connection.execute(
                        "SELECT work_ids_json FROM agenda_service_tickets "
                        "WHERE session_id=? AND service_identity=? AND state IN ('dispatched','agent_claimed') "
                        "ORDER BY updated_at DESC LIMIT 1",
                        (session_id, str(getattr(source, "user_id", "") or "")),
                    ).fetchone()
                    if row is not None:
                        work_ids = [str(value) for value in json.loads(str(row["work_ids_json"]))]
                        kinds = {
                            str(item["source_kind"])
                            for item in connection.execute(
                                "SELECT source_kind FROM work_facts WHERE work_id IN (" + ",".join("?" for _ in work_ids) + ")",
                                tuple(work_ids),
                            ).fetchall()
                        } if work_ids else set()
                        if kinds == {"workspace_task"}:
                            return ["agenda_task", "no_mcp"]
                        if kinds == {"personnel_service_governance"}:
                            return ["agenda_governance", "no_mcp"]
            except (sqlite3.Error, ValueError, TypeError):
                # Returning no model-visible business surface is safer than
                # guessing which source contract applies.
                return ["no_mcp"]
        return ["agenda_service", "no_mcp"]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.ticket_db_path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _trusted_chat_id(ticket: ServiceWorkTicket) -> str:
        """Derive a service chat scope from the attested work partition only.

        The source payload never participates.  Distinct trusted Workspace
        state versions therefore cannot share a Hermes conversation merely
        because they belong to the same service identity and tenant.
        """

        material = str(ticket.tenant_id) + "\x1f" + str(ticket.session_id)
        return "agenda-service:" + str(ticket.tenant_id) + ":" + hashlib.sha256(
            material.encode("utf-8")
        ).hexdigest()[:24]

    @staticmethod
    def _stage_terminal(
        ticket_id: str,
        *,
        terminal_state: str,
        provider_succeeded: bool | None,
        reply_text: str,
        trace_ref: str,
    ) -> bool:
        """Copy one terminal service fact into the existing durable outbox.

        The shared Work Runtime owns reply/delivery truth.  This adapter only
        passes the final Agent terminal to that boundary; it cannot create
        business content, select a Tool, or choose a delivery recipient.
        """

        try:
            import importlib

            stage_terminal = None
            module_name = ""
            last_error: Exception | None = None
            # Separate public plugins may be imported under a runtime-specific
            # package name.  First locate XiaoYou's *already loaded* Work
            # Runtime module by its own source identity and public bridge
            # method.  This intentionally does not inspect or call any
            # Hermes-private object: the manager is owned by XiaoYou and its
            # durable database is the shared capability contract.
            import sys
            for loaded_name, loaded_module in tuple(sys.modules.items()):
                source_file = str(getattr(loaded_module, "__file__", "") or "").replace("\\", "/")
                if not source_file.endswith("/tuoguan_core/direct_reply_recovery.py"):
                    continue
                try:
                    candidate = getattr(
                        loaded_module.get_direct_reply_recovery_manager(),
                        "stage_agenda_ticket_terminal",
                        None,
                    )
                    if callable(candidate):
                        stage_terminal = candidate
                        module_name = f"loaded:{loaded_name}"
                        break
                except AttributeError as exc:
                    last_error = exc
                    continue
            for name in (
                # Hermes loads plugins under this public plugin namespace in
                # the gateway process.  Prefer the already-loaded module so
                # the Agenda adapter and trusted-turn hook share one current
                # Work Runtime manager.
                "hermes_plugins.tuoguan_core.direct_reply_recovery",
                # These names make the adapter independently runnable for
                # isolated certification without assuming a private Hermes
                # loader detail.
                "tuoguan_core.direct_reply_recovery",
                "plugins.tuoguan_core.direct_reply_recovery",
            ) if stage_terminal is None else ():
                try:
                    module = importlib.import_module(name)
                    candidate = getattr(
                        module.get_direct_reply_recovery_manager(),
                        "stage_agenda_ticket_terminal",
                        None,
                    )
                    if callable(candidate):
                        stage_terminal = candidate
                        module_name = name
                        break
                    last_error = RuntimeError(f"{name}_lacks_agenda_reply_bridge")
                except (ImportError, AttributeError) as exc:
                    last_error = exc
                    continue
            if stage_terminal is None:
                logger.error(
                    "agenda_service_durable_reply_bridge_unavailable ticket_id=%s error=%s",
                    ticket_id,
                    type(last_error).__name__ if last_error else "module_not_found",
                )
                return False
            job = stage_terminal(
                ticket_id=str(ticket_id),
                terminal_state=str(terminal_state),
                provider_succeeded=provider_succeeded,
                final_reply_text=str(reply_text or ""),
                raw_trace_ref=str(trace_ref),
            )
            if job is None:
                logger.warning(
                    "agenda_service_durable_reply_not_staged ticket_id=%s terminal_state=%s reply_present=%s",
                    ticket_id, terminal_state, bool(str(reply_text or "").strip()),
                )
            else:
                logger.info(
                    "agenda_service_durable_reply_staged ticket_id=%s module=%s reply_id=%s",
                    ticket_id,
                    module_name,
                    str(getattr(job, "reply_id", "") or ""),
                )
            return job is not None or str(terminal_state) != "completed" or not str(reply_text or "").strip()
        except Exception:
            logger.exception("agenda_service_durable_reply_stage_failed ticket_id=%s", ticket_id)
            return False

    @staticmethod
    def _enforce_task_continuation_contract(ticket_id: str) -> bool:
        """Persist an explicit attention gap for an open task when necessary.

        This function has deliberately no payload, schedule, recipient, or
        business-action parameter. It can only ask XiaoYou's Workspace-owned
        contract whether the exact server-issued ticket left an open task
        without a model-selected future condition, then record that factual
        gap. Hermes remains the only component that can choose the successor
        time through its visible Tool.
        """

        try:
            import importlib
            import sys

            status_fn = None
            record_fn = None
            for _name, module in tuple(sys.modules.items()):
                source_file = str(getattr(module, "__file__", "") or "").replace("\\", "/")
                if not source_file.endswith("/tuoguan_core/agenda_task_lifecycle.py"):
                    continue
                status_fn = getattr(module, "task_attention_contract_status", None)
                record_fn = getattr(module, "record_task_continuation_required", None)
                if callable(status_fn) and callable(record_fn):
                    break
                status_fn = record_fn = None
            if status_fn is None or record_fn is None:
                for name in (
                    "hermes_plugins.tuoguan_core.agenda_task_lifecycle",
                    "tuoguan_core.agenda_task_lifecycle",
                    "plugins.tuoguan_core.agenda_task_lifecycle",
                ):
                    try:
                        module = importlib.import_module(name)
                        status_fn = getattr(module, "task_attention_contract_status", None)
                        record_fn = getattr(module, "record_task_continuation_required", None)
                        if callable(status_fn) and callable(record_fn):
                            break
                    except ImportError:
                        continue
            if not callable(status_fn) or not callable(record_fn):
                logger.error("agenda_service_task_continuation_contract_unavailable ticket_id=%s", ticket_id)
                return False
            status = dict(status_fn(ticket_id=str(ticket_id)))
            if not bool(status.get("applies")) or bool(status.get("satisfied")):
                return True
            recorded = dict(record_fn(ticket_id=str(ticket_id), contact_staged=True))
            if bool(recorded.get("recorded")):
                logger.warning(
                    "agenda_service_task_continuation_required ticket_id=%s task_id=%s",
                    ticket_id,
                    str(recorded.get("task_id") or ""),
                )
                return True
            logger.error(
                "agenda_service_task_continuation_record_failed ticket_id=%s reason=%s",
                ticket_id,
                str(recorded.get("reason") or "unknown"),
            )
            return False
        except Exception:
            logger.exception("agenda_service_task_continuation_contract_failed ticket_id=%s", ticket_id)
            return False

    @staticmethod
    def _row_to_ticket(row: sqlite3.Row) -> ServiceWorkTicket:
        ticket = ServiceWorkTicket(
            ticket_id=str(row["ticket_id"]), tenant_id=str(row["tenant_id"]),
            service_identity=str(row["service_identity"]), source_identity=str(row["source_identity"]),
            session_id=str(row["session_id"]), turn_id=str(row["turn_id"]),
            message_id=str(row["message_id"]), model_message=str(row["model_message"]),
            payload_sha256=str(row["payload_sha256"]),
        )
        expected = SERVICE_PREFIX + ticket.tenant_id
        if ticket.service_identity != expected or not ticket.model_message:
            raise AgendaServiceIngressRejected("agenda_service_ticket_identity_invalid")
        if hashlib.sha256(ticket.model_message.encode("utf-8")).hexdigest() != ticket.payload_sha256:
            raise AgendaServiceIngressRejected("agenda_service_ticket_payload_invalid")
        return ticket

    def _claim_issued_ticket(self, ticket_id: str) -> ServiceWorkTicket:
        if not ticket_id or len(ticket_id) > 160:
            raise AgendaServiceIngressRejected("agenda_service_ticket_id_invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM agenda_service_tickets WHERE ticket_id=?", (ticket_id,)
                ).fetchone()
                if row is None or str(row["state"]) != "issued":
                    raise AgendaServiceIngressRejected("agenda_service_ticket_not_issued")
                if float(row["expires_at"]) < datetime.now(timezone.utc).timestamp():
                    connection.execute(
                        "UPDATE agenda_service_tickets SET state='expired', updated_at=? WHERE ticket_id=?",
                        (_utc_now(), ticket_id),
                    )
                    raise AgendaServiceIngressRejected("agenda_service_ticket_expired")
                ticket = self._row_to_ticket(row)
                changed = connection.execute(
                    "UPDATE agenda_service_tickets SET state='dispatched', dispatched_at=?, updated_at=? "
                    "WHERE ticket_id=? AND state='issued'",
                    (_utc_now(), _utc_now(), ticket_id),
                ).rowcount
                if changed != 1:
                    raise AgendaServiceIngressRejected("agenda_service_ticket_claim_race")
                connection.execute("COMMIT")
                return ticket
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def submit_ticket(self, ticket_id: str) -> dict[str, Any]:
        """Server-only public ingress.  An opaque issued id is the whole API."""

        # Keep the ticket in ``issued`` until the capability package has
        # registered its attested surfaces.  An unready runtime is not an
        # Agent attempt and must not consume the durable work fact.
        if not self._source_toolsets_ready():
            raise AgendaServiceIngressRejected("agenda_service_toolsets_not_ready")
        ticket = self._claim_issued_ticket(str(ticket_id))
        # Hermes owns the opaque Agent session/turn ids and may replace the
        # service ticket's durable partition id when it creates the turn.  Do
        # not pre-bind the ticket to those placeholders here: that would make
        # the real public pre-LLM hook appear uncorrelated.  The hook is the
        # one trusted moment that sees Hermes' actual public ids; it atomically
        # claims this ``dispatched`` ticket, binds the Runtime Contract, and
        # captures the durable reply turn *before* the model can use a Tool.
        # This remains identity/lifecycle plumbing only—no source prose is
        # examined and the Adapter still cannot select a Tool or a recipient.
        operation_id = ticket.message_id
        chat_id = self._trusted_chat_id(ticket)
        async with self._lock:
            if chat_id in self._active_ticket_by_chat:
                raise AgendaServiceIngressRejected("agenda_service_partition_busy")
            future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._completed_ticket_by_chat.pop(chat_id, None)
            self._active_ticket_by_chat[chat_id] = ticket.ticket_id
            self._futures[ticket.ticket_id] = future
            self._future_loops[ticket.ticket_id] = asyncio.get_running_loop()
        event = self._build_event(ticket, chat_id=chat_id)
        try:
            await self._dispatch(event)
            response = await asyncio.wait_for(asyncio.shield(future), timeout=self.response_timeout_seconds)
            return deepcopy(response)
        except asyncio.TimeoutError:
            self._stage_terminal(
                ticket.ticket_id,
                terminal_state="failed",
                provider_succeeded=False,
                reply_text="",
                trace_ref="agenda-service:reply-timeout",
            )
            return await self._complete(ticket.ticket_id, {
                "ok": False, "state": "timeout", "operation_id": operation_id,
                "error": "agenda_service_reply_timeout",
                "message": "Hermes 未在时限内产生最终回复；未声明任何业务成功。",
            })
        except Exception as exc:
            self._stage_terminal(
                ticket.ticket_id,
                terminal_state="failed",
                provider_succeeded=False,
                reply_text="",
                trace_ref="agenda-service:dispatch-failed:" + type(exc).__name__,
            )
            return await self._complete(ticket.ticket_id, {
                "ok": False, "state": "failed", "operation_id": operation_id,
                "error": type(exc).__name__,
                "message": "Agenda 服务工作未获得 Hermes 有效终态；未声明任何业务成功。",
            })
        finally:
            async with self._lock:
                self._active_ticket_by_chat.pop(chat_id, None)
                self._future_loops.pop(ticket.ticket_id, None)

    async def send(
        self, chat_id: str, content: str, reply_to: str | None = None, metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Acknowledge Gateway transport sends without deciding terminality.

        Hermes can call a platform adapter while an Agent is still running:
        stream fragments, commentary before a Tool boundary, and status
        messages share this public method with the true final reply.  The
        adapter cannot infer lifecycle from model prose.  The only terminal
        transition is therefore made by :meth:`settle_public_model_terminal`,
        which is invoked from Hermes' documented final-output lifecycle hook.
        """

        normalized_chat_id = str(chat_id)
        with self._terminal_lock:
            ticket_id = self._active_ticket_by_chat.get(normalized_chat_id)
        if not ticket_id:
            completed_ticket = self._completed_ticket_by_chat.get(normalized_chat_id)
            if completed_ticket:
                # Delivery truth was already staged exactly once.  This is a
                # duplicate gateway acknowledgement, not a new user-facing
                # message and never creates another outbox job.
                logger.info(
                    "agenda_service_duplicate_terminal_ack chat_hash=%s ticket_id=%s",
                    hashlib.sha256(normalized_chat_id.encode("utf-8")).hexdigest()[:16],
                    completed_ticket,
                )
                return _result(ok=True, message_id=self._ticket_message_id(completed_ticket))
            logger.warning(
                "agenda_service_reply_uncorrelated chat_hash=%s active_partitions=%s content_length=%s metadata_keys=%s",
                hashlib.sha256(normalized_chat_id.encode("utf-8")).hexdigest()[:16],
                len(self._active_ticket_by_chat),
                len(str(content or "")),
                sorted(str(key) for key in (metadata or {}).keys()),
            )
            return _result(ok=False, error="agenda_service_reply_uncorrelated")
        metadata_map = metadata if isinstance(metadata, dict) else {}
        if self._is_intermediate_gateway_send(metadata_map):
            logger.info(
                "agenda_service_intermediate_send_ignored ticket_id=%s metadata_keys=%s",
                ticket_id,
                sorted(str(key) for key in metadata_map),
            )
            return _result(ok=True, message_id="agenda_service_intermediate_ack")
        # A regular adapter send is only transport acknowledgement.  It is not
        # enough evidence that the Agent loop has finished: only the public
        # terminal lifecycle bridge may stage a durable reply, ack the ticket,
        # or release the bound trusted turn.
        logger.info(
            "agenda_service_send_deferred_to_public_terminal ticket_id=%s content_length=%s metadata_keys=%s",
            ticket_id,
            len(str(content or "")),
            sorted(str(key) for key in metadata_map),
        )
        return _result(ok=True, message_id=self._ticket_message_id(ticket_id))

    @staticmethod
    def _is_intermediate_gateway_send(metadata: dict[str, Any]) -> bool:
        """Recognise documented transport lifecycle metadata, never prose.

        ``_interim_send`` and ``expect_edits`` are Hermes Gateway metadata
        contracts for commentary/progress and mutable previews.  The approval
        marker is likewise a framework control surface.  No user text, model
        wording, Tool name, or business content participates in this test.
        """

        return (
            metadata.get("_interim_send") is True
            or metadata.get("expect_edits") is True
            or metadata.get("is_approval_prompt") is True
        )

    @staticmethod
    def _framework_terminal_reply_unavailable(value: str) -> bool:
        """Recognise only Hermes' own final diagnostics, never business text."""

        lowered = str(value or "").strip().lower()
        return not lowered or any(
            marker in lowered
            for marker in (
                "model returned no content after all retries",
                "the request failed:",
                "processing completed but no response was generated",
                "iteration budget exhausted",
            )
        )

    def settle_public_model_terminal(
        self,
        *,
        chat_id: str,
        message_id: str,
        session_id: str,
        turn_id: str,
        final_reply_text: str,
        trace_ref: str,
    ) -> bool:
        """Settle an Agenda ticket from Hermes' public final-output hook.

        The bridge verifies the already attested chat/message binding before
        it touches delivery state.  It owns no business decision: the model
        has already completed its loop and any desired Agenda Tool calls.
        """

        normalized_chat_id = str(chat_id)
        expected_message_id = str(message_id)
        with self._terminal_lock:
            ticket_id = self._active_ticket_by_chat.get(normalized_chat_id)
            if not ticket_id:
                completed_ticket = self._completed_ticket_by_chat.get(normalized_chat_id)
                if completed_ticket:
                    logger.info(
                        "agenda_service_public_terminal_duplicate ticket_id=%s session_id=%s",
                        completed_ticket,
                        str(session_id)[:32],
                    )
                    return True
                logger.error(
                    "agenda_service_public_terminal_uncorrelated chat_hash=%s session_id=%s",
                    hashlib.sha256(normalized_chat_id.encode("utf-8")).hexdigest()[:16],
                    str(session_id)[:32],
                )
                return False
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT message_id,state FROM agenda_service_tickets WHERE ticket_id=?",
                    (ticket_id,),
                ).fetchone()
            if row is None or str(row["state"]) != "agent_claimed":
                logger.error(
                    "agenda_service_public_terminal_invalid_ticket ticket_id=%s state=%s",
                    ticket_id,
                    str(row["state"]) if row is not None else "missing",
                )
                return False
            if str(row["message_id"]) != expected_message_id:
                logger.error(
                    "agenda_service_public_terminal_message_mismatch ticket_id=%s",
                    ticket_id,
                )
                return False
            unavailable = self._framework_terminal_reply_unavailable(final_reply_text)
            terminal_state = "failed" if unavailable else "completed"
            if not self._stage_terminal(
                ticket_id,
                terminal_state=terminal_state,
                provider_succeeded=False if unavailable else True,
                reply_text="" if unavailable else str(final_reply_text),
                trace_ref=str(trace_ref),
            ):
                self._complete_from_public_terminal(
                    ticket_id,
                    {
                        "ok": False,
                        "state": "failed",
                        "operation_id": self._ticket_message_id(ticket_id),
                        "error": "agenda_service_durable_reply_stage_failed",
                        "message": "Hermes 已结束回合，但最终回复未能进入耐久投递；未声明新的业务成功。",
                        "delivery_status": "durable_reply_stage_failed",
                    },
                )
                return False
            if not unavailable and not self._enforce_task_continuation_contract(ticket_id):
                self._complete_from_public_terminal(
                    ticket_id,
                    {
                        "ok": False,
                        "state": "failed",
                        "operation_id": self._ticket_message_id(ticket_id),
                        "error": "agenda_service_task_continuation_contract_failed",
                        "message": "Hermes 已结束回合，但未能持久化未终态事项的关注缺口；未声明新的业务成功。",
                        "delivery_status": "task_continuation_contract_failed",
                    },
                )
                return False
            response = {
                "ok": not unavailable,
                "state": terminal_state,
                "operation_id": self._ticket_message_id(ticket_id),
                "reply_text": "" if unavailable else str(final_reply_text),
                "delivery_status": "agent_terminal_unavailable" if unavailable else "service_ledger_recorded",
            }
            self._complete_from_public_terminal(ticket_id, response)
            self._completed_ticket_by_chat[normalized_chat_id] = ticket_id
            logger.info(
                "agenda_service_public_terminal_settled ticket_id=%s state=%s session_id=%s turn_id=%s",
                ticket_id,
                terminal_state,
                str(session_id)[:32],
                str(turn_id)[:32],
            )
            return bool(response["ok"])

    def _complete_from_public_terminal(self, ticket_id: str, response: dict[str, Any]) -> None:
        """Durably complete a ticket and wake the awaiting Gateway coroutine."""

        with self._connect() as connection:
            connection.execute(
                "UPDATE agenda_service_tickets SET state=?, reply_text=?, delivery_state=?, completed_at=?, updated_at=? "
                "WHERE ticket_id=? AND state='agent_claimed'",
                (
                    str(response.get("state") or "completed"),
                    str(response.get("reply_text") or ""),
                    str(response.get("delivery_status") or "failed"),
                    _utc_now(),
                    _utc_now(),
                    ticket_id,
                ),
            )
        future = self._futures.pop(ticket_id, None)
        loop = self._future_loops.get(ticket_id)
        if future is None or future.done():
            return

        def settle_future() -> None:
            if not future.done():
                future.set_result(deepcopy(response))

        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(settle_future)
        else:
            settle_future()

    def _next_issued_ticket_id(self) -> str:
        with self._connect() as connection:
            now = datetime.now(timezone.utc).timestamp()
            connection.execute(
                "UPDATE agenda_service_tickets SET state='expired',updated_at=? WHERE state='issued' AND expires_at<?",
                (_utc_now(), now),
            )
            row = connection.execute(
                "SELECT ticket_id FROM agenda_service_tickets WHERE state='issued' AND expires_at>=? ORDER BY rowid LIMIT 1",
                (now,),
            ).fetchone()
        return str(row["ticket_id"]) if row is not None else ""

    async def _drain_issued_tickets(self) -> None:
        """Consume only server-issued opaque tickets in one service partition."""

        while not self._stop.is_set():
            progressed = False
            try:
                ticket_id = self._next_issued_ticket_id()
                if ticket_id:
                    try:
                        await self.submit_ticket(ticket_id)
                        progressed = True
                    except AgendaServiceIngressRejected:
                        # A concurrent/expired ticket remains durable for the
                        # scheduler to reconcile. No alternate ingress or
                        # business fallback is attempted here.
                        progressed = False
                    except Exception:
                        progressed = True
            except asyncio.CancelledError:
                raise
            except Exception:
                progressed = False
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.15 if progressed else 0.8)
            except asyncio.TimeoutError:
                continue

    def _ticket_message_id(self, ticket_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT message_id FROM agenda_service_tickets WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
        return str(row["message_id"]) if row is not None else ""

    async def _complete(self, ticket_id: str, response: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                "UPDATE agenda_service_tickets SET state=?, reply_text=?, delivery_state=?, completed_at=?, updated_at=? "
                "WHERE ticket_id=?",
                (str(response.get("state") or "completed"), str(response.get("reply_text") or ""),
                 str(response.get("delivery_status") or "failed"), _utc_now(), _utc_now(), ticket_id),
            )
        async with self._lock:
            future = self._futures.pop(ticket_id, None)
            if future is not None and not future.done():
                future.set_result(deepcopy(response))
        return deepcopy(response)

    def _build_event(self, ticket: ServiceWorkTicket, *, chat_id: str) -> MessageEvent:
        source = SessionSource(
            platform=_platform_value(), chat_id=chat_id, chat_name="XiaoYou Agenda service",
            chat_type="service", user_id=ticket.service_identity, user_name="小优 Agenda 服务",
            thread_id=ticket.session_id,
        )
        try:
            source.message_id = ticket.message_id
        except (AttributeError, TypeError):
            pass
        metadata = {
            "agenda_service_work": True, "service_ticket_id": ticket.ticket_id,
            "service_source_identity": ticket.source_identity,
            "service_payload_sha256": ticket.payload_sha256,
            "operation_id": ticket.message_id,
        }
        try:
            return MessageEvent(
                text=ticket.model_message, message_type=MessageType.TEXT,
                user_id=ticket.service_identity, user_name="小优 Agenda 服务", source=source,
                raw_message=None, message_id=ticket.message_id, metadata=metadata,
            )
        except TypeError:
            return MessageEvent(text=ticket.model_message, message_type=MessageType.TEXT, source=source, message_id=ticket.message_id, metadata=metadata)

    async def _dispatch(self, event: MessageEvent) -> None:
        inherited_handler = getattr(self, "handle_message", None)
        if callable(inherited_handler):
            await inherited_handler(event)
            return
        handler = getattr(self, "_message_handler", None)
        if not callable(handler):
            raise RuntimeError("agenda_service_message_handler_missing")
        result = handler(event)
        if asyncio.iscoroutine(result):
            await result


def settle_agenda_ticket_from_public_model_terminal(
    *,
    chat_id: str,
    message_id: str,
    session_id: str,
    turn_id: str,
    final_reply_text: str,
    trace_ref: str,
) -> bool:
    """Public bridge called only from XiaoYou's Hermes final-output hook.

    This is intentionally a narrow capability API.  It cannot accept an
    Agenda payload, invent an actor, select a Tool, or make a business
    decision; it merely settles the one live, server-issued ticket whose
    already-attested turn has just reached Hermes' documented terminal hook.
    """

    adapter = _ACTIVE_ADAPTER
    if not isinstance(adapter, AgendaServiceWorkAdapter):
        logger.error("agenda_service_public_terminal_adapter_unavailable")
        return False
    return adapter.settle_public_model_terminal(
        chat_id=chat_id,
        message_id=message_id,
        session_id=session_id,
        turn_id=turn_id,
        final_reply_text=final_reply_text,
        trace_ref=trace_ref,
    )
