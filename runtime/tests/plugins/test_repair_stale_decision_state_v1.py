from __future__ import annotations

from datetime import datetime
import json


def _write_json(root, name, value):
    (root / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _write_jsonl(root, name, rows):
    (root / name).write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_stale_decision_repair_is_dry_run_first_and_append_only(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import query_attention_threads, query_hermes_work_items
    from plugins.tuoguan_core.employee_identity import system_identity
    from plugins.tuoguan_core.store import TuoguanStore
    from scripts.repair_stale_decision_state_v1 import repair

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_jsonl(tmp_path, "attention_threads.jsonl", [{
        "record_type": "attention_thread",
        "attention_id": "attention-old",
        "focus_key": "goal:one",
        "target_user_id": "boss1",
        "question_text": "请确认一条已经过期的事实。",
        "status": "queued",
        "created_at": "2026-08-01T09:00:00+08:00",
    }])
    _write_jsonl(tmp_path, "hermes_work_items.jsonl", [{
        "record_type": "work_item",
        "work_item_id": "work-old",
        "focus_key": "goal:one",
        "title": "等待老板确认旧事实",
        "focus_summary": "旧等待",
        "status": "active",
        "current_waiting": {"waiting_for": "owner_reply", "reason": "旧问题"},
        "next_attention_at": "2026-08-01T10:00:00+08:00",
        "created_at": "2026-08-01T08:00:00+08:00",
    }])
    reference = datetime.fromisoformat("2026-08-03T12:00:00+08:00")

    dry_run = repair(tmp_path, apply=False, now=reference)
    assert dry_run["ok"] is True
    assert len(dry_run["plan"]["attention_targets"]) == 1
    assert len(dry_run["plan"]["work_targets"]) == 1
    assert len((tmp_path / "attention_threads.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert len((tmp_path / "hermes_work_items.jsonl").read_text(encoding="utf-8").splitlines()) == 1

    applied = repair(tmp_path, apply=True, now=reference)
    assert applied["ok"] is True
    assert {row["status_after"] for row in applied["writes"]} == {"superseded"}
    assert len((tmp_path / "attention_threads.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    assert len((tmp_path / "hermes_work_items.jsonl").read_text(encoding="utf-8").splitlines()) == 2

    store = TuoguanStore(tmp_path)
    attention = query_attention_threads(store, identity=system_identity(), include_closed=False, limit=10)
    work = query_hermes_work_items(store, identity=system_identity(), include_closed=False, limit=10)
    assert attention["attention_count"] == 0
    assert work["work_item_count"] == 0
    assert repair(tmp_path, apply=False, now=reference)["plan"]["attention_targets"] == []
    assert repair(tmp_path, apply=False, now=reference)["plan"]["work_targets"] == []
