from __future__ import annotations

import asyncio
import json

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource


def _event(text: str = "你好") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_id="latency-msg-1",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.WECOM_CALLBACK,
            user_id="CeShi",
            chat_id="corp:CeShi",
            user_name="李老师",
            chat_type="dm",
        ),
    )


def test_turn_tool_budget_blocks_duplicate_and_runaway_calls():
    from plugins.tuoguan_core.runtime_performance import (
        guard_turn_tool_call,
        reset_turn_tool_budget,
        turn_tool_budget_snapshot,
    )

    session_id = "latency-session"
    reset_turn_tool_budget(session_id)
    assert guard_turn_tool_call(
        session_id,
        tool_name="tuoguan_tasks",
        args={"operation": "query_tasks", "arguments": {}},
    ) is None
    duplicate = guard_turn_tool_call(
        session_id,
        tool_name="tuoguan_tasks",
        args={"operation": "query_tasks", "arguments": {}},
    )
    assert duplicate == {
        "action": "block",
        "message": duplicate["message"],
        "reason": "duplicate_tool_call_in_turn",
    }

    for index in range(1, 4):
        assert guard_turn_tool_call(
            session_id,
            tool_name="tuoguan_tasks",
            args={"operation": "query_tasks", "arguments": {"page": index}},
        ) is None
    exhausted = guard_turn_tool_call(
        session_id,
        tool_name="tuoguan_tasks",
        args={"operation": "query_tasks", "arguments": {"page": 99}},
    )
    assert exhausted and exhausted["reason"] == "tool_call_budget_exhausted"
    assert turn_tool_budget_snapshot(session_id)["count"] == 4


def test_turn_tool_budget_allows_one_contract_correction_then_stops():
    from plugins.tuoguan_core.runtime_performance import (
        guard_turn_tool_call,
        observe_turn_tool_result,
        reset_turn_tool_budget,
        turn_tool_budget_snapshot,
    )

    session_id = "contract-correction-session"
    reset_turn_tool_budget(session_id)
    assert guard_turn_tool_call(
        session_id, tool_name="tuoguan_students", args={"operation": "bad"},
    ) is None
    observe_turn_tool_result(
        session_id, tool_name="tuoguan_students",
        result={"ok": False, "error": "unknown_facade_operation"},
    )
    assert guard_turn_tool_call(
        session_id, tool_name="tuoguan_query_students", args={"query_scope": "regular"},
    ) is None
    observe_turn_tool_result(
        session_id, tool_name="tuoguan_query_students",
        result={"ok": False, "error": "unsupported_arguments"},
    )
    blocked = guard_turn_tool_call(
        session_id, tool_name="tuoguan_query_students", args={"query_scope": "visible"},
    )
    assert blocked and blocked["reason"] == "corrective_tool_retry_exhausted"
    assert turn_tool_budget_snapshot(session_id)["correctable_failure_count"] == 2


def test_turn_trace_splits_model_and_tool_time_without_storing_content(tmp_path):
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.turn_trace import (
        begin_tool_event,
        begin_turn_trace,
        finalize_turn_trace,
        record_context_sources,
        record_tool_event,
    )

    store = TuoguanStore(tmp_path)
    begin_turn_trace(
        session_id="trace-session", message_id="message-1", tenant_id="demo_tuoguan",
        app_id="app-1", user_id="teacher-1", role="teacher",
        raw_text="private student sentence", visible_tool_count=23,
    )
    record_context_sources("trace-session", ["trusted_gateway_identity", "work_context_snapshot"])
    begin_tool_event("trace-session", tool_name="tuoguan_query_students")
    record_tool_event(
        "trace-session", tool_name="tuoguan_query_students",
        result={"ok": True, "data": {"rendered_text": "private result"}},
    )
    trace = finalize_turn_trace(
        store, session_id="trace-session", delivery_status="prepared", final_reply="private reply",
    )

    assert trace is not None
    assert trace["turn_class"] == "direct_read"
    assert trace["model_segment_count"] == 2
    assert [item["phase"] for item in trace["model_events"]] == ["initial_model", "final_model"]
    assert trace["final_outcome"] == "completed"
    serialized = __import__("json").dumps(trace, ensure_ascii=False)
    assert "private student sentence" not in serialized
    assert "private result" not in serialized
    assert "private reply" not in serialized


def test_turn_fence_discards_expired_or_superseded_model_work():
    from plugins.tuoguan_core.turn_fence import (
        begin_callback_turn,
        bind_session,
        block_reason,
        clear_turn_fences,
        mark_phase,
    )

    clear_turn_fences()
    begin_callback_turn(message_id="old", chat_id="corp:CeShi", budget_seconds=38)
    assert bind_session(message_id="old", session_id="session-old", chat_id="corp:CeShi")
    mark_phase(session_id="session-old", phase="model")
    begin_callback_turn(message_id="new", chat_id="corp:CeShi", budget_seconds=38)
    assert block_reason(session_id="session-old") == "turn_superseded"


