from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = REPO_ROOT / "runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from plugins.tuoguan_core.digital_employee_state import (  # noqa: E402
    ATTENTION_THREADS_FILE,
    AUTONOMOUS_WORK_ITEM_FRESHNESS_HOURS,
    HERMES_WORK_ITEMS_FILE,
    query_attention_threads,
    query_hermes_work_items,
    update_attention_thread,
    update_hermes_work_item,
)
from plugins.tuoguan_core.employee_identity import system_identity  # noqa: E402
from plugins.tuoguan_core.store import TuoguanStore  # noqa: E402
from plugins.tuoguan_core.write_guard import authorized_system_write  # noqa: E402


def _parse_time(value: Any, *, reference: datetime) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None and reference.tzinfo is not None:
        parsed = parsed.replace(tzinfo=reference.tzinfo)
    if parsed.tzinfo is not None and reference.tzinfo is None:
        parsed = parsed.replace(tzinfo=None)
    return parsed


def _is_stale(row: dict[str, Any], *, cutoff: datetime) -> bool:
    updated_at = _parse_time(row.get("updated_at") or row.get("created_at"), reference=cutoff)
    return updated_at is not None and updated_at < cutoff


def _stale_waiting_work_item(row: dict[str, Any], *, cutoff: datetime) -> bool:
    if not _is_stale(row, cutoff=cutoff):
        return False
    waiting = row.get("current_waiting")
    if isinstance(waiting, dict) and waiting:
        return True
    if str(row.get("status") or "") in {"waiting", "blocked"}:
        return True
    next_attention = _parse_time(row.get("next_attention_at"), reference=cutoff)
    return next_attention is not None and next_attention < cutoff


def build_plan(
    store: TuoguanStore,
    *,
    now: datetime | None = None,
    freshness_hours: int = AUTONOMOUS_WORK_ITEM_FRESHNESS_HOURS,
) -> dict[str, Any]:
    reference = now or datetime.now().astimezone()
    cutoff = reference - timedelta(hours=max(1, int(freshness_hours)))
    identity = system_identity()
    attention = query_attention_threads(store, identity=identity, include_closed=False, limit=100)
    work = query_hermes_work_items(store, identity=identity, include_closed=False, limit=200)
    stale_attention = [
        row for row in attention.get("attention_threads") or []
        if isinstance(row, dict) and _is_stale(row, cutoff=cutoff)
    ]
    stale_work = [
        row for row in work.get("items") or []
        if isinstance(row, dict) and _stale_waiting_work_item(row, cutoff=cutoff)
    ]
    return {
        "reference_time": reference.isoformat(timespec="seconds"),
        "cutoff_time": cutoff.isoformat(timespec="seconds"),
        "freshness_hours": max(1, int(freshness_hours)),
        "attention_targets": [
            {
                "attention_id": str(row.get("attention_id") or ""),
                "focus_key": str(row.get("focus_key") or ""),
                "status_before": str(row.get("status") or ""),
                "updated_at": str(row.get("updated_at") or row.get("created_at") or ""),
                "question_text": str(row.get("question_text") or "")[:240],
            }
            for row in stale_attention
        ],
        "work_targets": [
            {
                "work_item_id": str(row.get("work_item_id") or ""),
                "focus_key": str(row.get("focus_key") or ""),
                "status_before": str(row.get("status") or ""),
                "updated_at": str(row.get("updated_at") or row.get("created_at") or ""),
                "waiting": row.get("current_waiting") if isinstance(row.get("current_waiting"), dict) else {},
            }
            for row in stale_work
        ],
    }


def repair(
    data_dir: Path,
    *,
    apply: bool,
    now: datetime | None = None,
    freshness_hours: int = AUTONOMOUS_WORK_ITEM_FRESHNESS_HOURS,
) -> dict[str, Any]:
    store = TuoguanStore(data_dir)
    plan = build_plan(store, now=now, freshness_hours=freshness_hours)
    writes: list[dict[str, Any]] = []
    if apply and (plan["attention_targets"] or plan["work_targets"]):
        identity = system_identity()
        stamp = (now or datetime.now().astimezone()).strftime("%Y%m%d%H%M%S")
        with authorized_system_write(
            store.data_dir,
            job_name="repair_stale_decision_state_v1",
            allowed_files={ATTENTION_THREADS_FILE, HERMES_WORK_ITEMS_FILE},
        ):
            for target in plan["attention_targets"]:
                result = update_attention_thread(
                    store,
                    identity=identity,
                    attention_id=target["attention_id"],
                    status="superseded",
                    operation_id=f"repair:stale-decision:{stamp}:attention:{target['attention_id']}",
                    resolution_note="该待确认线程已超过当前工作材料新鲜度，保留历史但不再作为老板当前决策。若仍有价值，应基于当前事实重新提出。",
                    source_text="repair_stale_decision_state_v1.py",
                )
                writes.append({
                    "kind": "attention",
                    "object_id": target["attention_id"],
                    "ok": bool(result.get("ok")),
                    "writeback_verified": bool(result.get("writeback_verified")),
                    "status_after": str((result.get("attention_thread") or {}).get("status") or ""),
                    "error": str(result.get("error") or ""),
                })
            for target in plan["work_targets"]:
                result = update_hermes_work_item(
                    store,
                    identity=identity,
                    operation_id=f"repair:stale-decision:{stamp}:work:{target['work_item_id']}",
                    work_item_id=target["work_item_id"],
                    status="superseded",
                    stop_reason="旧等待条件已超过当前工作材料新鲜度；保留历史，后续从当前目标和权威事实重新恢复。",
                    update_text="状态一致性收口：旧等待事项不再进入当前工作上下文、日报或老板决策看板。",
                    source_text="repair_stale_decision_state_v1.py",
                )
                writes.append({
                    "kind": "work_item",
                    "object_id": target["work_item_id"],
                    "ok": bool(result.get("ok")),
                    "writeback_verified": bool(result.get("writeback_verified")),
                    "status_after": str((result.get("work_item") or {}).get("status") or ""),
                    "error": str(result.get("error") or ""),
                })
    return {
        "ok": all(row["ok"] and row["writeback_verified"] for row in writes) if apply else True,
        "dry_run": not apply,
        "data_dir": str(data_dir),
        "plan": plan,
        "writes": writes,
        "boundary": {
            "history_deleted": False,
            "messages_sent": False,
            "tasks_changed": False,
            "goals_changed": False,
            "permissions_changed": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Append-only cleanup for stale owner decisions and waiting work items.")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--freshness-hours", type=int, default=AUTONOMOUS_WORK_ITEM_FRESHNESS_HOURS)
    parser.add_argument("--now", default="")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    reference = datetime.fromisoformat(args.now) if args.now else None
    result = repair(
        Path(args.data_dir),
        apply=bool(args.apply),
        now=reference,
        freshness_hours=int(args.freshness_hours),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
