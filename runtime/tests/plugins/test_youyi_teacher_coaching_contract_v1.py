from __future__ import annotations

import json
from pathlib import Path


def _write_json(root: Path, name: str, payload) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _seed_store(tmp_path: Path):
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(tmp_path, "write_guard_config.json", {"enabled": False})
    _write_json(
        tmp_path,
        "wecom_whitelist.json",
        {
            "super_users": ["boss1"],
            "allowed_users": ["boss1", "teacher1", "teacher2"],
            "user_roles": {"boss1": "boss", "teacher1": "teacher", "teacher2": "teacher"},
        },
    )
    _write_json(tmp_path, "teacher_wecom_map.json", {"机构负责人": "boss1", "示例老师": "teacher1"})
    _write_json(
        tmp_path,
        "staff.json",
        {
            "boss1": {"user_id": "boss1", "name": "机构负责人", "role": "super_admin"},
            "teacher1": {"user_id": "teacher1", "name": "示例老师", "role": "teacher"},
            "teacher2": {"user_id": "teacher2", "name": "王老师", "role": "teacher"},
        },
    )
    _write_json(tmp_path, "students.json", {"李依晨": {"teacher": "teacher1"}})
    _write_json(tmp_path, "tasks.json", [])
    _write_json(tmp_path, "notification_outbox.json", [])
    return TuoguanStore(tmp_path)


def _service(store, user_id: str, name: str):
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    return TuoguanToolService(
        store=store,
        platform="wecom_callback",
        user_id=user_id,
        user_name=name,
        chat_id=user_id,
        session_key=f"wecom_callback:{user_id}",
    )


def test_task_contract_contains_full_employee_execution_material(tmp_path):
    store = _seed_store(tmp_path)
    result = _service(store, "boss1", "机构负责人").create_task(
        title="联系李依晨家长，了解续费顾虑并约定下次跟进",
        assignee_user_id="teacher1",
        operation_id="op-full-task-contract",
        due_at="2026-08-13T20:00:00+08:00",
        student_name="李依晨",
        evidence_requirement="记录家长的真实顾虑",
    )

    assert result["ok"] is True
    task = store.load_tasks()[0]
    contract = task["task_contract"]
    assert contract["version"] == 3
    assert contract["original_instruction"] == "联系李依晨家长，了解续费顾虑并约定下次跟进"
    assert contract["business_goal"]
    assert contract["task_object"] == {"object_type": "student", "student_name": "李依晨"}
    assert contract["responsible_actor"]["user_id"] == "teacher1"
    assert contract["assignment_authority"] == {"user_id": "boss1", "role": "boss"}
    assert contract["success_evidence"] == contract["success_criteria"]
    assert contract["closure_conditions"]["closed_task_stops_followups"] is True


def test_task_companion_rejects_cross_teacher_or_untrusted_focus(tmp_path):
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.tasks import task_companion_context

    store = _seed_store(tmp_path)
    _write_json(
        tmp_path,
        "tasks.json",
        [
            {
                "id": "task-other-teacher",
                "title": "王老师的任务",
                "status": "active",
                "assignee_userid": "teacher2",
                "created_by": "boss1",
                "created_by_role": "boss",
            },
            {
                "id": "task-untrusted",
                "title": "未知来源任务",
                "status": "pending",
                "assignee_userid": "teacher1",
                "created_by": "unknown-user",
            },
        ],
    )
    _write_json(tmp_path, "active_task_context.json", {"teacher1": {"task_id": "task-other-teacher"}})
    identity = UserIdentity("wecom", "teacher1", "teacher1", "示例老师", "teacher", "approved")

    assert task_companion_context(store, identity=identity, raw_text="我不知道怎么说") == ""


