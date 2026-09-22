"""Progressive, human-confirmed migration facts for XiaoYou governance.

Legacy Workspace files are evidence only. This capability records server-issued
reference observations and explicit human-confirmed claims; it never promotes a
legacy staff/student/teacher field on its own. Hermes chooses whether to query,
clarify or invoke a confirmation Tool. This module only validates scope,
idempotency, audit and the resulting authoritative write.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any
import uuid

from .models import UserIdentity
from .store import TuoguanStore, TuoguanStoreError

try:
    from .personnel_service_governance_v1 import GovernanceError, PersonnelServiceGovernance, SERVICE_TYPES
    from .personnel_service_tool_port_v1 import receipt_for_candidate_write
except ImportError:  # standalone certification import
    from personnel_service_governance_v1 import GovernanceError, PersonnelServiceGovernance, SERVICE_TYPES
    from personnel_service_tool_port_v1 import receipt_for_candidate_write


CLAIMS_FILE = "governance_claims_v1.json"
CLAIM_SCHEMA_VERSION = 1
CLAIM_TYPES = frozenset({
    "person_assignment", "person_status", "manager_scope", "student_master",
    "student_service", "legacy_teacher_interpretation", "work_disposition",
    "handover_open", "handover_complete",
    "duplicate_decision", "historical_record_classification", "attention_disposition",
})


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _hash(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _empty() -> dict[str, Any]:
    return {"schema_version": CLAIM_SCHEMA_VERSION, "observations": {}, "claims": {}, "operations": {}, "audit": []}


def _doc(value: Any) -> dict[str, Any]:
    base = _empty()
    if not isinstance(value, dict):
        return base
    for key, fallback in base.items():
        candidate = value.get(key, fallback)
        base[key] = candidate if isinstance(candidate, type(fallback)) else deepcopy(fallback)
    return base


class ClaimError(ValueError):
    pass


class ReadOnlyLegacyReference:
    """Read existing files without treating any field as authoritative truth."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)

    def _json(self, name: str) -> Any:
        path = self.data_dir / name
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return {}

    @staticmethod
    def _display(row: dict[str, Any], key: str, kind: str) -> dict[str, Any]:
        # Deliberately small: enough for Hermes to ask a precise question, not
        # enough to silently reconstruct a complete current profile.
        return {
            "legacy_key": key,
            "kind": kind,
            "name": str(row.get("name") or key),
            "campus_id": str(row.get("campus_id") or ""),
            "legacy_status": str(row.get("status") or row.get("summer_status") or ""),
            "legacy_teacher_present": bool(str(row.get("teacher") or "").strip()),
            "reference_only": True,
        }

    def observe(self, *, kind: str, query: str) -> list[dict[str, Any]]:
        query = str(query or "").strip()
        if not query:
            return []
        if kind == "person":
            source = self._json("staff.json")
            rows = source.items() if isinstance(source, dict) else []
            return [{"source_file": "staff.json", "source_key": str(key), "raw": deepcopy(value), "display": self._display(value, str(key), kind)}
                    for key, value in rows if isinstance(value, dict) and query in {str(key), str(value.get("name") or ""), str(value.get("display_name") or "") }]
        if kind == "student":
            source = self._json("students.json")
            rows = source.items() if isinstance(source, dict) else []
            normalized = "".join(ch for ch in query if ch.isdigit())
            matched = []
            for key, value in rows:
                if not isinstance(value, dict):
                    continue
                phone = "".join(ch for ch in str(value.get("phone") or "") if ch.isdigit())
                if query == str(key) or query == str(value.get("name") or "") or (normalized and normalized == phone):
                    matched.append({"source_file": "students.json", "source_key": str(key), "raw": deepcopy(value), "display": self._display(value, str(key), kind)})
            return matched
        if kind == "work":
            rows = []
            path = self.data_dir / "hermes_work_items.jsonl"
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    value = json.loads(line)
                    if isinstance(value, dict) and query in {str(value.get("work_item_id") or ""), str(value.get("title") or "")}:
                        rows.append({"source_file": "hermes_work_items.jsonl", "source_key": str(value.get("work_item_id") or ""), "raw": value, "display": {"legacy_key": str(value.get("work_item_id") or ""), "kind": kind, "legacy_status": str(value.get("status") or ""), "reference_only": True}})
            except (OSError, ValueError, UnicodeError):
                pass
            return rows
        raise ClaimError("unsupported_legacy_reference_kind")


