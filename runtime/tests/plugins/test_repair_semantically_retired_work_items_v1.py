from __future__ import annotations

import json
from pathlib import Path


def _write_json(path: Path, name: str, payload) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_jsonl(path: Path, name: str, rows: list[dict]) -> None:
    with (path / name).open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def test_repair_is_dry_run_first_and_appends_verified_superseded_update(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import query_hermes_work_items
    from plugins.tuoguan_core.employee_identity import system_identity
    from plugins.tuoguan_core.store import TuoguanStore
    from scripts.repair_semantically_retired_work_items import repair

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_jsonl(
        tmp_path,
        "hermes_work_items.jsonl",
        [
            {
                "record_type": "work_item",
                "work_item_id": "merged-legacy-item",
                "tenant_id": "demo_tuoguan",
                "focus_key": "history:merged",
                "title": "已合并到主工作项，不再独立推进。",
                "focus_summary": "保留历史即可。",
                "status": "active",
                "created_at": "2026-08-10T08:00:00+08:00",
                "updated_at": "2026-08-10T08:00:00+08:00",
            }
        ],
    )

    dry_run = repair(tmp_path, apply=False)
    assert dry_run["ok"] is True
    assert dry_run["match_count"] == 1
    assert len((tmp_path / "hermes_work_items.jsonl").read_text(encoding="utf-8").splitlines()) == 1

    applied = repair(tmp_path, apply=True)
    assert applied["ok"] is True
    assert applied["writes"] == [
        {
            "work_item_id": "merged-legacy-item",
            "ok": True,
            "writeback_verified": True,
            "state_changed": True,
            "status_after": "superseded",
            "error": "",
        }
    ]
    assert len((tmp_path / "hermes_work_items.jsonl").read_text(encoding="utf-8").splitlines()) == 2

    store = TuoguanStore(tmp_path)
    folded = query_hermes_work_items(
        store,
        identity=system_identity(),
        include_closed=True,
        limit=10,
    )["items"]
    assert folded[0]["status"] == "superseded"
    assert repair(tmp_path, apply=False)["match_count"] == 0
