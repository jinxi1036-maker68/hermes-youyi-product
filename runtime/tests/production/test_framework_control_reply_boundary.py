"""Regression for framework control text leaking through normal WeCom replies."""

from __future__ import annotations

import tuoguan_core as plugin


COMPACTION_REPLY = "[System: compacted (tokens: 34561 → 10343)]"


def test_compaction_control_text_is_not_a_business_reply() -> None:
    assert plugin._is_framework_compaction_reply(COMPACTION_REPLY)
    assert plugin._is_framework_compaction_reply("[system: compacted (tokens: 34,561 -> 10,343)]")
    assert plugin._is_framework_terminal_reply_unavailable(COMPACTION_REPLY)
    assert not plugin._is_framework_compaction_reply("上下文已经整理好了，我继续处理。")


def test_wecom_compaction_control_text_becomes_truthful_human_failure(monkeypatch) -> None:
    staged: list[dict[str, object]] = []
    guards: list[tuple[str, str, str]] = []

    monkeypatch.setattr(
        plugin,
        "_stage_direct_reply_terminal",
        lambda **kwargs: staged.append(dict(kwargs)),
    )
    monkeypatch.setattr(
        plugin,
        "_record_trace_guard_event",
        lambda session_id, *, guard, result: guards.append((session_id, guard, result)),
    )

    result = plugin._on_transform_llm_output(
        platform="wecom_callback",
        session_id="session-current-turn",
        turn_id="turn-current-turn",
        response_text=COMPACTION_REPLY,
    )

    assert result == "我刚才没有完成这次处理，请重新发一次刚才的指令。"
    assert guards == [
        ("session-current-turn", "framework_control_reply", "compaction_status_blocked"),
    ]
    assert len(staged) == 1
    assert staged[0]["terminal_state"] == "failed"
    assert staged[0]["provider_succeeded"] is False
    assert staged[0]["final_reply_text"] == ""
    assert COMPACTION_REPLY not in result