class GovernanceClaimService:
    """Durable claim/confirm saga over the candidate governance authority."""

    def __init__(self, *, governance: PersonnelServiceGovernance, legacy_data_dir: str | Path) -> None:
        self.governance = governance
        self.store = governance.store
        self.legacy = ReadOnlyLegacyReference(legacy_data_dir)

    @staticmethod
    def tool_contracts() -> list[dict[str, Any]]:
        """Model-visible Tool descriptions; no text classifier or router."""
        return [
            {"name": "query_legacy_governance_reference", "purpose": "Read a legacy staff, student or work reference as unconfirmed evidence and reveal gaps/conflicts."},
            {"name": "activate_confirmed_pending_identity", "purpose": "Atomically approve one server-recorded pending WeCom userid into a boss-confirmed active person and current employment; never compose it from status restoration steps."},
            {"name": "submit_governance_claim", "purpose": "Record an explicitly stated real-world governance fact for authorised human confirmation; does not itself make it true."},
            {"name": "confirm_governance_claim", "purpose": "Apply one authorised human-confirmed governance claim through Permission, Receipt and writeback verification."},
            {"name": "query_pending_governance_claims", "purpose": "Show outstanding confirmations or conflicts in the caller's permitted campus scope."},
            {"name": "query_authoritative_governance", "purpose": "Read only already-confirmed current v1 people or students needed to continue a claim; never use legacy fields as authority."},
        ]

    def _mutate(self, *, operation_id: str, identity: UserIdentity, tenant_id: str, action: str, callback) -> dict[str, Any]:
        operation_id = str(operation_id or "").strip()
        if not operation_id:
            raise ClaimError("missing_operation_id")
        if identity.approval_state != "approved" or identity.role not in {"boss", "manager", "teacher"}:
            raise ClaimError("identity_not_authorized")
        reused = False
        def update(raw: Any) -> dict[str, Any]:
            nonlocal reused
            doc = _doc(raw)
            if operation_id in doc["operations"]:
                reused = True
                return doc
            result = callback(doc)
            if not isinstance(result, dict):
                raise ClaimError("invalid_claim_mutation")
            doc["operations"][operation_id] = {"action": action, "result": deepcopy(result), "created_at": _now()}
            doc["audit"].append({"audit_id": "claim_audit_" + uuid.uuid4().hex, "operation_id": operation_id, "action": action, "tenant_id": tenant_id, "actor_user_id": identity.canonical_user_id, "actor_role": identity.role, "created_at": _now()})
            return doc
        try:
            saved = _doc(self.store.update_json(CLAIMS_FILE, _empty(), update))
        except TuoguanStoreError as exc:
            if isinstance(exc.__cause__, (ClaimError, GovernanceError)):
                raise exc.__cause__
            raise
        entry = saved["operations"].get(operation_id) or {}
        if str(entry.get("action") or "") != action:
            raise ClaimError("operation_id_reused_for_different_action")
        result = deepcopy(entry.get("result") or {})
        result.update({"operation_id": operation_id, "writeback_verified": True, "already_applied": reused})
        return result

    def _can_manage(self, identity: UserIdentity, tenant_id: str, campus_id: str) -> bool:
        return identity.role == "boss" or bool(campus_id and self.governance.can_manage_campus(identity=identity, tenant_id=tenant_id, campus_id=campus_id))

    @staticmethod
    def _validate_claim_payload(*, claim_type: str, payload: dict[str, Any]) -> None:
        """Fail early on a declared Tool contract; never infer missing facts."""
        required: dict[str, tuple[str, ...]] = {
            "person_assignment": ("staff_user_id", "role", "campus_id"),
            "person_status": ("staff_user_id", "state", "campus_id"),
            "manager_scope": ("employment_id", "managed_campus_ids"),
            "student_master": ("name", "campus_id"),
            "student_service": ("student_id", "service_type", "class_or_course_id"),
            "work_disposition": ("work_id", "disposition"),
            "handover_open": ("outgoing_user_id", "campus_id", "kind"),
            "handover_complete": ("handover_id", "campus_id"),
            "attention_disposition": ("attention_id", "relation_id", "outcome"),
        }
        missing = [
            key for key in required.get(claim_type, ())
            if payload.get(key) in (None, "", [])
        ]
        if claim_type == "student_service" and not (
            str(payload.get("assignee_user_id") or "").strip()
            or str(payload.get("assignee_name") or "").strip()
        ):
            missing.append("assignee_name_or_assignee_user_id")
        if missing:
            raise ClaimError("claim_missing_required_facts:" + ",".join(missing))
        if claim_type == "student_service" and str(payload.get("service_type") or "") not in SERVICE_TYPES:
            raise ClaimError("claim_invalid_service_type")
        if claim_type == "student_master":
            ordinary_profile = payload.get("ordinary_profile") or {}
            if not isinstance(ordinary_profile, dict):
                raise ClaimError("claim_ordinary_profile_must_be_object")
            permitted_ordinary = {"school", "grade", "class_name", "ordinary_contact", "ordinary_note"}
            if any(str(key) not in permitted_ordinary for key in ordinary_profile):
                raise ClaimError("claim_ordinary_profile_contains_restricted_fields")
        if claim_type == "person_assignment" and str(payload.get("role") or "") not in {"boss", "manager", "teacher"}:
            raise ClaimError("claim_invalid_person_role")
        if claim_type == "person_status" and str(payload.get("state") or "") not in {"active", "suspended", "left"}:
            raise ClaimError("claim_invalid_person_state")
        if claim_type == "person_status" and str(payload.get("state") or "") == "left" and not str(payload.get("handover_id") or "").strip():
            raise ClaimError("claim_missing_required_facts:handover_id")
        if claim_type == "work_disposition" and str(payload.get("disposition") or "") not in {"close", "transfer"}:
            raise ClaimError("claim_invalid_work_disposition")
        if claim_type == "handover_open" and str(payload.get("kind") or "") not in {"normal", "emergency"}:
            raise ClaimError("claim_invalid_handover_kind")

    def _authoritative_work_campus(self, *, tenant_id: str, work_id: str) -> str:
        """Find a Work scope only from an already-authoritative active link.

        A historical work row often contains no campus. A manager must not
        infer access from that omission. If that Work has already been linked
        to an active v1 responsibility, its relation provides the trusted
        campus scope; otherwise only a boss may inspect the unscoped
        reference.
        """
        state = self.governance.snapshot()
        link = next((
            row for row in state.get("work_links") or []
            if isinstance(row, dict)
            and str(row.get("tenant_id") or "") == tenant_id
            and str(row.get("work_id") or "") == str(work_id)
            and str(row.get("state") or "") == "active"
        ), None)
        if not link:
            return ""
        relation_id = str(link.get("service_relation_id") or "")
        relation = next((
            row for row in state.get("service_relations") or []
            if isinstance(row, dict) and str(row.get("relation_id") or "") == relation_id
        ), None)
        return str((relation or {}).get("campus_id") or "")

    def observe_legacy(self, *, identity: UserIdentity, tenant_id: str, kind: str, query: str, operation_id: str) -> dict[str, Any]:
        if identity.role == "teacher":
            raise ClaimError("teacher_cannot_browse_unconfirmed_governance_references")
        rows = self.legacy.observe(kind=kind, query=query)
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            visible = []
            for row in rows:
                display = deepcopy(row.get("display") or {})
                if kind == "work" and not str(display.get("campus_id") or ""):
                    display["campus_id"] = self._authoritative_work_campus(
                        tenant_id=tenant_id,
                        work_id=str(row.get("source_key") or ""),
                    )
                    display["scope_source"] = (
                        "authoritative_work_link"
                        if str(display.get("campus_id") or "")
                        else "unscoped_legacy_reference"
                    )
                campus = str(display.get("campus_id") or "")
                if identity.role == "manager" and not self._can_manage(identity, tenant_id, campus):
                    continue
                reference_id = "legacy_ref_" + _hash({"file": row["source_file"], "key": row["source_key"], "raw": row["raw"]})[:24]
                doc["observations"][reference_id] = {"reference_id": reference_id, "tenant_id": tenant_id, "kind": kind, "source_file": row["source_file"], "source_key": row["source_key"], "legacy_digest": _hash(row["raw"]), "display": display, "observed_at": _now(), "reference_only": True}
                visible.append(doc["observations"][reference_id])
            state = "not_found" if not visible else "ambiguous" if len(visible) > 1 else "unique"
            return {"reference_state": state, "references": deepcopy(visible), "legacy_is_authoritative": False, "next_requirement": "ask_for_human_confirmation" if state == "unique" else "clarify_or_resolve_conflict"}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="legacy_reference_observed", callback=apply)

    def submit_claim(
        self,
        *,
        identity: UserIdentity,
        tenant_id: str,
        claim_type: str,
        reference_ids: list[str],
        payload: dict[str, Any],
        operation_id: str,
        allow_authorized_statement_without_legacy: bool = False,
    ) -> dict[str, Any]:
        if claim_type not in CLAIM_TYPES:
            raise ClaimError("unsupported_claim_type")
        if identity.role == "teacher":
            raise ClaimError("teacher_cannot_submit_governance_claim")
        # Tool callers sometimes serialise an optional empty array as [""];
        # this is still “no legacy reference”, not a forged reference.  Keep
        # only actual server-issued ids before enforcing source provenance.
        reference_ids = [
            str(item).strip()
            for item in (reference_ids or [])
            if str(item).strip()
        ]
        normalized_payload = deepcopy(payload or {})
        if not isinstance(normalized_payload, dict):
            raise ClaimError("claim_payload_must_be_object")
        self._validate_claim_payload(claim_type=claim_type, payload=normalized_payload)
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            references = [doc["observations"].get(str(item)) for item in reference_ids]
            if references and any(not isinstance(row, dict) or row.get("tenant_id") != tenant_id for row in references):
                raise ClaimError("trusted_legacy_reference_required")
            if not references and not allow_authorized_statement_without_legacy:
                raise ClaimError("trusted_legacy_reference_required")
            campuses = {str((row.get("display") or {}).get("campus_id") or "") for row in references}
            requested_campus = str(normalized_payload.get("campus_id") or "")
            conflicts = []
            if len({value for value in campuses if value}) > 1:
                conflicts.append("legacy_references_span_multiple_campuses")
            if requested_campus and any(value and value != requested_campus for value in campuses):
                conflicts.append("claimed_campus_conflicts_with_legacy_reference")
            campus = requested_campus or next(iter({value for value in campuses if value}), "")
            if identity.role == "manager" and not self._can_manage(identity, tenant_id, campus):
                raise ClaimError("campus_scope_denied")
            claim_id = "claim_" + uuid.uuid4().hex
            row = {"claim_id": claim_id, "tenant_id": tenant_id, "claim_type": claim_type, "reference_ids": [str(item) for item in reference_ids], "payload": deepcopy(normalized_payload), "campus_id": campus, "state": "conflicted" if conflicts else "awaiting_confirmation", "conflicts": conflicts, "submitted_by": identity.canonical_user_id, "submitted_role": identity.role, "evidence_source": "legacy_reference_plus_authorized_statement" if references else "authorized_statement_only", "created_at": _now(), "updated_at": _now(), "authority_state": "unconfirmed"}
            doc["claims"][claim_id] = row
            return {"claim": deepcopy(row), "requires_human_confirmation": not conflicts, "legacy_is_authoritative": False}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="governance_claim_submitted", callback=apply)

    def _apply_claim(self, *, claim: dict[str, Any], identity: UserIdentity, tenant_id: str) -> dict[str, Any]:
        payload = claim.get("payload") or {}
        claim_id = str(claim["claim_id"])
        kind = str(claim["claim_type"])
        if kind == "student_master":
            result = self.governance.create_pending_student(identity=identity, tenant_id=tenant_id, name=str(payload.get("name") or ""), phone=str(payload.get("phone") or ""), campus_id=str(payload.get("campus_id") or claim.get("campus_id") or ""), ordinary_profile=payload.get("ordinary_profile") or {}, operation_id=claim_id + ":student_pending")
            student = (result.get("student") or {})
            confirmed = self.governance.confirm_student(identity=identity, tenant_id=tenant_id, student_id=str(student.get("student_id") or ""), operation_id=claim_id + ":student_confirm")
            return {"result": confirmed, "operation": "confirm_student_master"}
        if kind == "student_service":
            assignee_user_id = str(payload.get("assignee_user_id") or "")
            if not assignee_user_id:
                # A human may state “午托归相关老师”. Resolve that display name
                # only against one already-authoritative active v1 person;
                # never against a legacy teacher field or model guess.
                assignee_user_id = str(self.governance.resolve_active_person_by_display_name(
                    tenant_id=tenant_id,
                    person_name=str(payload.get("assignee_name") or ""),
                ).get("staff_user_id") or "")
            result = self.governance.assign_service(identity=identity, tenant_id=tenant_id, student_id=str(payload.get("student_id") or ""), service_type=str(payload.get("service_type") or ""), class_or_course_id=str(payload.get("class_or_course_id") or ""), assignee_user_id=assignee_user_id, operation_id=claim_id + ":service_assign")
            return {"result": result, "operation": "assign_student_service"}
        if kind == "person_assignment":
            staff_user_id = str(payload.get("staff_user_id") or "")
            desired_role = str(payload.get("role") or "")
            desired_campus = str(payload.get("campus_id") or claim.get("campus_id") or "")
            snapshot = self.governance.snapshot()
            existing_person = next(
                (
                    row for row in snapshot.get("people") or []
                    if isinstance(row, dict)
                    and str(row.get("tenant_id") or "") == tenant_id
                    and str(row.get("staff_user_id") or "") == staff_user_id
                    and str(row.get("state") or "") == "active"
                ),
                None,
            )
            current = next(
                (
                    row for row in snapshot.get("employments") or []
                    if isinstance(row, dict)
                    and str(row.get("tenant_id") or "") == tenant_id
                    and str(row.get("staff_user_id") or "") == staff_user_id
                    and str(row.get("state") or "") == "active"
                    and not str(row.get("effective_until") or "")
                ),
                None,
            )
            # A confirmed assignment for an existing active person is a
            # history-preserving role/campus transition, not a duplicate
            # person.  No natural-language inference happens here: the claim
            # payload already records the human-confirmed role and campus.
            if isinstance(existing_person, dict) and isinstance(current, dict):
                updated = current
                if str(updated.get("role") or "") != desired_role:
                    changed = self.governance.change_employment_role(
                        identity=identity,
                        tenant_id=tenant_id,
                        employment_id=str(updated.get("employment_id") or ""),
                        target_role=desired_role,
                        operation_id=claim_id + ":employment_role_change",
                    )
                    updated = dict(changed.get("current_employment") or {})
                if str(updated.get("campus_id") or "") != desired_campus:
                    moved = self.governance.transfer_employment(
                        identity=identity,
                        tenant_id=tenant_id,
                        employment_id=str(updated.get("employment_id") or ""),
                        target_campus_id=desired_campus,
                        operation_id=claim_id + ":employment_transfer",
                    )
                    updated = dict(moved.get("current_employment") or {})
                return {"result": {"person": existing_person, "employment": updated, "writeback_verified": True}, "operation": "update_current_employment"}
            result = self.governance.create_pending_employment(identity=identity, tenant_id=tenant_id, staff_user_id=str(payload.get("staff_user_id") or ""), role=str(payload.get("role") or ""), campus_id=str(payload.get("campus_id") or claim.get("campus_id") or ""), operation_id=claim_id + ":employment_pending", person_name=str(payload.get("person_name") or ""))
            if identity.role != "boss":
                return {"result": result, "operation": "create_pending_employment", "awaiting_boss_identity_activation": True}
            active = self.governance.activate_employment(identity=identity, tenant_id=tenant_id, employment_id=str((result.get("employment") or {}).get("employment_id") or ""), operation_id=claim_id + ":employment_activate")
            return {"result": active, "operation": "activate_employment"}
        if kind == "person_status":
            state = str(payload.get("state") or "")
            staff = str(payload.get("staff_user_id") or "")
            if state == "active":
                return {"result": self.governance.reactivate_person(identity=identity, tenant_id=tenant_id, staff_user_id=staff, operation_id=claim_id + ":reactivate"), "operation": "reactivate_person"}
            if state == "suspended":
                return {"result": self.governance.open_handover(identity=identity, tenant_id=tenant_id, outgoing_user_id=staff, kind="emergency", operation_id=claim_id + ":emergency_suspend"), "operation": "emergency_suspend_person"}
            if state == "left":
                return {"result": self.governance.offboard_after_handover(identity=identity, tenant_id=tenant_id, staff_user_id=staff, handover_id=str(payload.get("handover_id") or ""), operation_id=claim_id + ":offboard"), "operation": "offboard_after_handover"}
            raise ClaimError("unsupported_person_state_confirmation")
        if kind == "manager_scope":
            return {"result": self.governance.update_manager_scope(identity=identity, tenant_id=tenant_id, employment_id=str(payload.get("employment_id") or ""), managed_campus_ids=list(payload.get("managed_campus_ids") or []), operation_id=claim_id + ":manager_scope"), "operation": "update_manager_scope"}
        if kind == "work_disposition":
            return {"result": self.governance.resolve_unfinished_work(identity=identity, tenant_id=tenant_id, work_id=str(payload.get("work_id") or ""), disposition=str(payload.get("disposition") or ""), recipient_user_id=str(payload.get("recipient_user_id") or ""), recipient_relation_id=str(payload.get("recipient_relation_id") or ""), operation_id=claim_id + ":work"), "operation": "resolve_unfinished_work"}
        if kind == "handover_open":
            return {"result": self.governance.open_handover(identity=identity, tenant_id=tenant_id, outgoing_user_id=str(payload.get("outgoing_user_id") or ""), kind=str(payload.get("kind") or ""), operation_id=claim_id + ":handover_open"), "operation": "open_personnel_handover"}
        if kind == "handover_complete":
            return {"result": self.governance.complete_handover(identity=identity, tenant_id=tenant_id, handover_id=str(payload.get("handover_id") or ""), relation_recipients=dict(payload.get("relation_recipients") or {}), work_recipients=dict(payload.get("work_recipients") or {}), close_relation_ids=list(payload.get("close_relation_ids") or []), close_work_ids=list(payload.get("close_work_ids") or []), operation_id=claim_id + ":handover_complete"), "operation": "complete_personnel_handover"}
        if kind == "attention_disposition":
            return {"result": self.governance.resolve_attention_target(identity=identity, tenant_id=tenant_id, attention_id=str(payload.get("attention_id") or ""), relation_id=str(payload.get("relation_id") or ""), outcome=str(payload.get("outcome") or ""), operation_id=claim_id + ":attention"), "operation": "resolve_attention_target"}
        # Legacy teacher interpretation, duplicate decisions and unclassified
        # historical records are authoritative *decisions*, not automatic data
        # merges or fabricated service relationships.
        return {"result": {"decision_id": claim_id, "decision_kind": kind, "writeback_verified": True, "no_automatic_merge_or_assignment": True}, "operation": "confirm_governance_decision"}

    def confirm_claim(self, *, identity: UserIdentity, tenant_id: str, claim_id: str, operation_id: str) -> dict[str, Any]:
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            claim = doc["claims"].get(claim_id)
            if not isinstance(claim, dict):
                raise ClaimError("claim_not_found")
            if claim.get("state") == "confirmed":
                return {"claim": deepcopy(claim), "execution_receipt": deepcopy(claim.get("execution_receipt") or {}), "already_confirmed": True}
            if claim.get("state") not in {"awaiting_confirmation", "awaiting_boss_identity_activation"}:
                raise ClaimError("claim_conflict_or_not_confirmable")
            campus = str(claim.get("campus_id") or "")
            if not self._can_manage(identity, tenant_id, campus):
                raise ClaimError("claim_confirmation_scope_denied")
            applied = self._apply_claim(claim=claim, identity=identity, tenant_id=tenant_id)
            result = applied["result"]
            if applied.get("awaiting_boss_identity_activation"):
                claim.update({"state": "awaiting_boss_identity_activation", "updated_at": _now(), "authority_state": "pending"})
                return {"claim": deepcopy(claim), "next_requirement": "boss_confirm_identity_activation"}
            receipt_payload = receipt_for_candidate_write(operation=str(applied["operation"]), operation_id=claim_id + ":receipt", invoke=lambda: result)
            receipt = receipt_payload.get("execution_receipt") or {}
            if receipt.get("status") != "completed" or receipt.get("writeback_verified") is not True:
                raise ClaimError("claim_authoritative_writeback_not_verified")
            claim.update({"state": "confirmed", "authority_state": "confirmed", "confirmed_by": identity.canonical_user_id, "confirmed_at": _now(), "updated_at": _now(), "authoritative_result": deepcopy(result), "execution_receipt": deepcopy(receipt)})
            return {"claim": deepcopy(claim), "execution_receipt": deepcopy(receipt), "authoritative_writeback_verified": True}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="governance_claim_confirmed", callback=apply)

    def record_confirmed_claim(
        self,
        *,
        identity: UserIdentity,
        tenant_id: str,
        claim_type: str,
        reference_ids: list[str],
        payload: dict[str, Any],
        operation_id: str,
    ) -> dict[str, Any]:
        """Apply one explicit authorised fact through the full claim audit path.

        The model elects this Tool only when the current authorised person has
        made a complete, explicit confirmation.  This method never inspects
        natural-language text or legacy fields to manufacture that condition:
        it merely makes one Tool invocation atomic from the caller's point of
        view.  A conflict stays unconfirmed, and every completed write still
        goes through the same Permission, CommandBus, Receipt and writeback
        path used by the two-step submit/confirm flow.
        """
        submitted = self.submit_claim(
            identity=identity,
            tenant_id=tenant_id,
            claim_type=claim_type,
            reference_ids=reference_ids,
            payload=payload,
            operation_id=operation_id + ":submit",
            allow_authorized_statement_without_legacy=True,
        )
        # ``submit_claim`` is idempotent and intentionally returns its
        # original operation result on a replay.  The Claim itself may have
        # reached a later terminal state in the meantime, so direct-command
        # retries must re-read that authoritative state rather than mistake a
        # cached ``awaiting_confirmation`` snapshot for a new human decision.
        claim = submitted.get("claim") or {}
        claim_id = str(claim.get("claim_id") or "")
        if claim_id:
            current = _doc(self.store.read_json(CLAIMS_FILE, _empty())).get("claims", {}).get(claim_id)
            if isinstance(current, dict):
                claim = deepcopy(current)
                submitted = {**submitted, "claim": deepcopy(claim)}
        if str(claim.get("state") or "") == "execution_failed":
            # A duplicate callback must observe the same terminal failure,
            # never reinterpret it as a fresh request for human confirmation.
            return {
                **submitted,
                "recorded": False,
                "execution_failed": True,
                "error": str(claim.get("execution_error_code") or "governance_execution_failed"),
            }
        if str(claim.get("state") or "") == "confirmed":
            # This is the other legitimate direct-command replay: report the
            # original verified write, without calling the authority again.
            receipt = deepcopy(claim.get("execution_receipt") or {})
            return {
                **submitted,
                "execution_receipt": receipt,
                "recorded": receipt.get("writeback_verified") is True,
            }
        if str(claim.get("state") or "") != "awaiting_confirmation":
            return {
                **submitted,
                "recorded": False,
                "requires_explicit_resolution": True,
            }
        try:
            confirmed = self.confirm_claim(
                identity=identity,
                tenant_id=tenant_id,
                claim_id=claim_id,
                operation_id=operation_id + ":confirm",
            )
        except (ClaimError, GovernanceError) as exc:
            # A direct-confirmed Tool invocation has already received the
            # human decision.  Its execution failure is an auditable terminal
            # outcome, not a new request for the same human confirmation.  In
            # particular, Agenda only scans non-terminal governance claims;
            # leaving this one ``awaiting_confirmation`` would manufacture a
            # false follow-up and could re-surface raw internal claim material
            # to the owner.
            failed = self._mark_direct_execution_failed(
                identity=identity,
                tenant_id=tenant_id,
                claim_id=claim_id,
                error_code=str(exc),
                operation_id=operation_id + ":execution_failed",
            )
            return {
                **failed,
                "recorded": False,
                "execution_failed": True,
                "error": str(exc),
            }
        return {
            **confirmed,
            "recorded": bool((confirmed.get("execution_receipt") or {}).get("writeback_verified")),
        }

    def _mark_direct_execution_failed(
        self,
        *,
        identity: UserIdentity,
        tenant_id: str,
        claim_id: str,
        error_code: str,
        operation_id: str,
    ) -> dict[str, Any]:
        """Close an explicit-command Claim without making it Agenda work."""

        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            claim = doc["claims"].get(claim_id)
            if not isinstance(claim, dict):
                raise ClaimError("claim_not_found")
            if str(claim.get("tenant_id") or "") != tenant_id:
                raise ClaimError("claim_tenant_mismatch")
            if str(claim.get("state") or "") == "confirmed":
                raise ClaimError("claim_already_confirmed")
            claim.update({
                "state": "execution_failed",
                "authority_state": "unconfirmed",
                "execution_failed_at": _now(),
                "execution_failed_by": identity.canonical_user_id,
                "execution_error_code": str(error_code or "governance_execution_failed"),
                "requires_human_confirmation": False,
                "updated_at": _now(),
            })
            return {"claim": deepcopy(claim), "no_write_performed": True}

        return self._mutate(
            operation_id=operation_id,
            identity=identity,
            tenant_id=tenant_id,
            action="governance_claim_direct_execution_failed",
            callback=apply,
        )

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
        """Apply one boss-confirmed pending identity activation end-to-end.

        This is intentionally a governance operation rather than a Claim
        saga: a pending channel identity is already a server-recorded fact and
        the boss's current direct instruction is the confirmation.  There is
        therefore no intermediate ``awaiting_confirmation`` object to leak
        into Agenda if the protected write fails.
        """

        result = self.governance.activate_confirmed_pending_identity(
            identity=identity,
            tenant_id=tenant_id,
            staff_user_id=staff_user_id,
            person_name=person_name,
            role=role,
            campus_id=campus_id,
            operation_id=operation_id + ":authority_write",
        )
        receipt_payload = receipt_for_candidate_write(
            operation="activate_confirmed_pending_identity",
            operation_id=operation_id + ":receipt",
            invoke=lambda: result,
        )
        receipt = receipt_payload.get("execution_receipt") or {}
        if receipt.get("status") != "completed" or receipt.get("writeback_verified") is not True:
            raise ClaimError("pending_identity_authoritative_writeback_not_verified")
        supersession = self._supersede_claims_replaced_by_pending_identity_activation(
            identity=identity,
            tenant_id=tenant_id,
            staff_user_id=staff_user_id,
            role=role,
            campus_id=campus_id,
            successor_operation_id=str(result.get("operation_id") or operation_id),
            successor_receipt=receipt,
            operation_id=operation_id + ":supersede_replaced_claims",
        )
        return {
            "result": result,
            "execution_receipt": deepcopy(receipt),
            "authoritative_writeback_verified": True,
            "claim_supersession": supersession,
            "recorded": True,
        }

    @staticmethod
    def _is_replaced_by_pending_identity_activation(
        claim: dict[str, Any],
        *,
        tenant_id: str,
        staff_user_id: str,
        role: str,
        campus_id: str,
    ) -> bool:
        """Whether a previously active Claim asserts exactly the fact now verified.

        This is a structural lifecycle comparison over already-declared Tool
        arguments.  It neither interprets message text nor tries to decide
        whether a different unconfirmed personnel fact is important.  In
        particular, a different campus, role, suspension/exit state or any
        conflicted Claim remains open for its normal human resolution.
        """

        if str(claim.get("tenant_id") or "") != tenant_id:
            return False
        if str(claim.get("state") or "") not in {
            "awaiting_confirmation",
            "awaiting_boss_identity_activation",
        }:
            return False
        payload = claim.get("payload") or {}
        if not isinstance(payload, dict):
            return False
        if str(payload.get("staff_user_id") or "") != staff_user_id:
            return False
        if str(payload.get("campus_id") or claim.get("campus_id") or "") != campus_id:
            return False
        kind = str(claim.get("claim_type") or "")
        if kind == "person_status":
            return str(payload.get("state") or "") == "active"
        if kind == "person_assignment":
            return str(payload.get("role") or "") == role
        return False

    def _supersede_claims_replaced_by_pending_identity_activation(
        self,
        *,
        identity: UserIdentity,
        tenant_id: str,
        staff_user_id: str,
        role: str,
        campus_id: str,
        successor_operation_id: str,
        successor_receipt: dict[str, Any],
        operation_id: str,
    ) -> dict[str, Any]:
        """Close only obsolete active Claims after their fact is verified.

        Old direct-command failures from before the terminal-failure lifecycle
        existed can have only an ``awaiting_confirmation`` row.  The later,
        boss-authorised pending-identity activation is authoritative proof of
        the same identity's active employment.  Preserve those rows and their
        original evidence, but move only exact same-user/same-role/same-campus
        predecessor Claims to an explicit terminal state so Agenda cannot
        invent a new owner-confirmation task from obsolete work.
        """

        receipt_id = str(successor_receipt.get("receipt_id") or successor_receipt.get("operation_id") or "")

        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            superseded: list[dict[str, Any]] = []
            for claim in doc["claims"].values():
                if not isinstance(claim, dict) or not self._is_replaced_by_pending_identity_activation(
                    claim,
                    tenant_id=tenant_id,
                    staff_user_id=staff_user_id,
                    role=role,
                    campus_id=campus_id,
                ):
                    continue
                claim.update({
                    "state": "superseded_by_authoritative_activation",
                    "authority_state": "superseded",
                    "requires_human_confirmation": False,
                    "superseded_at": _now(),
                    "superseded_by": {
                        "kind": "pending_identity_activation",
                        "staff_user_id": staff_user_id,
                        "role": role,
                        "campus_id": campus_id,
                        "successor_operation_id": successor_operation_id,
                        "successor_receipt_id": receipt_id,
                        "authorised_by": identity.canonical_user_id,
                    },
                    "updated_at": _now(),
                })
                superseded.append(deepcopy(claim))
            return {
                "superseded_claim_ids": [str(row.get("claim_id") or "") for row in superseded],
                "superseded_claims": superseded,
            }

        return self._mutate(
            operation_id=operation_id,
            identity=identity,
            tenant_id=tenant_id,
            action="governance_claims_superseded_by_pending_identity_activation",
            callback=apply,
        )

    def resolve_claim_conflict(self, *, identity: UserIdentity, tenant_id: str, claim_id: str, resolution: str, operation_id: str) -> dict[str, Any]:
        """Record a human choice; it never merges legacy people or students."""
        if resolution not in {"references_are_distinct", "select_stated_reference", "reject_claim"}:
            raise ClaimError("unsupported_conflict_resolution")
        def apply(doc: dict[str, Any]) -> dict[str, Any]:
            claim = doc["claims"].get(claim_id)
            if not isinstance(claim, dict) or claim.get("state") != "conflicted":
                raise ClaimError("claim_not_conflicted")
            if not self._can_manage(identity, tenant_id, str(claim.get("campus_id") or "")):
                raise ClaimError("claim_confirmation_scope_denied")
            claim.update({"conflict_resolution": resolution, "conflict_resolved_by": identity.canonical_user_id, "conflict_resolved_at": _now(), "updated_at": _now()})
            if resolution == "reject_claim":
                claim.update({"state": "rejected", "authority_state": "unconfirmed"})
            else:
                claim.update({"state": "awaiting_confirmation"})
            return {"claim": deepcopy(claim), "automatic_merge": False}
        return self._mutate(operation_id=operation_id, identity=identity, tenant_id=tenant_id, action="governance_claim_conflict_resolved", callback=apply)

    def query_claims(self, *, identity: UserIdentity, tenant_id: str, states: set[str] | None = None) -> list[dict[str, Any]]:
        if identity.role == "teacher":
            raise ClaimError("teacher_cannot_query_governance_claims")
        doc = _doc(self.store.read_json(CLAIMS_FILE, _empty()))
        wanted = states or {"awaiting_confirmation", "awaiting_boss_identity_activation", "conflicted"}
        rows = []
        for claim in doc["claims"].values():
            if not isinstance(claim, dict) or claim.get("tenant_id") != tenant_id or claim.get("state") not in wanted:
                continue
            if self._can_manage(identity, tenant_id, str(claim.get("campus_id") or "")):
                rows.append(deepcopy(claim))
        return sorted(rows, key=lambda row: str(row.get("created_at") or ""))

    def query_authoritative_governance(
        self,
        *,
        identity: UserIdentity,
        tenant_id: str,
        kind: str,
        name: str = "",
        campus_id: str = "",
    ) -> list[dict[str, Any]]:
        """Read only the confirmed v1 authority needed to continue a claim.

        This is deliberately separate from the legacy-reference adapter.
        Managers receive only current rows in their active campus scope;
        unknown/unconfirmed legacy entries never appear as authority.
        """
        if identity.role == "teacher":
            raise ClaimError("teacher_cannot_query_governance_authority")
        wanted = str(name or "").strip()
        requested_campus = str(campus_id or "").strip()
        state = self.governance.snapshot()
        rows: list[dict[str, Any]] = []
        if kind == "student":
            for student in state.get("students") or []:
                if not isinstance(student, dict) or str(student.get("tenant_id") or "") != tenant_id:
                    continue
                if wanted and str(student.get("name") or "") != wanted:
                    continue
                campus = str(student.get("campus_id") or "")
                if requested_campus and campus != requested_campus:
                    continue
                if not self._can_manage(identity, tenant_id, campus):
                    continue
                rows.append({
                    "student_id": str(student.get("student_id") or ""),
                    "name": str(student.get("name") or ""),
                    "campus_id": campus,
                    "state": str(student.get("state") or ""),
                    "authority_state": "confirmed",
                })
        elif kind == "person":
            for person in state.get("people") or []:
                if not isinstance(person, dict) or str(person.get("tenant_id") or "") != tenant_id:
                    continue
                if wanted and str(person.get("display_name") or "") != wanted:
                    continue
                employment = next((
                    row for row in state.get("employments") or []
                    if isinstance(row, dict)
                    and str(row.get("tenant_id") or "") == tenant_id
                    and str(row.get("staff_user_id") or "") == str(person.get("staff_user_id") or "")
                    and str(row.get("state") or "") == "active"
                    and not str(row.get("effective_until") or "")
                ), None)
                campus = str((employment or {}).get("campus_id") or "")
                if requested_campus and campus != requested_campus:
                    continue
                if not employment or not self._can_manage(identity, tenant_id, campus):
                    continue
                rows.append({
                    "staff_user_id": str(person.get("staff_user_id") or ""),
                    "display_name": str(person.get("display_name") or ""),
                    "campus_id": campus,
                    "person_state": str(person.get("state") or ""),
                    "role": str(employment.get("role") or ""),
                    "authority_state": "confirmed",
                })
        else:
            raise ClaimError("unsupported_authoritative_governance_kind")
        return rows
