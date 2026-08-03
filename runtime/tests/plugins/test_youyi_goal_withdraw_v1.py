from __future__ import annotations

import json
from pathlib import Path

from gateway.config import Platform


def _write_json(path: Path, name: str, payload) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _seed_store(tmp_path: Path):
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    (tmp_path / "manual_context").mkdir(parents=True, exist_ok=True)
    _write_json(
        tmp_path / "manual_context",
        "hermes_model_context_injection_allowlist_v1.json",
        {"runtime_foundation": {"enabled": True}, "allowed_capability_cards": []},
    )
    _write_json(
        tmp_path,
        "wecom_whitelist.json",
        {
            "super_users": ["boss1"],
            "allowed_users": ["teacher1"],
            "user_roles": {"boss1": "boss", "teacher1": "teacher"},
        },
    )
    _write_json(tmp_path, "teacher_wecom_map.json", {"金总": "boss1", "李老师": "teacher1"})
    _write_json(tmp_path, "staff.json", {"teacher1": {"name": "李老师", "role": "teacher"}})
    _write_json(
        tmp_path,
        "students.json",
        {
            "小明": {"teacher": "teacher1", "status": "active", "program_id": "regular_tuoguan"},
            "小红": {"teacher": "teacher1", "status": "active", "program_id": "regular_tuoguan"},
        },
    )
    _write_json(tmp_path, "records.json", [])
    _write_json(tmp_path, "tasks.json", [])
    _write_json(tmp_path, "notification_outbox.json", [])
    return TuoguanStore(tmp_path)


def _service(store, user_id: str):
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    return TuoguanToolService(
        store=store,
        platform=Platform.WECOM_CALLBACK.value,
        user_id=user_id,
        user_name="",
        chat_id=f"wwcorp:{user_id}",
        session_key=f"session:{user_id}",
    )


def _enter_model(store, user_id: str, role: str, message_id: str, raw_text: str) -> None:
    from plugins.tuoguan_core.runtime_foundation import begin_inbound, inject_model_context

    assert begin_inbound(
        store=store,
        message_id=message_id,
        conversation_id=f"conv-{message_id}",
        user_id=user_id,
        role=role,
        raw_text=raw_text,
    )
    assert inject_model_context(session_id=f"session-{message_id}", sender_id=user_id, user_message=raw_text)


def test_boss_can_withdraw_test_goal_without_deleting_audit_history(tmp_path):
    from plugins.tuoguan_core.goal_operator import find_active_goal
    from plugins.tuoguan_core.runtime_foundation import clear_runtime_state

    clear_runtime_state()
    store = _seed_store(tmp_path)
    _enter_model(store, "boss1", "boss", "m-goal-withdraw-1", "确认这个测试目标，先保存一下。")
    confirm = _service(store, "boss1").goal_workspace(
        action="confirm",
        goal_text="测试用的八月份沟通目标",
        confirmation_text="确认，先测试一下目标保存。",
        operation_id="op-goal-withdraw-confirm-1",
    )
    assert confirm["ok"] is True
    goal_id = confirm["data"]["goal_id"]

    _enter_model(store, "boss1", "boss", "m-goal-withdraw-2", "刚才那个目标只是测试用的，请清除掉。")
    result = _service(store, "boss1").goal_workspace(
        action="withdraw",
        goal_id=goal_id,
        withdraw_reason="老板明确说明这是测试目标，请撤出当前推进。",
        operation_id="op-goal-withdraw-1",
    )

    assert result["ok"] is True
    assert result["data"]["writeback_verified"] is True
    assert result["data"]["goal"]["status"] == "withdrawn"
    assert result["data"]["goal"]["current_material_status"] == "not_current_decision_material"
    assert result["data"]["goal"].get("test_goal_removed") is True

    goals = store.read_json("goal_operator_goals.json", {}).get("goals", [])
    persisted = [item for item in goals if item.get("goal_id") == goal_id]
    assert persisted and persisted[0]["status"] == "withdrawn"
    assert find_active_goal(store, goal_id=goal_id) is None

    progress = _service(store, "boss1").query_goal_progress(goal_id=goal_id)
    assert progress["ok"] is False
    assert progress["error"] == "goal_not_found"

    events = (tmp_path / "goal_operator_events.jsonl").read_text(encoding="utf-8")
    assert "goal_confirmed" in events
    assert "goal_withdrawn" in events

    work_items = _service(store, "boss1").query_hermes_work_items(focus_key=f"goal:{goal_id}", include_closed=True)
    assert work_items["ok"] is True
    assert work_items["data"]["items"][0]["status"] == "superseded"


def test_teacher_cannot_withdraw_institution_goal(tmp_path):
    from plugins.tuoguan_core.runtime_foundation import clear_runtime_state

    clear_runtime_state()
    store = _seed_store(tmp_path)
    _enter_model(store, "boss1", "boss", "m-goal-withdraw-3", "确认这个测试目标。")
    confirm = _service(store, "boss1").goal_workspace(
        action="confirm",
        goal_text="老师不能撤回的测试目标",
        confirmation_text="确认测试。",
        operation_id="op-goal-withdraw-confirm-2",
    )
    goal_id = confirm["data"]["goal_id"]

    _enter_model(store, "teacher1", "teacher", "m-goal-withdraw-4", "把这个目标清除掉。")
    result = _service(store, "teacher1").goal_workspace(
        action="withdraw",
        goal_id=goal_id,
        withdraw_reason="老师请求撤回机构目标。",
        operation_id="op-goal-withdraw-denied-1",
    )

    assert result["ok"] is False
    assert result["error"] == "permission_denied"
    goal = store.read_json("goal_operator_goals.json", {}).get("goals", [])[0]
    assert goal["status"] == "confirmed"
