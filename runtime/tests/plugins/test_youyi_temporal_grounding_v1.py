from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from gateway.config import Platform


def _write_json(path: Path, name: str, payload) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_business_due_parser_respects_explicit_date_and_daypart():
    from plugins.tuoguan_core.temporal_grounding import parse_business_due_at

    now = datetime(2026, 8, 9, 18, 0, tzinfo=timezone(timedelta(hours=8)))

    assert parse_business_due_at("8月9号晚上跟小金家长沟通", now=now) == "2026-08-09T20:00:00+08:00"
    assert parse_business_due_at("明天早上8点汇报沟通结果", now=now) == "2026-08-10T08:00:00+08:00"
    assert parse_business_due_at("两小时后提醒李老师", now=now) == "2026-08-09T20:00:00+08:00"


def test_temporal_context_ignores_stale_task_focus_and_blocks_bedtime_language(tmp_path):
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.temporal_grounding import build_temporal_grounding_context

    _write_json(tmp_path, "tasks.json", [])
    _write_json(
        tmp_path,
        "active_task_context.json",
        {
            "teacher1": {
                "task_id": "task_old",
                "task_title": "8月5日旧任务",
                "expires_at": "2026-08-11T20:00:00+08:00",
            }
        },
    )
    _write_json(
        tmp_path,
        "pending_next_task_context.json",
        {
            "teacher1": {
                "task_id": "task_old",
                "task_title": "8月5日旧任务",
                "expires_at": "2026-08-11T20:00:00+08:00",
            }
        },
    )
    _write_json(
        tmp_path,
        "model_focus.json",
        {
            "agent:main:wecom_callback:dm:corp1:teacher1": {
                "task_id": "task_old",
                "student_name": "小金",
                "focus_source": "explicit_task_interaction",
                "focus_expires_at": "2026-08-09T18:34:00+08:00",
            }
        },
    )
    store = TuoguanStore(tmp_path)
    identity = UserIdentity("wecom", "teacher1", "teacher1", "李老师", "teacher", "approved")
    now = datetime(2026, 8, 9, 18, 0, tzinfo=timezone(timedelta(hours=8)))

    context = build_temporal_grounding_context(
        store,
        identity=identity,
        raw_text="这个任务已经完成",
        session_id="agent:main:wecom_callback:dm:corp1:teacher1",
        chat_id="corp1",
        now=now,
    )

    assert "2026-08-09T18:00:00+08:00" in context
    assert "现在不是睡前收尾时段" in context
    assert "不要说“晚安”“安心睡觉”" in context
    assert "当前账号没有开放任务" in context
    assert "模型任务焦点已失效：task_old" in context
    assert "历史任务焦点已失效：task_old" in context
    assert "历史下一个任务提示已失效：task_old" in context
    assert "不能声称状态已更新、提醒已停止或任务已闭环" in context


def test_pre_llm_injects_temporal_grounding_for_generic_task_completion(tmp_path, monkeypatch):
    import plugins.tuoguan_core as plugin
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.runtime_foundation import clear_runtime_state
    from plugins.tuoguan_core.store import TuoguanStore

    clear_runtime_state()
    _write_json(tmp_path, "tasks.json", [])
    _write_json(
        tmp_path,
        "active_task_context.json",
        {"teacher1": {"task_id": "task_old", "expires_at": "2026-08-11T20:00:00+08:00"}},
    )
    store = TuoguanStore(tmp_path)
    identity = UserIdentity("wecom", "teacher1", "teacher1", "李老师", "teacher", "approved")
    fake_router = SimpleNamespace(
        store=store,
        identities=SimpleNamespace(resolve=lambda *args, **kwargs: identity),
    )
    monkeypatch.setattr(plugin, "_router", lambda: fake_router)

    result = plugin._on_pre_llm_call(
        platform=Platform.WECOM_CALLBACK,
        sender_id="wecom_callback:teacher1",
        session_id="session-time",
        turn_id="turn-time",
        user_message="这个任务已经完成",
    )

    assert result is not None
    assert "小优当前时间锚点" in result["context"]
    assert "当前账号没有开放任务" in result["context"]
    assert "当前轮写入规则" in result["context"]


def test_unverified_task_completion_claim_is_sanitized_without_tool():
    from plugins.tuoguan_core.runtime_foundation import _sanitize_external_reply

    reply = (
        "任务正式闭环了！状态已更新为\"已完成\"，不会再给你发提醒了。\n"
        "小金家长沟通记录已存档，小金档案已更新，可以安心睡觉了，晚安李老师。"
    )

    sanitized = _sanitize_external_reply(
        reply,
        verified_state_change=False,
        used_trusted_tool=False,
        actor_role="teacher",
    )

    assert "状态已更新" not in sanitized
    assert "已存档" not in sanitized
    assert "任务工具确认" in sanitized
