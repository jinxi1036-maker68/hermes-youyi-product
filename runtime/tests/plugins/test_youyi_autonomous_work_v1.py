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
            "allowed_users": ["teacher1", "teacher2", "manager1"],
            "user_roles": {"boss1": "boss", "teacher1": "teacher", "teacher2": "teacher", "manager1": "manager"},
        },
    )
    _write_json(tmp_path, "teacher_wecom_map.json", {"金总": "boss1", "李老师": "teacher1", "赵老师": "teacher2", "王店长": "manager1"})
    _write_json(
        tmp_path,
        "staff.json",
        {
            "teacher1": {"name": "李老师", "role": "teacher", "campus_ids": ["main"], "program_ids": ["regular_tuoguan"]},
            "teacher2": {"name": "赵老师", "role": "teacher", "campus_ids": ["main"], "program_ids": ["regular_tuoguan"]},
            "manager1": {"name": "王店长", "role": "manager", "campus_ids": ["main"], "program_ids": ["regular_tuoguan"]},
        },
    )
    _write_json(
        tmp_path,
        "students.json",
        {
            "小明": {"teacher": "teacher1", "campus_id": "main", "status": "active", "program_id": "regular_tuoguan"},
            "小红": {"teacher": "teacher2", "campus_id": "main", "status": "active", "program_id": "regular_tuoguan"},
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


def test_autonomous_work_tools_are_registered_as_capabilities_not_routers():
    from plugins.tuoguan_core.tools import TOOLS

    expected = {
        "tuoguan_query_hermes_work_items",
        "tuoguan_query_wakeup_requests",
        "tuoguan_query_business_events",
        "tuoguan_query_action_executions",
        "tuoguan_query_autonomous_work_brief",
        "tuoguan_submit_hermes_work_item",
        "tuoguan_update_hermes_work_item",
        "tuoguan_submit_wakeup_request",
        "tuoguan_submit_business_event",
        "tuoguan_submit_action_execution",
    }
    names = {name for name, _schema, _handler in TOOLS}
    assert expected <= names

    descriptions = "\n".join(str(schema.get("description") or "") for name, schema, _handler in TOOLS if name in expected)
    for fragment in ["关键词", "用户说", "必须调用本工具", "model_intent", "next_tool", "workflow_step", "expected_reply", "自动发家长", "自动扣工资"]:
        assert fragment not in descriptions


def test_autonomous_state_write_is_blocked_without_model_context(tmp_path):
    from plugins.tuoguan_core.runtime_foundation import clear_runtime_state

    clear_runtime_state()
    store = _seed_store(tmp_path)
    result = _service(store, "boss1").submit_hermes_work_item(
        focus_key="renewal:2026-09",
        title="九月续费稳定性",
        focus_summary="老板希望九月份续费更稳，但还需要先看真实数据。",
        operation_id="op-without-model",
    )

    assert result["ok"] is False
    assert result["error"] == "unauthorized_write_blocked"
    assert not (tmp_path / "hermes_work_items.jsonl").exists()


def test_work_item_create_update_merge_and_forbidden_fields_are_not_saved(tmp_path):
    from plugins.tuoguan_core.runtime_foundation import clear_runtime_state

    clear_runtime_state()
    store = _seed_store(tmp_path)
    _enter_model(store, "boss1", "boss", "m-work-1", "九月份续费率更稳，先别直接安排老师。")
    service = _service(store, "boss1")
    created = service.submit_hermes_work_item(
        focus_key="renewal:2026-09",
        title="九月续费稳定性",
        focus_summary="先查事实、看缺口，不直接派老师任务。",
        related_staff_user_ids=["teacher1"],
        pending_judgements=[{"model_intent": "bad", "text": "需要查近况"}],
        current_waiting={"target_user_id": "teacher1", "target_person": "李老师", "reason": "等老师补充最近孩子状态", "next_tool": "bad"},
        next_attention_at="2026-07-28T10:00:00+08:00",
        status="waiting",
        operation_id="op-work-1",
    )
    assert created["ok"] is True
    assert created["data"]["writeback_verified"] is True

    clear_runtime_state()
    _enter_model(store, "boss1", "boss", "m-work-2", "刚才这个续费目标补充一下，先作为活跃事项继续看。")
    merged = service.submit_hermes_work_item(
        focus_key="renewal:2026-09",
        title="九月续费稳定性",
        focus_summary="同一焦点继续推进，先核验最新事实。",
        confirmed_facts=[{"text": "老板要求不要直接安排老师。"}],
        status="active",
        operation_id="op-work-2",
    )
    assert merged["ok"] is True

    queried = service.query_hermes_work_items(focus_key="renewal:2026-09", include_closed=True)
    assert queried["ok"] is True
    assert queried["data"]["work_item_count"] == 1
    item = queried["data"]["items"][0]
    assert item["status"] == "active"
    assert len(item["updates"]) == 1
    serialized = json.dumps(item, ensure_ascii=False)
    for forbidden in ["model_intent", "next_tool", "workflow_step", "expected_reply"]:
        assert forbidden not in serialized


def test_teacher_scope_only_sees_related_work_items(tmp_path):
    from plugins.tuoguan_core.runtime_foundation import clear_runtime_state

    clear_runtime_state()
    store = _seed_store(tmp_path)
    boss = _service(store, "boss1")
    _enter_model(store, "boss1", "boss", "m-scope-1", "保存两个自主事项做权限测试。")
    assert boss.submit_hermes_work_item(
        focus_key="teacher1:item",
        title="李老师相关事项",
        focus_summary="只和李老师相关。",
        related_staff_user_ids=["teacher1"],
        operation_id="op-scope-1",
    )["ok"]
    clear_runtime_state()
    _enter_model(store, "boss1", "boss", "m-scope-2", "保存第二个自主事项。")
    assert boss.submit_hermes_work_item(
        focus_key="teacher2:item",
        title="赵老师相关事项",
        focus_summary="只和赵老师相关。",
        related_staff_user_ids=["teacher2"],
        operation_id="op-scope-2",
    )["ok"]

    teacher_view = _service(store, "teacher1").query_hermes_work_items(include_closed=True)
    assert teacher_view["ok"] is True
    titles = {item["title"] for item in teacher_view["data"]["items"]}
    assert titles == {"李老师相关事项"}


def test_wakeup_business_event_action_execution_and_brief(tmp_path):
    from plugins.tuoguan_core.runtime_foundation import clear_runtime_state

    clear_runtime_state()
    store = _seed_store(tmp_path)
    service = _service(store, "boss1")
    _enter_model(store, "boss1", "boss", "m-brief-1", "这件事明天上午再看。")
    assert service.submit_wakeup_request(
        wakeup_source="self_attention",
        reason="明天上午重新查看等待事项。",
        scheduled_for="2026-07-28T10:00:00+08:00",
        operation_id="op-wakeup-1",
    )["ok"]
    clear_runtime_state()
    _enter_model(store, "boss1", "boss", "m-brief-2", "老师刚才还没有回复。")
    assert service.submit_business_event(
        event_type="teacher_no_reply",
        event_text="李老师暂未回复目标相关问题。",
        related_objects=[{"teacher_user_id": "teacher1"}],
        operation_id="op-event-1",
    )["ok"]
    clear_runtime_state()
    _enter_model(store, "boss1", "boss", "m-brief-3", "刚才工具结果未知，先记账。")
    assert service.submit_action_execution(
        action_type="tool_call",
        action_summary="查询目标状态返回不确定。",
        status="result_unknown",
        idempotency_key="idem-unknown-1",
        operation_id="op-action-1",
    )["ok"]

    brief = service.query_autonomous_work_brief()
    assert brief["ok"] is True
    assert brief["data"]["pending_wakeup_count"] == 1
    assert brief["data"]["recent_business_event_count"] == 1
    assert brief["data"]["result_unknown_action_count"] == 1
    assert not (tmp_path / "notification_outbox.json").read_text(encoding="utf-8").strip("[] \n")


def test_wakeup_v2_includes_autonomous_counts_and_stays_read_only(tmp_path):
    from datetime import datetime
    from plugins.tuoguan_core.runtime_foundation import clear_runtime_state
    from plugins.tuoguan_core.wakeup_v2 import run_wakeup_v2_dry_run

    clear_runtime_state()
    store = _seed_store(tmp_path)
    service = _service(store, "boss1")
    _enter_model(store, "boss1", "boss", "m-night-1", "今晚只做内部复盘。")
    assert service.submit_hermes_work_item(
        focus_key="night:review",
        title="夜间内部复盘",
        focus_summary="只读复盘老板摘要草稿。",
        status="waiting",
        current_waiting={"reason": "等待次日老板查看"},
        operation_id="op-night-1",
    )["ok"]
    before_notifications = (tmp_path / "notification_outbox.json").read_text(encoding="utf-8")

    result = run_wakeup_v2_dry_run(store, now=datetime(2026, 7, 26, 23, 0, 0), write_report=False)

    assert result["read_only"] is True
    assert result["source_counts"]["autonomous_work_item_count"] == 1
    assert result["source_counts"]["autonomous_waiting_count"] == 1
    assert "Hermes 自主工作：事项 1 条；等待 1 条" in result["rendered_text"]
    assert (tmp_path / "notification_outbox.json").read_text(encoding="utf-8") == before_notifications
