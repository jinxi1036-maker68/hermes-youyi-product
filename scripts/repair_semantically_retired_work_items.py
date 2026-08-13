from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = REPO_ROOT / "runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from plugins.tuoguan_core.digital_employee_state import (  # noqa: E402
    HERMES_WORK_ITEMS_FILE,
    hermes_work_item_is_semantically_retired,
    query_hermes_work_items,
    update_hermes_work_item,
)
from plugins.tuoguan_core.employee_identity import system_identity  # noqa: E402
from plugins.tuoguan_core.store import TuoguanStore  # noqa: E402
from plugins.tuoguan_core.write_guard import authorized_system_write  # noqa: E402


def repair(data_dir: Path, *, apply: bool) -> dict[str, Any]:
    store = TuoguanStore(data_dir)
    identity = system_identity()
    queried = query_hermes_work_items(
        store,
        identity=identity,
        include_closed=False,
        limit=200,
    )
    targets = [
        item
        for item in queried.get("items") or []
        if isinstance(item, dict) and hermes_work_item_is_semantically_retired(item)
    ]
    writes: list[dict[str, Any]] = []
    if apply and targets:
        stamp = datetime.now().astimezone().strftime("%Y%m%d%H%M%S")
        with authorized_system_write(
            store.data_dir,
            job_name="repair_semantically_retired_work_items",
            allowed_files={HERMES_WORK_ITEMS_FILE},
        ):
            for index, item in enumerate(targets):
                result = update_hermes_work_item(
                    store,
                    identity=identity,
                    operation_id=f"repair:semantically_retired:{stamp}:{index}",
                    work_item_id=str(item.get("work_item_id") or ""),
                    status="superseded",
                    stop_reason="工作项已有合并或停止独立推进的明确证据，状态收口为 superseded。",
                    update_text="状态一致性修复：保留历史，不再作为当前事项、提醒或日报材料。",
                    source_text="repair_semantically_retired_work_items.py",
                )
                writes.append({
                    "work_item_id": str(item.get("work_item_id") or ""),
                    "ok": bool(result.get("ok")),
                    "writeback_verified": bool(result.get("writeback_verified")),
                    "state_changed": bool(result.get("state_changed")),
                    "status_after": str((result.get("work_item") or {}).get("status") or ""),
                    "error": str(result.get("error") or ""),
                })
    return {
        "ok": all(row.get("ok") and row.get("writeback_verified") for row in writes) if apply else True,
        "dry_run": not apply,
        "data_dir": str(data_dir),
        "match_count": len(targets),
        "targets": [
            {
                "work_item_id": str(item.get("work_item_id") or ""),
                "focus_key": str(item.get("focus_key") or ""),
                "title": str(item.get("title") or ""),
                "status_before": str(item.get("status") or ""),
                "updated_at": str(item.get("updated_at") or item.get("created_at") or ""),
            }
            for item in targets
        ],
        "writes": writes,
        "boundary": {
            "history_deleted": False,
            "messages_sent": False,
            "tasks_changed": False,
            "permissions_changed": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Append superseded updates for open work items whose own text says they were merged or stopped.",
    )
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = repair(Path(args.data_dir), apply=bool(args.apply))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
