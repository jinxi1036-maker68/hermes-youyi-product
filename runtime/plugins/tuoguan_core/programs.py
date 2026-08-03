"""Program scope and isolation helpers for tutoring operations."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .models import UserIdentity
from .store import TuoguanStore


REGULAR_PROGRAM_ID = "regular_tuoguan"
SUMMER_PROGRAM_ID = "summer_2026"
LEGACY_SUMMER_PROGRAM_IDS = {"2026_summer"}
PROGRAMS_FILE = "programs.json"

_DEFAULT_PROGRAMS = {
    REGULAR_PROGRAM_ID: {
        "id": REGULAR_PROGRAM_ID,
        "name": "托管班",
        "status": "active",
        "is_active_default": True,
    },
    SUMMER_PROGRAM_ID: {
        "id": SUMMER_PROGRAM_ID,
        "name": "2026暑假班",
        "status": "configured",
        "is_active_default": False,
    },
}


def canonical_program_id(value: Any, *, default: str = REGULAR_PROGRAM_ID) -> str:
    program_id = str(value or "").strip()
    if program_id in LEGACY_SUMMER_PROGRAM_IDS:
        return SUMMER_PROGRAM_ID
    return program_id or default


def load_programs(store: TuoguanStore) -> dict[str, dict[str, Any]]:
    raw = store.read_json(PROGRAMS_FILE, {})
    programs = deepcopy(_DEFAULT_PROGRAMS)
    if isinstance(raw, dict):
        source = raw.get("programs") if isinstance(raw.get("programs"), dict) else raw
        for key, value in source.items():
            if not isinstance(value, dict):
                continue
            program_id = canonical_program_id(value.get("id") or key)
            current = programs.setdefault(program_id, {"id": program_id})
            current.update(deepcopy(value))
            current["id"] = program_id
    return programs


def active_default_program(store: TuoguanStore) -> str:
    programs = load_programs(store)
    for program_id, item in programs.items():
        if bool(item.get("is_active_default")) and str(item.get("status") or "") == "active":
            return program_id
    return REGULAR_PROGRAM_ID


def program_status(store: TuoguanStore, program_id: str) -> str:
    item = load_programs(store).get(canonical_program_id(program_id), {})
    return str(item.get("status") or "configured")


def item_program_id(item: dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return REGULAR_PROGRAM_ID
    source_meta = item.get("source_meta") if isinstance(item.get("source_meta"), dict) else {}
    return canonical_program_id(
        item.get("program_id")
        or source_meta.get("program_id")
        or (SUMMER_PROGRAM_ID if str(item.get("summer_status") or "") in {"active", "phone_pending"} else "")
    )


def student_program_ids(profile: dict[str, Any] | None) -> set[str]:
    if not isinstance(profile, dict):
        return set()
    ids: set[str] = set()
    for item in profile.get("program_enrollments") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "active") not in {"active", "phone_pending", "pending"}:
            continue
        ids.add(canonical_program_id(item.get("program_id")))
    if str(profile.get("summer_status") or "") in {"active", "phone_pending"}:
        ids.add(SUMMER_PROGRAM_ID)
    if not ids:
        ids.add(canonical_program_id(profile.get("program_id")))
    return ids


def user_program_ids(store: TuoguanStore, identity: UserIdentity) -> set[str] | None:
    if identity.role == "boss":
        return None
    whitelist = store.read_json("wecom_whitelist.json", {})
    whitelist = whitelist if isinstance(whitelist, dict) else {}
    if identity.canonical_user_id in {str(item) for item in whitelist.get("summer_manager_ids") or []}:
        return {SUMMER_PROGRAM_ID}
    staff = store.read_json("staff.json", {})
    profile = staff.get(identity.canonical_user_id, {}) if isinstance(staff, dict) else {}
    profile = profile if isinstance(profile, dict) else {}
    configured = {
        canonical_program_id(item)
        for item in (profile.get("program_ids") or [])
        if str(item).strip()
    }
    if configured:
        return configured
    campuses = {str(item) for item in profile.get("campus_ids") or []}
    if SUMMER_PROGRAM_ID in campuses or "2026_summer" in campuses:
        return {SUMMER_PROGRAM_ID}
    return {REGULAR_PROGRAM_ID}


def is_summer_operator(store: TuoguanStore, identity: UserIdentity) -> bool:
    scope = user_program_ids(store, identity)
    return scope == {SUMMER_PROGRAM_ID}


def can_access_program(store: TuoguanStore, identity: UserIdentity, program_id: str) -> bool:
    scope = user_program_ids(store, identity)
    return scope is None or canonical_program_id(program_id) in scope


def filter_program_items(items: list[dict[str, Any]], program_ids: set[str] | None) -> list[dict[str, Any]]:
    if program_ids is None:
        return [item for item in items if isinstance(item, dict)]
    normalized = {canonical_program_id(item) for item in program_ids}
    return [item for item in items if isinstance(item, dict) and item_program_id(item) in normalized]


def filter_program_students(
    students: dict[str, dict[str, Any]],
    program_ids: set[str] | None,
) -> dict[str, dict[str, Any]]:
    if program_ids is None:
        return dict(students)
    normalized = {canonical_program_id(item) for item in program_ids}
    return {
        name: profile
        for name, profile in students.items()
        if student_program_ids(profile) & normalized
    }


def explicit_program_from_text(text: str) -> str:
    compact = str(text or "").replace(" ", "")
    if any(word in compact for word in ("暑假班", "暑期班", "summer_2026")):
        return SUMMER_PROGRAM_ID
    if any(word in compact for word in ("托管班", "常规托管", "regular_tuoguan")):
        return REGULAR_PROGRAM_ID
    return ""


def resolve_record_program(
    store: TuoguanStore,
    identity: UserIdentity,
    text: str,
) -> tuple[str, str]:
    """Return (program_id, reason). Empty id means clarification is required."""

    explicit = explicit_program_from_text(text)
    if explicit:
        if can_access_program(store, identity, explicit):
            return explicit, "explicit"
        return "", "forbidden"
    scope = user_program_ids(store, identity)
    if scope is None:
        return active_default_program(store), "boss_default"
    active = [item for item in scope if program_status(store, item) == "active"]
    if len(active) == 1:
        return active[0], "single_active_scope"
    if scope == {SUMMER_PROGRAM_ID}:
        return SUMMER_PROGRAM_ID, "summer_scope"
    if REGULAR_PROGRAM_ID in scope and program_status(store, REGULAR_PROGRAM_ID) == "frozen_readonly":
        return "", "regular_frozen_requires_program"
    if len(scope) == 1:
        return next(iter(scope)), "single_scope"
    return "", "ambiguous"


def program_label(program_id: str) -> str:
    return "2026暑假班" if canonical_program_id(program_id) == SUMMER_PROGRAM_ID else "托管班"

