"""Institution-scoped authority for current Work Runtime proactive delivery.

This module is deliberately a delivery *authorization* boundary.  It does
not inspect a work payload, choose who should be contacted, generate text or
decide whether work is important.  It only verifies that a destination already
attested by the current Work Runtime may receive a previously generated Hermes
reply under an owner-approved institutional grant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any
import uuid

from .store import TuoguanStore, TuoguanStoreError
from .work_runtime import ReplyDestination, WorkRuntimeRejected
from .write_guard import authorized_system_write


AUTHORIZATION_FILE = "proactive_delivery_authorization_v1.json"
AUDIT_FILE = "proactive_delivery_authorization_audit_v1.jsonl"
SCHEMA_VERSION = "xiaoyou.proactive-delivery-authority.v1"
PERMITTED_ROLES = frozenset({"boss", "manager", "teacher"})
# These labels describe only server-attested Work Runtime origins.  They are
# not supplied by a model or parsed from a user message.  Keeping them here
# makes the institution-wide grant explicit without reintroducing a separate
# per-person outbound allowlist.
PERMITTED_SOURCE_KINDS = frozenset({"agenda", "task_delivery", "relationship_touch"})
_INACTIVE_PERSON_STATES = frozenset({"pending", "suspended", "left", "inactive", "offboarded", "terminated", "离职", "停用"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _data_dir(value: str | Path | None = None) -> Path | None:
    raw = str(value or os.getenv("HERMES_TUOGUAN_DATA_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    workspace = str(os.getenv("XIAOYOU_INSTITUTION_WORKSPACE") or "").strip()
    if not workspace:
        return None
    root = Path(workspace).expanduser().resolve()
    return root / "data" if (root / "data").is_dir() else root


def _recipient_key(value: object) -> str:
    text = str(value or "").strip()
    return text.rsplit(":", 1)[-1] if ":" in text else text


def _offboarded_user_ids(rows: object) -> set[str]:
    result: set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, str):
            result.add(row.strip())
        elif isinstance(row, dict):
            for field in ("user_id", "userid", "canonical_user_id", "staff_user_id", "id"):
                value = str(row.get(field) or "").strip()
                if value:
                    result.add(value)
                    break
    return result


def _effective_directory_role(directory: dict[str, Any], recipient: str) -> str:
    """Map the directory's owner marker to XiaoYou's stable ``boss`` role.

    The historic WeCom directory uses ``super_admin`` as a storage label for
    the sole institutional owner.  The trusted ``super_users`` membership is
    the authoritative server-side fact; model text never supplies this map.
    """

    owners = {str(item).strip() for item in (directory.get("super_users") or []) if str(item).strip()}
    if recipient in owners:
        return "boss"
    roles = directory.get("user_roles") if isinstance(directory.get("user_roles"), dict) else {}
    return str(roles.get(recipient) or "").strip().lower()


def _source_kind_for_tenant(source_identity: object, tenant_id: object) -> str:
    """Return an approved server-origin kind, otherwise an empty string.

    A destination is considered proactive only when its source identity has
    the exact ``kind:tenant`` shape emitted by the current Runtime.  Direct
    replies retain their authenticated human ingress identity and therefore
    never enter this branch.
    """

    source = str(source_identity or "").strip()
    tenant = str(tenant_id or "").strip()
    if not source or not tenant or ":" not in source:
        return ""
    kind, source_tenant = source.split(":", 1)
    return kind if kind in PERMITTED_SOURCE_KINDS and source_tenant == tenant else ""


@dataclass(frozen=True)
class DeliveryDecision:
    allowed: bool
    reason: str
    recipient_role: str = ""


class ProactiveDeliveryAuthority:
    """Persist and enforce a per-tenant owner authorization for work replies."""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        root = _data_dir(data_dir)
        if root is None:
            raise WorkRuntimeRejected("proactive_delivery_workspace_missing")
        self.store = TuoguanStore(root)
        self.data_dir = root

    def grant_institutional_wecom_delivery(self, *, tenant_id: str, authorized_by: str = "") -> dict[str, Any]:
        """Persist the owner's explicit non-parent proactive-contact grant.

        This is invoked only by the server operator while applying an explicit
        owner instruction.  It does not consume a model statement, and never
        broadens to another tenant or an untrusted directory identity.
        """

        tenant = str(tenant_id or "").strip()
        directory = self._directory()
        owners = [str(item).strip() for item in (directory.get("super_users") or []) if str(item).strip()]
        allowed = {str(item).strip() for item in (directory.get("allowed_users") or []) if str(item).strip()}
        if not tenant or len(owners) != 1 or owners[0] not in allowed:
            raise WorkRuntimeRejected("proactive_delivery_owner_directory_invalid")
        actor = str(authorized_by or owners[0]).strip()
        if actor != owners[0]:
            raise WorkRuntimeRejected("proactive_delivery_owner_authority_required")
        policy = {
            "schema_version": SCHEMA_VERSION,
            "tenant_id": tenant,
            "state": "active",
            "authorized_by": actor,
            "authorized_at": _now(),
            "scope": {
                "channel": "wecom_callback",
                "recipient_roles": sorted(PERMITTED_ROLES),
                "recipient_identity": "current_trusted_wecom_directory_only",
                "source_kinds": sorted(PERMITTED_SOURCE_KINDS),
                "sources": ["agenda:" + tenant],
                "excluded_recipient_classes": ["parent", "untrusted", "pending", "rejected", "offboarded"],
            },
            "authority_boundary": "delivery_only_no_business_judgment",
            "revision_id": "proactive_delivery_grant_" + uuid.uuid4().hex,
        }
        allowed_files = {AUTHORIZATION_FILE, AUDIT_FILE}
        with authorized_system_write(self.data_dir, job_name="apply_owner_proactive_delivery_authorization", allowed_files=allowed_files) as write:
            self.store.write_json(AUTHORIZATION_FILE, policy)
            persisted = self.store.read_json(AUTHORIZATION_FILE, {})
            if persisted != policy:
                raise WorkRuntimeRejected("proactive_delivery_authorization_writeback_failed")
            self.store.append_jsonl_verified(AUDIT_FILE, {
                "schema_version": SCHEMA_VERSION,
                "event": "institutional_proactive_delivery_authorization_granted",
                "tenant_id": tenant,
                "authorized_by": actor,
                "authorized_at": policy["authorized_at"],
                "revision_id": policy["revision_id"],
                "operation_id": write.operation_id,
                "ledger_id": write.ledger_id,
                "audit_id": write.audit_id,
                "writeback_verified": True,
            })
        return {"ok": True, "policy": policy, "writeback_verified": True}

    def upgrade_scope_to_current_runtime(self, *, tenant_id: str) -> dict[str, Any]:
        """Make a persisted owner grant cover all current trusted work origins.

        Older grants predate the Work Runtime's task-delivery and
        relationship-touch origins and list only ``agenda`` literally.  This
        is a schema migration of the *existing institution-wide* owner grant,
        not a new model-controlled authorization and not a recipient-list
        change.  The migration is auditable and fails closed for another
        tenant or an inactive grant.
        """

        tenant = str(tenant_id or "").strip()
        policy = self.store.read_json(AUTHORIZATION_FILE, {})
        if not isinstance(policy, dict) or policy.get("state") != "active":
            raise WorkRuntimeRejected("institutional_proactive_authorization_missing")
        if not tenant or str(policy.get("tenant_id") or "") != tenant:
            raise WorkRuntimeRejected("proactive_authorization_tenant_mismatch")
        scope = dict(policy.get("scope") or {})
        before = sorted(str(value) for value in (scope.get("source_kinds") or []))
        expected = sorted(PERMITTED_SOURCE_KINDS)
        if before == expected:
            return {"ok": True, "policy": policy, "writeback_verified": True, "already_applied": True}
        scope["source_kinds"] = expected
        # Retain the historical literal entry for audit/read compatibility;
        # enforcement below uses the typed, tenant-bound source kind.
        scope["sources"] = sorted({str(value) for value in (scope.get("sources") or []) if str(value)} | {"agenda:" + tenant})
        upgraded = {**policy, "scope": scope, "runtime_scope_revision": "2", "runtime_scope_upgraded_at": _now()}
        allowed_files = {AUTHORIZATION_FILE, AUDIT_FILE}
        with authorized_system_write(self.data_dir, job_name="upgrade_institutional_proactive_delivery_scope", allowed_files=allowed_files) as write:
            self.store.write_json(AUTHORIZATION_FILE, upgraded)
            persisted = self.store.read_json(AUTHORIZATION_FILE, {})
            if persisted != upgraded:
                raise WorkRuntimeRejected("proactive_delivery_authorization_writeback_failed")
            self.store.append_jsonl_verified(AUDIT_FILE, {
                "schema_version": SCHEMA_VERSION,
                "event": "institutional_proactive_delivery_scope_upgraded",
                "tenant_id": tenant,
                "revision_id": str(upgraded.get("revision_id") or ""),
                "source_kinds": expected,
                "operation_id": write.operation_id,
                "ledger_id": write.ledger_id,
                "audit_id": write.audit_id,
                "writeback_verified": True,
            })
        return {"ok": True, "policy": upgraded, "writeback_verified": True, "already_applied": False}

    def decide(self, destination: ReplyDestination) -> DeliveryDecision:
        """Return only a factual transport decision for one pre-bound destination."""

        destination.canonical()
        source = str(destination.source_identity or "")
        tenant = str(destination.tenant_id or "").strip()
        source_kind = _source_kind_for_tenant(source, tenant)
        # Existing direct replies have an authenticated human ingress and do
        # not require the owner's proactive-delivery grant.  The Work Runtime
        # has already bound their destination; no recipient is selected here.
        if not source_kind:
            return DeliveryDecision(True, "non_proactive_bound_destination")
        policy = self.store.read_json(AUTHORIZATION_FILE, {})
        if not isinstance(policy, dict) or policy.get("state") != "active":
            return DeliveryDecision(False, "institutional_proactive_authorization_missing")
        if str(policy.get("tenant_id") or "") != tenant:
            return DeliveryDecision(False, "proactive_authorization_tenant_mismatch")
        scope = policy.get("scope") if isinstance(policy.get("scope"), dict) else {}
        if str(scope.get("channel") or "") != destination.channel:
            return DeliveryDecision(False, "proactive_authorization_channel_denied")
        source_kinds = {str(item).strip() for item in (scope.get("source_kinds") or []) if str(item).strip()}
        # v1 grant compatibility: only its literal Agenda source was valid
        # until the audited migration above adds typed source kinds.
        sources = {str(item) for item in (scope.get("sources") or [])}
        if source_kinds:
            source_allowed = source_kind in source_kinds
        else:
            source_allowed = source in sources
        if not source_allowed:
            return DeliveryDecision(False, "proactive_authorization_source_denied")
        recipient = _recipient_key(destination.recipient_id)
        directory = self._directory()
        allowed = {str(item).strip() for item in (directory.get("allowed_users") or []) if str(item).strip()}
        contacts = directory.get("wecom_contacts") if isinstance(directory.get("wecom_contacts"), dict) else {}
        pending = {str(item).strip() for item in (directory.get("pending_users") or []) if str(item).strip()}
        rejected = {str(item).strip() for item in (directory.get("rejected_users") or []) if str(item).strip()}
        offboarded = _offboarded_user_ids(directory.get("offboarded_users"))
        role = _effective_directory_role(directory, recipient)
        permitted_roles = {str(item).lower() for item in (scope.get("recipient_roles") or [])}
        if recipient not in allowed:
            return DeliveryDecision(False, "proactive_recipient_identity_untrusted", role)
        if recipient not in contacts:
            return DeliveryDecision(False, "proactive_recipient_wecom_binding_missing", role)
        if recipient in pending:
            return DeliveryDecision(False, "proactive_recipient_pending", role)
        if recipient in rejected:
            return DeliveryDecision(False, "proactive_recipient_rejected", role)
        if recipient in offboarded:
            return DeliveryDecision(False, "proactive_recipient_offboarded", role)
        lifecycle_reason = self._recipient_lifecycle_denial(tenant_id=tenant, recipient=recipient)
        if lifecycle_reason:
            return DeliveryDecision(False, lifecycle_reason, role)
        if role not in permitted_roles:
            return DeliveryDecision(False, "proactive_recipient_role_denied", role)
        return DeliveryDecision(True, "institutional_proactive_authorization_active", role)

    def status(self, *, tenant_id: str) -> dict[str, Any]:
        """Return a non-sensitive, current delivery-authority snapshot."""

        tenant = str(tenant_id or "").strip()
        policy = self.store.read_json(AUTHORIZATION_FILE, {})
        directory = self._directory()
        allowed = {str(item).strip() for item in (directory.get("allowed_users") or []) if str(item).strip()}
        contacts = directory.get("wecom_contacts") if isinstance(directory.get("wecom_contacts"), dict) else {}
        pending = {str(item).strip() for item in (directory.get("pending_users") or []) if str(item).strip()}
        rejected = {str(item).strip() for item in (directory.get("rejected_users") or []) if str(item).strip()}
        offboarded = _offboarded_user_ids(directory.get("offboarded_users"))
        active_roles = {"boss": 0, "manager": 0, "teacher": 0}
        for recipient in allowed & set(contacts):
            role = _effective_directory_role(directory, recipient)
            if (
                recipient not in pending
                and recipient not in rejected
                and recipient not in offboarded
                and not self._recipient_lifecycle_denial(tenant_id=tenant, recipient=recipient)
                and role in active_roles
            ):
                active_roles[role] += 1
        active = bool(
            isinstance(policy, dict)
            and policy.get("state") == "active"
            and str(policy.get("tenant_id") or "") == tenant
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "tenant_id": tenant,
            "active": active,
            "recipient_roles": list((policy.get("scope") or {}).get("recipient_roles") or []) if isinstance(policy, dict) else [],
            "trusted_effective_recipient_counts": active_roles,
            "source_scope": list((policy.get("scope") or {}).get("sources") or []) if isinstance(policy, dict) else [],
            "source_kinds": list((policy.get("scope") or {}).get("source_kinds") or []) if isinstance(policy, dict) else [],
            "delivery_only": True,
        }

    def _directory(self) -> dict[str, Any]:
        try:
            value = self.store.read_json("wecom_whitelist.json", {})
        except TuoguanStoreError as exc:
            raise WorkRuntimeRejected("proactive_delivery_directory_unavailable") from exc
        if not isinstance(value, dict):
            raise WorkRuntimeRejected("proactive_delivery_directory_invalid")
        return value

    def _recipient_lifecycle_denial(self, *, tenant_id: str, recipient: str) -> str:
        """Honor explicit current personnel facts without guessing legacy data.

        The trusted WeCom directory is the operational active-identity source
        while progressive personnel claims are incomplete.  Once either the
        formal governance document or a legacy staff profile explicitly marks
        a person non-active, however, delivery must fail closed.  A missing
        historical profile never becomes an invented offboarding decision.
        """

        governance = self.store.read_json("personnel_service_governance_v1.json", {})
        if isinstance(governance, dict):
            people = governance.get("people") if isinstance(governance.get("people"), list) else []
            person = next(
                (
                    row for row in people
                    if isinstance(row, dict)
                    and str(row.get("tenant_id") or "") == str(tenant_id)
                    and str(row.get("staff_user_id") or "") == str(recipient)
                ),
                None,
            )
            if isinstance(person, dict):
                state = str(person.get("state") or "").strip().lower()
                if state != "active":
                    return "proactive_recipient_personnel_" + (state or "not_active")
                employments = governance.get("employments") if isinstance(governance.get("employments"), list) else []
                has_active_employment = any(
                    isinstance(row, dict)
                    and str(row.get("tenant_id") or "") == str(tenant_id)
                    and str(row.get("staff_user_id") or "") == str(recipient)
                    and str(row.get("state") or "") == "active"
                    and not str(row.get("effective_until") or "")
                    for row in employments
                )
                if not has_active_employment:
                    return "proactive_recipient_no_active_employment"

        staff = self.store.read_json("staff.json", {})
        profile = staff.get(recipient) if isinstance(staff, dict) else None
        if isinstance(profile, dict):
            state = str(profile.get("employment_status") or profile.get("status") or "").strip().lower()
            if state in _INACTIVE_PERSON_STATES:
                return "proactive_recipient_staff_" + state
        return ""
