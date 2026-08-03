"""Shared identity helpers for autonomous system jobs."""

from __future__ import annotations

from .models import UserIdentity
from .store import TuoguanStore


def system_identity() -> UserIdentity:
    return UserIdentity(
        platform="system",
        platform_user_id="autonomous_employee_loop",
        canonical_user_id="autonomous_employee_loop",
        person_name="Hermes autonomous employee",
        role="boss",
        approval_state="approved",
    )


def owner_user_id(store: TuoguanStore) -> str:
    whitelist = store.read_json("wecom_whitelist.json", {})
    if isinstance(whitelist, dict):
        super_users = whitelist.get("super_users")
        if isinstance(super_users, list):
            for item in super_users:
                if str(item or "").strip():
                    return str(item).strip()
    mapping = store.read_json("teacher_wecom_map.json", {})
    if isinstance(mapping, dict):
        for name in ("金总", "老板", "JinWenJie"):
            if str(mapping.get(name) or "").strip():
                return str(mapping[name]).strip()
    return ""
