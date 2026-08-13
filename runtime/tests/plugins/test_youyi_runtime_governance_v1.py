from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_all_core_modules_have_exactly_one_owner_category():
    from plugins.tuoguan_core.runtime_governance import module_inventory

    report = module_inventory(ROOT / "runtime" / "plugins" / "tuoguan_core")
    assert report["module_count"] >= 90
    assert report["classified_count"] == report["module_count"]
    assert report["unclassified"] == []
    assert report["missing_from_disk"] == []
    assert report["duplicates"] == []


def test_every_protected_business_state_has_an_owner():
    from plugins.tuoguan_core.runtime_governance import state_ownership
    from plugins.tuoguan_core.write_guard import PROTECTED_BUSINESS_FILES

    report = state_ownership(PROTECTED_BUSINESS_FILES)
    assert report["owned_count"] == report["resource_count"]
    assert report["unowned"] == []


def test_adversarial_replay_baseline_has_sixty_synthetic_cases():
    payload = json.loads(
        (ROOT / "work" / "commercialization" / "xiaoyou_adversarial_replays_v1.json").read_text(encoding="utf-8")
    )
    cases = payload["cases"]
    assert len(cases) >= 60
    assert len({item["id"] for item in cases}) == len(cases)
    assert {item["category"] for item in cases} == {
        "identity", "context", "time", "tool", "task", "outbound", "learning", "model"
    }
    assert payload["privacy"] == "synthetic_only_no_chat_transcripts"


def test_turn_trace_is_sanitized_and_writeback_verified(tmp_path):
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.turn_trace import (
        begin_tool_event,
        begin_turn_trace,
        clear_turn_traces,
        finalize_turn_trace,
        record_context_sources,
        record_guard_event,
        record_tool_event,
    )

    secret_text = "李老师说学生小明家长电话是13800000000"
    secret_reply = "我已经记录学生小明的信息"
    clear_turn_traces()
    begin_turn_trace(
        session_id="session-1", message_id="message-1", tenant_id="demo",
        app_id="corp-secret", user_id="teacher-secret", role="teacher",
        raw_text=secret_text, visible_tool_count=12,
    )
    record_context_sources("session-1", ["trusted_identity", "current_time", "task_context"])
    begin_tool_event("session-1", tool_name="tuoguan_query_tasks")
    record_tool_event("session-1", tool_name="tuoguan_query_tasks", result={"ok": True, "data": {"count": 1}})
    record_guard_event("session-1", guard="claim_guard", result="allowed")
    result = finalize_turn_trace(
        TuoguanStore(tmp_path), session_id="session-1", delivery_status="delivered", final_reply=secret_reply
    )

    assert result and result["writeback_verified"] is True
    raw = (tmp_path / "turn_traces.jsonl").read_text(encoding="utf-8")
    for forbidden in (secret_text, secret_reply, "小明", "13800000000", "teacher-secret", "corp-secret"):
        assert forbidden not in raw
    row = json.loads(raw)
    assert row["context_sources"] == ["trusted_identity", "current_time", "task_context"]
    assert row["tool_events"][0]["tool"] == "tuoguan_query_tasks"
    assert row["tool_events"][0]["duration_ms"] is not None
    assert row["delivery_status"] == "delivered"


def test_turn_trace_marks_tool_without_completion_receipt(tmp_path):
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.turn_trace import begin_tool_event, begin_turn_trace, clear_turn_traces, finalize_turn_trace

    clear_turn_traces()
    begin_turn_trace(
        session_id="session-timeout",
        message_id="message-timeout",
        tenant_id="demo",
        app_id="wecom",
        user_id="teacher1",
        role="teacher",
        raw_text="测试工具未返回",
    )
    begin_tool_event("session-timeout", tool_name="tuoguan_update_task")
    result = finalize_turn_trace(TuoguanStore(tmp_path), session_id="session-timeout", final_reply="本轮工具未完成。")

    assert result is not None
    assert result["tool_events"][0]["error"] == "tool_completion_missing"
    assert result["tool_events"][0]["ok"] is False
