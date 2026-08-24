from __future__ import annotations

import json
from pathlib import Path


def _json(path: Path, name: str, value: object) -> None:
    (path / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_migration_is_dry_run_first_and_never_resends_history(tmp_path: Path):
    from scripts.migrate_institution_work_closure_v1 import (
        OLD_MONTHLY_TASK_ID,
        WRONG_INTERNAL_TASK_ID,
        migrate,
    )

    _json(tmp_path, "write_guard_config.json", {"enabled": True})
    _json(tmp_path, "wecom_whitelist.json", {"super_users": ["JinWenJie"], "user_roles": {"JinWenJie": "boss"}})
    _json(tmp_path, "teacher_wecom_map.json", {"金总": "JinWenJie"})
    _json(tmp_path, "students.json", {})
    _json(tmp_path, "records.json", [])
    _json(tmp_path, "tasks.json", [
        {"id": WRONG_INTERNAL_TASK_ID, "title": "起草制度", "status": "pending", "assignee_userid": "JinWenJie"},
        {"id": OLD_MONTHLY_TASK_ID, "title": "月度巡查", "status": "pending", "assignee_userid": "JinWenJie"},
    ])
    _json(tmp_path, "notification_outbox.json", [
        {"id": "old-pending", "task_id": OLD_MONTHLY_TASK_ID, "status": "pending"},
        {"id": "old-sent", "task_id": WRONG_INTERNAL_TASK_ID, "status": "sent", "sent_at": "2026-08-24T10:00:00+08:00"},
    ])
    _json(tmp_path, "active_task_context.json", {"JinWenJie": {"task_id": WRONG_INTERNAL_TASK_ID}})
    _json(tmp_path, "pending_next_task_context.json", {"JinWenJie": {"task_id": OLD_MONTHLY_TASK_ID}})
    _json(tmp_path, "model_focus.json", {})
    (tmp_path / "person_workstyle_events.jsonl").write_text(
        json.dumps({"record_type": "person_workstyle_preference", "preference_id": "workstyle_pref_e1554ace1a1f", "target_user_id": "JinWenJie", "dimension_key": "length", "preference_text": "自主决定找谁", "status": "active"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    dry = migrate(tmp_path, apply=False)
    assert dry["ok"] and dry["dry_run"]
    assert json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8"))[0]["status"] == "pending"

    applied = migrate(tmp_path, apply=True)
    assert applied["ok"] and applied["writeback_verified"]
    assert applied["writes"]["safety"]["institution_stage"] == "awaiting_content_approval"
    tasks = {row["id"]: row for row in json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8"))}
    assert tasks[WRONG_INTERNAL_TASK_ID]["status"] == "superseded"
    assert tasks[OLD_MONTHLY_TASK_ID]["status"] == "superseded"
    outbox = {row["id"]: row for row in json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))}
    assert outbox["old-pending"]["status"] == "superseded"
    assert outbox["old-sent"]["status"] == "sent"
    assert json.loads((tmp_path / "active_task_context.json").read_text(encoding="utf-8")) == {}
    events = (tmp_path / "hermes_work_items.jsonl").read_text(encoding="utf-8")
    assert "优益托管安全管理制度 V0.1" in events
    assert "awaiting_content_approval" in events
