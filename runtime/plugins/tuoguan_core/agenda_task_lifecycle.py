"""Receipt-backed lifecycle scheduling for one attested Workspace task.

This is deliberately a tiny state port, not an Agenda planner. Hermes has
already selected the declared Tool and selected a next review time. The port
only proves that the task is the single task carried by the current,
server-attested Agenda ticket, persists that structural state through the
normal capability write fence, and verifies it afterwards.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from contextlib import closing
from pathlib import Path
from typing import Any
import sqlite3


class AgendaTaskLifecycleError(ValueError):
    """The selected lifecycle Tool cannot safely act on this service turn."""


_CLOSED_TASK_STATES = frozenset({"completed", "closed", "cancelled", "superseded", "archived"})
_MIN_RECHECK_DELAY = timedelta(minutes=15)
_MAX_RECHECK_DELAY = timedelta(days=31)


def _workspace_data_dir() -> Path:
    """Resolve XiaoYou's authority-owned Workspace, never a Hermes session.

    The continuation invariant is an Institution Workspace lifecycle fact. It
    must therefore survive a Gateway restart and cannot be inferred from a
    model response, an opaque Hermes session id, or a channel payload.
    """

    raw = str(os.getenv("HERMES_TUOGUAN_DATA_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    workspace = str(os.getenv("XIAOYOU_INSTITUTION_WORKSPACE") or "").strip()
    if workspace:
        root = Path(workspace).expanduser().resolve()
        return root / "data" if (root / "data").is_dir() else root
    raise AgendaTaskLifecycleError("agenda_task_workspace_missing")


def _ticket_task_binding_from_database(*, ticket_id: str, database_path: str | None = None) -> dict[str, str] | None:
    """Resolve one attested task identity from a server-issued Agenda ticket.

    This consumes only immutable ticket/fact metadata. In particular, no model
    text, recipient hint, tenant argument, or task selector can influence the
    result. ``None`` means that this is simply not a one-task Agenda ticket.
    """

    database = str(database_path or os.getenv("XIAOYOU_AGENDA_SERVICE_INGRESS_DB") or "").strip()
    if not database or not str(ticket_id or "").strip():
        return None
    try:
        with closing(sqlite3.connect(database, timeout=5.0)) as connection:
            connection.row_factory = sqlite3.Row
            ticket = connection.execute(
                "SELECT ticket_id,tenant_id,destination_json,work_ids_json FROM agenda_service_tickets WHERE ticket_id=?",
                (str(ticket_id),),
            ).fetchone()
            if ticket is None:
                return None
            work_ids = [str(value) for value in json.loads(str(ticket["work_ids_json"]))]
            if len(work_ids) != 1:
                return None
            fact = connection.execute(
                "SELECT payload_ref,source_kind FROM work_facts WHERE work_id=? AND tenant_id=?",
                (work_ids[0], str(ticket["tenant_id"])),
            ).fetchone()
            if fact is None or str(fact["source_kind"] or "") != "workspace_task":
                return None
            payload = connection.execute(
                "SELECT payload_json FROM agenda_workspace_payloads WHERE payload_ref=? AND tenant_id=?",
                (str(fact["payload_ref"]), str(ticket["tenant_id"])),
            ).fetchone()
            if payload is None:
                return None
            decoded = json.loads(str(payload["payload_json"]))
            destination = json.loads(str(ticket["destination_json"]))
    except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
        return None
    task_id = str(decoded.get("task_id") or "").strip() if isinstance(decoded, dict) else ""
    recipient_id = str(destination.get("recipient_id") or "").strip() if isinstance(destination, dict) else ""
    tenant_id = str(ticket["tenant_id"] or "").strip()
    if not task_id or not recipient_id or not tenant_id:
        return None
    return {
        "ticket_id": str(ticket["ticket_id"]),
        "tenant_id": tenant_id,
        "task_id": task_id,
        "recipient_id": recipient_id,
    }


def _live_task_for_binding(binding: dict[str, str]) -> tuple[Any, dict[str, Any] | None]:
    from .store import TuoguanStore

    store = TuoguanStore(_workspace_data_dir())
    current = next(
        (
            row for row in store.load_tasks()
            if isinstance(row, dict) and str(row.get("id") or "") == binding["task_id"]
        ),
        None,
    )
    if not isinstance(current, dict):
        return store, None
    if (
        str(current.get("tenant_id") or "") != binding["tenant_id"]
        or str(current.get("assignee_userid") or "") != binding["recipient_id"]
    ):
        return store, None
    return store, current


def task_attention_contract_status(*, ticket_id: str, now: datetime | None = None) -> dict[str, Any]:
    """Return the structural continuation status for one actual Agenda turn.

    A nonterminal task is satisfied only by a *future* follow-up persisted for
    this exact attested ticket, or by a later authoritative terminal task
    state. This is a lifecycle verifier; it never chooses a follow-up time.
    """

    binding = _ticket_task_binding_from_database(ticket_id=str(ticket_id))
    if binding is None:
        return {"applies": False, "satisfied": True, "reason": "not_workspace_task"}
    _, current = _live_task_for_binding(binding)
    if current is None:
        return {"applies": True, "satisfied": False, "reason": "task_binding_no_longer_current", **binding}
    status = str(current.get("status") or "pending").strip().lower()
    if status in _CLOSED_TASK_STATES:
        return {"applies": True, "satisfied": True, "reason": "task_terminal", **binding}
    followup = current.get("agenda_followup") if isinstance(current.get("agenda_followup"), dict) else {}
    scheduled = _parse_time(followup.get("next_attention_at"))
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if (
        str(followup.get("state") or "") == "scheduled"
        and str(followup.get("ticket_id") or "") == binding["ticket_id"]
        and scheduled is not None
        and scheduled > reference
    ):
        return {
            "applies": True,
            "satisfied": True,
            "reason": "future_attention_scheduled",
            "next_attention_at": _utc_text(scheduled),
            **binding,
        }
    return {"applies": True, "satisfied": False, "reason": "future_attention_missing", **binding}


def record_task_continuation_required(*, ticket_id: str, contact_staged: bool) -> dict[str, Any]:
    """Persist a content-free integrity gap after a normal Agenda terminal.

    This is deliberately not a business Tool or a reminder scheduler. The
    backend records only that an open task left its turn without a future
    condition. Agenda exposes that durable fact back to the same Hermes model,
    which retains sole authority to select a later review time and any wording.
    """

    status = task_attention_contract_status(ticket_id=str(ticket_id))
    if not bool(status.get("applies")) or bool(status.get("satisfied")):
        return {**status, "recorded": False}
    binding = {key: str(status[key]) for key in ("ticket_id", "tenant_id", "task_id", "recipient_id")}
    store, current = _live_task_for_binding(binding)
    if current is None:
        return {**status, "recorded": False, "reason": "task_binding_no_longer_current"}
    recorded_at = _utc_text(datetime.now(timezone.utc))

    def update(row: dict[str, Any]) -> dict[str, Any]:
        if str(row.get("id") or "") != binding["task_id"]:
            return row
        continuation = row.get("agenda_continuation") if isinstance(row.get("agenda_continuation"), dict) else {}
        contacts = row.get("agenda_contact_history") if isinstance(row.get("agenda_contact_history"), list) else []
        contact_rows = [dict(value) for value in contacts if isinstance(value, dict)]
        if contact_staged and not any(str(value.get("ticket_id") or "") == binding["ticket_id"] for value in contact_rows):
            contact_rows.append({
                "schema_version": "xiaoyou.agenda-contact.v1",
                "ticket_id": binding["ticket_id"],
                "recipient_id": binding["recipient_id"],
                "state": "durable_reply_staged",
                "recorded_at": recorded_at,
            })
        if (
            str(continuation.get("state") or "") == "required"
            and str(continuation.get("ticket_id") or "") == binding["ticket_id"]
        ):
            return {**row, "agenda_contact_history": contact_rows[-100:]}
        return {
            **row,
            "agenda_continuation": {
                "schema_version": "xiaoyou.agenda-task-continuation.v1",
                "state": "required",
                "ticket_id": binding["ticket_id"],
                "required_at": recorded_at,
                "reason": "future_attention_missing",
            },
            "agenda_contact_history": contact_rows[-100:],
            "updated_at": recorded_at,
        }

    persisted = store.update_task(binding["task_id"], update)
    continuation = persisted.get("agenda_continuation") if isinstance(persisted, dict) else {}
    recorded = bool(
        isinstance(persisted, dict)
        and str(continuation.get("state") or "") == "required"
        and str(continuation.get("ticket_id") or "") == binding["ticket_id"]
    )
    return {**status, "recorded": recorded, "required_at": recorded_at}


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _current_task_binding(service: Any) -> dict[str, str]:
    """Load one task fact from the active authenticated service ticket.

    The handler receives no model-provided recipient, tenant or task scope.
    Any missing or ambiguous ticket record fails closed.
    """

    from .runtime_contract import current_trusted_turn

    turn = current_trusted_turn()
    database = str(os.getenv("XIAOYOU_AGENDA_SERVICE_INGRESS_DB") or "").strip()
    if turn is None or not database or str(getattr(turn, "platform", "") or "") != "agenda_service_work":
        raise AgendaTaskLifecycleError("agenda_task_turn_untrusted")
    tenant_id = str(getattr(turn, "tenant_id", "") or "").strip()
    actor = str(getattr(service.identity, "canonical_user_id", "") or "").strip()
    if not tenant_id or not actor:
        raise AgendaTaskLifecycleError("agenda_task_identity_missing")
    try:
        with closing(sqlite3.connect(database, timeout=5.0)) as connection:
            connection.row_factory = sqlite3.Row
            ticket = connection.execute(
                "SELECT ticket_id, message_id, work_ids_json, destination_json FROM agenda_service_tickets "
                "WHERE tenant_id=? AND service_identity=? AND agent_session_id=? AND agent_turn_id=? AND state='agent_claimed'",
                (tenant_id, actor, str(getattr(turn, "session_id", "") or ""), str(getattr(turn, "turn_id", "") or "")),
            ).fetchone()
            if ticket is None:
                raise AgendaTaskLifecycleError("agenda_task_ticket_missing")
            work_ids = [str(value) for value in json.loads(str(ticket["work_ids_json"]))]
            if len(work_ids) != 1:
                raise AgendaTaskLifecycleError("agenda_task_ticket_ambiguous")
            fact = connection.execute(
                "SELECT payload_ref, source_kind FROM work_facts WHERE work_id=? AND tenant_id=?",
                (work_ids[0], tenant_id),
            ).fetchone()
            if fact is None or str(fact["source_kind"] or "") != "workspace_task":
                raise AgendaTaskLifecycleError("agenda_task_source_not_attested")
            payload = connection.execute(
                "SELECT payload_json FROM agenda_workspace_payloads WHERE payload_ref=? AND tenant_id=?",
                (str(fact["payload_ref"]), tenant_id),
            ).fetchone()
            if payload is None:
                raise AgendaTaskLifecycleError("agenda_task_payload_missing")
            decoded = json.loads(str(payload["payload_json"]))
            destination = json.loads(str(ticket["destination_json"]))
    except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
        if isinstance(exc, AgendaTaskLifecycleError):
            raise
        raise AgendaTaskLifecycleError("agenda_task_ticket_unreadable") from exc
    task_id = str(decoded.get("task_id") or "").strip() if isinstance(decoded, dict) else ""
    recipient_id = str(destination.get("recipient_id") or "").strip() if isinstance(destination, dict) else ""
    if not task_id or not recipient_id:
        raise AgendaTaskLifecycleError("agenda_task_binding_incomplete")
    return {
        "tenant_id": tenant_id,
        "task_id": task_id,
        "recipient_id": recipient_id,
        "ticket_id": str(ticket["ticket_id"]),
        "message_id": str(ticket["message_id"]),
    }


def current_task_contact_target(service: Any) -> dict[str, str]:
    """Return the one server-attested responsible party for this task turn.

    This is a scope proof, not an Agenda decision: it reads the sole task
    carried by the currently claimed ticket and verifies that its current
    assignee is still the recipient embedded in that ticket.  A model never
    supplies a user, tenant, task or recipient to this function.
    """

    binding = _current_task_binding(service)
    current = next(
        (
            row for row in service.store.load_tasks()
            if isinstance(row, dict) and str(row.get("id") or "") == binding["task_id"]
        ),
        None,
    )
    if current is None:
        raise AgendaTaskLifecycleError("agenda_task_not_found")
    if str(current.get("tenant_id") or "") != binding["tenant_id"]:
        raise AgendaTaskLifecycleError("agenda_task_cross_tenant")
    if str(current.get("status") or "pending").strip().lower() in _CLOSED_TASK_STATES:
        raise AgendaTaskLifecycleError("agenda_task_already_terminal")
    target_user_id = str(current.get("assignee_userid") or "").strip()
    if not target_user_id or target_user_id != binding["recipient_id"]:
        raise AgendaTaskLifecycleError("agenda_task_recipient_binding_changed")
    try:
        from .staff_directory import query_staff_directory

        directory = query_staff_directory(
            service.store,
            query=target_user_id,
            include_inactive=False,
            limit=5,
        )
        profile = next(
            (
                row for row in (directory.get("staff") or [])
                if isinstance(row, dict)
                and str(row.get("user_id") or "") == target_user_id
                and bool(row.get("is_active_staff"))
            ),
            None,
        )
    except Exception as exc:
        raise AgendaTaskLifecycleError("agenda_task_responsible_party_unavailable") from exc
    if not isinstance(profile, dict):
        raise AgendaTaskLifecycleError("agenda_task_responsible_party_unavailable")
    role = str(profile.get("role") or "").strip()
    if role not in {"boss", "manager", "teacher"}:
        raise AgendaTaskLifecycleError("agenda_task_responsible_party_role_invalid")
    return {
        **binding,
        "target_user_id": target_user_id,
        "target_role": role,
        "target_name": str(profile.get("business_name") or profile.get("staff_name") or target_user_id),
    }


def current_task_contact_candidate_attested(*, store: Any, identity: Any, candidate: dict[str, Any]) -> bool:
    """Verify an Agenda service can execute only its own attested task contact.

    ``proactive_work`` calls this immediately before delivery.  This keeps the
    general proactive executor closed to service identities while allowing the
    narrowly scoped, model-selected task-party tool to use the shared durable
    outbox path.
    """

    try:
        service = type("AgendaServiceScope", (), {"store": store, "identity": identity})()
        target = current_task_contact_target(service)
    except Exception:
        return False
    return bool(
        str(candidate.get("tenant_id") or "") == target["tenant_id"]
        and str(candidate.get("agenda_ticket_id") or "") == target["ticket_id"]
        and str(candidate.get("related_task_id") or "") == target["task_id"]
        and str(candidate.get("target_user_id") or "") == target["target_user_id"]
        and str(candidate.get("target_role") or "") == target["target_role"]
        and str(candidate.get("action_type") or "") in {"ask_task_fact", "ask_task_result", "task_companion_followup"}
        and bool(candidate.get("work_related"))
    )


def record_current_task_contact_attempt(
    *,
    service: Any,
    target_user_id: str,
    candidate_id: str,
    delivery_state: str,
    delivery_id: str,
    operation_id: str,
) -> dict[str, Any]:
    """Append a factual contact attempt to the current task's audit history.

    The state is deliberately only a delivery observation.  It never means a
    person read the message, supplied the requested fact, or completed the
    task.  Later Agenda turns separately load the current outbox disposition.
    """

    target = current_task_contact_target(service)
    if str(target_user_id or "") != target["target_user_id"]:
        raise AgendaTaskLifecycleError("agenda_task_contact_target_changed")
    now = _utc_text(datetime.now(timezone.utc))

    def update(row: dict[str, Any]) -> dict[str, Any]:
        if str(row.get("id") or "") != target["task_id"]:
            return row
        history = row.get("agenda_contact_history")
        history = [dict(item) for item in history if isinstance(item, dict)] if isinstance(history, list) else []
        if any(str(item.get("operation_id") or "") == str(operation_id) for item in history):
            return row
        history.append({
            "schema_version": "xiaoyou.agenda-contact.v2",
            "kind": "model_selected_task_party_followup",
            "ticket_id": target["ticket_id"],
            "recipient_id": target["target_user_id"],
            "candidate_id": str(candidate_id or ""),
            "operation_id": str(operation_id),
            "delivery_state": str(delivery_state or "not_ready"),
            "delivery_id": str(delivery_id or ""),
            "recorded_at": now,
        })
        return {**row, "agenda_contact_history": history[-100:], "updated_at": now}

    persisted = service.store.update_task(target["task_id"], update)
    verified = any(
        isinstance(item, dict) and str(item.get("operation_id") or "") == str(operation_id)
        for item in (persisted.get("agenda_contact_history") or [])
    ) if isinstance(persisted, dict) else False
    return {
        "ok": verified,
        "task_id": target["task_id"],
        "ticket_id": target["ticket_id"],
        "writeback_verified": verified,
    }


def schedule_current_task_recheck(
    *,
    service: Any,
    task_id: str,
    next_attention_at: str,
    operation_id: str,
) -> dict[str, Any]:
    """Schedule the next structural review of the current attested task.

    This never marks a task complete and cannot select a different task,
    person, tenant or destination. The model's only policy choice is whether
    a further review is needed and, if so, a bounded time at which to ask the
    current Agenda to look again.
    """

    binding = _current_task_binding(service)
    if str(task_id or "").strip() != binding["task_id"]:
        raise AgendaTaskLifecycleError("agenda_task_id_not_current_ticket")
    requested_at = _parse_time(next_attention_at)
    now = datetime.now(timezone.utc)
    if requested_at is None:
        raise AgendaTaskLifecycleError("agenda_task_recheck_time_invalid")
    if requested_at < now + _MIN_RECHECK_DELAY or requested_at > now + _MAX_RECHECK_DELAY:
        raise AgendaTaskLifecycleError("agenda_task_recheck_time_out_of_bounds")
    task_rows = service.store.load_tasks()
    current = next(
        (row for row in task_rows if isinstance(row, dict) and str(row.get("id") or "") == binding["task_id"]),
        None,
    )
    if current is None:
        raise AgendaTaskLifecycleError("agenda_task_not_found")
    if str(current.get("tenant_id") or "") != binding["tenant_id"]:
        raise AgendaTaskLifecycleError("agenda_task_cross_tenant")
    if str(current.get("assignee_userid") or "") != binding["recipient_id"]:
        raise AgendaTaskLifecycleError("agenda_task_recipient_binding_changed")
    if str(current.get("status") or "pending").strip().lower() in _CLOSED_TASK_STATES:
        raise AgendaTaskLifecycleError("agenda_task_already_terminal")

    scheduled_at = _utc_text(now)
    attention_at = _utc_text(requested_at)
    prior_followup = current.get("agenda_followup") if isinstance(current.get("agenda_followup"), dict) else {}
    if (
        str(prior_followup.get("operation_id") or "") == str(operation_id)
        and str(prior_followup.get("next_attention_at") or "") == attention_at
    ):
        return {
            "ok": True,
            "task_id": binding["task_id"],
            "ticket_id": binding["ticket_id"],
            "next_attention_at": attention_at,
            "already_applied": True,
            "writeback_verified": True,
        }

    def update(row: dict[str, Any]) -> dict[str, Any]:
        if str(row.get("id") or "") != binding["task_id"]:
            return row
        history = row.get("agenda_followup_history")
        history = [dict(entry) for entry in history if isinstance(entry, dict)] if isinstance(history, list) else []
        continuation_history = row.get("agenda_continuation_history")
        continuation_history = (
            [dict(entry) for entry in continuation_history if isinstance(entry, dict)]
            if isinstance(continuation_history, list)
            else []
        )
        prior_continuation = row.get("agenda_continuation") if isinstance(row.get("agenda_continuation"), dict) else {}
        if str(prior_continuation.get("state") or "") == "required":
            continuation_history.append({
                **prior_continuation,
                "state": "satisfied",
                "satisfied_at": scheduled_at,
                "satisfied_by_ticket_id": binding["ticket_id"],
            })
        history.append({
            "schema_version": "xiaoyou.agenda-task-followup.v1",
            "operation_id": str(operation_id),
            "ticket_id": binding["ticket_id"],
            "scheduled_at": scheduled_at,
            "next_attention_at": attention_at,
            "actor": str(service.identity.canonical_user_id),
        })
        return {
            **row,
            "agenda_followup": {
                "schema_version": "xiaoyou.agenda-task-followup.v1",
                "state": "scheduled",
                "operation_id": str(operation_id),
                "ticket_id": binding["ticket_id"],
                "scheduled_at": scheduled_at,
                "next_attention_at": attention_at,
                "actor": str(service.identity.canonical_user_id),
            },
            "agenda_followup_history": history[-100:],
            # Retain a compact audit of a repaired continuity gap without
            # keeping the live task in the required state once Hermes has
            # selected and verified its successor attention time.
            "agenda_continuation": {},
            "agenda_continuation_history": continuation_history[-100:],
            "updated_at": scheduled_at,
        }

    persisted = service.store.update_task(binding["task_id"], update)
    followup = persisted.get("agenda_followup") if isinstance(persisted, dict) else {}
    verified = bool(
        isinstance(persisted, dict)
        and str(persisted.get("id") or "") == binding["task_id"]
        and str(followup.get("operation_id") or "") == str(operation_id)
        and str(followup.get("ticket_id") or "") == binding["ticket_id"]
        and str(followup.get("next_attention_at") or "") == attention_at
    )
    return {
        "ok": verified,
        "task_id": binding["task_id"],
        "ticket_id": binding["ticket_id"],
        "next_attention_at": attention_at,
        "writeback_verified": verified,
        "already_applied": False,
    }
