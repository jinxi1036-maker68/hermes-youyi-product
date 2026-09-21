"""Backward-compatible, ID-aware access to the student repository.

``students.json`` has historically been keyed by display name.  That format
cannot express two different children with the same name.  This module keeps
all existing name-keyed fixtures valid while also accepting ID-keyed profiles
whose ``student_name`` is the human display name.  It does not choose a
student: callers must reject non-unique display-name matches.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .store import TuoguanStore
from .tenant_context import current_tenant_id


def _same_tenant(profile: dict[str, Any]) -> bool:
    tenant_id = current_tenant_id()
    return str(profile.get("tenant_id") or tenant_id) == tenant_id


def student_entries(store: TuoguanStore) -> list[dict[str, Any]]:
    """Return normalized, same-tenant entries without altering repository data."""

    students = store.read_json("students.json", {})
    if not isinstance(students, dict):
        return []
    entries: list[dict[str, Any]] = []
    for raw_key, raw_profile in students.items():
        if not isinstance(raw_profile, dict) or not _same_tenant(raw_profile):
            continue
        profile_key = str(raw_key or "").strip()
        student_id = str(raw_profile.get("student_id") or profile_key).strip()
        student_name = str(raw_profile.get("student_name") or profile_key).strip()
        if not profile_key or not student_id or not student_name:
            continue
        profile = deepcopy(raw_profile)
        profile.update({
            "student_id": student_id,
            "student_name": student_name,
            "tenant_id": current_tenant_id(),
        })
        entries.append({
            "profile_key": profile_key,
            "student_id": student_id,
            "student_name": student_name,
            "profile": profile,
        })
    return entries


def find_student_candidates(
    store: TuoguanStore,
    reference: str,
    *,
    allow_identifiers: bool = True,
) -> list[dict[str, Any]]:
    """Find exact display candidates, optionally accepting canonical IDs.

    A write Tool has separate ``student_name`` and ``student_id`` fields.  It
    must not silently treat an identifier placed in the human-name field as a
    user-confirmed same-name selection.
    """

    requested = str(reference or "").strip()
    if not requested:
        return []
    matches: list[dict[str, Any]] = []
    for entry in student_entries(store):
        aliases = entry["profile"].get("aliases") or []
        terms = {str(entry["student_name"]), *(str(item).strip() for item in aliases if str(item).strip())}
        if allow_identifiers:
            terms.update({str(entry["profile_key"]), str(entry["student_id"])})
        if requested in terms:
            matches.append(deepcopy(entry))
    return matches


def safe_candidate_labels(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return only the attributes needed for an authorised disambiguation."""

    labels: list[dict[str, str]] = []
    for entry in entries:
        profile = entry.get("profile") if isinstance(entry.get("profile"), dict) else {}
        label = {
            "student_id": str(entry.get("student_id") or ""),
            "student_name": str(entry.get("student_name") or ""),
        }
        for key in ("grade", "class_name", "class", "campus_id"):
            value = str(profile.get(key) or "").strip()
            if value:
                label[key] = value
        labels.append(label)
    return labels


def filter_candidates_by_classroom(
    entries: list[dict[str, Any]],
    *,
    class_name: str = "",
    grade: str = "",
) -> list[dict[str, Any]]:
    """Filter an already-authorised candidate set by explicit classroom facts.

    This is an exact repository filter, never a selection heuristic.  Callers
    must still reject zero or multiple matches.  ``grade`` intentionally
    accepts a grade embedded in a class label (for example ``三年级`` in
    ``三年级1班``), while ``class_name`` requires an exact class/classroom
    match after whitespace normalization.
    """

    requested_class = _compact(class_name)
    requested_grade = _compact(grade)
    if not requested_class and not requested_grade:
        return list(entries)
    filtered: list[dict[str, Any]] = []
    for entry in entries:
        profile = entry.get("profile") if isinstance(entry.get("profile"), dict) else {}
        class_values = {_compact(profile.get(key)) for key in ("class_name", "class")}
        grade_values = {_compact(profile.get("grade"))}
        class_values.discard("")
        grade_values.discard("")
        class_matches = not requested_class or requested_class in class_values
        grade_matches = not requested_grade or any(
            value == requested_grade or requested_grade in value
            for value in (*class_values, *grade_values)
        )
        if class_matches and grade_matches:
            filtered.append(deepcopy(entry))
    return filtered


def _compact(value: Any) -> str:
    return "".join(str(value or "").split())
