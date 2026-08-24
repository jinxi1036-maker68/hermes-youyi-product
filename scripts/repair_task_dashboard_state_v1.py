from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = REPO_ROOT / "runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from plugins.tuoguan_core.dashboard_builder import refresh_dashboard_cache  # noqa: E402
from plugins.tuoguan_core.digital_employee_state import (  # noqa: E402
    _fold_relationship_touch_candidates,
    update_relationship_touch_candidate_status,
)
from plugins.tuoguan_core.employee_identity import system_identity  # noqa: E402
from plugins.tuoguan_core.store import TuoguanStore  # noqa: E402
from plugins.tuoguan_core.write_guard import authorized_system_write  # noqa: E402


PRIMARY_RENEWAL_TASK_ID = "task_aed339156d82"
DUPLICATE_RENEWAL_TASK_ID = "task_330ac6392d73"
OWNER_CONTACT_TASK_ID = "task_e3cb89456b6a"
STALE_TOUCH_IDS = (
    "relationship_touch_642bde9626c5",
    "relationship_touch_5b8bdf808ea4",
)
TASK_CONTEXT_FILES = (
    "active_task_context.json",
    "pending_next_task_context.json",
    "model_focus.json",
)


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now().astimezone()


def _task_map(store: TuoguanStore) -> dict[str, dict[str, Any]]:
    return {
        str(task.get("id") or ""): task
        for task in store.load_tasks()
        if isinstance(task, dict) and str(task.get("id") or "")
    }


def build_plan(store: TuoguanStore) -> dict[str, Any]:
    tasks = _task_map(store)
    touches = _fold_relationship_touch_candidates(store)
    expected = {
        PRIMARY_RENEWAL_TASK_ID: "waiting_confirmation",
        DUPLICATE_RENEWAL_TASK_ID: "superseded",
        OWNER_CONTACT_TASK_ID: "pending",
    }
    task_targets: list[dict[str, Any]] = []
    mismatches: list[dict[str, str]] = []
    for task_id, expected_status in expected.items():
        task = tasks.get(task_id)
        actual = str((task or {}).get("status") or "")
        if task is None or actual != expected_status:
            mismatches.append({"task_id": task_id, "expected_status": expected_status, "actual_status": actual or "missing"})
        else:
            task_targets.append({
                "task_id": task_id,
                "title": str(task.get("title") or ""),
                "status_before": actual,
            })
    touch_targets: list[dict[str, Any]] = []
    for candidate_id in STALE_TOUCH_IDS:
        row = touches.get(candidate_id)
        actual = str((row or {}).get("status") or "")
        if row is None:
            mismatches.append({"candidate_id": candidate_id, "expected_status": "candidate_or_queued", "actual_status": "missing"})
        elif actual == "superseded":
            # A prior controlled repair already removed this old thread.  It
            # remains historical evidence but must not make this repair fail
            # or cause a second update event.
            continue
        elif actual not in {"candidate", "authorized", "queued", "sending", "sent", "retry_pending", "result_unknown"}:
            mismatches.append({"candidate_id": candidate_id, "expected_status": "candidate_or_queued", "actual_status": actual})
        else:
            touch_targets.append({
                "candidate_id": candidate_id,
                "target_user_id": str(row.get("target_user_id") or ""),
                "status_before": actual,
                "message": str(row.get("message") or "")[:160],
            })
    return {
        "ok": not mismatches,
        "task_targets": task_targets,
        "touch_targets": touch_targets,
        "mismatches": mismatches,
        "boundary": {
            "history_deleted": False,
            "messages_sent": False,
            "new_tasks_created": False,
            "permissions_changed": False,
        },
    }


def _append_closure_event(task: dict[str, Any], *, action: str, text: str, stamp: str) -> None:
    events = task.get("closure_events") if isinstance(task.get("closure_events"), list) else []
    events.append({
        "action": action,
        "actor_userid": "system_maintenance",
        "text": text,
        "at": stamp,
        "source": "repair_task_dashboard_state_v1",
    })
    task["closure_events"] = events[-50:]


def _close_task(task: dict[str, Any], *, action: str, text: str, stamp: str) -> None:
    previous = str(task.get("evidence_summary") or "").strip()
    evidence_lines = [line for line in previous.splitlines() if line.strip()]
    if text not in evidence_lines:
        evidence_lines.append(text)
    task["evidence_summary"] = "\n".join(evidence_lines)
    task["status"] = "completed"
    task["completed_at"] = stamp
    task["updated_at"] = stamp
    task["closure_summary"] = text
    _append_closure_event(task, action=action, text=text, stamp=stamp)


def _repair_tasks(store: TuoguanStore, *, stamp: str) -> list[dict[str, Any]]:
    changed: list[dict[str, Any]] = []

    def mutate(value: Any) -> list[dict[str, Any]]:
        tasks = value if isinstance(value, list) else []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("id") or "")
            if task_id == PRIMARY_RENEWAL_TASK_ID:
                text = "维护收口：家长沟通已完成，家长表示开学时再考虑；该沟通任务按老板确认闭环，是否续费保留为后续经营事实。"
                _close_task(task, action="admin_confirmed_completed", text=text, stamp=stamp)
                changed.append({"task_id": task_id, "status_after": "completed"})
            elif task_id == OWNER_CONTACT_TASK_ID:
                text = "维护收口：老板确认“下午4点联系金总”已完成。"
                _close_task(task, action="owner_confirmed_completed", text=text, stamp=stamp)
                changed.append({"task_id": task_id, "status_after": "completed"})
        return tasks

    store.update_json("tasks.json", [], mutate)
    current = _task_map(store)
    for row in changed:
        verified = str(current.get(row["task_id"], {}).get("status") or "") == "completed"
        row["writeback_verified"] = verified
    return changed


