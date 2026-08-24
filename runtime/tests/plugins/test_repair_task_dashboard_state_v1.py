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


def test_verified_task_dashboard_repair_is_dry_run_first_and_append_only(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import _fold_relationship_touch_candidates
    from plugins.tuoguan_core.store import TuoguanStore
    from scripts.repair_task_dashboard_state_v1 import (
        DUPLICATE_RENEWAL_TASK_ID,
        OWNER_CONTACT_TASK_ID,
        PRIMARY_RENEWAL_TASK_ID,
        STALE_TOUCH_IDS,
        repair,
    )

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_json(tmp_path, "students.json", {})
    _write_json(tmp_path, "records.json", [])
    _write_json(tmp_path, "tasks.json", [
        {
            "id": PRIMARY_RENEWAL_TASK_ID,
            "title": "联系李依晨家长沟通下学期续费事宜",
            "status": "waiting_confirmation",
            "assignee_userid": "CeShi",
            "evidence_summary": "老师已联系家长。",
        },
        {
            "id": DUPLICATE_RENEWAL_TASK_ID,
            "title": "重复续费风险任务",
            "status": "superseded",
            "assignee_userid": "CeShi",
        },
        {
            "id": OWNER_CONTACT_TASK_ID,
            "title": "下午4点联系金总",
            "status": "pending",
            "assignee_userid": "CeShi",
        },
    ])
    _write_json(tmp_path, "active_task_context.json", {
        "CeShi": {"task_id": PRIMARY_RENEWAL_TASK_ID},
    })
    _write_json(tmp_path, "pending_next_task_context.json", {
        "CeShi": {"task_id": OWNER_CONTACT_TASK_ID},
    })
    _write_json(tmp_path, "model_focus.json", {
        "wecom_callback:CeShi": {"task_id": DUPLICATE_RENEWAL_TASK_ID},
    })
    _write_jsonl(tmp_path, "relationship_touch_candidates.jsonl", [
        {
            "record_type": "relationship_touch_candidate",
            "candidate_id": STALE_TOUCH_IDS[0],
            "target_role": "teacher",
            "target_user_id": "CeShi",
            "target_name": "李老师",
            "message": "李老师，请补充一条过期结果。",
            "status": "candidate",
            "created_at": "2026-08-13T15:09:00+08:00",
        },
        {
            "record_type": "relationship_touch_candidate",
            "candidate_id": STALE_TOUCH_IDS[1],
            "target_role": "teacher",
            "target_user_id": "CeShi",
            "target_name": "李老师",
            "message": "崔老师您好，请补充一条过期结果。",
            "status": "queued",
            "created_at": "2026-08-20T15:09:00+08:00",
        },
    ])
    reference = datetime.fromisoformat("2026-08-24T18:00:00+08:00")

    dry_run = repair(tmp_path, apply=False, now=reference)
    assert dry_run["ok"] is True
    assert {row["task_id"] for row in dry_run["plan"]["task_targets"]} == {
        PRIMARY_RENEWAL_TASK_ID, DUPLICATE_RENEWAL_TASK_ID, OWNER_CONTACT_TASK_ID,
    }
    assert {row["candidate_id"] for row in dry_run["plan"]["touch_targets"]} == set(STALE_TOUCH_IDS)
    assert json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8"))[0]["status"] == "waiting_confirmation"

    applied = repair(tmp_path, apply=True, now=reference)
    assert applied["ok"] is True
    assert applied["writeback_verified"] is True
    tasks = {row["id"]: row for row in json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8"))}
    assert tasks[PRIMARY_RENEWAL_TASK_ID]["status"] == "completed"
    assert tasks[DUPLICATE_RENEWAL_TASK_ID]["status"] == "superseded"
    assert tasks[OWNER_CONTACT_TASK_ID]["status"] == "completed"
    assert "开学时再考虑" in tasks[PRIMARY_RENEWAL_TASK_ID]["evidence_summary"]
    assert json.loads((tmp_path / "active_task_context.json").read_text(encoding="utf-8")) == {}
    assert json.loads((tmp_path / "pending_next_task_context.json").read_text(encoding="utf-8")) == {}
    assert json.loads((tmp_path / "model_focus.json").read_text(encoding="utf-8")) == {}
    folded = _fold_relationship_touch_candidates(TuoguanStore(tmp_path))
    assert {folded[item_id]["status"] for item_id in STALE_TOUCH_IDS} == {"superseded"}
    assert (tmp_path / "dashboard_cache.json").exists()
    assert len((tmp_path / "relationship_touch_candidates.jsonl").read_text(encoding="utf-8").splitlines()) == 4


def test_repair_accepts_an_already_superseded_touch_but_closes_a_sent_stale_thread(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import _fold_relationship_touch_candidates
    from plugins.tuoguan_core.store import TuoguanStore
    from scripts.repair_task_dashboard_state_v1 import (
        DUPLICATE_RENEWAL_TASK_ID,
        OWNER_CONTACT_TASK_ID,
        PRIMARY_RENEWAL_TASK_ID,
        STALE_TOUCH_IDS,
        repair,
    )

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_json(tmp_path, "tasks.json", [
        {"id": PRIMARY_RENEWAL_TASK_ID, "status": "waiting_confirmation"},
        {"id": DUPLICATE_RENEWAL_TASK_ID, "status": "superseded"},
        {"id": OWNER_CONTACT_TASK_ID, "status": "pending"},
    ])
    _write_jsonl(tmp_path, "relationship_touch_candidates.jsonl", [
        {"record_type": "relationship_touch_candidate", "candidate_id": STALE_TOUCH_IDS[0], "target_role": "teacher", "target_user_id": "CeShi", "message": "李老师，旧问题。", "status": "candidate", "created_at": "2026-08-13T15:09:00+08:00"},
        {"record_type": "relationship_touch_update", "candidate_id": STALE_TOUCH_IDS[0], "status": "superseded", "created_at": "2026-08-20T15:00:00+08:00"},
        {"record_type": "relationship_touch_candidate", "candidate_id": STALE_TOUCH_IDS[1], "target_role": "teacher", "target_user_id": "CeShi", "message": "崔老师您好，旧问题。", "status": "sent", "created_at": "2026-08-20T15:09:00+08:00"},
    ])

    result = repair(tmp_path, apply=False, now=datetime.fromisoformat("2026-08-24T18:00:00+08:00"))

    assert result["ok"] is True
    assert [row["candidate_id"] for row in result["plan"]["touch_targets"]] == [STALE_TOUCH_IDS[1]]
    assert _fold_relationship_touch_candidates(TuoguanStore(tmp_path))[STALE_TOUCH_IDS[1]]["status"] == "sent"
