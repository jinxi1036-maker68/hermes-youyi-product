"""Shared identity helpers for autonomous system jobs."""

from __future__ import annotations

from .models import UserIdentity
from .store import TuoguanStore


def system_identity() -> UserIdentity:
    return UserIdentity(
        platform="system",
        platform_user_id="autonomous_employee_loop",
        canonical_user_id="autonomous_employee_loop",
        person_name="小优",
        role="boss",
        approval_state="approved",
    )


def owner_user_id(store: TuoguanStore) -> str:
    """Resolve the current owner from server-owned identity facts.

    Human display names and historical aliases are deliberately not authority.
    The primary source is the trusted WeCom super-user list.  A legacy
    directory-only workspace can still resolve an owner when its canonical
    staff record or explicit trusted role says ``boss``; an arbitrary alias
    map on its own can never grant owner authority.
    """

    whitelist = store.read_json("wecom_whitelist.json", {})
    if isinstance(whitelist, dict):
        super_users = whitelist.get("super_users")
        if isinstance(super_users, list):
            for item in super_users:
                if str(item or "").strip():
                    return str(item).strip()
    roles = whitelist.get("user_roles") if isinstance(whitelist, dict) else {}
    roles = roles if isinstance(roles, dict) else {}
    staff = store.read_json("staff.json", {})
    if isinstance(staff, dict):
        for user_id, profile in staff.items():
            if not isinstance(profile, dict):
                continue
            canonical_user_id = str(user_id or "").strip()
            if not canonical_user_id:
                continue
            role = str(profile.get("role") or roles.get(canonical_user_id) or "").strip()
            status = str(profile.get("status") or "active").strip().lower()
            if role in {"boss", "owner", "super_admin"} and status in {"", "active", "approved"}:
                return canonical_user_id
    for user_id, role in roles.items():
        if str(role or "").strip() in {"boss", "owner", "super_admin"} and str(user_id or "").strip():
            return str(user_id).strip()
    return ""


def owner_display_names(store: TuoguanStore) -> tuple[str, ...]:
    """Return display aliases tied to the already-trusted owner identity.

    These labels are only used to prevent a model from greeting a non-owner
    as the owner.  They cannot grant authority: resolution first establishes
    the canonical owner id, then accepts only directory/staff aliases already
    mapped to that exact id.
    """

    owner_id = owner_user_id(store)
    if not owner_id:
        return ("老板", "机构负责人")
    labels: list[str] = ["老板", "机构负责人"]
    staff = store.read_json("staff.json", {})
    profile = staff.get(owner_id) if isinstance(staff, dict) else {}
    if isinstance(profile, dict):
        labels.extend(str(profile.get(key) or "").strip() for key in ("business_name", "name"))
    mapping = store.read_json("teacher_wecom_map.json", {})
    if isinstance(mapping, dict):
        labels.extend(str(name or "").strip() for name, user_id in mapping.items() if str(user_id or "").strip() == owner_id)
    result: list[str] = []
    for label in labels:
        if label and label not in result:
            result.append(label)
    return tuple(result)
