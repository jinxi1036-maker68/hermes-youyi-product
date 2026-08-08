"""Repair missing active task context for existing manual assignments."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta
import json
from typing import Any

from .store import TuoguanStore
from .tenant_context import current_tenant_id
from .write_guard import authorized_system_write

ACTIVE_TASK_CONTEXT_FILE = "active_task_context.json"
PENDING_NEXT_TASK_CONTEXT_FILE = "pending_next_task_context.json"
TASKS_FILE = "tasks.json"
NOTIFICATION_OUTBOX_FILE = "notification_outbox.json"
CLOSED_STATUSES = {"completed", "cancelled", "closed", "done", "closed_by_admin", "completed_by_admin"}


def repair_missing_task_contexts(
    store: TuoguanStore | None = None,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    ttl_hours: int = 72,
) -> dict[str, Any]:
    actual_store = store or TuoguanStore()
    timestamp = now or datetime.now().astimezone()
    active = actual_store.read_json(ACTIVE_TASK_CONTEXT_FILE, {})
    pending = actual_store.read_json(PENDING_NEXT_TASK_CONTEXT_FILE, {})
    tasks = actual_store.read_json(TASKS_FILE, [])
    outbox = actual_store.read_json(NOTIFICATION_OUTBOX_FILE, [])
    if not isinstance(active, dict):
        active = {}
    if not isinstance(pending, dict):
        pending = {}
    if not isinstance(tasks, list):
        tasks = []
    if not isinstance(outbox, list):
        outbox = []

    repaired: list[dict[str, Any]] = []
    next_active = deepcopy(active)
    next_pending = deepcopy(pending)
    expires_at = (timestamp + timedelta(hours=max(1, int(ttl_hours or 72)))).isoformat(timespec="seconds")
    for task in tasks:
        if not isinstance(task, dict) or str(task.get("status") or "") in CLOSED_STATUSES:
            continue
        if str(task.get("type") or task.get("source_type") or "") != "manual_assignment":
            continue
        assignee = str(task.get("assignee_userid") or task.get("assignee_user_id") or "").strip()
        task_id = str(task.get("id") or "").strip()
        if not assignee or not task_id:
            continue
        assignee_role = _assignee_role(actual_store, assignee)
        if assignee_role not in {"teacher", "manager"}:
            continue
        existing = next_active.get(assignee) if isinstance(next_active.get(assignee), dict) else {}
        if str(existing.get("task_id") or "") == task_id:
            continue
        latest_outbox = _latest_task_outbox(outbox, task_id)
        context = _context_for_task(
            task,
            assignee=assignee,
            timestamp=timestamp,
            expires_at=expires_at,
            latest_outbox=latest_outbox,
        )
        next_active[assignee] = context["active"]
        next_pending[assignee] = context["pending"]
        repaired.append({
            "task_id": task_id,
            "assignee_userid": assignee,
            "task_title": str(task.get("title") or ""),
            "assignee_role": assignee_role,
            "latest_outbox_id": str(latest_outbox.get("id") or "") if latest_outbox else "",
        })

    if repaired and not dry_run:
        with authorized_system_write(
            actual_store.data_dir,
            job_name="repair_task_context_v1",
            allowed_files={ACTIVE_TASK_CONTEXT_FILE, PENDING_NEXT_TASK_CONTEXT_FILE},
        ):
            actual_store.write_json(ACTIVE_TASK_CONTEXT_FILE, next_active)
            actual_store.write_json(PENDING_NEXT_TASK_CONTEXT_FILE, next_pending)

    return {
        "ok": True,
        "tenant_id": current_tenant_id(),
        "dry_run": dry_run,
        "repaired_count": len(repaired),
        "repaired": repaired,
        "writeback_verified": _verify_contexts(actual_store, repaired) if repaired and not dry_run else False,
    }


def _context_for_task(
    task: dict[str, Any],
    *,
    assignee: str,
    timestamp: datetime,
    expires_at: str,
    latest_outbox: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    task_id = str(task.get("id") or "")
    title = str(task.get("title") or "")
    active = {
        "user_id": assignee,
        "task_id": task_id,
        "task_title": title,
        "student_id": str(task.get("student_id") or task.get("student_name") or ""),
        "student_name": str(task.get("student_name") or ""),
        "task_type": str(task.get("type") or "manual_assignment"),
        "status": "selected",
        "started_at": timestamp.isoformat(timespec="seconds"),
        "expires_at": expires_at,
        "candidate_task_ids": [],
        "source": "repair_task_context_v1",
        "owner_user_id": str(task.get("created_by") or task.get("assigned_by") or ""),
        "expected_report_at": str(task.get("due_at") or ""),
        "latest_outbox_id": str((latest_outbox or {}).get("id") or ""),
        "latest_outbox_status": str((latest_outbox or {}).get("status") or ""),
        "original_owner_text": title,
    }
    pending = {
        "user_id": assignee,
        "task_id": task_id,
        "task_title": title,
        "task_level": str(task.get("level") or "C"),
        "task_type": str(task.get("type") or "manual_assignment"),
        "student_id": active["student_id"],
        "student_name": active["student_name"],
        "source": "repair_task_context_v1",
        "trigger_words": ["继续", "开始", "处理", "完成", "汇报", "进展", "结果"],
        "created_at": timestamp.isoformat(timespec="seconds"),
        "expires_at": expires_at,
        "owner_user_id": active["owner_user_id"],
        "expected_report_at": active["expected_report_at"],
        "latest_outbox_id": active["latest_outbox_id"],
        "original_owner_text": title,
    }
    return {"active": active, "pending": pending}


def _latest_task_outbox(outbox: list[Any], task_id: str) -> dict[str, Any] | None:
    matches = [
        item for item in outbox
        if isinstance(item, dict)
        and str(item.get("task_id") or "") == task_id
        and str(item.get("action") or "") in {"task_created", "task_due", "manual_assignment"}
    ]
    if not matches:
        return None
    matches.sort(key=lambda item: str(item.get("last_attempt_at") or item.get("sent_at") or item.get("created_at") or ""))
    return matches[-1]


def _assignee_role(store: TuoguanStore, user_id: str) -> str:
    whitelist = store.read_json("wecom_whitelist.json", {})
    if isinstance(whitelist, dict):
        roles = whitelist.get("user_roles") if isinstance(whitelist.get("user_roles"), dict) else {}
        role = str(roles.get(user_id) or "")
        if role:
            return {"super_admin": "boss", "owner": "boss"}.get(role, role)
        if user_id in {str(item) for item in whitelist.get("super_users") or []}:
            return "boss"
        if user_id in {str(item) for item in whitelist.get("summer_manager_ids") or []}:
            return "manager"
    staff = store.read_json("staff.json", {})
    if isinstance(staff, dict):
        item = staff.get(user_id)
        if isinstance(item, dict):
            role = str(item.get("role") or "")
            return {"super_admin": "boss", "owner": "boss"}.get(role, role)
    return ""


def _verify_contexts(store: TuoguanStore, repaired: list[dict[str, Any]]) -> bool:
    active = store.read_json(ACTIVE_TASK_CONTEXT_FILE, {})
    pending = store.read_json(PENDING_NEXT_TASK_CONTEXT_FILE, {})
    if not isinstance(active, dict) or not isinstance(pending, dict):
        return False
    for item in repaired:
        assignee = str(item.get("assignee_userid") or "")
        task_id = str(item.get("task_id") or "")
        if str((active.get(assignee) or {}).get("task_id") or "") != task_id:
            return False
        if str((pending.get(assignee) or {}).get("task_id") or "") != task_id:
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repair missing manual-assignment task contexts.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    result = repair_missing_task_contexts(dry_run=bool(args.dry_run))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
