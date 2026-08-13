"""Verified, non-performance evidence about teacher task coaching."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from typing import Any

from .store import TuoguanStore
from .tenant_context import current_tenant_id


TEACHER_COACHING_EVENTS_FILE = "teacher_coaching_events.jsonl"
_RECORDED_ACTIONS = {
    "started",
    "helped",
    "fact_added",
    "needs_closure_evidence",
    "closure_ready",
    "completed",
}


def _event_id(*, task_id: str, operation_id: str, action: str) -> str:
    payload = f"{current_tenant_id()}:{task_id}:{operation_id}:{action}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"teacher_coaching_{digest}"


def _read_rows(store: TuoguanStore) -> list[dict[str, Any]]:
    path = store.path_for(TEACHER_COACHING_EVENTS_FILE)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _support_level(action: str) -> str:
    return {
        "started": "orientation",
        "helped": "guided_support",
        "fact_added": "evidence_review",
        "needs_closure_evidence": "evidence_gap_support",
        "closure_ready": "closure_review",
        "completed": "verified_completion",
    }.get(action, "task_support")


def record_teacher_coaching_event(
    store: TuoguanStore,
    *,
    task: dict[str, Any],
    teacher_user_id: str,
    action: str,
    operation_id: str,
    missing_fields: list[str] | None = None,
) -> dict[str, Any]:
    """Record verified support evidence without creating a performance signal."""

    normalized_action = str(action or "").strip()
    task_id = str(task.get("id") or "").strip()
    teacher_id = str(teacher_user_id or "").strip()
    if normalized_action not in _RECORDED_ACTIONS or not task_id or not teacher_id:
        return {
            "ok": True,
            "recorded": False,
            "reason_code": "coaching_event_not_applicable",
            "writeback_verified": True,
        }
    if str(task.get("assignee_userid") or "") != teacher_id:
        return {
            "ok": False,
            "recorded": False,
            "reason_code": "teacher_task_mismatch",
            "writeback_verified": False,
        }
    contract = task.get("task_contract") if isinstance(task.get("task_contract"), dict) else {}
    event_id = _event_id(task_id=task_id, operation_id=operation_id, action=normalized_action)
    existing = next(
        (row for row in _read_rows(store) if str(row.get("event_id") or "") == event_id),
        None,
    )
    if existing is not None:
        return {
            "ok": True,
            "recorded": True,
            "event": deepcopy(existing),
            "already_applied": True,
            "writeback_verified": True,
        }

    mastered_points: list[str] = []
    if normalized_action == "completed":
        mastered_points.append("submitted_verified_task_evidence")
        if str(contract.get("task_domain") or "") in {"parent_communication", "renewal_conversation"}:
            mastered_points.append("completed_parent_communication_evidence_loop")
    row = {
        "record_type": "teacher_coaching_event",
        "event_id": event_id,
        "tenant_id": current_tenant_id(),
        "task_id": task_id,
        "task_type": str(task.get("type") or ""),
        "task_domain": str(contract.get("task_domain") or "general_internal_task"),
        "teacher_user_id": teacher_id,
        "action": normalized_action,
        "support_level": _support_level(normalized_action),
        "missing_fields": [str(value) for value in missing_fields or [] if str(value)],
        "mastered_points": mastered_points,
        "next_support_suggestion": (
            "reduce_repeated_guidance_and_verify_result"
            if normalized_action == "completed"
            else "continue_from_current_task_stage"
        ),
        "source": "verified_task_update",
        "operation_id": str(operation_id or ""),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "performance_boundary": {
            "used_for_payroll": False,
            "used_for_performance": False,
            "used_for_penalty": False,
            "purpose": "adapt_future_task_coaching_only",
        },
    }
    verified = store.append_jsonl_verified(TEACHER_COACHING_EVENTS_FILE, row)
    reread = next(
        (item for item in _read_rows(store) if str(item.get("event_id") or "") == event_id),
        None,
    )
    verified = bool(verified and isinstance(reread, dict))
    return {
        "ok": verified,
        "recorded": verified,
        "event": deepcopy(reread or row),
        "already_applied": False,
        "writeback_verified": verified,
    }


def query_teacher_coaching_context(
    store: TuoguanStore,
    *,
    teacher_user_id: str,
    task_domain: str = "",
    limit: int = 6,
) -> dict[str, Any]:
    """Return same-person coaching evidence; never return scores or rankings."""

    teacher_id = str(teacher_user_id or "").strip()
    rows = [
        deepcopy(row)
        for row in _read_rows(store)
        if str(row.get("teacher_user_id") or "") == teacher_id
        and (not task_domain or str(row.get("task_domain") or "") == str(task_domain))
    ]
    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    rows = rows[: max(1, min(int(limit or 6), 20))]
    mastered: list[str] = []
    for row in rows:
        for value in row.get("mastered_points") or []:
            item = str(value or "")
            if item and item not in mastered:
                mastered.append(item)
    return {
        "ok": True,
        "teacher_user_id": teacher_id,
        "task_domain": str(task_domain or ""),
        "recent_events": rows,
        "mastered_points": mastered,
        "event_count": len(rows),
        "read_only": True,
        "performance_use_allowed": False,
    }
