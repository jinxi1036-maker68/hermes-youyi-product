from __future__ import annotations

import asyncio

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

    for index in range(1, 12):
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
    assert turn_tool_budget_snapshot(session_id)["count"] == 12


@pytest.mark.asyncio
async def test_wecom_empty_model_result_gets_visible_chinese_failure_receipt():
    from plugins.platforms.wecom.callback_adapter import WecomCallbackAdapter

    adapter = WecomCallbackAdapter.__new__(WecomCallbackAdapter)

    async def empty_handler(_event):
        return None

    adapter.set_message_handler(empty_handler)
    response = await adapter._message_handler(_event())

    assert "没有拿到可靠结果" in response
    assert "继续" in response


@pytest.mark.asyncio
async def test_wecom_handler_error_does_not_leave_user_in_silence():
    from plugins.platforms.wecom.callback_adapter import WecomCallbackAdapter

    adapter = WecomCallbackAdapter.__new__(WecomCallbackAdapter)

    async def failed_handler(_event):
        raise RuntimeError("provider timeout")

    adapter.set_message_handler(failed_handler)
    response = await adapter._message_handler(_event())
    assert "超时或中断" in response


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