def test_teacher_coaching_is_verified_and_never_performance_data(tmp_path, monkeypatch):
    from plugins.tuoguan_core import runtime_foundation

    store = _seed_store(tmp_path)
    boss = _service(store, "boss1", "机构负责人")
    created = boss.create_task(
        title="联系李依晨家长沟通近期学习情况",
        assignee_user_id="teacher1",
        operation_id="op-coaching-create",
        due_at="2026-08-13T20:00:00+08:00",
        student_name="李依晨",
    )
    assert created["ok"] is True
    teacher = _service(store, "teacher1", "示例老师")
    monkeypatch.setattr(runtime_foundation, "current_raw_text", lambda _user_id: "开始")
    started = teacher.update_task(
        task_id=created["task_id"],
        reply="开始",
        operation_id="op-coaching-start",
    )
    assert started["ok"] is True
    assert started["data"]["teacher_coaching_event"]["writeback_verified"] is True

    feedback = "我已经和家长沟通了，家长表示认可，我说明了孩子近期表现，明天继续跟进。"
    monkeypatch.setattr(runtime_foundation, "current_raw_text", lambda _user_id: feedback)
    completed = teacher.update_task(
        task_id=created["task_id"],
        reply=feedback,
        operation_id="op-coaching-complete",
    )
    assert completed["ok"] is True
    assert completed["data"]["task"]["status"] == "completed"
    assert completed["data"]["closed_task_cleanup"]["writeback_verified"] is True

    rows = [
        json.loads(line)
        for line in (tmp_path / "teacher_coaching_events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert {row["action"] for row in rows} == {"started", "completed"}
    completed_event = next(row for row in rows if row["action"] == "completed")
    assert completed_event["performance_boundary"] == {
        "used_for_payroll": False,
        "used_for_performance": False,
        "used_for_penalty": False,
        "purpose": "adapt_future_task_coaching_only",
    }
    assert "score" not in completed_event
    assert "rating" not in completed_event
    active = json.loads((tmp_path / "active_task_context.json").read_text(encoding="utf-8"))
    pending = json.loads((tmp_path / "pending_next_task_context.json").read_text(encoding="utf-8"))
    assert "teacher1" not in active
    assert "teacher1" not in pending
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert all(item["status"] == "suppressed" for item in outbox)


def test_inflight_task_delivery_becomes_result_unknown_when_task_closes(tmp_path):
    store = _seed_store(tmp_path)
    service = _service(store, "boss1", "机构负责人")
    _write_json(
        tmp_path,
        "notification_outbox.json",
        [{"id": "task-1:teacher:task_due", "task_id": "task-1", "status": "sending"}],
    )

    changed = service._suppress_pending_notifications_for_task("task-1", reason="task_closed")

    assert changed == 1
    item = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))[0]
    assert item["status"] == "result_unknown"
    assert item["last_error"] == "task_closed_while_send_in_flight"


def test_closing_task_retires_linked_proactive_thread_and_pending_delivery(tmp_path):
    store = _seed_store(tmp_path)
    candidate = {
        "record_type": "relationship_touch_candidate",
        "candidate_id": "touch-task-1",
        "target_role": "teacher",
        "target_user_id": "teacher1",
        "related_task_id": "task-1",
        "status": "queued",
        "created_at": "2026-08-13T18:00:00+08:00",
    }
    (tmp_path / "relationship_touch_candidates.jsonl").write_text(
        json.dumps(candidate, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _write_json(
        tmp_path,
        "notification_outbox.json",
        [{"id": "relationship_touch:touch-task-1", "status": "pending"}],
    )
    service = _service(store, "boss1", "机构负责人")

    result = service._retire_task_linked_work(
        {"id": "task-1", "status": "completed"},
        operation_id="op-retire-linked",
        reason="task_closed",
    )

    assert result["writeback_verified"] is True
    assert result["retired_relationship_touch_ids"] == ["touch-task-1"]
    assert result["retired_relationship_outbox"] == {"suppressed": 1, "result_unknown": 0}
    item = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))[0]
    assert item["status"] == "suppressed"
