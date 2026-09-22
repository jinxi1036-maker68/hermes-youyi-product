"""One authoritative runtime view of a trusted WeCom staff identity.

This module deliberately contains no conversational interpretation.  It maps
an already verified channel userid to the current personnel lifecycle and
role stored in the Personnel & Service Governance aggregate.  In particular,
names, aliases and model arguments never select an identity or grant a role.

The aggregate remains opt-in until the explicit, audited migration marks its
``runtime_identity_authority`` mode as ``enforced``.  This lets an existing
institution keep its legacy directory untouched until its authoritative
records have been reviewed; once enforced, malformed or conflicting authority
data fails closed rather than silently falling back to the old whitelist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from .store import TuoguanStore, TuoguanStoreError


DATA_FILE = "personnel_service_governance_v1.json"
AUTHORITY_KEY = "runtime_identity_authority"
ACCESS_KEY = "identity_access"
AUTHORITY_MODE = "enforced"
ALLOWED_ROLES = frozenset({"boss", "manager", "teacher"})
PERSON_STATES = frozenset({"pending", "active", "suspended", "left"})


class IdentityAuthorityError(ValueError):
    """A fail-closed authority aggregate error."""


@dataclass(frozen=True)
class RuntimeIdentityResolution:
    """A server-owned resolution for one verified transport userid."""

    enforced: bool
    approval_state: str
    role: str
    display_name: str
    reason: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _authority(value: Any) -> dict[str, Any]:
    return _as_dict(_as_dict(value).get(AUTHORITY_KEY))


def authority_is_enforced(value: Any) -> bool:
    return str(_authority(value).get("mode") or "").strip().lower() == AUTHORITY_MODE


def _active_employments(doc: dict[str, Any], *, tenant_id: str, user_id: str) -> list[dict[str, Any]]:
    return [
        row for row in _as_rows(doc.get("employments"))
        if str(row.get("tenant_id") or "") == tenant_id
        and str(row.get("staff_user_id") or "") == user_id
        and str(row.get("state") or "") == "active"
        and not str(row.get("effective_until") or "")
    ]


def validate_runtime_identity_document(value: Any) -> None:
    """Validate the enforced aggregate before it becomes runtime authority.

    The validation is intentionally stricter than a directory lookup: an
    active person must have exactly one current employment and the institution
    must have exactly one active boss.  A broken aggregate is an authorization
    failure, not a reason to reactivate an old whitelist.
    """

    doc = _as_dict(value)
    if not authority_is_enforced(doc):
        return

    authority = _authority(doc)
    tenant_id = str(authority.get("tenant_id") or "").strip()
    if not tenant_id:
        raise IdentityAuthorityError("identity_authority_tenant_missing")

    active_bosses: list[str] = []
    seen_people: set[str] = set()
    for person in _as_rows(doc.get("people")):
        if str(person.get("tenant_id") or "") != tenant_id:
            continue
        user_id = str(person.get("staff_user_id") or "").strip()
        state = str(person.get("state") or "").strip().lower()
        if not user_id or state not in PERSON_STATES:
            raise IdentityAuthorityError("identity_authority_person_invalid")
        if user_id in seen_people:
            raise IdentityAuthorityError("identity_authority_duplicate_userid")
        seen_people.add(user_id)
        if state != "active":
            continue
        employments = _active_employments(doc, tenant_id=tenant_id, user_id=user_id)
        if len(employments) != 1:
            raise IdentityAuthorityError("identity_authority_active_employment_invalid")
        role = str(employments[0].get("role") or "").strip().lower()
        if role not in ALLOWED_ROLES:
            raise IdentityAuthorityError("identity_authority_role_invalid")
        if role == "boss":
            active_bosses.append(user_id)
    if len(active_bosses) != 1:
        raise IdentityAuthorityError("identity_authority_requires_exactly_one_active_boss")

    access = _as_dict(doc.get(ACCESS_KEY))
    pending = _as_dict(access.get("pending"))
    rejected = _as_dict(access.get("rejected"))
    if set(pending) & set(rejected):
        raise IdentityAuthorityError("identity_authority_access_state_conflict")
    if set(pending) & seen_people or set(rejected) & seen_people:
        raise IdentityAuthorityError("identity_authority_person_access_conflict")


def resolve_runtime_identity(
    store: TuoguanStore,
    *,
    tenant_id: str,
    user_id: str,
) -> RuntimeIdentityResolution | None:
    """Resolve an enforced authority record, or ``None`` before migration."""

    doc = _as_dict(store.read_json(DATA_FILE, {}))
    if not authority_is_enforced(doc):
        return None
    try:
        validate_runtime_identity_document(doc)
    except IdentityAuthorityError as exc:
        return RuntimeIdentityResolution(True, "authority_invalid", "unknown", "", str(exc))

    authority = _authority(doc)
    expected_tenant = str(authority.get("tenant_id") or "").strip()
    if str(tenant_id or "").strip() != expected_tenant:
        return RuntimeIdentityResolution(True, "authority_invalid", "unknown", "", "identity_authority_tenant_mismatch")

    canonical = str(user_id or "").strip()
    person = next(
        (
            row for row in _as_rows(doc.get("people"))
            if str(row.get("tenant_id") or "") == expected_tenant
            and str(row.get("staff_user_id") or "") == canonical
        ),
        None,
    )
    if isinstance(person, dict):
        state = str(person.get("state") or "").strip().lower()
        display = str(person.get("display_name") or "").strip()
        if state != "active":
            return RuntimeIdentityResolution(True, state, "unknown", display, "person_not_active")
        employment = _active_employments(doc, tenant_id=expected_tenant, user_id=canonical)[0]
        return RuntimeIdentityResolution(
            True,
            "approved",
            str(employment.get("role") or "").strip().lower(),
            display,
            "authoritative_personnel_record",
        )

    access = _as_dict(doc.get(ACCESS_KEY))
    if canonical in _as_dict(access.get("rejected")):
        return RuntimeIdentityResolution(True, "rejected", "unknown", "", "rejected_identity_access")
    if canonical in _as_dict(access.get("pending")):
        return RuntimeIdentityResolution(True, "pending", "unknown", "", "pending_identity_access")
    return RuntimeIdentityResolution(True, "pending", "unknown", "", "unrecognized_verified_userid")


def record_pending_runtime_identity(
    store: TuoguanStore,
    *,
    tenant_id: str,
    user_id: str,
    platform: str,
    user_name: str = "",
    chat_id: str = "",
) -> None:
    """Persist an unknown verified userid without mutating the legacy directory."""

    canonical = str(user_id or "").strip()
    if not canonical:
        return

    def update(raw: Any) -> dict[str, Any]:
        doc = _as_dict(raw)
        if not authority_is_enforced(doc):
            return doc
        validate_runtime_identity_document(doc)
        authority = _authority(doc)
        if str(authority.get("tenant_id") or "").strip() != str(tenant_id or "").strip():
            raise IdentityAuthorityError("identity_authority_tenant_mismatch")
        access = _as_dict(doc.get(ACCESS_KEY))
        pending = _as_dict(access.get("pending"))
        rejected = _as_dict(access.get("rejected"))
        if canonical in rejected:
            return doc
        now = _now()
        row = _as_dict(pending.get(canonical))
        pending[canonical] = {
            "platform": str(platform or "wecom"),
            "user_id": canonical,
            "display_hint": str(user_name or "").strip(),
            "chat_scope": str(chat_id or "").strip(),
            "first_seen_at": str(row.get("first_seen_at") or now),
            "last_seen_at": now,
        }
        access["pending"] = pending
        access["rejected"] = rejected
        doc[ACCESS_KEY] = access
        return doc

    try:
        store.update_json(DATA_FILE, {}, update)
    except TuoguanStoreError as exc:
        if isinstance(exc.__cause__, IdentityAuthorityError):
            raise exc.__cause__
        raise


def active_identity_snapshot(store: TuoguanStore, *, tenant_id: str) -> dict[str, RuntimeIdentityResolution] | None:
    """Return current authoritative identities for read-only directory views."""

    doc = _as_dict(store.read_json(DATA_FILE, {}))
    if not authority_is_enforced(doc):
        return None
    try:
        validate_runtime_identity_document(doc)
    except IdentityAuthorityError:
        return {}
    if str(_authority(doc).get("tenant_id") or "").strip() != str(tenant_id or "").strip():
        return {}
    result: dict[str, RuntimeIdentityResolution] = {}
    for person in _as_rows(doc.get("people")):
        if str(person.get("tenant_id") or "") != str(tenant_id or "").strip():
            continue
        user_id = str(person.get("staff_user_id") or "").strip()
        if user_id:
            resolved = resolve_runtime_identity(store, tenant_id=tenant_id, user_id=user_id)
            if resolved is not None:
                result[user_id] = resolved
    return result


def authority_owner_user_id(store: TuoguanStore, *, tenant_id: str) -> str | None:
    """Return the sole active boss only after enforced-record validation."""

    snapshot = active_identity_snapshot(store, tenant_id=tenant_id)
    if snapshot is None:
        return None
    owners = [user_id for user_id, identity in snapshot.items() if identity.approval_state == "approved" and identity.role == "boss"]
    return owners[0] if len(owners) == 1 else ""


def _legacy_role(whitelist: dict[str, Any], user_id: str) -> str:
    super_users = {str(value).strip() for value in whitelist.get("super_users") or [] if str(value).strip()}
    if user_id in super_users:
        return "boss"
    aliases = {"admin": "boss", "super_admin": "boss", "boss": "boss", "manager": "manager", "teacher": "teacher"}
    raw = str(_as_dict(whitelist.get("user_roles")).get(user_id) or "teacher").strip().lower()
    summer = {str(value).strip() for value in whitelist.get("summer_manager_ids") or [] if str(value).strip()}
    if user_id in summer and aliases.get(raw, raw) != "boss":
        return "manager"
    return aliases.get(raw, "")


def _legacy_state(profile: dict[str, Any]) -> str:
    raw = str(profile.get("employment_status") or profile.get("status") or "active").strip().lower()
    if raw in {"left", "offboarded", "terminated", "离职"}:
        return "left"
    if raw in {"suspended", "inactive", "停用", "暂停"}:
        return "suspended"
    return "active"


def build_legacy_identity_authority_bootstrap(
    *,
    tenant_id: str,
    whitelist: Any,
    staff: Any,
    existing_governance: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build, but do not write, a reviewed legacy-to-authority migration.

    The returned report is deliberately strict.  Any old approval/lifecycle
    conflict is a human-review blocker, never an automatic status guess.
    """

    tenant = str(tenant_id or "").strip()
    source = _as_dict(whitelist)
    profiles = _as_dict(staff)
    existing = _as_dict(existing_governance)
    blockers: list[str] = []
    if not tenant:
        blockers.append("tenant_id_required")
    if existing and (authority_is_enforced(existing) or any(_as_rows(existing.get(key)) for key in ("people", "employments", "students", "service_relations", "work_links", "handovers"))):
        blockers.append("existing_governance_requires_explicit_reconciliation")

    allowed = {str(value).strip() for value in source.get("allowed_users") or [] if str(value).strip()}
    super_users = {str(value).strip() for value in source.get("super_users") or [] if str(value).strip()}
    approved = sorted(allowed | super_users)
    people: list[dict[str, Any]] = []
    employments: list[dict[str, Any]] = []
    active_bosses: list[str] = []
    for user_id in approved:
        profile = _as_dict(profiles.get(user_id))
        role = _legacy_role(source, user_id)
        state = _legacy_state(profile)
        if role not in ALLOWED_ROLES:
            blockers.append("legacy_role_unrecognized:" + user_id)
            continue
        # A current legacy approval combined with a non-active staff state is
        # exactly the contradiction this migration must surface for a boss,
        # not silently resolve.
        if state != "active":
            blockers.append("legacy_approval_lifecycle_conflict:" + user_id)
            continue
        display_name = str(profile.get("business_name") or profile.get("name") or "").strip()
        people.append({
            "person_id": "person_migrated_" + sha256(user_id.encode("utf-8")).hexdigest()[:20],
            "tenant_id": tenant,
            "staff_user_id": user_id,
            "display_name": display_name,
            "state": "active",
            "created_at": "migration_pending_apply",
            "created_by": "legacy_identity_authority_migration",
        })
        employment = {
            "employment_id": "employment_migrated_" + sha256(user_id.encode("utf-8")).hexdigest()[:20],
            "tenant_id": tenant,
            "staff_user_id": user_id,
            "role": role,
            "campus_id": str(profile.get("campus_id") or "").strip(),
            "managed_campus_ids": [str(value) for value in profile.get("campus_ids") or [] if str(value)] if role == "manager" else [],
            "state": "active",
            "effective_from": "migration_pending_apply",
            "effective_until": "",
            "created_at": "migration_pending_apply",
            "created_by": "legacy_identity_authority_migration",
        }
        employments.append(employment)
        if role == "boss":
            active_bosses.append(user_id)
    if len(active_bosses) != 1:
        blockers.append("legacy_requires_exactly_one_active_boss")

    pending = {str(value).strip(): {"migrated_from": "legacy_pending"} for value in source.get("pending_users") or [] if str(value).strip()}
    rejected = {str(value).strip(): {"migrated_from": "legacy_rejected"} for value in source.get("rejected_users") or [] if str(value).strip()}
    if set(pending) & set(rejected):
        blockers.append("legacy_pending_rejected_overlap")
    if (set(pending) | set(rejected)) & set(approved):
        blockers.append("legacy_approved_access_state_overlap")

    document = {
        "schema_version": 3,
        AUTHORITY_KEY: {
            "mode": AUTHORITY_MODE,
            "tenant_id": tenant,
            "migration_source": "wecom_whitelist_and_staff_reviewed_snapshot",
            "activated_at": "migration_pending_apply",
        },
        ACCESS_KEY: {"pending": pending, "rejected": rejected},
        "people": people,
        "employments": employments,
        "students": [], "service_relations": [], "service_records": [], "attentions": [],
        "work_links": [], "handovers": [], "change_requests": [], "operations": {}, "audit": [],
    }
    if not blockers:
        try:
            validate_runtime_identity_document(document)
        except IdentityAuthorityError as exc:
            blockers.append(str(exc))
    report = {
        "tenant_id": tenant,
        "approved_identity_count": len(approved),
        "pending_identity_count": len(pending),
        "rejected_identity_count": len(rejected),
        "active_boss_count": len(active_bosses),
        "blockers": blockers,
        "safe_to_apply": not blockers,
    }
    return document, report
