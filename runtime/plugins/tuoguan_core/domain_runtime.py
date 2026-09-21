"""Small domain-service façade with no deterministic message dispatch."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .identity import IdentityService
from .permissions import PermissionService
from .store import TuoguanStore


class DomainRuntime:
    """Domain state access used by Hooks and delivery adapters.

    It intentionally has no message-routing method or semantic dispatcher.
    Hermes' Agent Loop remains the
    only component that selects a business Tool.
    """

    def __init__(self, store: TuoguanStore | None = None) -> None:
        self.store = store or TuoguanStore()
        self.identities = IdentityService(self.store)
        self.permissions = PermissionService(self.store)

    def confirm_notifications_delivered(self, notifications: list[dict[str, Any]]) -> None:
        actions = {
            str(item.get("task_id") or ""): str(item.get("action") or "")
            for item in notifications
            if isinstance(item, dict) and item.get("task_id") and item.get("action")
        }
        if not actions:
            return
        tasks = self.store.load_tasks()
        stamp = datetime.now().isoformat(timespec="seconds")
        changed = False
        for task in tasks:
            task_id = str(task.get("id") or "")
            action = actions.get(task_id)
            if not action:
                continue
            task["last_escalation_action"] = action
            task["last_escalated_at"] = stamp
            task["escalation_count"] = int(task.get("escalation_count") or 0) + 1
            changed = True
        if changed:
            self.store.save_tasks(tasks)


_RUNTIME: DomainRuntime | None = None


def domain_runtime() -> DomainRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = DomainRuntime()
    return _RUNTIME


def reset_domain_runtime() -> None:
    global _RUNTIME
    _RUNTIME = None
