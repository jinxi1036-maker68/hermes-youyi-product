from __future__ import annotations

import json
from pathlib import Path


def _json(path: Path, name: str, value: object) -> None:
    (path / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _rows(path: Path, name: str) -> list[dict]:
    target = path / name
    if not target.exists():
        return []
    return [json.loads(line) for line in target.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def test_dialogue_truth_migration_requires_real_owner_review_and_never_sends(tmp_path: Path):
    from plugins.tuoguan_core.digital_employee_state import query_institution_work
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.store import TuoguanStore
    from scripts.migrate_institution_dialogue_truth_v1 import migrate

    _json(tmp_path, "write_guard_config.json", {"enabled": True})
    _json(tmp_path, "wecom_whitelist.json", {"super_users": ["owner_test"], "user_roles": {"owner_test": "boss"}})
    _json(tmp_path, "teacher_wecom_map.json", {"机构负责人": "owner_test"})
    _json(tmp_path, "students.json", {})
    _json(tmp_path, "records.json", [])
    _json(tmp_path, "tasks.json", [])
    _json(tmp_path, "notification_outbox.json", [])
    (tmp_path / "hermes_work_items.jsonl").write_text(
        json.dumps({
            "record_type": "work_item",
            "work_item_id": "legacy-umbrella",
            "tenant_id": "example_institution",
            "work_kind": "institution_change",
            "institution_stage": "discovered",
            "focus_key": "institution:operating_rules_missing",
            "title": "四项制度缺失",
            "focus_summary": "历史总事项。",
            "status": "active",
            "evidence": [], "artifacts": [], "owner_decisions": [], "execution_links": [], "verifications": [],
            "current_waiting": {}, "created_at": "2026-08-25T20:00:00+08:00", "updated_at": "2026-08-25T20:00:00+08:00",
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    dry = migrate(tmp_path, apply=False)
    assert dry["ok"] and dry["dry_run"]
    assert _rows(tmp_path, "hermes_work_items.jsonl")[0]["institution_stage"] == "discovered"

    applied = migrate(tmp_path, apply=True)
    assert applied["ok"] and applied["writeback_verified"]
    assert applied["boundary"] == {
        "messages_sent": False,
        "staff_tasks_created": False,
        "policy_effective": False,
        "owner_approval_inferred": False,
        "history_deleted": False,
    }
    assert _json_text(tmp_path, "notification_outbox.json") == "[]"
    assert _json_text(tmp_path, "tasks.json") == "[]"

    store = TuoguanStore(tmp_path)
    boss = UserIdentity("test", "owner_test", "owner_test", "机构负责人", "boss", "approved")
    work = query_institution_work(store, identity=boss, include_closed=True, limit=20)["items"]
    by_focus = {item["focus_key"]: item for item in work}
    assert by_focus["institution:student_record_policy"]["institution_stage"] == "awaiting_content_approval"
    assert by_focus["institution:task_authorization_boundary"]["institution_stage"] == "awaiting_content_approval"
    assert by_focus["institution:performance_pay_boundary"]["institution_stage"] == "drafting"
    assert by_focus["institution:operating_rules_missing"]["institution_stage"] == "closed"
    assert all(not item.get("owner_decisions") for key, item in by_focus.items() if key != "institution:operating_rules_missing")

    wakeups = _rows(tmp_path, "wakeup_requests.jsonl")
    assert len(wakeups) == 1
    assert wakeups[0]["scheduled_for"] == "2026-08-31T09:30:00+08:00"
    assert wakeups[0]["auto_effects"]["executes_business_action"] is False

    second = migrate(tmp_path, apply=True)
    assert second["ok"] and second["writeback_verified"]
    assert len(_rows(tmp_path, "wakeup_requests.jsonl")) == 1


def _json_text(path: Path, name: str) -> str:
    return (path / name).read_text(encoding="utf-8")
