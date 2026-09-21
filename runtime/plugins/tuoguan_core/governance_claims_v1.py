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
        if claim_type == "person_assignment" and str(payload.get("role") or "") not in {"manager", "teacher"}:
            raise ClaimError("claim_invalid_initial_role")
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
                # A human may state “午托归示例老师”. Resolve that display name
                # only against one already-authoritative active v1 person;
                # never against a legacy teacher field or model guess.
                assignee_user_id = str(self.governance.resolve_active_person_by_display_name(
                    tenant_id=tenant_id,
                    person_name=str(payload.get("assignee_name") or ""),
                ).get("staff_user_id") or "")
            result = self.governance.assign_service(identity=identity, tenant_id=tenant_id, student_id=str(payload.get("student_id") or ""), service_type=str(payload.get("service_type") or ""), class_or_course_id=str(payload.get("class_or_course_id") or ""), assignee_user_id=assignee_user_id, operation_id=claim_id + ":service_assign")
            return {"result": result, "operation": "assign_student_service"}
        if kind == "person_assignment":
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
        claim = submitted.get("claim") or {}
        if str(claim.get("state") or "") != "awaiting_confirmation":
            return {
                **submitted,
                "recorded": False,
                "requires_explicit_resolution": True,
            }
        confirmed = self.confirm_claim(
            identity=identity,
            tenant_id=tenant_id,
            claim_id=str(claim.get("claim_id") or ""),
            operation_id=operation_id + ":confirm",
        )
        return {
            **confirmed,
            "recorded": bool((confirmed.get("execution_receipt") or {}).get("writeback_verified")),
        }

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
