"""Canonical student resolution shared by summer-program capabilities."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .models import UserIdentity
from .programs import SUMMER_PROGRAM_ID, canonical_program_id
from .permissions import PermissionService
from .store import TuoguanStore
from .student_directory import find_student_candidates, safe_candidate_labels
from .tenant_context import current_tenant_id


ACTIVE_STATUSES = {"active", "phone_pending"}


def _same_tenant(item: dict[str, Any]) -> bool:
    tenant_id = current_tenant_id()
    return str(item.get("tenant_id") or tenant_id) == tenant_id


def _active_profile(profile: dict[str, Any]) -> bool:
    if str(profile.get("summer_status") or "") in ACTIVE_STATUSES:
        return True
    return any(
        isinstance(item, dict)
        and canonical_program_id(item.get("program_id")) == SUMMER_PROGRAM_ID
        and str(item.get("status") or "active") in ACTIVE_STATUSES
        for item in (profile.get("program_enrollments") or [])
    )


def active_summer_students(store: TuoguanStore) -> dict[str, dict[str, Any]]:
    """Merge profile and enrollment sources into one canonical active roster."""
    candidates: dict[str, list[dict[str, Any]]] = {}
    students = store.read_json("students.json", {})
    if isinstance(students, dict):
        for name, profile in students.items():
            if not isinstance(profile, dict) or not _same_tenant(profile) or not _active_profile(profile):
                continue
            clean_name = str(name or profile.get("student_name") or "").strip()
            if clean_name:
                row = deepcopy(profile)
                row["_resolver_source"] = "student_profile"
                candidates.setdefault(clean_name, []).append(row)

    enrollments = store.read_json("summer_enrollments.json", [])
    if isinstance(enrollments, list):
        for item in enrollments:
            if not isinstance(item, dict) or not _same_tenant(item):
                continue
            if canonical_program_id(item.get("program_id"), default=SUMMER_PROGRAM_ID) != SUMMER_PROGRAM_ID:
                continue
            if str(item.get("status") or "active") not in ACTIVE_STATUSES:
                continue
            clean_name = str(item.get("student_name") or "").strip()
            if clean_name:
                row = deepcopy(item)
                row["_resolver_source"] = "summer_enrollment"
                candidates.setdefault(clean_name, []).append(row)

    resolved: dict[str, dict[str, Any]] = {}
    for name, rows in candidates.items():
        student_ids = {str(row.get("student_id") or "").strip() for row in rows if str(row.get("student_id") or "").strip()}
        phones = {
            str(row.get("phone") or row.get("parent_phone") or "").strip()
            for row in rows
            if str(row.get("phone") or row.get("parent_phone") or "").strip()
        }
        enrollment_rows = [row for row in rows if row.get("_resolver_source") == "summer_enrollment"]
        enrollment_phones = {
            str(row.get("parent_phone") or row.get("phone") or "").strip()
            for row in enrollment_rows
            if str(row.get("parent_phone") or row.get("phone") or "").strip()
        }
        merged: dict[str, Any] = {}
        for row in rows:
            for key, value in row.items():
                if value not in (None, "", [], {}):
                    merged[key] = deepcopy(value)
        merged.update({
            "student_name": name,
            "student_id": next(iter(student_ids), str(merged.get("student_id") or "")),
            "program_id": SUMMER_PROGRAM_ID,
            "status": "active",
            "tenant_id": current_tenant_id(),
            "resolution_ambiguous": len(student_ids) > 1 or (len(enrollment_rows) > 1 and len(enrollment_phones) > 1),
            "compatibility_warning": ["phone_conflict_between_sources"] if len(phones) > 1 else [],
        })
        merged.pop("_resolver_source", None)
        resolved[name] = merged
    return resolved


def resolve_active_summer_student(
    store: TuoguanStore,
    identity: UserIdentity,
    requested_name: str,
) -> tuple[str | None, dict[str, Any]]:
    if identity.approval_state != "approved" or identity.role not in {"teacher", "boss"}:
        return None, {"reason_code": "permission_denied"}
    requested = str(requested_name or "").strip()
    roster = active_summer_students(store)
    if requested in roster:
        profile = roster[requested]
        if profile.get("resolution_ambiguous"):
            return None, {"reason_code": "student_name_ambiguous", "matches": [requested]}
        return requested, deepcopy(profile)
    if len(requested) == 2 and requested.startswith("小"):
        matches = sorted(name for name in roster if name.startswith(requested[1]))
        if len(matches) == 1 and not roster[matches[0]].get("resolution_ambiguous"):
            return matches[0], deepcopy(roster[matches[0]])
        if matches:
            return None, {"reason_code": "student_name_ambiguous", "matches": matches}
    return None, {"reason_code": "student_not_found"}


def all_student_names(store: TuoguanStore) -> list[str]:
    names = set(active_summer_students(store))
    students = store.read_json("students.json", {})
    if isinstance(students, dict):
        names.update(
            str(name)
            for name, profile in students.items()
            if isinstance(profile, dict) and _same_tenant(profile)
        )
    return sorted((name for name in names if name), key=len, reverse=True)


def resolve_student_for_record(
    store: TuoguanStore,
    identity: UserIdentity,
    requested_name: str,
    *,
    allow_student_id: bool = True,
) -> tuple[str | None, dict[str, Any]]:
    if identity.approval_state != "approved" or identity.role not in {"teacher", "manager", "boss"}:
        return None, {"reason_code": "permission_denied"}
    requested = str(requested_name or "").strip()
    directory_candidates = find_student_candidates(
        store, requested, allow_identifiers=allow_student_id,
    )
    if directory_candidates:
        permissions = PermissionService(store)
        authorised = [
            entry for entry in directory_candidates
            if permissions.can_write_student_record(identity, str(entry.get("student_id") or entry.get("profile_key") or ""))
        ]
        if len(directory_candidates) > 1:
            # Do not reveal candidates that the current actor cannot access,
            # and never choose by class/grade heuristics.
            if not authorised:
                return None, {"reason_code": "permission_denied"}
            return None, {
                "reason_code": "student_name_ambiguous",
                "candidates": safe_candidate_labels(authorised),
            }
        entry = directory_candidates[0]
        if not authorised:
            return None, {"reason_code": "permission_denied"}
        profile = deepcopy(entry.get("profile") or {})
        profile.update({
            "student_id": str(entry.get("student_id") or ""),
            "student_name": str(entry.get("student_name") or ""),
            "profile_key": str(entry.get("profile_key") or ""),
            "program_id": canonical_program_id(profile.get("program_id")),
            "tenant_id": current_tenant_id(),
        })
        return str(entry.get("student_name") or ""), profile
    summer = active_summer_students(store)
    if requested in summer:
        profile = summer[requested]
        if profile.get("resolution_ambiguous"):
            return None, {"reason_code": "student_name_ambiguous", "matches": [requested]}
        if identity.role in {"teacher", "boss"} or PermissionService(store).can_write_student_record(identity, requested):
            return requested, deepcopy(profile)
        return None, {"reason_code": "permission_denied"}

    students = store.read_json("students.json", {})
    profile = students.get(requested) if isinstance(students, dict) and allow_student_id else None
    if not isinstance(profile, dict):
        return None, {"reason_code": "student_not_found"}
    if not _same_tenant(profile):
        return None, {"reason_code": "cross_tenant_denied"}
    if not PermissionService(store).can_write_student_record(identity, requested):
        return None, {"reason_code": "permission_denied"}
    result = deepcopy(profile)
    result.update({
        "student_name": requested,
        "program_id": canonical_program_id(profile.get("program_id")),
        "tenant_id": current_tenant_id(),
    })
    return requested, result
