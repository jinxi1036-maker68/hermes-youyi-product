"""Least-privilege policy for server-attested Agenda service work.

This is an authorization boundary, not a planner.  It never inspects natural
language to infer an action and never selects a Tool.  Hermes selects among
the small capability surface; this module only verifies that a service turn
may use a selected operation against an explicitly entrusted Work Item.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


SERVICE_PLATFORM = "agenda_service_work"
SERVICE_ROLE = "agenda_service"
SERVICE_PREFIX = "service:agenda:"
ALLOWED_TOOL_METHODS = frozenset({
    "context",
    "read_agenda_work_facts",
    "query_hermes_work_items",
    "update_hermes_work_item",
    "schedule_agenda_task_recheck",
    "contact_current_task_party",
})
ALLOWED_UPDATE_FIELDS = frozenset({
    "operation_id", "work_item_id", "focus_key", "status", "focus_summary",
    "current_phase", "next_actions", "progress_evidence", "confirmed_facts", "completed_actions",
    "current_waiting", "blocked_by", "next_attention_at", "stop_reason",
    "update_text", "source_text", "source_message_id",
})
ALLOWED_TRANSITIONS = frozenset({"waiting", "blocked", "closed", "superseded"})


def _deny(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": code, "message": message, "data": {}}


def is_agenda_service_identity(identity: Any) -> bool:
    actor = str(getattr(identity, "canonical_user_id", "") or "").strip()
    platform_user = str(getattr(identity, "platform_user_id", "") or "").strip()
    platform = str(getattr(identity, "platform", "") or "").strip()
    role = str(getattr(identity, "role", "") or "").strip()
    approval = str(getattr(identity, "approval_state", "") or "").strip()
    tenant = actor.removeprefix(SERVICE_PREFIX)
    return bool(
        tenant
        and actor == SERVICE_PREFIX + tenant
        and platform_user == actor
        and platform == SERVICE_PLATFORM
        and role == SERVICE_ROLE
        and approval == "approved"
    )


def _trusted_service_turn(identity: Any) -> bool:
    try:
        from .runtime_contract import current_trusted_turn

        turn = current_trusted_turn()
    except Exception:
        return False
    if turn is None or not is_agenda_service_identity(identity):
        return False
    return bool(
        str(getattr(turn, "platform", "") or "") == SERVICE_PLATFORM
        and str(getattr(turn, "actor_user_id", "") or "") == str(getattr(identity, "canonical_user_id", "") or "")
        and str(getattr(turn, "tenant_id", "") or "")
        == str(getattr(identity, "canonical_user_id", "") or "").removeprefix(SERVICE_PREFIX)
        and str(getattr(turn, "source", "") or "") == "agenda_service_work_ticket_public_hook_bridge"
    )


def _record_audit(store: Any, *, identity: Any, method: str, decision: str, reason: str, work_item_id: str = "") -> bool:
    row = {
        "schema_version": 1,
        "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "policy": "agenda_service_v1",
        "service_identity": str(getattr(identity, "canonical_user_id", "") or ""),
        "tenant": str(getattr(identity, "canonical_user_id", "") or "").removeprefix(SERVICE_PREFIX),
        "operation": str(method),
        "work_item_id": str(work_item_id or ""),
        "decision": decision,
        "reason": reason,
    }
    try:
        store.append_jsonl_verified("agenda_service_policy_audit.jsonl", row)
        return True
    except Exception:
        return False


def _visible_target(store: Any, identity: Any, *, work_item_id: str, focus_key: str) -> dict[str, Any] | None:
    try:
        from .digital_employee_state import query_hermes_work_items

        result = query_hermes_work_items(
            store,
            identity=identity,
            include_closed=True,
            focus_key=str(focus_key or ""),
            limit=100,
        )
    except Exception:
        return None
    items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
    if work_item_id:
        for item in items:
            if str(item.get("work_item_id") or "") == work_item_id:
                return item
        return None
    # A focus key is an existing Tool-contract identity.  It is acceptable
    # only when the service's own visibility filter yields exactly one item;
    # ambiguity cannot become a policy shortcut.
    if focus_key and len(items) == 1:
        return items[0]
    return None


def enforce_agenda_service_tool_call(*, service: Any, method: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """Return a truthful denial for an Agenda service call, otherwise ``None``.

    Non-service identities are deliberately not governed here; they continue
    through the established Permission / CommandBus policy path.
    """

    identity = getattr(service, "identity", None)
    agenda_shaped = (
        str(getattr(identity, "platform", "") or "") == SERVICE_PLATFORM
        or str(getattr(identity, "canonical_user_id", "") or "").startswith(SERVICE_PREFIX)
    )
    if not agenda_shaped:
        return None
    if not is_agenda_service_identity(identity) or not _trusted_service_turn(identity):
        _record_audit(service.store, identity=identity, method=method, decision="denied", reason="untrusted_service_identity")
        return _deny("agenda_service_identity_untrusted", "后台服务身份未通过可信绑定，本轮未执行。")
    if method not in ALLOWED_TOOL_METHODS:
        _record_audit(service.store, identity=identity, method=method, decision="denied", reason="tool_not_granted")
        return _deny("agenda_service_tool_not_granted", "该后台服务没有这项能力授权，本轮未执行。")
    if method != "update_hermes_work_item":
        if not _record_audit(service.store, identity=identity, method=method, decision="allowed", reason="read_or_context_scope"):
            return _deny("agenda_service_policy_audit_unavailable", "服务策略审计不可用，本轮未执行。")
        return None
    return enforce_agenda_work_item_update(service=service, args=args)


def enforce_agenda_service_write_operation(*, service: Any, operation: str) -> dict[str, Any] | None:
    """Fence direct in-process writes as well as model-visible Tool calls."""

    identity = getattr(service, "identity", None)
    agenda_shaped = (
        str(getattr(identity, "platform", "") or "") == SERVICE_PLATFORM
        or str(getattr(identity, "canonical_user_id", "") or "").startswith(SERVICE_PREFIX)
    )
    if not agenda_shaped:
        return None
    if not is_agenda_service_identity(identity) or not _trusted_service_turn(identity):
        _record_audit(service.store, identity=identity, method=operation, decision="denied", reason="untrusted_service_identity")
        return _deny("agenda_service_identity_untrusted", "后台服务身份未通过可信绑定，本轮未执行。")
    if operation not in {
        "update_hermes_work_item",
        "agenda_schedule_task_recheck",
        "agenda_contact_current_task_party",
    }:
        _record_audit(service.store, identity=identity, method=operation, decision="denied", reason="write_operation_not_granted")
        return _deny("agenda_service_write_operation_not_granted", "后台服务没有这项写入授权，本轮未执行。")
    return None


def enforce_agenda_work_item_update(*, service: Any, args: dict[str, Any]) -> dict[str, Any] | None:
    """Allow only bounded lifecycle updates on an explicitly entrusted item."""

    identity = getattr(service, "identity", None)
    method = "update_hermes_work_item"
    if not is_agenda_service_identity(identity) or not _trusted_service_turn(identity):
        _record_audit(service.store, identity=identity, method=method, decision="denied", reason="untrusted_service_identity")
        return _deny("agenda_service_identity_untrusted", "后台服务身份未通过可信绑定，本轮未执行。")
    supplied = {str(key) for key in dict(args or {})}
    unsupported = sorted(supplied - ALLOWED_UPDATE_FIELDS)
    if unsupported:
        _record_audit(service.store, identity=identity, method=method, decision="denied", reason="field_not_granted")
        return _deny("agenda_service_update_field_not_granted", "后台服务不能修改该工作字段，本轮未执行。")
    work_item_id = str(dict(args or {}).get("work_item_id") or "").strip()
    focus_key = str(dict(args or {}).get("focus_key") or "").strip()
    if not work_item_id and not focus_key:
        _record_audit(service.store, identity=identity, method=method, decision="denied", reason="work_item_id_missing")
        return _deny("agenda_service_work_item_identity_required", "后台服务只能更新明确关联的工作项，本轮未执行。")
    item = _visible_target(service.store, identity, work_item_id=work_item_id, focus_key=focus_key)
    if item is None:
        _record_audit(service.store, identity=identity, method=method, decision="denied", reason="work_item_not_entrusted", work_item_id=work_item_id or focus_key)
        return _deny("agenda_service_work_item_not_entrusted", "该工作项没有委托给当前后台服务，本轮未执行。")
    target_id = str(item.get("work_item_id") or work_item_id or focus_key)
    related = {str(value) for value in (item.get("related_staff_user_ids") or [])}
    if str(getattr(identity, "canonical_user_id", "") or "") not in related:
        _record_audit(service.store, identity=identity, method=method, decision="denied", reason="service_binding_missing", work_item_id=target_id)
        return _deny("agenda_service_work_item_binding_missing", "该工作项缺少后台服务委托绑定，本轮未执行。")
    status = str(dict(args or {}).get("status") or "").strip()
    if status and status not in ALLOWED_TRANSITIONS:
        _record_audit(service.store, identity=identity, method=method, decision="denied", reason="transition_not_granted", work_item_id=target_id)
        return _deny("agenda_service_transition_not_granted", "后台服务不能把工作项变更为该状态，本轮未执行。")
    if str(item.get("status") or "") in {"closed", "superseded"} and status not in {"", str(item.get("status") or "")}:
        _record_audit(service.store, identity=identity, method=method, decision="denied", reason="terminal_reopen_denied", work_item_id=target_id)
        return _deny("agenda_service_terminal_reopen_denied", "后台服务不能重新打开已终态的工作项。")
    if not _record_audit(service.store, identity=identity, method=method, decision="allowed", reason="entrusted_lifecycle_update", work_item_id=target_id):
        return _deny("agenda_service_policy_audit_unavailable", "服务策略审计不可用，本轮未执行。")
    return None
