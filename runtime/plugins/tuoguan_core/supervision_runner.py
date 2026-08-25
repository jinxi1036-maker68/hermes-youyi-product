"""Scheduler entry point for Xiaoyou's asynchronous supervision pass."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path

from .store import TuoguanStore
from .supervision import (
    SUPERVISION_FINDINGS_FILE,
    SUPERVISION_REPAIRS_FILE,
    SUPERVISION_RUNS_FILE,
    restricted_advisor_call,
    scan_supervision,
)
from .write_guard import authorized_system_write


def run_supervision_once(
    store: TuoguanStore | None = None,
    *,
    now: datetime | None = None,
    apply_repairs: bool | None = None,
    write_report: bool = True,
) -> dict:
    actual_store = store or TuoguanStore()
    timestamp = now or datetime.now().astimezone()
    enabled = (
        str(os.getenv("HERMES_SUPERVISION_APPLY_REPAIRS") or "").strip().lower() in {"1", "true", "yes", "on"}
        if apply_repairs is None
        else bool(apply_repairs)
    )
    council_enabled = str(os.getenv("HERMES_SUPERVISION_COUNCIL_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}
    allowed = {
        SUPERVISION_RUNS_FILE,
        SUPERVISION_FINDINGS_FILE,
        SUPERVISION_REPAIRS_FILE,
        "dashboard_cache.json",
        "active_task_context.json",
        "pending_next_task_context.json",
        "model_focus.json",
        "notification_outbox.json",
        "self_evolution_events.jsonl",
    }
    with authorized_system_write(
        actual_store.data_dir,
        job_name="xiaoyou_supervision_runner",
        allowed_files=allowed,
    ):
        result = scan_supervision(
            actual_store,
            now=timestamp,
            run_kind="nightly" if timestamp.hour == 23 and timestamp.minute >= 35 else "periodic",
            apply_repairs=enabled,
            advisor_call=restricted_advisor_call if council_enabled else None,
        )
    result["apply_repairs"] = enabled
    result["council_enabled"] = council_enabled
    result["boundary"] = {
        "runs_outside_wecom_reply_path": True,
        "sends_messages": False,
        "changes_business_facts": False,
        "changes_tasks": False,
        "changes_permissions": False,
        "changes_code_or_config": False,
        "advisors_read_only": True,
    }
    if write_report:
        reports = actual_store.data_dir / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        path = reports / f"xiaoyou-supervision-v1-{timestamp.strftime('%Y%m%d-%H%M%S')}.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        result["report_path"] = str(path)
    return result


def main() -> int:
    result = run_supervision_once()
    print(result.get("rendered_text") or (result.get("run") or {}).get("status") or "completed")
    if result.get("report_path"):
        print(f"REPORT:{result['report_path']}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
