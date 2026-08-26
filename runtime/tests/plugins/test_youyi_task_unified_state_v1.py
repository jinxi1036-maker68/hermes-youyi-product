from __future__ import annotations

import json


def _write(root, name, value):
    (root / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _store(root):
    from plugins.tuoguan_core.store import TuoguanStore

    _write(root, "students.json", {"周温暖": {"teacher": "CeShi", "business_signals": {"open_task_count": 9}}})
    _write(root, "staff.json", {
        "JinWenJie": {"name": "金总", "role": "boss", "status": "active"},
        "CeShi": {"name": "李老师", "role": "teacher", "status": "active"},
    })
    _write(root, "teacher_wecom_map.json", {"李老师": "CeShi"})
    _write(root, "wecom_whitelist.json", {
        "allowed_users": ["JinWenJie", "CeShi"],
        "super_users": ["JinWenJie"],
        "user_roles": {"JinWenJie": "boss", "CeShi": "teacher"},
    })
    _write(root, "wecom_directory_cache.json", {"members": [{"user_id": "JinWenJie", "name": "金总"}, {"user_id": "CeShi", "name": "李老师"}]})
    _write(root, "tasks.json", [])
    _write(root, "notification_outbox.json", [])
    return TuoguanStore(root)


def _service(store, user_id, name):
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    return TuoguanToolService(
        store=store,
        platform="wecom_callback",
        user_id=user_id,
        user_name=name,
        chat_id=user_id,
        session_key=user_id,
    )


def test_create_task_resolves_teacher_name_and_reports_queue_receipt(tmp_path):
    store = _store(tmp_path)
    result = _service(store, "JinWenJie", "金总").create_task(
        title="上午10点联系周温暖家长",
        teacher_name="李老师",
        student_name="周温暖",
        operation_id="message-create-by-name",
    )

    assert result["ok"] is True
    assert result["task"]["assignee_userid"] == "CeShi"
    assert result["task"]["original_instruction"] == "上午10点联系周温暖家长"
    assert result["data"]["notification_queued"] is True
    assert result["data"]["notification_sent"] is False
    assert "入队回执" in result["message"]


def test_natural_completion_variants_close_ordinary_task(tmp_path):
    from plugins.tuoguan_core.tasks import apply_task_reply

    for index, phrase in enumerate(("做完了", "已经弄完", "联系过了", "处理好了")):
        task = {
            "id": f"task-{index}", "title": "联系家长", "type": "manual_assignment", "level": "A",
            "status": "active", "assignee_userid": "CeShi",
            "task_contract": {"completion_policy": "natural_confirmation"},
        }
        result = apply_task_reply([task], "CeShi", phrase)
        assert result.action == "completed"
        assert task["status"] == "completed"
        assert task["coach_stage"] == "closed"


def test_closed_status_has_one_definition_in_task_queries_and_reports(tmp_path):
    store = _store(tmp_path)
    _write(tmp_path, "tasks.json", [
        {"id": "open", "title": "开放", "status": "active", "assignee_userid": "CeShi"},
        {"id": "old", "title": "已替代", "status": "superseded", "assignee_userid": "CeShi"},
        {"id": "expired", "title": "过期", "status": "expired", "assignee_userid": "CeShi"},
    ])
    boss = _service(store, "JinWenJie", "金总")
    queried = boss.query_tasks(status="open", scope="all")
    assert queried["data"]["task_ids"] == ["open"]
    from plugins.tuoguan_core.youyi_batch_capabilities import operations_report
    assert operations_report(store, report_type="operations")["summary"]["open_task_count"] == 1


def test_task_repair_only_updates_contracts_and_projections(tmp_path):
    from plugins.tuoguan_core.repair_task_unified_state_v1 import task_unified_state_repair

    store = _store(tmp_path)
    _write(tmp_path, "tasks.json", [
        {"id": "closed", "title": "已完成", "status": "completed", "coach_stage": "collecting_evidence", "assignee_userid": "CeShi", "student_name": "周温暖"},
    ])
    _write(tmp_path, "active_task_context.json", {"CeShi": {"task_id": "closed"}})
    _write(tmp_path, "pending_next_task_context.json", {"CeShi": {"task_id": "missing"}})
    _write(tmp_path, "model_focus.json", {"CeShi": {"task_id": "missing"}})

    preview = task_unified_state_repair(store)
    assert preview["dry_run"] is True
    assert preview["task_contract_repairs"] == ["closed"]
    applied = task_unified_state_repair(store, apply=True)
    assert applied["ok"] is True
    saved = store.load_tasks()[0]
    assert saved["status"] == "completed"
    assert saved["coach_stage"] == "closed"
    assert saved["task_contract"]["version"] == 3
    assert store.read_json("active_task_context.json", {}) == {}
    assert store.read_json("pending_next_task_context.json", {}) == {}
    assert store.read_json("model_focus.json", {}) == {}
    assert store.read_json("students.json", {})["周温暖"]["business_signals"]["open_task_count"] == 0


def test_successful_tool_result_resets_corrective_retry_sequence():
    from plugins.tuoguan_core.runtime_performance import (
        guard_turn_tool_call,
        observe_turn_tool_result,
        reset_turn_tool_budget,
        turn_tool_budget_snapshot,
    )

    session = "task-retry-reset"
    reset_turn_tool_budget(session)
    observe_turn_tool_result(session, tool_name="tuoguan_create_task", result={"ok": False, "error": "unsupported_arguments"})
    observe_turn_tool_result(session, tool_name="tuoguan_create_task", result={"ok": True})
    assert turn_tool_budget_snapshot(session)["correctable_failure_count"] == 0
    assert guard_turn_tool_call(session, tool_name="tuoguan_create_task", args={"teacher_name": "李老师"}) is None