def test_turn_fence_never_cancels_a_turn_after_tool_phase_begins():
    from plugins.tuoguan_core.turn_fence import (
        begin_callback_turn,
        bind_session,
        block_reason,
        clear_turn_fences,
        mark_phase,
    )

    clear_turn_fences()
    begin_callback_turn(message_id="write", chat_id="corp:CeShi", budget_seconds=38)
    assert bind_session(message_id="write", session_id="session-write", chat_id="corp:CeShi")
    mark_phase(session_id="session-write", phase="tool")
    begin_callback_turn(message_id="next", chat_id="corp:CeShi", budget_seconds=38)
    assert block_reason(session_id="session-write") == ""


def test_provider_circuit_opens_for_agnes_transport_failures_without_secrets(tmp_path, monkeypatch):
    from plugins.tuoguan_core import provider_resilience

    monkeypatch.setattr(provider_resilience, "get_hermes_home", lambda: tmp_path)
    provider_resilience.open_circuit(RuntimeError("TLS handshake timed out"))
    snapshot = provider_resilience.provider_health_snapshot()

    assert snapshot["agnes"]["state"] == "open"
    assert snapshot["agnes"]["last_error_class"] == "tls"
    raw = (tmp_path / "state" / provider_resilience.STATE_FILE_NAME).read_text(encoding="utf-8")
    assert "api_key" not in raw
    assert "https://" not in raw


def test_provider_circuit_requires_two_recovery_probes_before_restoring_agnes(tmp_path, monkeypatch):
    from plugins.tuoguan_core import provider_resilience

    monkeypatch.setattr(provider_resilience, "get_hermes_home", lambda: tmp_path)
    original_time = provider_resilience.time.time
    provider_resilience.open_circuit(RuntimeError("502 upstream"))
    monkeypatch.setattr(provider_resilience.time, "time", lambda: original_time() + 601)
    assert provider_resilience.circuit_state()["state"] == "half_open"
    provider_resilience._mark_probe_result(success=True)
    assert provider_resilience.circuit_state()["state"] == "half_open"
    provider_resilience._mark_probe_result(success=True)
    assert provider_resilience.circuit_state()["state"] == "closed"


