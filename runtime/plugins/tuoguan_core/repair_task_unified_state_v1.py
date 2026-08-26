"""Append-only repair for task contracts and derived task projections."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import uuid
from typing import Any

from .dashboard_builder import refresh_dashboard_cache
from .store import JSON_NO_CHANGE, TuoguanStore
from .tasks import build_task_contract, task_is_closed, task_is_open
from .write_guard import authorized_system_write


REPAIR_EVENTS_FILE = "task_unified_state_repair_events.jsonl"
_CONTRACT_KEYS = {
    "original_instruction", "responsible_actor", "completion_policy",
    "guidance_points", "coaching_mode", "closure_conditions",
}
_PROJECTION_FILES = (
    "active_task_context.json",
    "pending_next_task_context.json",
    "model_focus.json",
)


def _stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _contract_for(task: dict[str, Any]) -> dict[str, Any]:
    return build_task_contract(
        title=str(task.get("title") or task.get("task_name") or "未命名任务"),
        source_text=str(task.get("original_instruction") or task.get("source_text") or task.get("title") or ""),
        student_name=str(task.get("student_name") or ""),
        due_at=str(task.get("due_at") or ""),
        evidence_requirement=str(task.get("evidence_requirement") or ""),
        business_goal=str(task.get("business_goal") or task.get("title") or ""),
        assignee_user_id=str(task.get("assignee_userid") or ""),
        assignee_name=str(task.get("assignee_name") or ""),
        assignee_role=str(task.get("assignee_role") or "teacher"),
        assigned_by_user_id=str(task.get("created_by") or task.get("assigned_by") or ""),
        assigned_by_role=str(task.get("created_by_role") or task.get("assigned_by_role") or ""),
    )


def _needs_contract(task: dict[str, Any]) -> bool:
    contract = task.get("task_contract")
    return not isinstance(contract, dict) or not _CONTRACT_KEYS <= set(contract)


def _projection_without_stale(value: Any, tasks: dict[str, dict[str, Any]]) -> tuple[Any, list[str]]:
    if not isinstance(value, dict):
        return JSON_NO_CHANGE, []
    cleaned: dict[str, Any] = {}
    removed: list[str] = []
    for key, row in value.items():
        if not isinstance(row, dict):
            cleaned[key] = deepcopy(row)
            continue
        task_id = str(row.get("task_id") or row.get("id") or "")
        task = tasks.get(task_id)
        if task_id and (task is None or task_is_closed(task)):
            removed.append(task_id)
            continue
        cleaned[key] = deepcopy(row)
    return (cleaned if removed else JSON_NO_CHANGE), removed


def task_unified_state_repair(store: TuoguanStore, *, apply: bool = False) -> dict[str, Any]:
    tasks = [row for row in store.load_tasks() if isinstance(row, dict)]
    task_by_id = {str(row.get("id") or ""): row for row in tasks if str(row.get("id") or "")}
    contract_ids = [str(row.get("id") or "") for row in tasks if _needs_contract(row)]
    coach_ids = [
        str(row.get("id") or "")
        for row in tasks
        if task_is_closed(row) and str(row.get("coach_stage") or "") != "closed"
    ]
    stale_projection_ids: dict[str, list[str]] = {}
    for name in _PROJECTION_FILES:
        _updated, stale = _projection_without_stale(store.read_json(name, {}), task_by_id)
        stale_projection_ids[name] = stale
    students = store.read_json("students.json", {})
    students = students if isinstance(students, dict) else {}
    open_counts: dict[str, int] = {}
    for task in tasks:
        if task_is_open(task) and str(task.get("student_name") or ""):
            name = str(task.get("student_name") or "")
            open_counts[name] = int(open_counts.get(name) or 0) + 1
    stale_student_counts = [
        name for name, profile in students.items()
        if isinstance(profile, dict)
        and int((profile.get("business_signals") or {}).get("open_task_count") or 0) != int(open_counts.get(str(name)) or 0)
    ]
    result = {
        "ok": True,
        "dry_run": not apply,
        "task_contract_repairs": contract_ids,
        "terminal_coach_stage_repairs": coach_ids,
        "stale_projection_task_ids": stale_projection_ids,
        "student_open_count_repairs": stale_student_counts,
        "task_count": len(tasks),
        "open_task_count": sum(1 for task in tasks if task_is_open(task)),
        "writeback_verified": False,
        "boundary": {
            "history_deleted": False,
            "task_business_status_changed": False,
            "messages_sent": False,
            "new_task_created": False,
        },
    }
    if not apply:
        result["preview_verified"] = True
        return result

    stamp = _stamp()
    repair_id = f"task_unified_repair_{uuid.uuid4().hex}"
    files = {"tasks.json", "students.json", "dashboard_cache.json", REPAIR_EVENTS_FILE, *_PROJECTION_FILES}
    with authorized_system_write(
        store.data_dir,
        job_name="repair_task_unified_state_v1",
        allowed_files=files,
    ):
        def update_tasks(rows: list[dict[str, Any]]) -> None:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                changed: list[str] = []
                if _needs_contract(row):
                    row["task_contract"] = _contract_for(row)
                    changed.append("task_contract")
                if task_is_closed(row) and str(row.get("coach_stage") or "") != "closed":
                    row["coach_stage"] = "closed"
                    changed.append("coach_stage")
                if changed:
                    row["updated_at"] = stamp
                    events = row.get("closure_events") if isinstance(row.get("closure_events"), list) else []
                    events.append({
                        "at": stamp,
                        "by": "repair_task_unified_state_v1",
                        "action": "compatibility_projection_repaired",
                        "fields": changed,
                    })
                    row["closure_events"] = events[-50:]

        store.update_tasks(update_tasks)
        refreshed_tasks = {str(row.get("id") or ""): row for row in store.load_tasks() if isinstance(row, dict)}
        removed_by_file: dict[str, list[str]] = {}
        for name in _PROJECTION_FILES:
            def prune(value: Any, *, source=name) -> Any:
                updated, removed = _projection_without_stale(value, refreshed_tasks)
                removed_by_file[source] = removed
                return updated
            store.update_json(name, {}, prune)

        def update_students(rows: Any) -> Any:
            if not isinstance(rows, dict):
                return JSON_NO_CHANGE
            changed = False
            for name, profile in rows.items():
                if not isinstance(profile, dict):
                    continue
                signals = profile.get("business_signals") if isinstance(profile.get("business_signals"), dict) else {}
                expected = int(open_counts.get(str(name)) or 0)
                if int(signals.get("open_task_count") or 0) == expected and signals.get("open_task_count_source") == "tasks.json":
                    continue
                profile["business_signals"] = {
                    **signals,
                    "open_task_count": expected,
                    "open_task_count_source": "tasks.json",
                    "open_task_count_as_of": stamp,
                }
                changed = True
            return rows if changed else JSON_NO_CHANGE

        store.update_json("students.json", {}, update_students)
        dashboard = refresh_dashboard_cache(store)
        event = {
            "record_type": "task_unified_state_repair",
            "repair_id": repair_id,
            "created_at": stamp,
            "task_contract_repairs": contract_ids,
            "terminal_coach_stage_repairs": coach_ids,
            "stale_projection_task_ids": removed_by_file,
            "student_open_count_repairs": stale_student_counts,
            "dashboard_refreshed": bool(dashboard.get("ok", True) if isinstance(dashboard, dict) else True),
        }
        store.append_jsonl_verified(REPAIR_EVENTS_FILE, event)

    persisted = {str(row.get("id") or ""): row for row in store.load_tasks() if isinstance(row, dict)}
    cache = store.read_json("dashboard_cache.json", {})
    verified = bool(
        all(isinstance(persisted.get(task_id, {}).get("task_contract"), dict) for task_id in contract_ids)
        and all(str(persisted.get(task_id, {}).get("coach_stage") or "") == "closed" for task_id in coach_ids)
        and isinstance(cache, dict)
        and cache.get("generated_at")
    )
    result.update({"ok": verified, "writeback_verified": verified, "repair_id": repair_id, "dry_run": False})
    return result
