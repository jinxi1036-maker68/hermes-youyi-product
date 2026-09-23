"""XiaoYou personnel, service ownership and handover capability candidate.

This module is deliberately a Workspace capability: it accepts an already
trusted identity and explicit tool arguments, but never parses a conversation,
selects a tool, or sends a reply.  It has no Hermes-Core dependency.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any, Callable
import uuid

# This capability is loaded under Hermes' public plugin namespace in a live
# gateway, not necessarily as top-level ``tuoguan_core``.  Relative imports
# keep the governance package runtime-neutral while still working in isolated
# certification where it is imported as ``tuoguan_core``.
from .models import UserIdentity
from .personnel_identity_authority import ACCESS_KEY, IdentityAuthorityError, validate_runtime_identity_document
from .store import TuoguanStore, TuoguanStoreError


CAPABILITY_ID = "xiaoyou.personnel_service_governance"
CAPABILITY_VERSION = "0.3.0-candidate"
# v4 adds a durable, non-business follower ledger for Claim cleanup after a
# verified pending-identity activation.  Older v3 documents are upgraded
# additively by ``_state``; no people, employments or history are rewritten.
SCHEMA_VERSION = 4
DATA_FILE = "personnel_service_governance_v1.json"

# A person has a lifecycle state. An employment row is an assignment period,
# not the person's state: transfer closes one assignment and opens another.
PERSON_STATES = frozenset({"pending", "active", "suspended", "left"})
ASSIGNMENT_STATES = frozenset({"pending", "active", "closed"})
STUDENT_STATES = frozenset({"pending_confirmation", "active", "archived"})
RELATION_STATES = frozenset({"active", "transferred", "closed", "cancelled"})
SERVICE_TYPES = frozenset({"lunch_care", "evening_care", "weekend_class", "summer_class"})
ATTENTION_STATES = frozenset({"awaiting_confirmation", "open", "acknowledged", "closed", "cancelled"})
HIGH_RISK_PUBLIC_FIELDS = frozenset({
    "name", "pickup_authorization", "emergency_contacts", "allergies",
    "dietary_restrictions", "safety_notes",
})
ORDINARY_PUBLIC_FIELDS = frozenset({"school", "grade", "class_name", "contacts", "ordinary_notes"})


class GovernanceError(ValueError):
    """A fail-closed business-contract error suitable for a Tool terminal."""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _id(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex


def _norm_phone(value: str) -> str:
    return "".join(re.findall(r"\d", str(value or "")))


def _blank_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        # Once explicitly enabled by the reviewed migration, this aggregate
        # is also the single runtime authority for a verified WeCom userid's
        # role and lifecycle.  Until then the key is empty and legacy inbound
        # identity remains untouched for a safe staged rollout.
        "runtime_identity_authority": {},
        "identity_access": {"pending": {}, "rejected": {}},
        "people": [],
        "employments": [],
        "students": [],
        "service_relations": [],
        "service_records": [],
        "attentions": [],
        "work_links": [],
        "handovers": [],
        "change_requests": [],
        # A completed pending-identity activation may need to close old
        # Claim projections in a separate ledger.  The task is written in the
        # same authority mutation as the identity itself, so a follower write
        # outage can never make the verified identity success disappear.
        "identity_claim_reconciliations": [],
        "operations": {},
        "audit": [],
    }


def _state(value: Any) -> dict[str, Any]:
    base = _blank_state()
    if not isinstance(value, dict):
        return base
    for key, fallback in base.items():
        current = value.get(key, fallback)
        if isinstance(fallback, list):
            base[key] = current if isinstance(current, list) else []
        elif isinstance(fallback, dict):
            base[key] = current if isinstance(current, dict) else {}
        else:
            base[key] = current
    base["schema_version"] = SCHEMA_VERSION
    return base


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class PersonnelServiceGovernance:
    """Transactional, idempotent v1 candidate over a product-owned Store."""

    def __init__(self, store: TuoguanStore) -> None:
        self.store = store

    # --- Stable capability descriptor ---------------------------------
    @staticmethod
    def descriptor() -> dict[str, Any]:
        return {
            "capability_id": CAPABILITY_ID,
            "version": CAPABILITY_VERSION,
            "schema_version": SCHEMA_VERSION,
            "hermes_dependencies": [],
            "runtime_contract": "trusted_identity_and_tenant_only",
            "truth_chain": ["permission", "command_bus", "receipt", "writeback"],
            "prohibits": ["intent_router", "second_model", "automatic_legacy_teacher_migration", "physical_history_delete"],
        }

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(_state(self.store.read_json(DATA_FILE, _blank_state())))

    def can_manage_campus(self, *, identity: UserIdentity, tenant_id: str, campus_id: str) -> bool:
        """Public, content-blind policy query for Capability adapters."""
        try:
            self._trusted(identity, tenant_id)
            self._require_campus_manager(self.snapshot(), identity, tenant_id, campus_id)
            return True
        except GovernanceError:
            return False

    # --- idempotent mutation and trusted access primitives -------------
    def _mutate(
        self,
        *,
        operation_id: str,
        identity: UserIdentity,
        tenant_id: str,
        action: str,
        mutate: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        operation_id = str(operation_id or "").strip()
        if not operation_id:
            raise GovernanceError("missing_operation_id")
        self._trusted(identity, tenant_id)

        reused = False

        def update(raw: Any) -> dict[str, Any]:
            nonlocal reused
            doc = _state(raw)
            operations = doc["operations"]
            if operation_id in operations:
                reused = True
                return doc
            before = _digest({key: value for key, value in doc.items() if key not in {"operations", "audit"}})
            result = mutate(doc)
            if not isinstance(result, dict):
                raise GovernanceError("invalid_mutation_result")
            try:
                validate_runtime_identity_document(doc)
            except IdentityAuthorityError as exc:
                raise GovernanceError(str(exc)) from exc
            result = deepcopy(result)
            result.update({"operation_id": operation_id, "writeback_verified": True, "already_applied": False})
            operations[operation_id] = {"action": action, "result": result, "created_at": _now()}
            doc["audit"].append({
                "audit_id": _id("audit"), "operation_id": operation_id, "action": action,
                "tenant_id": tenant_id, "actor_user_id": identity.canonical_user_id,
                "actor_role": identity.role, "created_at": _now(), "before_digest": before,
                "after_digest": _digest({key: value for key, value in doc.items() if key not in {"operations", "audit"}}),
            })
            return doc

        try:
            saved = _state(self.store.update_json(DATA_FILE, _blank_state(), update))
        except TuoguanStoreError as exc:
            # Store correctly keeps the aggregate unchanged when a contract
            # precondition fails.  Preserve that domain terminal for Tool code.
            if isinstance(exc.__cause__, GovernanceError):
                raise exc.__cause__
            raise
        entry = saved["operations"].get(operation_id)
        if not isinstance(entry, dict) or not isinstance(entry.get("result"), dict):
            raise GovernanceError("writeback_consistency_failed")
        result = deepcopy(entry["result"])
        if str(entry.get("action") or "") != action:
            raise GovernanceError("operation_id_reused_for_different_action")
        if reused:
            # A durable duplicate is correct; never perform the mutation twice.
            result["already_applied"] = True
        return result

    @staticmethod
    def _trusted(identity: UserIdentity, tenant_id: str) -> None:
        if not str(tenant_id or "").strip():
            raise GovernanceError("missing_trusted_tenant")
        if not identity or identity.approval_state != "approved":
            raise GovernanceError("identity_not_approved")
        if identity.role not in {"boss", "manager", "teacher"}:
            raise GovernanceError("unsupported_role")
        if not str(identity.canonical_user_id or "").strip():
            raise GovernanceError("missing_canonical_actor")

    @staticmethod
    def _person(doc: dict[str, Any], *, tenant_id: str, user_id: str) -> dict[str, Any] | None:
        for row in doc["people"]:
            if isinstance(row, dict) and str(row.get("tenant_id") or "") == tenant_id and str(row.get("staff_user_id") or "") == user_id:
                return row
        return None

    @classmethod
    def _active_employment(cls, doc: dict[str, Any], *, tenant_id: str, user_id: str) -> dict[str, Any] | None:
        person = cls._person(doc, tenant_id=tenant_id, user_id=user_id)
        if not person or str(person.get("state") or "") != "active":
            return None
        rows = [row for row in doc["employments"] if isinstance(row, dict)
                and str(row.get("tenant_id") or "") == tenant_id
                and str(row.get("staff_user_id") or "") == user_id
                and str(row.get("state") or "") == "active"
                and not str(row.get("effective_until") or "")]
        if not rows:
            return None
        return sorted(rows, key=lambda row: str(row.get("effective_from") or ""), reverse=True)[0]

    @staticmethod
    def _resolve_activation_campus_id(
        doc: dict[str, Any],
        *,
        tenant_id: str,
        requested_campus_id: str = "",
    ) -> str:
        """Resolve a pending activation against current authoritative assignments.

        A single-campus institution should not make the owner restate a fact the
        authority already knows.  Wildcard boss scope is not a physical campus.
        When multiple current campuses genuinely exist, an explicit requested
        campus is accepted only if it is one of those authoritative campuses.
        """

        campuses = {
            str(row.get("campus_id") or "").strip()
            for row in doc["employments"]
            if isinstance(row, dict)
            and str(row.get("tenant_id") or "") == tenant_id
            and str(row.get("state") or "") == "active"
            and not str(row.get("effective_until") or "")
            and str(row.get("campus_id") or "").strip()
            and str(row.get("campus_id") or "").strip() != "*"
        }
        requested = str(requested_campus_id or "").strip()
        if requested:
            if campuses and requested not in campuses:
                raise GovernanceError("pending_identity_campus_not_current")
            return requested
        if len(campuses) == 1:
            return next(iter(campuses))
        if not campuses:
            raise GovernanceError("pending_identity_campus_unavailable")
        raise GovernanceError("pending_identity_campus_ambiguous")

    def _require_boss(self, identity: UserIdentity) -> None:
        if identity.role != "boss":
            raise GovernanceError("boss_confirmation_required")

    def _require_campus_manager(self, doc: dict[str, Any], identity: UserIdentity, tenant_id: str, campus_id: str) -> None:
        if identity.role == "boss":
            return
        employment = self._active_employment(doc, tenant_id=tenant_id, user_id=identity.canonical_user_id)
        managed = {str(value) for value in employment.get("managed_campus_ids") or []} if employment else set()
        if employment and not managed:
            managed = {str(employment.get("campus_id") or "")}
        if not employment or identity.role != "manager" or str(campus_id or "") not in managed:
            raise GovernanceError("campus_manager_permission_denied")

    def _require_active_employee(self, doc: dict[str, Any], identity: UserIdentity, tenant_id: str) -> dict[str, Any]:
        if identity.role == "boss":
            return {"role": "boss", "campus_id": "*"}
        employment = self._active_employment(doc, tenant_id=tenant_id, user_id=identity.canonical_user_id)
        if not employment:
            raise GovernanceError("employment_not_active")
        return employment

    @staticmethod
    def _find(rows: list[dict[str, Any]], key: str, value: str) -> dict[str, Any]:
        for row in rows:
            if isinstance(row, dict) and str(row.get(key) or "") == str(value or ""):
                return row
        raise GovernanceError("object_not_found")

    @staticmethod
    def _student(doc: dict[str, Any], student_id: str) -> dict[str, Any]:
        return PersonnelServiceGovernance._find(doc["students"], "student_id", student_id)

    @staticmethod
    def _relation(doc: dict[str, Any], relation_id: str) -> dict[str, Any]:
        return PersonnelServiceGovernance._find(doc["service_relations"], "relation_id", relation_id)

    # --- personnel and student lifecycle --------------------------------
    def create_pending_employment(self, *, identity: UserIdentity, tenant_id: str, staff_user_id: str, role: str, campus_id: str, operation_id: str, person_name: str = "") -> dict[str, Any]:
        if role not in {"boss", "manager", "teacher"}:
            raise GovernanceError("invalid_initial_role")

        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            if role == "boss":
                self._require_boss(identity)
            elif identity.role == "manager":
                self._require_campus_manager(doc, identity, tenant_id, campus_id)
            else:
                self._require_boss(identity)
            existing_person = self._person(doc, tenant_id=tenant_id, user_id=str(staff_user_id))
            if existing_person and str(existing_person.get("state") or "") in {"pending", "active", "suspended"}:
                raise GovernanceError("staff_person_already_exists")
            display_name = str(person_name or "").strip()
            if not existing_person:
                doc["people"].append({"person_id": _id("person"), "tenant_id": tenant_id, "staff_user_id": str(staff_user_id),
                                      "display_name": display_name, "state": "pending", "created_at": _now(), "created_by": identity.canonical_user_id})
            row = {"employment_id": _id("employment"), "tenant_id": tenant_id, "staff_user_id": str(staff_user_id),
                   "role": role, "campus_id": str(campus_id), "managed_campus_ids": [str(campus_id)] if role == "manager" else [], "state": "pending", "effective_from": "",
                   "effective_until": "", "created_at": _now(), "created_by": identity.canonical_user_id}
            doc["employments"].append(row)
            return {"employment": deepcopy(row)}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="employment_pending_created", mutate=apply)

    def resolve_active_person_by_display_name(self, *, tenant_id: str, person_name: str) -> dict[str, Any]:
        """Return one already-authoritative active person for a stated name.

        This is a trusted governance lookup, not a legacy-field migration. A
        person name can make a natural-language claim usable only if it maps to
        exactly one active candidate identity; otherwise Hermes must clarify.
        """
        requested = str(person_name or "").strip()
        if not requested:
            raise GovernanceError("person_name_required")
        doc = self.snapshot()
        matches = [
            row for row in doc["people"]
            if str(row.get("tenant_id") or "") == tenant_id
            and str(row.get("state") or "") == "active"
            and str(row.get("display_name") or "").strip() == requested
            and self._active_employment(doc, tenant_id=tenant_id, user_id=str(row.get("staff_user_id") or ""))
        ]
        if len(matches) != 1:
            raise GovernanceError("current_person_not_uniquely_confirmed")
        return deepcopy(matches[0])

    def activate_employment(self, *, identity: UserIdentity, tenant_id: str, employment_id: str, operation_id: str) -> dict[str, Any]:
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            self._require_boss(identity)
            row = self._find(doc["employments"], "employment_id", employment_id)
            if row.get("state") != "pending":
                raise GovernanceError("employment_not_pending")
            person = self._person(doc, tenant_id=tenant_id, user_id=str(row.get("staff_user_id") or ""))
            if not person or person.get("state") != "pending":
                raise GovernanceError("person_not_pending")
            row.update({"state": "active", "effective_from": _now(), "activated_by": identity.canonical_user_id})
            person.update({"state": "active", "activated_at": _now(), "activated_by": identity.canonical_user_id})
            return {"person": deepcopy(person), "employment": deepcopy(row), "identity_activation_requires_runtime_bridge": True}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="employment_activated", mutate=apply)

    def activate_confirmed_pending_identity(
        self,
        *,
        identity: UserIdentity,
        tenant_id: str,
        staff_user_id: str,
        person_name: str,
        role: str,
        campus_id: str,
        operation_id: str,
    ) -> dict[str, Any]:
        """Atomically promote one server-recorded pending channel identity.

        This is intentionally *not* composed from ``create_pending_employment``
        and ``activate_employment``.  In an enforced authority aggregate a
        verified channel userid cannot temporarily exist in both
        ``identity_access.pending`` and ``people``.  Creating the person in one
        operation and removing pending access in another would violate that
        invariant and, more importantly, would leave a half-enabled identity
        after a failure.  The caller is a boss whose current Turn was already
        attested by the Runtime Contract; this method never consults a name or
        legacy directory to select the userid.
        """

        staff_user_id = str(staff_user_id or "").strip()
        person_name = str(person_name or "").strip()
        campus_id = str(campus_id or "").strip()
        role = str(role or "").strip().lower()
        if not staff_user_id:
            raise GovernanceError("pending_identity_userid_required")
        if not person_name:
            raise GovernanceError("pending_identity_display_name_required")
        if role not in {"boss", "manager", "teacher"}:
            raise GovernanceError("pending_identity_role_invalid")

        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            self._require_boss(identity)
            resolved_campus_id = self._resolve_activation_campus_id(
                doc,
                tenant_id=tenant_id,
                requested_campus_id=campus_id,
            )
            access = doc.setdefault(ACCESS_KEY, {"pending": {}, "rejected": {}})
            pending = access.setdefault("pending", {})
            rejected = access.setdefault("rejected", {})
            if staff_user_id in rejected:
                raise GovernanceError("pending_identity_rejected")
            pending_record = pending.get(staff_user_id)
            if not isinstance(pending_record, dict):
                raise GovernanceError("pending_identity_not_found")
            if self._person(doc, tenant_id=tenant_id, user_id=staff_user_id) is not None:
                # Never guess how a pre-existing partial record should be
                # reconciled.  The mutation is all-or-nothing, so this leaves
                # the authority aggregate untouched for a separately audited
                # correction.
                raise GovernanceError("pending_identity_person_conflict")
            if any(
                isinstance(row, dict)
                and str(row.get("tenant_id") or "") == tenant_id
                and str(row.get("staff_user_id") or "") == staff_user_id
                and not str(row.get("effective_until") or "")
                for row in doc["employments"]
            ):
                raise GovernanceError("pending_identity_employment_conflict")

            now = _now()
            person = {
                "person_id": _id("person"),
                "tenant_id": tenant_id,
                "staff_user_id": staff_user_id,
                "display_name": person_name,
                "state": "active",
                "created_at": now,
                "created_by": identity.canonical_user_id,
                "activated_at": now,
                "activated_by": identity.canonical_user_id,
                "authority_origin": "confirmed_pending_runtime_identity",
            }
            employment = {
                "employment_id": _id("employment"),
                "tenant_id": tenant_id,
                "staff_user_id": staff_user_id,
                "role": role,
                "campus_id": resolved_campus_id,
                "managed_campus_ids": [resolved_campus_id] if role == "manager" else [],
                "state": "active",
                "effective_from": now,
                "effective_until": "",
                "created_at": now,
                "created_by": identity.canonical_user_id,
                "activated_by": identity.canonical_user_id,
            }
            reconciliation = {
                "reconciliation_id": _id("identity_claim_reconciliation"),
                "tenant_id": tenant_id,
                "staff_user_id": staff_user_id,
                "role": role,
                "campus_id": resolved_campus_id,
                "state": "pending",
                # Do not retrospectively close a Claim created after this
                # formal activation.  Only predecessor Claims can be a
                # mechanically derived projection of the just-verified fact.
                "source_cutoff_at": now,
                "successor_operation_id": operation_id,
                "authorised_by": identity.canonical_user_id,
                "created_at": now,
                "updated_at": now,
                "attempt_count": 0,
                "last_error_code": "",
                "completed_at": "",
            }
            doc["people"].append(person)
            doc["employments"].append(employment)
            doc["identity_claim_reconciliations"].append(reconciliation)
            # This transition is deliberately in the same persisted document
            # mutation as person/employment creation.  The post-mutation
            # identity-authority validator therefore sees only one coherent
            # truth: approved active person, active employment, no pending
            # access entry.
            del pending[staff_user_id]
            access["pending"] = pending
            access["rejected"] = rejected
            doc[ACCESS_KEY] = access
            return {
                "person": deepcopy(person),
                "employment": deepcopy(employment),
                "identity_transition": {
                    "staff_user_id": staff_user_id,
                    "from": "pending",
                    "to": "approved",
                    "pending_observed_at": str(pending_record.get("first_seen_at") or ""),
                },
                "claim_reconciliation": deepcopy(reconciliation),
            }

        return self._mutate(
            operation_id=operation_id,
            identity=identity,
            tenant_id=tenant_id,
            action="pending_runtime_identity_confirmed_and_activated",
            mutate=apply,
        )

    def pending_identity_claim_reconciliations(self, *, tenant_id: str) -> list[dict[str, Any]]:
        """Return durable follower work created by already-verified activations.

        This is not an Agenda business fact and is never model input.  It is
        an execution-integrity ledger used only to finish a later Claim
        projection without reconsidering the boss's already completed action.
        """

        return [
            deepcopy(row)
            for row in self.snapshot().get("identity_claim_reconciliations") or []
            if isinstance(row, dict)
            and str(row.get("tenant_id") or "") == tenant_id
            and str(row.get("state") or "") == "pending"
        ]

    def identity_claim_reconciliation(self, *, tenant_id: str, reconciliation_id: str) -> dict[str, Any] | None:
        """Read one follower task for an idempotent Tool replay."""

        wanted = str(reconciliation_id or "").strip()
        if not wanted:
            return None
        for row in self.snapshot().get("identity_claim_reconciliations") or []:
            if (
                isinstance(row, dict)
                and str(row.get("tenant_id") or "") == tenant_id
                and str(row.get("reconciliation_id") or "") == wanted
            ):
                return deepcopy(row)
        return None

    def record_identity_claim_reconciliation_outcome(
        self,
        *,
        tenant_id: str,
        reconciliation_id: str,
        succeeded: bool,
        claim_operation_id: str,
        error_code: str = "",
    ) -> dict[str, Any]:
        """Record a system follower outcome without changing business truth.

        The caller is an internal, fixed reconciliation worker, not a model
        Tool.  It may only update an existing task born from a verified
        pending-identity activation; it cannot create people, employments or
        permissions and therefore cannot act as a second business decider.
        """

        task_id = str(reconciliation_id or "").strip()
        if not task_id:
            raise GovernanceError("identity_claim_reconciliation_id_required")
        service_identity = UserIdentity(
            platform="identity_claim_reconciliation",
            platform_user_id="service:identity_claim_reconciliation:" + tenant_id,
            canonical_user_id="service:identity_claim_reconciliation:" + tenant_id,
            person_name="身份 Claim 收尾服务",
            role="boss",
            approval_state="approved",
        )
        attempt_key = str(claim_operation_id or "").strip() or "attempt"

        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            row = next((
                item for item in doc["identity_claim_reconciliations"]
                if isinstance(item, dict) and str(item.get("reconciliation_id") or "") == task_id
            ), None)
            if not isinstance(row, dict) or str(row.get("tenant_id") or "") != tenant_id:
                raise GovernanceError("identity_claim_reconciliation_not_found")
            if str(row.get("state") or "") == "completed":
                return {"reconciliation": deepcopy(row), "already_completed": True}
            row.update({
                "state": "completed" if succeeded else "pending",
                "updated_at": _now(),
                "last_claim_operation_id": attempt_key,
                "last_error_code": "" if succeeded else str(error_code or "claim_reconciliation_failed"),
                "attempt_count": int(row.get("attempt_count") or 0) + 1,
                "completed_at": _now() if succeeded else "",
            })
            return {"reconciliation": deepcopy(row), "already_completed": False}

        # This server-created actor is deliberately scoped to technical
        # follower bookkeeping.  The normal trusted identity validator still
        # protects the aggregate and the audit makes the service actor clear.
        return self._mutate(
            operation_id="system:identity_claim_reconciliation:" + task_id + ":" + attempt_key,
            identity=service_identity,
            tenant_id=tenant_id,
            action="identity_claim_reconciliation_outcome_recorded",
            mutate=apply,
        )

    def transfer_employment(self, *, identity: UserIdentity, tenant_id: str, employment_id: str, target_campus_id: str, operation_id: str) -> dict[str, Any]:
        """Close one active employment period and open another without role escalation.

        A manager may make a work-location/shift move only where its trusted
        manager scope covers both campuses. A boss may make any such move.
        Changing role is deliberately absent here and requires a boss-owned
        activation/role workflow.
        """
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            old = self._find(doc["employments"], "employment_id", employment_id)
            person = self._person(doc, tenant_id=tenant_id, user_id=str(old.get("staff_user_id") or ""))
            if old.get("state") != "active" or str(old.get("effective_until") or "") or not person or person.get("state") != "active":
                raise GovernanceError("employment_not_current_active")
            self._require_campus_manager(doc, identity, tenant_id, str(old.get("campus_id") or ""))
            if identity.role == "manager":
                self._require_campus_manager(doc, identity, tenant_id, str(target_campus_id or ""))
            old.update({"state": "closed", "effective_until": _now(), "closed_by": identity.canonical_user_id, "close_reason": "assignment_transfer"})
            replacement = {key: deepcopy(value) for key, value in old.items() if key not in {"employment_id", "state", "effective_from", "effective_until", "transferred_by"}}
            replacement.pop("closed_by", None); replacement.pop("close_reason", None)
            replacement.update({"employment_id": _id("employment"), "campus_id": str(target_campus_id), "state": "active", "effective_from": _now(), "effective_until": "", "supersedes_employment_id": employment_id, "created_by": identity.canonical_user_id})
            if replacement.get("role") == "manager" and not replacement.get("managed_campus_ids"):
                replacement["managed_campus_ids"] = [str(target_campus_id)]
            doc["employments"].append(replacement)
            return {"person": deepcopy(person), "previous_employment": deepcopy(old), "current_employment": deepcopy(replacement), "role_changed": False}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="employment_transferred", mutate=apply)

    def change_employment_role(
        self,
        *,
        identity: UserIdentity,
        tenant_id: str,
        employment_id: str,
        target_role: str,
        operation_id: str,
    ) -> dict[str, Any]:
        """Close one current assignment and open a role-correct successor.

        A role is never overwritten in place: history keeps the old role and
        its effective period.  The enclosing aggregate validation rejects a
        promotion that would create a second active boss or a demotion that
        would leave the institution without one.
        """

        if target_role not in {"boss", "manager", "teacher"}:
            raise GovernanceError("invalid_target_role")

        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            self._require_boss(identity)
            old = self._find(doc["employments"], "employment_id", employment_id)
            person = self._person(doc, tenant_id=tenant_id, user_id=str(old.get("staff_user_id") or ""))
            if old.get("state") != "active" or str(old.get("effective_until") or "") or not person or person.get("state") != "active":
                raise GovernanceError("employment_not_current_active")
            old.update({"state": "closed", "effective_until": _now(), "closed_by": identity.canonical_user_id, "close_reason": "role_change"})
            replacement = {key: deepcopy(value) for key, value in old.items() if key not in {"employment_id", "state", "effective_from", "effective_until", "closed_by", "close_reason"}}
            replacement.update({
                "employment_id": _id("employment"),
                "role": target_role,
                "state": "active",
                "effective_from": _now(),
                "effective_until": "",
                "supersedes_employment_id": employment_id,
                "created_by": identity.canonical_user_id,
            })
            if target_role != "manager":
                replacement["managed_campus_ids"] = []
            elif not replacement.get("managed_campus_ids"):
                replacement["managed_campus_ids"] = [str(replacement.get("campus_id") or "")]
            doc["employments"].append(replacement)
            return {"person": deepcopy(person), "previous_employment": deepcopy(old), "current_employment": deepcopy(replacement), "role_changed": True}

        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="employment_role_changed", mutate=apply)

    def request_employment_pause(self, *, identity: UserIdentity, tenant_id: str, employment_id: str, reason: str, operation_id: str) -> dict[str, Any]:
        """A manager may initiate, but cannot silently apply, an access pause."""
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            employment = self._find(doc["employments"], "employment_id", employment_id)
            self._require_campus_manager(doc, identity, tenant_id, str(employment.get("campus_id") or ""))
            if employment.get("state") != "active":
                raise GovernanceError("employment_not_current_active")
            request = {"request_id": _id("employment_pause"), "tenant_id": tenant_id, "employment_id": employment_id, "staff_user_id": employment["staff_user_id"], "state": "pending_boss_confirmation", "reason": str(reason or ""), "requested_by": identity.canonical_user_id, "created_at": _now()}
            doc["change_requests"].append(request)
            return {"change_request": deepcopy(request), "access_changed": False}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="employment_pause_requested", mutate=apply)

    def confirm_employment_pause(self, *, identity: UserIdentity, tenant_id: str, request_id: str, operation_id: str) -> dict[str, Any]:
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            self._require_boss(identity)
            request = self._find(doc["change_requests"], "request_id", request_id)
            if request.get("state") != "pending_boss_confirmation":
                raise GovernanceError("employment_pause_request_not_pending")
            employment = self._find(doc["employments"], "employment_id", str(request.get("employment_id") or ""))
            if employment.get("state") != "active":
                raise GovernanceError("employment_not_current_active")
            person = self._person(doc, tenant_id=tenant_id, user_id=str(employment.get("staff_user_id") or ""))
            if not person or person.get("state") != "active":
                raise GovernanceError("person_not_current_active")
            person.update({"state": "suspended", "suspended_at": _now(), "suspended_by": identity.canonical_user_id, "suspension_reason": request.get("reason")})
            request.update({"state": "confirmed", "confirmed_at": _now(), "confirmed_by": identity.canonical_user_id})
            return {"person": deepcopy(person), "employment": deepcopy(employment), "request": deepcopy(request), "runtime_identity_revocation_requires_existing_staff_bridge": True}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="employment_pause_confirmed", mutate=apply)

    def reactivate_person(self, *, identity: UserIdentity, tenant_id: str, staff_user_id: str, operation_id: str) -> dict[str, Any]:
        """Boss-confirmed restoration of a suspended, still-current person."""
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            self._require_boss(identity)
            person = self._person(doc, tenant_id=tenant_id, user_id=staff_user_id)
            if not person or person.get("state") != "suspended":
                raise GovernanceError("person_not_suspended")
            assignment = next((row for row in doc["employments"] if str(row.get("tenant_id") or "") == tenant_id and str(row.get("staff_user_id") or "") == staff_user_id and row.get("state") == "active" and not str(row.get("effective_until") or "")), None)
            if not assignment:
                raise GovernanceError("active_assignment_required_for_reactivation")
            person.update({"state": "active", "reactivated_at": _now(), "reactivated_by": identity.canonical_user_id})
            return {"person": deepcopy(person), "employment": deepcopy(assignment), "runtime_identity_activation_requires_existing_staff_bridge": True}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="person_reactivated", mutate=apply)

    def update_manager_scope(self, *, identity: UserIdentity, tenant_id: str, employment_id: str, managed_campus_ids: list[str], operation_id: str) -> dict[str, Any]:
        """A manager's data scope is a boss-confirmed authority change."""
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            self._require_boss(identity)
            assignment = self._find(doc["employments"], "employment_id", employment_id)
            if assignment.get("role") != "manager" or assignment.get("state") != "active":
                raise GovernanceError("active_manager_assignment_required")
            normalized = sorted({str(item).strip() for item in managed_campus_ids if str(item).strip()})
            if not normalized:
                raise GovernanceError("manager_scope_required")
            assignment["managed_campus_ids"] = normalized
            assignment["manager_scope_updated_at"] = _now(); assignment["manager_scope_updated_by"] = identity.canonical_user_id
            return {"employment": deepcopy(assignment)}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="manager_scope_updated", mutate=apply)

    def create_pending_student(self, *, identity: UserIdentity, tenant_id: str, name: str, phone: str, campus_id: str, ordinary_profile: dict[str, Any], operation_id: str) -> dict[str, Any]:
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            employee = self._require_active_employee(doc, identity, tenant_id)
            if identity.role == "teacher" and str(employee.get("campus_id") or "") != str(campus_id):
                raise GovernanceError("teacher_campus_scope_denied")
            normalized_name = str(name or "").strip()
            normalized_phone = _norm_phone(phone)
            if not normalized_name:
                raise GovernanceError("student_name_required")
            collisions = [row for row in doc["students"] if isinstance(row, dict) and str(row.get("tenant_id") or "") == tenant_id and (
                str(row.get("name") or "").strip() == normalized_name or (normalized_phone and str(row.get("phone_normalized") or "") == normalized_phone))]
            if collisions:
                raise GovernanceError("student_possible_duplicate_requires_human_review")
            unsafe = set(ordinary_profile or {}) - ORDINARY_PUBLIC_FIELDS
            if unsafe:
                raise GovernanceError("pending_student_profile_contains_restricted_fields")
            row = {"student_id": _id("student"), "tenant_id": tenant_id, "name": normalized_name,
                   "phone_normalized": normalized_phone, "campus_id": str(campus_id), "state": "pending_confirmation",
                   "ordinary_profile": deepcopy(ordinary_profile or {}), "created_at": _now(), "created_by": identity.canonical_user_id}
            doc["students"].append(row)
            return {"student": deepcopy(row), "confirmation_required": True}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="student_pending_created", mutate=apply)

    def confirm_student(self, *, identity: UserIdentity, tenant_id: str, student_id: str, operation_id: str) -> dict[str, Any]:
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            student = self._student(doc, student_id)
            self._require_campus_manager(doc, identity, tenant_id, str(student.get("campus_id") or ""))
            if student.get("state") != "pending_confirmation":
                raise GovernanceError("student_not_pending_confirmation")
            student.update({"state": "active", "confirmed_at": _now(), "confirmed_by": identity.canonical_user_id})
            return {"student": deepcopy(student)}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="student_confirmed", mutate=apply)

    def archive_student(self, *, identity: UserIdentity, tenant_id: str, student_id: str, reason: str, operation_id: str) -> dict[str, Any]:
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            self._require_boss(identity)
            student = self._student(doc, student_id)
            active_relations = [row for row in doc["service_relations"] if str(row.get("student_id") or "") == student_id and row.get("state") == "active"]
            open_attention = [row for row in doc["attentions"] if str(row.get("student_id") or "") == student_id and row.get("state") != "closed"]
            if active_relations or open_attention:
                raise GovernanceError("student_archive_requires_closed_services_and_attention")
            student.update({"state": "archived", "archived_at": _now(), "archived_by": identity.canonical_user_id, "archive_reason": str(reason or "")})
            return {"student": deepcopy(student), "physical_delete": False}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="student_archived", mutate=apply)

    # --- service relationship and data-scope permission -----------------
    def assign_service(self, *, identity: UserIdentity, tenant_id: str, student_id: str, service_type: str, class_or_course_id: str, assignee_user_id: str, operation_id: str) -> dict[str, Any]:
        if service_type not in SERVICE_TYPES:
            raise GovernanceError("unsupported_service_type")
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            student = self._student(doc, student_id)
            if student.get("state") != "active":
                raise GovernanceError("student_not_active")
            campus_id = str(student.get("campus_id") or "")
            self._require_campus_manager(doc, identity, tenant_id, campus_id)
            assignee = self._active_employment(doc, tenant_id=tenant_id, user_id=assignee_user_id)
            if not assignee or str(assignee.get("campus_id") or "") != campus_id:
                raise GovernanceError("assignee_not_active_in_student_campus")
            key = (student_id, service_type, str(class_or_course_id or ""))
            if any((str(row.get("student_id") or ""), str(row.get("service_type") or ""), str(row.get("class_or_course_id") or "")) == key and row.get("state") == "active" for row in doc["service_relations"]):
                raise GovernanceError("active_service_relation_already_exists")
            row = {"relation_id": _id("relation"), "tenant_id": tenant_id, "student_id": student_id, "campus_id": campus_id,
                   "service_type": service_type, "class_or_course_id": str(class_or_course_id or ""), "assignee_user_id": str(assignee_user_id),
                   "state": "active", "effective_from": _now(), "effective_until": "", "created_by": identity.canonical_user_id}
            doc["service_relations"].append(row)
            return {"service_relation": deepcopy(row)}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="service_assigned", mutate=apply)

    def permission(self, *, identity: UserIdentity, tenant_id: str, student_id: str, scope: str, relation_id: str = "", write: bool = False) -> bool:
        self._trusted(identity, tenant_id)
        doc = self.snapshot()
        student = self._student(doc, student_id)
        if str(student.get("tenant_id") or "") != tenant_id or student.get("state") != "active":
            return False
        if identity.role == "boss":
            return True
        employment = self._active_employment(doc, tenant_id=tenant_id, user_id=identity.canonical_user_id)
        if not employment:
            return False
        campus_id = str(student.get("campus_id") or "")
        if identity.role == "manager":
            return str(employment.get("role") or "") == "manager" and str(employment.get("campus_id") or "") == campus_id
        active = [row for row in doc["service_relations"] if isinstance(row, dict) and row.get("state") == "active" and str(row.get("student_id") or "") == student_id and str(row.get("assignee_user_id") or "") == identity.canonical_user_id]
        if scope == "public_master":
            return bool(active)
        return bool(relation_id and any(str(row.get("relation_id") or "") == relation_id for row in active))

    def update_public_profile(self, *, identity: UserIdentity, tenant_id: str, student_id: str, changes: dict[str, Any], operation_id: str) -> dict[str, Any]:
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            if not self.permission(identity=identity, tenant_id=tenant_id, student_id=student_id, scope="public_master", write=True):
                raise GovernanceError("public_profile_permission_denied")
            fields = set(changes or {})
            if fields & HIGH_RISK_PUBLIC_FIELDS:
                request = {"request_id": _id("profile_change"), "tenant_id": tenant_id, "student_id": student_id,
                           "requested_changes": deepcopy(changes), "state": "pending_high_risk_confirmation", "requested_by": identity.canonical_user_id, "created_at": _now()}
                doc["change_requests"].append(request)
                return {"change_request": deepcopy(request), "applied": False}
            if not fields <= ORDINARY_PUBLIC_FIELDS:
                raise GovernanceError("unsupported_public_profile_field")
            student = self._student(doc, student_id)
            before = deepcopy(student.get("ordinary_profile") or {})
            student["ordinary_profile"] = {**before, **deepcopy(changes)}
            student["ordinary_profile_updated_at"] = _now()
            student["ordinary_profile_updated_by"] = identity.canonical_user_id
            return {"student_id": student_id, "before": before, "after": deepcopy(student["ordinary_profile"]), "applied": True}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="public_profile_change", mutate=apply)

    def confirm_high_risk_public_change(self, *, identity: UserIdentity, tenant_id: str, request_id: str, operation_id: str) -> dict[str, Any]:
        """Campus manager confirms routine high-risk changes; boss remains global."""
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            request = self._find(doc["change_requests"], "request_id", request_id)
            if request.get("state") != "pending_high_risk_confirmation":
                raise GovernanceError("profile_change_request_not_pending")
            student = self._student(doc, str(request.get("student_id") or ""))
            self._require_campus_manager(doc, identity, tenant_id, str(student.get("campus_id") or ""))
            before = deepcopy(student.get("high_risk_profile") or {})
            student["high_risk_profile"] = {**before, **deepcopy(request.get("requested_changes") or {})}
            request.update({"state": "confirmed", "confirmed_by": identity.canonical_user_id, "confirmed_at": _now()})
            return {"student_id": student["student_id"], "before": before, "after": deepcopy(student["high_risk_profile"]), "request": deepcopy(request)}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="high_risk_public_profile_confirmed", mutate=apply)

    def read_service_records(self, *, identity: UserIdentity, tenant_id: str, student_id: str, relation_id: str) -> list[dict[str, Any]]:
        """Read only the specified service's immutable originals."""
        if not self.permission(identity=identity, tenant_id=tenant_id, student_id=student_id, scope="service_record", relation_id=relation_id):
            raise GovernanceError("service_record_permission_denied")
        doc = self.snapshot()
        return [deepcopy(row) for row in doc["service_records"] if str(row.get("student_id") or "") == student_id and str(row.get("service_relation_id") or "") == relation_id]

    def create_service_record(self, *, identity: UserIdentity, tenant_id: str, relation_id: str, content: str, operation_id: str) -> dict[str, Any]:
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            relation = self._relation(doc, relation_id)
            if relation.get("state") != "active" or not self.permission(identity=identity, tenant_id=tenant_id, student_id=str(relation.get("student_id") or ""), scope="service_record", relation_id=relation_id, write=True):
                raise GovernanceError("service_record_permission_denied")
            if identity.role == "manager":
                raise GovernanceError("manager_cannot_overwrite_service_original_record")
            if not str(content or "").strip():
                raise GovernanceError("service_record_content_required")
            row = {"record_id": _id("service_record"), "tenant_id": tenant_id, "student_id": relation["student_id"], "service_relation_id": relation_id,
                   "service_type": relation["service_type"], "content": str(content).strip(), "author_user_id": identity.canonical_user_id, "created_at": _now(), "state": "final"}
            doc["service_records"].append(row)
            return {"record": deepcopy(row)}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="service_record_created", mutate=apply)

    # --- cross-service attention ----------------------------------------
    def create_attention(self, *, identity: UserIdentity, tenant_id: str, source_record_id: str, target_relation_ids: list[str], category: str, summary: str, operation_id: str) -> dict[str, Any]:
        if category not in {"mandatory", "suggested"}:
            raise GovernanceError("attention_category_invalid")
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            record = self._find(doc["service_records"], "record_id", source_record_id)
            source_relation = self._relation(doc, str(record.get("service_relation_id") or ""))
            if not self.permission(identity=identity, tenant_id=tenant_id, student_id=str(source_relation.get("student_id") or ""), scope="service_record", relation_id=str(source_relation.get("relation_id") or ""), write=True):
                raise GovernanceError("attention_source_permission_denied")
            targets = [self._relation(doc, value) for value in target_relation_ids]
            if not targets or any(target.get("state") != "active" or str(target.get("student_id") or "") != str(source_relation.get("student_id") or "") for target in targets):
                raise GovernanceError("attention_target_relation_invalid")
            row = {"attention_id": _id("attention"), "tenant_id": tenant_id, "student_id": source_relation["student_id"], "source_record_id": source_record_id,
                   "source_relation_id": source_relation["relation_id"], "category": category, "summary": str(summary or "").strip(),
                   "state": "open" if category == "mandatory" else "awaiting_confirmation", "targets": [{"relation_id": target["relation_id"], "state": "open" if category == "mandatory" else "awaiting_confirmation", "outcome": ""} for target in targets],
                   "created_by": identity.canonical_user_id, "created_at": _now(), "updated_at": _now()}
            doc["attentions"].append(row)
            return {"attention": deepcopy(row), "agenda_eligible": row["state"] == "open"}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="cross_service_attention_created", mutate=apply)

    def confirm_attention(self, *, identity: UserIdentity, tenant_id: str, attention_id: str, operation_id: str) -> dict[str, Any]:
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            row = self._find(doc["attentions"], "attention_id", attention_id)
            if row.get("state") != "awaiting_confirmation":
                raise GovernanceError("attention_not_awaiting_confirmation")
            allowed = identity.role == "boss"
            if not allowed:
                for target in row.get("targets") or []:
                    relation = self._relation(doc, str(target.get("relation_id") or ""))
                    if self.permission(identity=identity, tenant_id=tenant_id, student_id=str(relation.get("student_id") or ""), scope="service_record", relation_id=str(relation.get("relation_id") or "")):
                        allowed = True
                        break
            if not allowed:
                raise GovernanceError("attention_confirmation_permission_denied")
            row["state"] = "open"
            for target in row["targets"]:
                target["state"] = "open"
            row["confirmed_at"] = _now(); row["confirmed_by"] = identity.canonical_user_id; row["updated_at"] = _now()
            return {"attention": deepcopy(row), "agenda_eligible": True}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="cross_service_attention_confirmed", mutate=apply)

    def resolve_attention_target(self, *, identity: UserIdentity, tenant_id: str, attention_id: str, relation_id: str, outcome: str, operation_id: str) -> dict[str, Any]:
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            row = self._find(doc["attentions"], "attention_id", attention_id)
            target = next((item for item in row.get("targets") or [] if str(item.get("relation_id") or "") == relation_id), None)
            if not isinstance(target, dict) or target.get("state") in {"closed", "cancelled"}:
                raise GovernanceError("attention_target_not_open")
            relation = self._relation(doc, relation_id)
            if not self.permission(identity=identity, tenant_id=tenant_id, student_id=str(relation.get("student_id") or ""), scope="service_record", relation_id=relation_id, write=True):
                raise GovernanceError("attention_target_permission_denied")
            target.update({"state": "closed", "outcome": str(outcome or "").strip(), "closed_by": identity.canonical_user_id, "closed_at": _now()})
            row["state"] = "closed" if all(item.get("state") == "closed" for item in row["targets"]) else "acknowledged"
            row["updated_at"] = _now()
            return {"attention": deepcopy(row), "agenda_eligible": row["state"] != "closed"}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="cross_service_attention_target_closed", mutate=apply)

    # --- work ownership and handover ------------------------------------
    def link_unfinished_work(self, *, identity: UserIdentity, tenant_id: str, work_id: str, responsible_user_id: str, service_relation_id: str, operation_id: str) -> dict[str, Any]:
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            relation = self._relation(doc, service_relation_id)
            self._require_campus_manager(doc, identity, tenant_id, str(relation.get("campus_id") or ""))
            if relation.get("state") != "active" or str(relation.get("assignee_user_id") or "") != str(responsible_user_id):
                raise GovernanceError("work_owner_must_match_active_service_relation")
            if any(str(row.get("work_id") or "") == str(work_id) and row.get("state") == "active" for row in doc["work_links"]):
                raise GovernanceError("active_work_link_exists")
            row = {"link_id": _id("work_link"), "tenant_id": tenant_id, "work_id": str(work_id), "responsible_user_id": str(responsible_user_id),
                   "service_relation_id": service_relation_id, "state": "active", "created_at": _now(), "created_by": identity.canonical_user_id}
            doc["work_links"].append(row)
            return {"work_link": deepcopy(row)}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="unfinished_work_linked", mutate=apply)

    def resolve_unfinished_work(self, *, identity: UserIdentity, tenant_id: str, work_id: str, disposition: str, recipient_user_id: str = "", recipient_relation_id: str = "", operation_id: str = "") -> dict[str, Any]:
        """Close or transfer an explicitly identified current Work responsibility."""
        if disposition not in {"close", "transfer"}:
            raise GovernanceError("work_disposition_invalid")
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            link = next((row for row in doc["work_links"] if str(row.get("work_id") or "") == str(work_id) and row.get("state") == "active"), None)
            if not link:
                raise GovernanceError("active_work_link_not_found")
            relation = self._relation(doc, str(link.get("service_relation_id") or ""))
            self._require_campus_manager(doc, identity, tenant_id, str(relation.get("campus_id") or ""))
            if disposition == "close":
                link.update({"state": "closed", "closed_at": _now(), "closed_by": identity.canonical_user_id, "close_reason": "explicit_governance_disposition"})
            else:
                target = self._relation(doc, recipient_relation_id)
                if target.get("state") != "active" or str(target.get("campus_id") or "") != str(relation.get("campus_id") or "") or str(target.get("assignee_user_id") or "") != str(recipient_user_id):
                    raise GovernanceError("work_transfer_target_invalid")
                link.update({"responsible_user_id": recipient_user_id, "service_relation_id": recipient_relation_id, "transferred_at": _now(), "transferred_by": identity.canonical_user_id})
            return {"work_link": deepcopy(link)}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="unfinished_work_resolved", mutate=apply)

    def open_handover(self, *, identity: UserIdentity, tenant_id: str, outgoing_user_id: str, kind: str, operation_id: str) -> dict[str, Any]:
        if kind not in {"normal", "emergency"}:
            raise GovernanceError("handover_kind_invalid")
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            outgoing = self._active_employment(doc, tenant_id=tenant_id, user_id=outgoing_user_id)
            if not outgoing:
                raise GovernanceError("outgoing_employee_not_active")
            self._require_campus_manager(doc, identity, tenant_id, str(outgoing.get("campus_id") or ""))
            if kind == "emergency":
                self._require_boss(identity)
                person = self._person(doc, tenant_id=tenant_id, user_id=outgoing_user_id)
                if not person:
                    raise GovernanceError("person_not_found")
                person.update({"state": "suspended", "suspended_at": _now(), "suspended_by": identity.canonical_user_id})
            relations = [str(row["relation_id"]) for row in doc["service_relations"] if row.get("state") == "active" and str(row.get("assignee_user_id") or "") == outgoing_user_id]
            work = [str(row["work_id"]) for row in doc["work_links"] if row.get("state") == "active" and str(row.get("responsible_user_id") or "") == outgoing_user_id]
            attention_targets = []
            for attention in doc["attentions"]:
                if attention.get("state") == "closed":
                    continue
                for target in attention.get("targets") or []:
                    relation = self._relation(doc, str(target.get("relation_id") or ""))
                    if relation.get("state") == "active" and str(relation.get("assignee_user_id") or "") == outgoing_user_id and target.get("state") != "closed":
                        attention_targets.append({"attention_id": attention["attention_id"], "relation_id": relation["relation_id"]})
            row = {"handover_id": _id("handover"), "tenant_id": tenant_id, "outgoing_user_id": outgoing_user_id, "kind": kind,
                   "state": "awaiting_handover", "inventory": {"relation_ids": relations, "work_ids": work, "attention_targets": attention_targets},
                   "created_at": _now(), "created_by": identity.canonical_user_id}
            doc["handovers"].append(row)
            return {"handover": deepcopy(row), "access_revoked_immediately": kind == "emergency"}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="handover_opened", mutate=apply)

    def complete_handover(self, *, identity: UserIdentity, tenant_id: str, handover_id: str, relation_recipients: dict[str, str], work_recipients: dict[str, str], close_relation_ids: list[str], close_work_ids: list[str], operation_id: str) -> dict[str, Any]:
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            handover = self._find(doc["handovers"], "handover_id", handover_id)
            if handover.get("state") != "awaiting_handover":
                raise GovernanceError("handover_not_open")
            outgoing_id = str(handover.get("outgoing_user_id") or "")
            inventory = handover.get("inventory") or {}
            closed_relations = set(close_relation_ids or [])
            closed_work = set(close_work_ids or [])
            replacement_map: dict[str, str] = {}
            for old_id in inventory.get("relation_ids") or []:
                old = self._relation(doc, str(old_id))
                self._require_campus_manager(doc, identity, tenant_id, str(old.get("campus_id") or ""))
                if old_id in closed_relations:
                    old.update({"state": "closed", "effective_until": _now(), "closed_by": identity.canonical_user_id, "close_reason": "handover_explicitly_closed"})
                    continue
                recipient = str((relation_recipients or {}).get(old_id) or "")
                target = self._active_employment(doc, tenant_id=tenant_id, user_id=recipient)
                if not target or str(target.get("campus_id") or "") != str(old.get("campus_id") or ""):
                    raise GovernanceError("handover_relation_recipient_invalid")
                old.update({"state": "transferred", "effective_until": _now(), "transferred_by": identity.canonical_user_id})
                new = {**{key: value for key, value in old.items() if key not in {"relation_id", "state", "effective_from", "effective_until", "transferred_by"}},
                       "relation_id": _id("relation"), "assignee_user_id": recipient, "state": "active", "effective_from": _now(), "effective_until": "", "supersedes_relation_id": old_id, "created_by": identity.canonical_user_id}
                doc["service_relations"].append(new)
                replacement_map[old_id] = new["relation_id"]
            for work_id in inventory.get("work_ids") or []:
                link = next((row for row in doc["work_links"] if str(row.get("work_id") or "") == str(work_id) and row.get("state") == "active"), None)
                if not link:
                    continue
                if work_id in closed_work:
                    link.update({"state": "closed", "closed_at": _now(), "closed_by": identity.canonical_user_id, "close_reason": "handover_explicitly_closed"})
                    continue
                recipient = str((work_recipients or {}).get(work_id) or "")
                if not self._active_employment(doc, tenant_id=tenant_id, user_id=recipient):
                    raise GovernanceError("handover_work_recipient_invalid")
                link.update({"responsible_user_id": recipient, "service_relation_id": replacement_map.get(str(link.get("service_relation_id") or ""), str(link.get("service_relation_id") or "")), "transferred_at": _now(), "transferred_by": identity.canonical_user_id})
            for attention in doc["attentions"]:
                for target in attention.get("targets") or []:
                    old_relation_id = str(target.get("relation_id") or "")
                    if old_relation_id in replacement_map and target.get("state") != "closed":
                        target["relation_id"] = replacement_map[old_relation_id]
                        target["reassigned_at"] = _now(); target["reassigned_by"] = identity.canonical_user_id
            remaining_relations = [row for row in doc["service_relations"] if row.get("state") == "active" and str(row.get("assignee_user_id") or "") == outgoing_id]
            remaining_work = [row for row in doc["work_links"] if row.get("state") == "active" and str(row.get("responsible_user_id") or "") == outgoing_id]
            if remaining_relations or remaining_work:
                raise GovernanceError("handover_inventory_not_fully_reassigned_or_closed")
            handover.update({"state": "completed", "completed_at": _now(), "completed_by": identity.canonical_user_id, "relation_replacements": replacement_map})
            return {"handover": deepcopy(handover), "relation_replacements": deepcopy(replacement_map)}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="handover_completed", mutate=apply)

    def offboard_after_handover(self, *, identity: UserIdentity, tenant_id: str, staff_user_id: str, handover_id: str, operation_id: str) -> dict[str, Any]:
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            self._require_boss(identity)
            handover = self._find(doc["handovers"], "handover_id", handover_id)
            if handover.get("state") != "completed" or str(handover.get("outgoing_user_id") or "") != str(staff_user_id):
                raise GovernanceError("completed_handover_required_before_offboarding")
            person = self._person(doc, tenant_id=tenant_id, user_id=staff_user_id)
            if not person or person.get("state") not in {"active", "suspended"}:
                raise GovernanceError("person_not_active_or_suspended")
            open_assignments = [row for row in doc["employments"] if str(row.get("tenant_id") or "") == tenant_id and str(row.get("staff_user_id") or "") == staff_user_id and row.get("state") == "active" and not str(row.get("effective_until") or "")]
            for assignment in open_assignments:
                assignment.update({"state": "closed", "effective_until": _now(), "closed_by": identity.canonical_user_id, "close_reason": "offboarding"})
            person.update({"state": "left", "left_at": _now(), "left_by": identity.canonical_user_id})
            return {"staff_user_id": staff_user_id, "person_state": "left", "closed_assignment_count": len(open_assignments), "history_preserved": True, "runtime_identity_revocation_requires_existing_staff_bridge": True}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="employment_offboarded_after_handover", mutate=apply)

    # --- facts only: public Agenda input --------------------------------
    def agenda_facts(self, *, tenant_id: str) -> list[dict[str, Any]]:
        """Return state facts only; caller must use Work Runtime to wake Hermes."""
        doc = self.snapshot()
        facts: list[dict[str, Any]] = []
        for handover in doc["handovers"]:
            if handover.get("tenant_id") == tenant_id and handover.get("state") == "awaiting_handover":
                facts.append({"kind": "personnel_handover_awaiting", "handover_id": handover["handover_id"], "source_version": _digest({"state": handover.get("state"), "inventory": handover.get("inventory")})[:24]})
        for attention in doc["attentions"]:
            if attention.get("tenant_id") == tenant_id and attention.get("state") in {"open", "acknowledged"}:
                for target in attention.get("targets") or []:
                    if target.get("state") != "closed":
                        facts.append({"kind": "cross_service_attention_open", "attention_id": attention["attention_id"], "target_relation_id": target.get("relation_id"), "source_version": _digest({"state": attention.get("state"), "target": target})[:24]})
        return facts