def test_wecom_agnes_patch_skips_extra_primary_recovery_and_uses_fallback(tmp_path, monkeypatch):
    import sys
    import types
    from plugins.tuoguan_core import provider_resilience

    class FakeAgent:
        def __init__(self):
            self.platform = "wecom_callback"
            self.model = "agnes-2.5-flash"
            self.base_url = "https://apihub.agnes-ai.com/v1"
            self._primary_runtime = {"model": self.model, "base_url": self.base_url}
            self._api_max_retries = 2
            self.fallback_calls = 0

        def _restore_primary_runtime(self):
            return False

        def _try_recover_primary_transport(self, _error, *, retry_count, max_retries):
            return True

        def _try_activate_fallback(self, _reason=None):
            self.fallback_calls += 1
            return True

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(provider_resilience, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(provider_resilience, "_PATCHED", False)

    assert provider_resilience.install_hermes_model_resilience_patch()
    agent = FakeAgent()
    assert agent._try_recover_primary_transport(RuntimeError("TLS timeout"), retry_count=1, max_retries=1) is False
    assert provider_resilience.circuit_state()["state"] == "open"
    agent._restore_primary_runtime()
    assert agent._api_max_retries == 1
    assert agent.fallback_calls == 1


def test_plugin_blocks_any_more_business_tools_after_authoritative_result(tmp_path):
    import plugins.tuoguan_core as plugin
    from plugins.tuoguan_core.runtime_foundation import (
        begin_inbound,
        clear_runtime_state,
        inject_model_context,
        observe_tool_result,
    )
    from plugins.tuoguan_core.store import TuoguanStore

    manual = tmp_path / "manual_context"
    manual.mkdir()
    (manual / "hermes_model_context_injection_allowlist_v1.json").write_text(
        json.dumps({"runtime_foundation": {"enabled": True}}), encoding="utf-8",
    )
    store = TuoguanStore(tmp_path)
    clear_runtime_state()
    begin_inbound(
        store=store, message_id="terminal-message", conversation_id="teacher-1",
        user_id="teacher-1", role="teacher", raw_text="查正式托管学生",
    )
    assert inject_model_context(
        session_id="terminal-session", sender_id="teacher-1", user_message="查正式托管学生",
    )
    observe_tool_result(
        session_id="terminal-session", tool_name="tuoguan_query_students",
        args={"query_scope": "regular"},
        result={"ok": True, "data": {"rendered_text": "正式托管学生共10名"}},
    )
    plugin._reset_turn_tool_budget("terminal-session")

    blocked = plugin._on_pre_tool_call(
        session_id="terminal-session", tool_name="tuoguan_students",
        args={"operation": "query_student_service_relations", "arguments": {}},
    )
    assert blocked and blocked["reason"] == "terminal_tool_result_already_recorded"

    initial = plugin._on_llm_request_middleware(
        platform="wecom_callback", session_id="new-session",
        request={"model": "agnes-2.5-flash", "tools": [{"type": "function"}]},
    )
    assert initial["request"]["parallel_tool_calls"] is False
    assert "tools" in initial["request"]

    finalized = plugin._on_llm_request_middleware(
        platform="wecom_callback", session_id="terminal-session",
        request={"model": "agnes-2.5-flash", "tools": [{"type": "function"}]},
    )
    assert finalized["request"]["parallel_tool_calls"] is False
    assert finalized["request"]["tool_choice"] == "none"
    assert "tools" not in finalized["request"]

    plugin._reset_turn_tool_budget("budget-session")
    for index in range(4):
        assert plugin._guard_turn_tool_call(
            "budget-session", tool_name="tuoguan_query_tasks", args={"index": index},
        ) is None
    exhausted = plugin._on_llm_request_middleware(
        platform="wecom_callback", session_id="budget-session",
        request={"model": "agnes-2.5-flash", "tools": [{"type": "function"}]},
    )
    assert exhausted["reason"] == "tool_budget_requires_final_reply"
    assert exhausted["request"]["tool_choice"] == "none"
    assert "tools" not in exhausted["request"]


@pytest.mark.asyncio
async def test_wecom_empty_model_result_gets_visible_chinese_failure_receipt():
    from plugins.platforms.wecom.callback_adapter import WecomCallbackAdapter

    adapter = WecomCallbackAdapter.__new__(WecomCallbackAdapter)

    async def empty_handler(_event):
        return None

    adapter.set_message_handler(empty_handler)
    response = await adapter._message_handler(_event())

    assert "模型服务暂时不稳定" in response
    assert "继续" not in response


@pytest.mark.asyncio
async def test_wecom_handler_error_does_not_leave_user_in_silence():
    from plugins.platforms.wecom.callback_adapter import WecomCallbackAdapter

    adapter = WecomCallbackAdapter.__new__(WecomCallbackAdapter)

    async def failed_handler(_event):
        raise RuntimeError("provider timeout")

    adapter.set_message_handler(failed_handler)
    response = await adapter._message_handler(_event())
    assert "模型服务暂时不稳定" in response


@pytest.mark.asyncio
async def test_wecom_slow_model_turn_gets_bounded_visible_failure(monkeypatch):
    from plugins.platforms.wecom import callback_adapter

    adapter = callback_adapter.WecomCallbackAdapter.__new__(callback_adapter.WecomCallbackAdapter)
    monkeypatch.setattr(callback_adapter, "_model_turn_timeout_seconds", lambda: 0.01)

    async def slow_handler(_event):
        await asyncio.sleep(0.05)
        return "不应送达"

    adapter.set_message_handler(slow_handler)
    response = await adapter._message_handler(_event())

    assert "模型服务暂时不稳定" in response
    assert "不应送达" not in response


@pytest.mark.asyncio
async def test_wecom_cancelled_handler_remains_cancellable():
    from plugins.platforms.wecom.callback_adapter import WecomCallbackAdapter

    adapter = WecomCallbackAdapter.__new__(WecomCallbackAdapter)

    async def cancelled_handler(_event):
        raise asyncio.CancelledError

    adapter.set_message_handler(cancelled_handler)
    with pytest.raises(asyncio.CancelledError):
        await adapter._message_handler(_event())


@pytest.mark.asyncio
async def test_wecom_valid_reply_and_command_none_are_not_rewritten():
    from plugins.platforms.wecom.callback_adapter import WecomCallbackAdapter

    adapter = WecomCallbackAdapter.__new__(WecomCallbackAdapter)

    async def normal_handler(_event):
        return "在的，我是小优。"

    adapter.set_message_handler(normal_handler)
    assert await adapter._message_handler(_event()) == "在的，我是小优。"

    async def command_handler(_event):
        return None

    adapter.set_message_handler(command_handler)
    assert await adapter._message_handler(_event("/status")) is None


def test_core_contract_keeps_simple_greetings_lightweight():
    from plugins.tuoguan_core import _xiaoyou_core_skill_context
    from plugins.tuoguan_core.models import UserIdentity

    identity = UserIdentity(
        platform="wecom_callback",
        platform_user_id="CeShi",
        canonical_user_id="CeShi",
        person_name="李老师",
        role="teacher",
        approval_state="approved",
    )
    context = _xiaoyou_core_skill_context(identity=identity)
    assert "简单问候" in context
    assert "不要调用工具" in context
    assert "同一参数不得重复查询" in context
