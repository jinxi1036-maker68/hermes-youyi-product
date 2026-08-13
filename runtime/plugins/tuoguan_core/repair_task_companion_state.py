"""Repair one proven duplicate task pair without deleting history."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from .store import JSON_NO_CHANGE, TuoguanStore
from .tasks import build_task_contract, closure_missing_fields
from .write_guard import authorized_system_write


_CLOSED = {"completed", "cancelled", "closed", "done", "closed_by_admin", "completed_by_admin", "superseded", "expired"}


def repair_duplicate_task_pair(
    store: TuoguanStore,
    *,
    primary_task_id: str,
    duplicate_task_id: str,
    evidence_text: str,
    apply: bool = False,
) -> dict[str, Any]:
    tasks = [item for item in store.load_tasks() if isinstance(item, dict)]
    primary = next((item for item in tasks if str(item.get("id") or "") == str(primary_task_id or "")), None)
    duplicate = next((item for item in tasks if str(item.get("id") or "") == str(duplicate_task_id or "")), None)
    evidence = str(evidence_text or "").strip()
    errors: list[str] = []
    if not primary or not duplicate or primary is duplicate:
        errors.append("task_pair_not_found")
    if primary and duplicate:
        if str(primary.get("student_name") or "") != str(duplicate.get("student_name") or ""):
            errors.append("student_mismatch")
        if str(primary.get("assignee_userid") or "") != str(duplicate.get("assignee_userid") or ""):
            errors.append("assignee_mismatch")
        if str(primary.get("status") or "") in _CLOSED:
            errors.append("primary_task_not_open")
        if str(duplicate.get("status") or "") == "superseded":
            errors.append("duplicate_already_superseded")
    if not evidence:
        errors.append("trusted_evidence_required")
    if errors:
        return {
            "ok": False,
            "dry_run": not apply,
            "errors": errors,
            "primary_task_id": str(primary_task_id or ""),
            "duplicate_task_id": str(duplicate_task_id or ""),
            "writeback_verified": False,
        }

    preview = deepcopy(primary)
    preview["task_contract"] = build_task_contract(
        title=str(preview.get("title") or ""),
        source_text=str(preview.get("source_text") or preview.get("title") or ""),
        student_name=str(preview.get("student_name") or ""),
        due_at=str(preview.get("due_at") or ""),
        evidence_requirement=str(preview.get("evidence_requirement") or ""),
    )
    previous = [line.strip() for line in str(preview.get("evidence_summary") or "").splitlines() if line.strip()]
    if evidence not in previous:
        previous.append(evidence)
    preview["evidence_summary"] = "\n".join(previous)
    missing = closure_missing_fields(preview, preview["evidence_summary"])
    preview["status"] = "waiting_confirmation" if missing else "completed"
    preview["coach_stage"] = "waiting_closure_evidence" if missing else "closed"
    result = {
        "ok": True,
        "dry_run": not apply,
        "primary_task_id": str(primary_task_id),
        "duplicate_task_id": str(duplicate_task_id),
        "student_name": str(primary.get("student_name") or ""),
        "assignee_userid": str(primary.get("assignee_userid") or ""),
        "primary_status_before": str(primary.get("status") or ""),
        "primary_status_after": str(preview.get("status") or ""),
        "duplicate_status_before": str(duplicate.get("status") or ""),
        "duplicate_status_after": "superseded",
        "missing_fields": missing,
        "writeback_verified": False,
        "preview_verified": not apply,
        "boundary": {"history_deleted": False, "messages_sent": False, "new_task_created": False},
    }
    if not apply:
        return result

    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    with authorized_system_write(
        store.data_dir,
        job_name="repair_task_companion_state",
        allowed_files={"tasks.json", "notification_outbox.json"},
    ):
        def update_tasks(rows: list[dict[str, Any]]) -> None:
            for item in rows:
                task_id = str(item.get("id") or "")
                if task_id == str(primary_task_id):
                    item.update(deepcopy(preview))
                    item["updated_at"] = stamp
                    events = item.get("closure_events") if isinstance(item.get("closure_events"), list) else []
                    events.append({
                        "at": stamp,
                        "by": "repair_task_companion_state",
                        "action": "trusted_evidence_merged",
                        "text": evidence,
                        "missing_fields": missing,
                        "source_task_id": str(duplicate_task_id),
                    })
                    item["closure_events"] = events[-50:]
                elif task_id == str(duplicate_task_id):
                    item["status_before_superseded"] = str(item.get("status") or "")
                    item["status"] = "superseded"
                    item["coach_stage"] = "closed"
                    item["superseded_at"] = stamp
                    item["superseded_by_task_id"] = str(primary_task_id)
                    item["superseded_reason"] = "任务结果误建了重复任务；证据已回收到原始老板任务。"
                    item["updated_at"] = stamp

        store.update_tasks(update_tasks)

        def suppress_duplicate(rows: Any) -> Any:
            rows = rows if isinstance(rows, list) else []
            changed = False
            for item in rows:
                if not isinstance(item, dict) or str(item.get("task_id") or "") != str(duplicate_task_id):
                    continue
                if str(item.get("status") or "") not in {"pending", "retry_pending", "sending"}:
                    continue
                item["status"] = "superseded"
                item["suppressed_reason"] = "duplicate_task_superseded"
                item["updated_at"] = stamp
                changed = True
            return rows if changed else JSON_NO_CHANGE

        store.update_json("notification_outbox.json", [], suppress_duplicate)

    saved = {str(item.get("id") or ""): item for item in store.load_tasks() if isinstance(item, dict)}
    primary_after = saved.get(str(primary_task_id), {})
    duplicate_after = saved.get(str(duplicate_task_id), {})
    verified = bool(
        str(primary_after.get("status") or "") == str(preview.get("status") or "")
        and evidence in str(primary_after.get("evidence_summary") or "")
        and str(duplicate_after.get("status") or "") == "superseded"
        and str(duplicate_after.get("superseded_by_task_id") or "") == str(primary_task_id)
    )
    result["writeback_verified"] = verified
    result["ok"] = verified
    return result
