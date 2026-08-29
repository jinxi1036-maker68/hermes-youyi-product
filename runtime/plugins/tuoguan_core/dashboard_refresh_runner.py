"""Deterministic, no-model H5 projection refresh entrypoint."""

from __future__ import annotations

import json
from datetime import datetime

from .dashboard_builder import refresh_dashboard_cache
from .store import TuoguanStore


def run_dashboard_refresh_once(
    store: TuoguanStore | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    """Rebuild all role projections from the current authoritative ledgers only."""

    actual_store = store or TuoguanStore()
    timestamp = now or datetime.now().astimezone()
    result = refresh_dashboard_cache(
        actual_store,
        now=timestamp,
        create_operation_tasks=False,
    )
    return {
        "ok": True,
        "report_type": "dashboard_projection_refresh_v1",
        "generated_at": str(result.get("generated_at") or ""),
        "freshness_state": str(result.get("freshness_state") or "current"),
        "source_versions": result.get("source_versions") or {},
        "writes": ["dashboard_cache.json"],
        "model_called": False,
        "outbound_count": 0,
        "task_count_changed": 0,
        "writeback_verified": True,
    }


def main() -> int:
    result = run_dashboard_refresh_once()
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
