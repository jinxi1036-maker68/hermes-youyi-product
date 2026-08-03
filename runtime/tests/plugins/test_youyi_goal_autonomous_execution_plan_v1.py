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
        {"super_users": ["boss1"], "allowed_users": ["teacher1"], "user_roles": {"boss1": "boss", "teacher1": "teacher"}},
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

    assert begin_inbound(store=store, message_id=message_id, conversation_id=f"conv-{message_id}", user_id=user_id, role=role, raw_text=raw_text)
    assert inject_model_context(session_id=f"session-{message_id}", sender_id=user_id, user_message=raw_text)


def test_confirm_goal_creates_autonomous_execution_plan(tmp_path):
    from plugins.tuoguan_core.runtime_foundation import clear_runtime_state

    clear_runtime_state()
    store = _seed_store(tmp_path)
    _enter_model(store, "boss1", "boss", "m-goal-plan-1", "确认这个九月份续费更稳的方案，请你持续推进。")
    result = _service(store, "boss1").confirm_goal(
        goal_text="九月份续费率更稳",
        confirmation_text="确认方案，请持续推进目标完成。",
        operation_id="op-goal-plan-1",
    )

    assert result["ok"] is True
    assert result["data"]["writeback_verified"] is True
    goal_id = result["data"]["goal_id"]
    work_item_result = result["data"]["autonomous_work_item"]
    assert work_item_result["ok"] is True
    assert work_item_result["writeback_verified"] is True

    items = _service(store, "boss1").query_hermes_work_items(focus_key=f"goal:{goal_id}", include_closed=True)
    assert items["ok"] is True
    assert items["data"]["work_item_count"] == 1
    item = items["data"]["items"][0]
    assert item["execution_plan"]
    assert item["current_phase"]["phase_key"] in {"responsibility_confirmation", "first_batch_teacher_feedback"}
    assert item["next_actions"]
    assert item["current_phase"]["auto_execute"] is False
    serialized = json.dumps(item, ensure_ascii=False)
    for forbidden in ["model_intent", "next_tool", "workflow_step", "expected_reply"]:
        assert forbidden not in serialized


def test_work_item_execution_plan_fields_are_reference_material(tmp_path):
    from plugins.tuoguan_core.runtime_foundation import clear_runtime_state

    clear_runtime_state()
    store = _seed_store(tmp_path)
    _enter_model(store, "boss1", "boss", "m-goal-plan-2", "保存一个带推进计划的自主事项。")
    result = _service(store, "boss1").submit_hermes_work_item(
        focus_key="goal:test",
        title="目标推进测试",
        focus_summary="按阶段推进，不只汇总。",
        execution_plan=[{"phase": "阶段1", "focus": "先确认事实"}],
        current_phase={"phase_key": "fact_check", "phase_name": "阶段1：事实确认", "auto_execute": False},
        next_actions=[{"action_type": "ask_owner", "requires_model_decision": True, "auto_execute": False}],
        progress_evidence=[],
        operation_id="op-goal-plan-2",
    )

    assert result["ok"] is True
    item = result["data"]["work_item"]
    assert item["execution_plan"][0]["phase"] == "阶段1"
    assert item["current_phase"]["auto_execute"] is False
    assert item["next_actions"][0]["requires_model_decision"] is True
    assert item["auto_effects"]["forces_next_action"] is False
