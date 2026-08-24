from __future__ import annotations

import json


def _write(root, name, value):
    (root / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_duplicate_task_repair_preserves_history_and_closes_completed_contact(tmp_path):
    from plugins.tuoguan_core.repair_task_companion_state import repair_duplicate_task_pair
    from plugins.tuoguan_core.store import TuoguanStore

    _write(tmp_path, "write_guard_config.json", {"enabled": False})
    _write(tmp_path, "notification_outbox.json", [{"id": "n1", "task_id": "duplicate", "status": "pending"}])
    _write(tmp_path, "tasks.json", [
        {
            "id": "primary", "title": "联系李依晨家长沟通续费", "type": "manual_assignment",
            "status": "pending", "student_name": "李依晨", "assignee_userid": "CeShi", "created_by": "JinWenJie",
        },
        {
            "id": "duplicate", "title": "李依晨续费风险任务", "type": "renewal_risk",
            "status": "completed", "student_name": "李依晨", "assignee_userid": "CeShi",
        },
    ])
    store = TuoguanStore(tmp_path)
    evidence = "我已经沟通过了，他家长说到开学的时候再考虑。"

    dry = repair_duplicate_task_pair(
        store, primary_task_id="primary", duplicate_task_id="duplicate", evidence_text=evidence, apply=False,
    )
    assert dry["ok"] is True
    assert dry["primary_status_after"] == "completed"
    assert dry["missing_fields"] == []
    assert store.load_tasks()[1]["status"] == "completed"

    applied = repair_duplicate_task_pair(
        store, primary_task_id="primary", duplicate_task_id="duplicate", evidence_text=evidence, apply=True,
    )
    assert applied["writeback_verified"] is True
    saved = {item["id"]: item for item in store.load_tasks()}
    assert saved["primary"]["status"] == "completed"
    assert evidence in saved["primary"]["evidence_summary"]
    assert saved["duplicate"]["status"] == "superseded"
    assert saved["duplicate"]["status_before_superseded"] == "completed"
    assert store.read_json("notification_outbox.json", [])[0]["status"] == "superseded"


def test_duplicate_task_repair_refuses_cross_student_pair(tmp_path):
    from plugins.tuoguan_core.repair_task_companion_state import repair_duplicate_task_pair
    from plugins.tuoguan_core.store import TuoguanStore

    _write(tmp_path, "write_guard_config.json", {"enabled": False})
    _write(tmp_path, "tasks.json", [
        {"id": "primary", "status": "pending", "student_name": "李依晨", "assignee_userid": "CeShi"},
        {"id": "duplicate", "status": "completed", "student_name": "小金", "assignee_userid": "CeShi"},
    ])
    result = repair_duplicate_task_pair(
        TuoguanStore(tmp_path), primary_task_id="primary", duplicate_task_id="duplicate", evidence_text="真实反馈", apply=True,
    )
    assert result["ok"] is False
    assert "student_mismatch" in result["errors"]