def _append_task_closure_ledger(store: TuoguanStore, *, changed: list[dict[str, Any]], stamp: str) -> bool:
    if not changed:
        return True

    def mutate(value: Any) -> list[dict[str, Any]]:
        rows = value if isinstance(value, list) else []
        existing = {(str(row.get("task_id") or ""), str(row.get("action") or ""), str(row.get("at") or "")) for row in rows if isinstance(row, dict)}
        for item in changed:
            key = (str(item["task_id"]), "admin_confirmed_completed", stamp)
            if key in existing:
                continue
            rows.append({
                "task_id": item["task_id"],
                "action": "admin_confirmed_completed",
                "by": "system_maintenance",
                "at": stamp,
                "text": "维护收口已完成，详见任务原始关闭证据。",
                "source": "repair_task_dashboard_state_v1",
            })
        return rows[-5000:]

    store.update_json("task_closure_events.json", [], mutate)
    rows = store.read_json("task_closure_events.json", [])
    return isinstance(rows, list) and all(
        any(isinstance(row, dict) and str(row.get("task_id") or "") == item["task_id"] and str(row.get("at") or "") == stamp for row in rows)
        for item in changed
    )


def _clear_task_contexts(store: TuoguanStore, *, task_ids: set[str]) -> dict[str, int]:
    cleared: dict[str, int] = {}
    for filename in TASK_CONTEXT_FILES:
        count = {"value": 0}

        def mutate(value: Any) -> Any:
            mapping = value if isinstance(value, dict) else {}
            for key, item in list(mapping.items()):
                if isinstance(item, dict) and str(item.get("task_id") or "") in task_ids:
                    del mapping[key]
                    count["value"] += 1
            return mapping

        store.update_json(filename, {}, mutate)
        cleared[filename] = count["value"]
    return cleared


def repair(data_dir: Path, *, apply: bool, now: datetime | None = None) -> dict[str, Any]:
    store = TuoguanStore(data_dir)
    plan = build_plan(store)
    if not apply or not plan["ok"]:
        return {"ok": bool(plan["ok"]), "dry_run": not apply, "data_dir": str(data_dir), "plan": plan, "writes": []}

    stamp = _now(now).isoformat(timespec="seconds")
    task_ids = {PRIMARY_RENEWAL_TASK_ID, DUPLICATE_RENEWAL_TASK_ID, OWNER_CONTACT_TASK_ID}
    writes: dict[str, Any] = {}
    with authorized_system_write(
        store.data_dir,
        job_name="repair_task_dashboard_state_v1",
        allowed_files={
            "tasks.json", "task_closure_events.json", *TASK_CONTEXT_FILES,
            "relationship_touch_candidates.jsonl", "dashboard_cache.json",
        },
    ):
        writes["tasks"] = _repair_tasks(store, stamp=stamp)
        writes["task_closure_ledger_verified"] = _append_task_closure_ledger(store, changed=writes["tasks"], stamp=stamp)
        writes["cleared_contexts"] = _clear_task_contexts(store, task_ids=task_ids)
        identity = system_identity()
        touch_updates = []
        for target in plan["touch_targets"]:
            result = update_relationship_touch_candidate_status(
                store,
                identity=identity,
                candidate_id=target["candidate_id"],
                status="superseded",
                operation_id=f"repair:task-dashboard:{stamp}:{target['candidate_id']}",
                failure_reason="过期或称呼错误的主动联系候选，不再进入当前上下文或发送链路。",
                source_text="repair_task_dashboard_state_v1.py",
            )
            touch_updates.append({
                "candidate_id": target["candidate_id"],
                "ok": bool(result.get("ok")),
                "writeback_verified": bool(result.get("writeback_verified")),
                "status_after": str((result.get("candidate") or {}).get("status") or ""),
            })
        writes["relationship_touches"] = touch_updates
        writes["dashboard"] = refresh_dashboard_cache(store, create_operation_tasks=False)

    tasks = _task_map(store)
    touches = _fold_relationship_touch_candidates(store)
    task_verified = all(str(tasks.get(task_id, {}).get("status") or "") in {"completed", "superseded"} for task_id in task_ids)
    touch_verified = all(str(touches.get(candidate_id, {}).get("status") or "") == "superseded" for candidate_id in STALE_TOUCH_IDS)
    context_verified = True
    for filename in TASK_CONTEXT_FILES:
        mapping = store.read_json(filename, {})
        if isinstance(mapping, dict) and any(isinstance(item, dict) and str(item.get("task_id") or "") in task_ids for item in mapping.values()):
            context_verified = False
    ok = bool(task_verified and touch_verified and context_verified and writes["task_closure_ledger_verified"])
    return {
        "ok": ok,
        "dry_run": False,
        "data_dir": str(data_dir),
        "plan": plan,
        "writes": writes,
        "writeback_verified": ok,
        "boundary": plan["boundary"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Append-only repair for verified stale task and dashboard state.")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--now", default="")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    reference = datetime.fromisoformat(args.now) if args.now else None
    result = repair(Path(args.data_dir), apply=bool(args.apply), now=reference)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
