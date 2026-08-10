"""Tests for the tuoguan_core gateway plugin entrypoint."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


@pytest.fixture(autouse=True)
def _isolated_tuoguan_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    data_dir = hermes_home / "tuoguan-data"
    data_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_TUOGUAN_DATA_DIR", str(data_dir))
    monkeypatch.setenv("HERMES_TUOGUAN_DAILY_PUSH_ENABLED", "0")
    yield data_dir


def _write_json(path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _append_jsonl(path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _seed_teacher(data_dir) -> None:
    _write_json(
        data_dir / "wecom_whitelist.json",
        """
        {
          "allowed_users": ["teacher1"],
          "super_users": [],
          "pending_users": [],
          "rejected_users": [],
          "user_roles": {"teacher1": "teacher"}
        }
        """,
    )
    _write_json(data_dir / "teacher_wecom_map.json", "{\"王老师\": \"teacher1\"}")
    _write_json(data_dir / "teacher_feishu_map.json", "{}")
    _write_json(data_dir / "students.json", "{}")
    _write_json(data_dir / "tasks.json", "[]")


def _seed_payroll_users(data_dir) -> None:
    _write_json(
        data_dir / "wecom_whitelist.json",
        """
        {
          "allowed_users": ["teacher1", "manager1"],
          "super_users": ["boss1"],
          "pending_users": [],
          "rejected_users": [],
          "user_roles": {
            "teacher1": "teacher",
            "manager1": "manager",
            "boss1": "boss"
          }
        }
        """,
    )
    _write_json(
        data_dir / "teacher_wecom_map.json",
        "{\"王老师\": \"teacher1\", \"赵店长\": \"manager1\", \"老板\": \"boss1\"}",
    )
    _write_json(data_dir / "teacher_feishu_map.json", "{}")
    _write_json(data_dir / "students.json", "{}")
    _write_json(data_dir / "tasks.json", "[]")


def _seed_manual_assignment_users(data_dir) -> None:
    _seed_payroll_users(data_dir)
    _write_json(
        data_dir / "students.json",
        """
        {
          "周温暖": {"teacher": "teacher1", "campus_id": "main"}
        }
        """,
    )


def _seed_summer_mode_users(data_dir) -> None:
    _seed_payroll_users(data_dir)
    _write_json(
        data_dir / "programs.json",
        json.dumps({
            "regular_tuoguan": {"id": "regular_tuoguan", "status": "frozen_readonly", "is_active_default": False},
            "summer_2026": {"id": "summer_2026", "status": "active", "is_active_default": True},
        }, ensure_ascii=False),
    )
    _write_json(
        data_dir / "staff.json",
        json.dumps({
            "teacher1": {"role": "teacher", "program_ids": ["summer_2026"]},
            "manager1": {"role": "manager", "program_ids": ["regular_tuoguan"], "campus_ids": ["main"]},
        }, ensure_ascii=False),
    )
    _write_json(
        data_dir / "students.json",
        json.dumps({
            "暑假小明": {
                "teacher": "teacher1", "campus_id": "summer_2026", "summer_status": "active",
                "program_enrollments": [{"program_id": "summer_2026", "status": "active"}],
            },
            "托管小红": {
                "teacher": "manager1", "campus_id": "main",
                "program_enrollments": [{"program_id": "regular_tuoguan", "status": "active"}],
            },
        }, ensure_ascii=False),
    )


def _event(text: str, *, platform: Platform = Platform.WECOM_CALLBACK) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_id="msg-1",
        source=SessionSource(
            platform=platform,
            user_id="teacher1",
            chat_id="wwcorp:teacher1",
            user_name="王老师",
            chat_type="dm",
        ),
    )


def _event_from(
    text: str,
    user_id: str,
    user_name: str,
    *,
    platform: Platform = Platform.WECOM_CALLBACK,
) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_id="msg-1",
        source=SessionSource(
            platform=platform,
            user_id=user_id,
            chat_id=f"wwcorp:{user_id}",
            user_name=user_name,
            chat_type="dm",
        ),
    )


def _gateway(adapter):
    return SimpleNamespace(adapters={Platform.WECOM_CALLBACK: adapter})


def test_register_keeps_model_mainline_hooks_only():
    import plugins.tuoguan_core as plugin

    hooks = []
    tools = []
    ctx = SimpleNamespace(
        register_hook=lambda name, fn: hooks.append((name, fn)),
        register_tool=lambda **kwargs: tools.append(kwargs),
    )

    plugin.register(ctx)

    assert hooks == [
        ("pre_llm_call", plugin._on_pre_llm_call),
        ("post_tool_call", plugin._on_post_tool_call),
        ("post_gateway_response", plugin._on_post_gateway_response),
    ]
    forbidden = {
        "pre_gateway_dispatch",
        "pre_tool_call",
        "transform_llm_output",
    }
    assert not (forbidden & {name for name, _fn in hooks})
    names = {item["name"] for item in tools}
    assert "tuoguan_context" in names
    assert "tuoguan_record_student" in names
    assert "tuoguan_report_safety_event" in names
    assert "tuoguan_submit_learning_candidate" in names


def test_pre_llm_recalls_recent_owner_attention_without_routing(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    _append_jsonl(
        _isolated_tuoguan_home / "attention_threads.jsonl",
        [
            {
                "record_type": "attention_thread",
                "attention_id": "attention:today:goal-renewal",
                "status": "sent",
                "target_user_id": "boss1",
                "focus_key": "goal:renewal",
                "question_text": "需要确认服务类型、主责老师、经营优先序。",
                "needed_facts": ["服务类型", "主责老师", "经营优先序"],
                "created_at": now,
            }
        ],
    )

    result = plugin._on_pre_llm_call(
        platform=Platform.WECOM_CALLBACK,
        sender_id="boss1",
        user_message="你需要我怎么确认",
        session_id="wwcorp:boss1",
    )

    assert result is not None
    context = result["context"]
    assert "优益主动提问回复锚点" in context
    assert "goal:renewal" in context
    assert "需要确认服务类型、主责老师、经营优先序" in context
    assert "不是 Router" in context
    assert "next_tool" not in context
    assert "workflow_step" not in context


def test_pre_llm_does_not_attach_owner_attention_to_unrelated_query(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    _write_json(
        _isolated_tuoguan_home / "notification_outbox.json",
        json.dumps([
            {
                "id": "autonomous_owner_attention:today:goal-renewal",
                "status": "sent",
                "notification_type": "autonomous_owner_attention",
                "touser": "boss1",
                "target_user_id": "boss1",
                "focus_key": "goal:renewal",
                "summary": "续费目标卡在责任确认。",
                "content": "需要确认服务类型、主责老师、经营优先序。",
                "sent_at": now,
            }
        ], ensure_ascii=False),
    )

    result = plugin._on_pre_llm_call(
        platform=Platform.WECOM_CALLBACK,
        sender_id="boss1",
        user_message="查一下李老师",
        session_id="wwcorp:boss1",
    )

    context = (result or {}).get("context", "")
    assert "优益最近主动外发消息锚点" not in context
    assert "autonomous_owner_attention:today:goal-renewal" not in context


def test_pre_llm_recalls_recent_external_learning_report_for_follow_up(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    _write_json(
        _isolated_tuoguan_home / "notification_outbox.json",
        json.dumps([
            {
                "id": "external_learning_report:20260803:weekly_industry",
                "status": "sent",
                "notification_type": "external_learning_report",
                "action": "external_learning_weekly_industry",
                "touser": "boss1",
                "target_user_id": "boss1",
                "summary": "小优托管行业学习周报",
                "content": "金总，我做了一轮托管/教培行业公开学习，给你汇报一下本周可参考的东西。我的判断：优先把外部方法转成续费证据、家校沟通话术、老师减负素材。",
                "sent_at": now,
            }
        ], ensure_ascii=False),
    )

    result = plugin._on_pre_llm_call(
        platform=Platform.WECOM_CALLBACK,
        sender_id="boss1",
        user_message="这里边的内容你都总结了吗？",
        session_id="wwcorp:boss1",
    )

    assert result is not None
    context = result["context"]
    assert "优益最近主动外发消息锚点" in context
    assert "external_learning_report:20260803:weekly_industry" in context
    assert "托管/教培行业公开学习" in context
    assert "强衔接规则" in context
    assert "next_tool" not in context
    assert "workflow_step" not in context


def test_pre_llm_recent_external_learning_follow_up_suppresses_old_attention(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    _write_json(
        _isolated_tuoguan_home / "notification_outbox.json",
        json.dumps([
            {
                "id": "external_learning_report:20260810:weekly_industry",
                "status": "sent",
                "notification_type": "external_learning_report",
                "action": "external_learning_weekly_industry",
                "touser": "boss1",
                "target_user_id": "boss1",
                "summary": "小优托管行业学习周报",
                "content": "金总，我做了一轮托管/教培行业公开学习。我的判断：转成续费证据、家校沟通话术、老师减负素材。",
                "sent_at": now,
            }
        ], ensure_ascii=False),
    )
    _append_jsonl(
        _isolated_tuoguan_home / "attention_threads.jsonl",
        [
            {
                "record_type": "attention_thread",
                "attention_id": "attention:old",
                "focus_key": "goal:old",
                "target_user_id": "boss1",
                "status": "sent",
                "question_text": "旧问题：她是冯老师还是李老师？",
                "created_at": "2026-08-09T11:30:51+08:00",
                "updated_at": "2026-08-09T11:30:51+08:00",
            }
        ],
    )

    result = plugin._on_pre_llm_call(
        platform=Platform.WECOM_CALLBACK,
        sender_id="boss1",
        user_message="你给讲讲，它里面都具体讲了什么内容",
        session_id="wwcorp:boss1",
    )

    assert result is not None
    context = result["context"]
    assert "优益最近主动外发消息锚点" in context
    assert "external_learning_report:20260810:weekly_industry" in context
    assert "优益主动提问回复锚点" not in context
    assert "她是冯老师还是李老师" not in context


def test_pre_llm_does_not_attach_recent_external_report_to_unrelated_query(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    _write_json(
        _isolated_tuoguan_home / "notification_outbox.json",
        json.dumps([
            {
                "id": "external_learning_report:20260803:weekly_industry",
                "status": "sent",
                "notification_type": "external_learning_report",
                "action": "external_learning_weekly_industry",
                "touser": "boss1",
                "target_user_id": "boss1",
                "summary": "小优托管行业学习周报",
                "content": "金总，我做了一轮托管/教培行业公开学习。",
                "sent_at": now,
            }
        ], ensure_ascii=False),
    )

    result = plugin._on_pre_llm_call(
        platform=Platform.WECOM_CALLBACK,
        sender_id="boss1",
        user_message="查一下李老师",
        session_id="wwcorp:boss1",
    )

    context = (result or {}).get("context", "")
    assert "优益最近主动外发消息锚点" not in context
    assert "external_learning_report:20260803:weekly_industry" not in context


def test_pre_llm_injects_term_boundary_for_related_goal_chat(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "academic_term_state.json",
        json.dumps({
            "service_relation_policy": "defer_until_new_term",
            "confirmation_window_start": "2026-08-25",
            "confirmation_window_end": "2026-09-10",
        }, ensure_ascii=False),
    )

    result = plugin._on_pre_llm_call(
        platform=Platform.WECOM_CALLBACK,
        sender_id="boss1",
        user_message="同意，你先把候选材料整理出来，重点看价格、转校，等开学这三类",
        session_id="wwcorp:boss1",
    )

    assert result is not None
    context = result["context"]
    assert "优益当前学期边界材料" in context
    assert "不要在当前回复里追问具体学生的主责老师、服务类型或开学后责任归属" in context
    assert "不是 Router" in context
    assert "next_tool" not in context
    assert "workflow_step" not in context


def test_pre_llm_does_not_inject_term_boundary_for_plain_chat(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "academic_term_state.json",
        json.dumps({"service_relation_policy": "defer_until_new_term"}, ensure_ascii=False),
    )

    result = plugin._on_pre_llm_call(
        platform=Platform.WECOM_CALLBACK,
        sender_id="boss1",
        user_message="在线吗",
        session_id="wwcorp:boss1",
    )

    assert result is None


def test_pre_llm_injects_confirmed_public_employee_name(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "operational_facts.json",
        json.dumps({
            "schema_version": 1,
            "tenant_id": "youyi_tuoguan",
            "facts": [
                {
                    "fact_id": "fact_name_xiaoyou",
                    "tenant_id": "youyi_tuoguan",
                    "fact_type": "owner_rule",
                    "subject": "数字员工称呼",
                    "value": "对外称呼为\"小优\"",
                    "scope": "institution",
                    "risk_level": "low",
                    "status": "active",
                    "source_text": "金总说：我给你起一个名字，你以后叫小优",
                    "confirmed_by": "boss1",
                    "confirmed_at": "2026-08-01T21:37:53+08:00",
                }
            ],
        }, ensure_ascii=False),
    )

    result = plugin._on_pre_llm_call(
        platform=Platform.WECOM_CALLBACK,
        sender_id="boss1",
        user_message="在线吗",
        session_id="wwcorp:boss1",
    )

    assert result is not None
    context = result["context"]
    assert "优益数字员工身份称呼" in context
    assert "优先自称“小优”" in context
    assert "Hermes 只作为内部产品/架构名称" in context
    assert "不是 Router" in context
    assert "next_tool" not in context
    assert "workflow_step" not in context


def test_retired_pre_gateway_dispatch_cannot_own_business_message(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    _seed_teacher(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event("查一下自己班孩子"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )

    assert result is None
    adapter.send.assert_not_called()


def test_tuoguan_record_tool_is_idempotent(_isolated_tuoguan_home):
    import json
    from plugins.tuoguan_core.tools import TOOLS

    _seed_teacher(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "students.json",
        """
        {
          "周温暖": {"teacher": "teacher1", "campus_id": "main"}
        }
        """,
    )
    handler = {name: fn for name, _schema, fn in TOOLS}["tuoguan_record_student"]
    args = {
        "platform": "wecom_callback",
        "user_id": "teacher1",
        "user_name": "王老师",
        "student_name": "周温暖",
        "content": "今天数学作业完成认真",
        "operation_id": "msg-123",
    }

    first = json.loads(handler(args))
    second = json.loads(handler(args))

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["already_applied"] is True
    records = json.loads((_isolated_tuoguan_home / "records.json").read_text(encoding="utf-8"))
    assert len(records) == 1


@pytest.mark.asyncio
async def test_wecom_callback_business_reply_short_circuits_model(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_teacher(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event("帮助"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    adapter.send.assert_awaited_once()
    args, kwargs = adapter.send.await_args
    assert args[0] == "wwcorp:teacher1"
    assert "【老师使用教程】" in args[1]
    assert "记录孩子" in args[1]
    assert kwargs["reply_to"] == "msg-1"
    assert kwargs["metadata"]["handled_by"] == "tuoguan_core"
    assert kwargs["metadata"]["outbound_source"] == "deterministic_fallback"
    assert kwargs["metadata"]["conversation_id"] == "wecom_callback:dm:wwcorp:teacher1"


@pytest.mark.asyncio
async def test_unknown_wecom_short_dm_enters_identity_binding(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_teacher(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("在吗", "new_user_1", "未绑定用户"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, kwargs = adapter.send.await_args
    assert args[0] == "wwcorp:new_user_1"
    assert "还没有绑定到 Hermes" in args[1]
    assert "金总" in args[1]
    assert kwargs["metadata"]["handled_by"] == "tuoguan_core"
    assert kwargs["metadata"]["outbound_source"] == "deterministic_fallback"
    assert kwargs["metadata"]["conversation_id"] == "wecom_callback:dm:wwcorp:new_user_1"
    whitelist = json.loads((_isolated_tuoguan_home / "wecom_whitelist.json").read_text(encoding="utf-8"))
    assert "new_user_1" in whitelist["pending_users"]
    assert whitelist["pending_applications"][0]["last_message"] == "在吗"


@pytest.mark.asyncio
async def test_teacher_cannot_switch_identity_by_saying_i_am_boss(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_teacher(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("我是金总", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, kwargs = adapter.send.await_args
    assert args[0] == "wwcorp:teacher1"
    assert "身份不能通过聊天内容切换" in args[1]
    assert "王老师（老师）" in args[1]
    assert "老板看板" not in args[1]
    assert kwargs["metadata"]["handled_by"] == "tuoguan_core"
    assert kwargs["metadata"]["outbound_source"] == "deterministic_fallback"
    assert kwargs["metadata"]["conversation_id"] == "wecom_callback:dm:wwcorp:teacher1"


@pytest.mark.asyncio
async def test_teacher_greeting_is_identity_safe_and_does_not_fall_to_general_model(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_teacher(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("你好", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, kwargs = adapter.send.await_args
    assert args[0] == "wwcorp:teacher1"
    assert "王老师，你好" in args[1]
    assert "老师" in args[1]
    assert "金总" not in args[1]
    assert kwargs["metadata"]["handled_by"] == "tuoguan_core"
    assert kwargs["metadata"]["outbound_source"] == "deterministic_fallback"
    assert kwargs["metadata"]["conversation_id"] == "wecom_callback:dm:wwcorp:teacher1"


@pytest.mark.asyncio
async def test_boss_general_greeting_falls_through_to_normal_chat(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("你好", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result is None
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_boss_short_ok_without_pending_falls_through_to_normal_chat(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("可以", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result is None
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("打印", "Hermes 不执行打印"),
        ("这个可以吧", "请说明你指的是哪名学生的哪份审核资料"),
    ],
)
async def test_boss_high_risk_short_review_command_requires_context(
    _isolated_tuoguan_home, text, expected
):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from(text, "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert expected in args[1]
    assert "P4-14" not in args[1]
    assert "executed" not in args[1]


@pytest.mark.asyncio
async def test_teacher_can_ask_natural_usage_tutorial(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_teacher(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("这个系统我都可以怎么用", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "【老师使用教程】" in args[1]
    assert "记录孩子" in args[1]
    assert "处理任务" in args[1]
    assert "撤销上一条记录" in args[1]


@pytest.mark.asyncio
async def test_boss_can_ask_natural_usage_tutorial(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("Hermes这个系统我都可以怎么用", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "【老板使用教程】" in args[1]
    assert "老板看板" in args[1]
    assert "安排任务" in args[1]
    assert "确认执行" in args[1]
    assert "普通聊天" in args[1]


@pytest.mark.asyncio
async def test_natural_start_current_task_keeps_followup_in_task_context(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "students.json",
        json.dumps(
            {
                "周温暖": {"teacher": "teacher1", "campus_id": "main"},
                "小陈": {"teacher": "teacher1", "campus_id": "main"},
            },
            ensure_ascii=False,
        ),
    )
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        json.dumps(
            [
                {
                    "id": "task-current-natural",
                    "title": "周温暖本周成长观察",
                    "type": "student_daily",
                    "level": "B",
                    "status": "pending",
                    "student_name": "周温暖",
                    "assignee_userid": "teacher1",
                    "due_at": "2026-06-22T21:00:00",
                    "created_at": "2026-06-22T19:00:00",
                },
                {
                    "id": "task-next-natural",
                    "title": "小陈数学计算跟进",
                    "type": "academic_issue",
                    "level": "B",
                    "status": "pending",
                    "student_name": "小陈",
                    "assignee_userid": "teacher1",
                    "due_at": "2026-06-23T21:00:00",
                    "created_at": "2026-06-22T19:05:00",
                },
            ],
            ensure_ascii=False,
        ),
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("开始处理我现在的任务", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    active = json.loads((_isolated_tuoguan_home / "active_task_context.json").read_text(encoding="utf-8"))
    assert active["teacher1"]["task_id"] == "task-current-natural"

    adapter.send.reset_mock()
    result = plugin._on_pre_gateway_dispatch(
        event=_event_from(
            "他近期作业完成还行，阅读也愿意认真去读。我对他的语文背诵做了提醒，后续明天继续关注背诵情况。",
            "teacher1",
            "王老师",
        ),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    sent = "\n".join(call.args[1] for call in adapter.send.await_args_list)
    assert "日常记录沉淀" not in sent
    assert "任务" in sent or "闭环" in sent
    logs = (_isolated_tuoguan_home / "semantic_route_logs.jsonl").read_text(encoding="utf-8")
    assert '"final_intent": "task_evidence_update"' in logs


@pytest.mark.asyncio
async def test_active_growth_task_treats_life_observation_as_task_evidence(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "students.json",
        json.dumps({"位俊丞": {"teacher": "teacher1", "campus_id": "main"}}, ensure_ascii=False),
    )
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        json.dumps(
            [
                {
                    "id": "task-growth-active",
                    "title": "位俊丞本周成长观察",
                    "type": "student_daily",
                    "level": "B",
                    "status": "active",
                    "student_name": "位俊丞",
                    "assignee_userid": "teacher1",
                    "due_at": "2026-06-22T21:00:00",
                    "created_at": "2026-06-22T19:00:00",
                }
            ],
            ensure_ascii=False,
        ),
    )
    _write_json(
        _isolated_tuoguan_home / "active_task_context.json",
        json.dumps(
            {
                "teacher1": {
                    "user_id": "teacher1",
                    "task_id": "task-growth-active",
                    "student_name": "位俊丞",
                    "task_type": "student_daily",
                    "status": "processing",
                    "started_at": "2026-06-22T20:30:00",
                    "expires_at": "2026-06-22T21:30:00",
                }
            },
            ensure_ascii=False,
        ),
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    evidence_text = (
        "位俊丞这段时间的表现还可以能够主动帮助同学，然后他吃饭的时候也非常的好，"
        "不挑食，每次都能把饭吃完，再一个就是他午休的时候也比较听话，"
        "反正最近的转变非常的好，表现也很好，包括作业的动作够主动的去完成"
    )
    result = plugin._on_pre_gateway_dispatch(
        event=_event_from(evidence_text, "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    sent = "\n".join(call.args[1] for call in adapter.send.await_args_list)
    assert "任务处理情况已记录" in sent
    assert "日常记录沉淀" not in sent
    logs = [
        json.loads(line)
        for line in (_isolated_tuoguan_home / "semantic_route_logs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert logs[-1]["final_intent"] == "task_evidence_update"
    assert logs[-1]["handler"] == "tasks"
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    assert "主动帮助同学" in tasks[0]["evidence_summary"]


@pytest.mark.asyncio
async def test_active_growth_task_with_talk_word_does_not_generate_parent_script(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "students.json",
        json.dumps({"周温暖": {"teacher": "teacher1", "campus_id": "main"}}, ensure_ascii=False),
    )
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        json.dumps(
            [
                {
                    "id": "task-growth-talk-word",
                    "title": "周温暖本周成长观察",
                    "type": "student_daily",
                    "level": "B",
                    "status": "active",
                    "student_name": "周温暖",
                    "assignee_userid": "teacher1",
                    "due_at": "2026-06-22T21:00:00",
                    "created_at": "2026-06-22T19:00:00",
                }
            ],
            ensure_ascii=False,
        ),
    )
    _write_json(
        _isolated_tuoguan_home / "active_task_context.json",
        json.dumps(
            {
                "teacher1": {
                    "user_id": "teacher1",
                    "task_id": "task-growth-talk-word",
                    "student_name": "周温暖",
                    "task_type": "student_daily",
                    "status": "processing",
                    "started_at": "2026-06-22T20:30:00",
                    "expires_at": "2026-06-22T21:30:00",
                }
            },
            ensure_ascii=False,
        ),
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from(
            "近期周温暖的变化比较好，然后能够主动写作业了，但是在午睡的时候还是不听话，"
            "总是喜欢和别人说话，这个我已经提醒了很多次了，下一步还需要多关注，多提醒他",
            "teacher1",
            "王老师",
        ),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    sent = "\n".join(call.args[1] for call in adapter.send.await_args_list)
    assert "任务处理情况已记录" in sent
    assert "发给家长" not in sent
    assert "家长您好" not in sent
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    assert "喜欢和别人说话" in tasks[0]["evidence_summary"]
    assert tasks[0]["coach_stage"] == "collecting_evidence"


@pytest.mark.asyncio
async def test_active_task_meta_reply_does_not_trigger_summer_unknown_student(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    now = datetime.now()
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        json.dumps(
            [
                {
                    "id": "task-growth-meta",
                    "title": "周温暖本周成长观察",
                    "type": "student_daily",
                    "level": "B",
                    "status": "active",
                    "student_name": "周温暖",
                    "assignee_userid": "teacher1",
                    "due_at": (now + timedelta(hours=2)).isoformat(timespec="seconds"),
                    "created_at": (now - timedelta(minutes=30)).isoformat(timespec="seconds"),
                }
            ],
            ensure_ascii=False,
        ),
    )
    _write_json(
        _isolated_tuoguan_home / "active_task_context.json",
        json.dumps(
            {
                "teacher1": {
                    "user_id": "teacher1",
                    "task_id": "task-growth-meta",
                    "student_name": "周温暖",
                    "task_type": "student_daily",
                    "status": "processing",
                    "started_at": (now - timedelta(minutes=10)).isoformat(timespec="seconds"),
                    "expires_at": (now + timedelta(minutes=50)).isoformat(timespec="seconds"),
                }
            },
            ensure_ascii=False,
        ),
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("我是在处理任务", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    sent = "\n".join(call.args[1] for call in adapter.send.await_args_list)
    assert "你当前正在处理" in sent
    assert "未找到" not in sent
    assert not (_isolated_tuoguan_home / "summer_pending_unknown_records.json").exists()


@pytest.mark.asyncio
async def test_concrete_learning_text_is_archived_without_explicit_record_word(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_teacher(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "students.json",
        """
        {
          "周温暖": {"teacher": "teacher1", "campus_id": "main"}
        }
        """,
    )
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event("今天周温暖数学测试拿了98分，值得表扬"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "已记录周温暖" in args[1]
    records = json.loads((_isolated_tuoguan_home / "records.json").read_text(encoding="utf-8"))
    assert records[0]["student_name"] == "周温暖"


@pytest.mark.asyncio
async def test_boss_can_manually_assign_teacher_task(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_manual_assignment_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("安排王老师明天前跟进周温暖家长沟通，A级", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    assert len(tasks) == 1
    task = tasks[0]
    assert task["source_type"] == "manual_assignment"
    assert task["source_label"] == "老板安排"
    assert task["assigned_by"] == "boss1"
    assert task["assignee_userid"] == "teacher1"
    assert task["student_name"] == "周温暖"
    assert task["level"] == "A"
    assert task["type"] == "parent_anxiety"
    assert task["due_at"]
    sent = [call.args for call in adapter.send.await_args_list]
    assert any(args[0] == "wwcorp:boss1" and "已安排A级任务给王老师" in args[1] for args in sent)
    assert any(args[0] == "teacher1" and "【A级任务安排】" in args[1] for args in sent)


@pytest.mark.asyncio
async def test_boss_teacher_handover_requires_confirmation_before_execution(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_manual_assignment_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("王老师离职，张老师从7月1日起接手王老师负责的孩子", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "不能直接执行" in args[1]
    assert "确认执行" in args[1]
    pending = json.loads((_isolated_tuoguan_home / "pending_config_changes.json").read_text(encoding="utf-8"))
    assert pending[0]["kind"] == "teacher_handover"
    assert pending[0]["status"] == "waiting_confirmation"
    students = json.loads((_isolated_tuoguan_home / "students.json").read_text(encoding="utf-8"))
    assert students["周温暖"]["teacher"] == "teacher1"

    adapter.send.reset_mock()
    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("确认执行", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "已确认配置变更" in args[1]
    pending = json.loads((_isolated_tuoguan_home / "pending_config_changes.json").read_text(encoding="utf-8"))
    assert pending[0]["status"] == "confirmed_pending_execution"
    assert (_isolated_tuoguan_home / "config_change_audit.jsonl").exists()


@pytest.mark.asyncio
async def test_teacher_cannot_request_identity_or_handover_config_change(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_manual_assignment_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("新增老师张老师", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "必须由金总/老板账号发起并确认" in args[1]
    assert not (_isolated_tuoguan_home / "pending_config_changes.json").exists()


def test_summer_bulk_import_creates_students_and_flags_issues(_isolated_tuoguan_home):
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.summer_enrollment import bulk_import_summer_students

    store = TuoguanStore(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "students.json",
        json.dumps(
            {
                "小金": {"grade": "二年级", "phone": "13800000000", "teacher": "teacher1", "status": "active"},
                "李明": {"grade": "三年级", "phone": "13900000000", "teacher": "teacher1", "status": "active"},
            },
            ensure_ascii=False,
        ),
    )

    result = bulk_import_summer_students(
        store,
        [
            {"孩子姓名": "王小明", "年级": "三年级", "暑假班分组": "三四年级组", "家长联系电话": "13811112222", "特殊注意事项": "花生过敏"},
            {"孩子姓名": "李四", "年级": "五年级", "暑假班分组": "五六年级组", "家长联系电话": "13822223333", "特殊注意事项": "不能剧烈运动"},
            {"孩子姓名": "张三", "年级": "一年级", "暑假班分组": "一二年级组", "家长联系电话": "13833334444"},
            {"孩子姓名": "赵六", "年级": "一年级", "暑假班分组": "一二年级组", "家长联系电话": ""},
            {"孩子姓名": "小金", "年级": "二年级", "暑假班分组": "一二年级组", "家长联系电话": "13844445555"},
            {"孩子姓名": "李小明", "年级": "三年级", "暑假班分组": "三四年级组", "家长联系电话": "13900000000"},
        ],
        actor_userid="boss1",
        actor_role="boss",
        confirmed=True,
    )

    assert result["ok"] is True
    assert result["created_count"] == 4
    assert result["phone_missing_count"] == 1
    assert result["duplicate_pending_count"] == 2
    students = json.loads((_isolated_tuoguan_home / "students.json").read_text(encoding="utf-8"))
    assert "王小明" in students
    assert students["王小明"]["program_enrollments"][0]["program_id"] == "summer_2026"
    assert students["王小明"]["special_attention"]["safety"] == ["花生过敏"]
    issues = json.loads((_isolated_tuoguan_home / "summer_import_issues.json").read_text(encoding="utf-8"))
    assert any(item["issue_type"] == "missing_phone" and item["import_student"]["student_name"] == "赵六" for item in issues)
    assert any(item["issue_type"] == "duplicate_candidate" and item["import_student"]["student_name"] == "小金" for item in issues)
    assert any(item["issue_type"] == "duplicate_candidate" and item["import_student"]["student_name"] == "李小明" for item in issues)


@pytest.mark.asyncio
async def test_summer_unknown_student_record_is_pending_not_created(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "staff.json",
        json.dumps({"teacher1": {"role": "teacher", "program_ids": ["summer_2026"]}}, ensure_ascii=False),
    )
    _write_json(_isolated_tuoguan_home / "students.json", "{}")
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("王小明今天数学课计算速度比较快。", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    sent = "\n".join(call.args[1] for call in adapter.send.await_args_list)
    assert "未找到“王小明”的暑假班学生档案" in sent
    pending = json.loads((_isolated_tuoguan_home / "pending_unknown_summer_records.json").read_text(encoding="utf-8"))
    assert pending[0]["student_name"] == "王小明"
    students = json.loads((_isolated_tuoguan_home / "students.json").read_text(encoding="utf-8"))
    assert "王小明" not in students


@pytest.mark.asyncio
async def test_summer_safety_attention_reminders_on_records(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.summer_enrollment import bulk_import_summer_students

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "staff.json",
        json.dumps({"teacher1": {"role": "teacher", "program_ids": ["summer_2026"]}}, ensure_ascii=False),
    )
    store = TuoguanStore(_isolated_tuoguan_home)
    bulk_import_summer_students(
        store,
        [
            {"孩子姓名": "王小明", "年级": "三年级", "暑假班分组": "三四年级组", "家长联系电话": "13811112222", "特殊注意事项": "花生过敏"},
            {"孩子姓名": "李四", "年级": "五年级", "暑假班分组": "五六年级组", "家长联系电话": "13822223333", "特殊注意事项": "不能剧烈运动"},
        ],
        actor_userid="boss1",
        actor_role="boss",
        confirmed=True,
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("王小明今天午餐米饭吃完了，青菜也吃了一些。", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "花生过敏" in args[1]
    assert "确认今日餐食无相关风险" in args[1]

    adapter.send.reset_mock()
    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("李四今天科学实验活动参与积极，我提醒他降低强度。", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "不能剧烈运动" in args[1]
    assert "降低强度" in args[1]


def test_summer_import_status_in_manager_and_boss_dashboard(_isolated_tuoguan_home):
    from plugins.tuoguan_core.dashboard_builder import refresh_dashboard_cache
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.summer_enrollment import bulk_import_summer_students, remember_unknown_summer_record

    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "staff.json",
        json.dumps({"manager1": {"role": "manager", "campus_ids": ["summer_2026", "main"]}}, ensure_ascii=False),
    )
    _write_json(
        _isolated_tuoguan_home / "students.json",
        json.dumps({"小金": {"grade": "二年级", "phone": "13800000000", "teacher": "teacher1", "status": "active"}}, ensure_ascii=False),
    )
    store = TuoguanStore(_isolated_tuoguan_home)
    bulk_import_summer_students(
        store,
        [
            {"孩子姓名": "王小明", "年级": "三年级", "暑假班分组": "三四年级组", "家长联系电话": "13811112222", "特殊注意事项": "花生过敏"},
            {"孩子姓名": "赵六", "年级": "一年级", "暑假班分组": "一二年级组", "家长联系电话": ""},
            {"孩子姓名": "小金", "年级": "二年级", "暑假班分组": "一二年级组", "家长联系电话": "13844445555"},
        ],
        actor_userid="boss1",
        actor_role="boss",
        confirmed=True,
    )
    remember_unknown_summer_record(
        store,
        student_name="未入库孩子",
        text="未入库孩子今天数学课表现不错。",
        teacher_userid="teacher1",
        teacher_name="王老师",
    )

    refresh_dashboard_cache(store, create_operation_tasks=False)
    cache = json.loads((_isolated_tuoguan_home / "dashboard_cache.json").read_text(encoding="utf-8"))
    boss_summer = cache["boss_dashboard"]["summer_import"]
    manager_summer = cache["manager_dashboards"]["manager1"]["summer_import"]
    assert boss_summer["summary"]["total_students"] == 2
    assert boss_summer["summary"]["phone_pending_count"] == 1
    assert boss_summer["summary"]["duplicate_pending_count"] == 1
    assert boss_summer["summary"]["safety_attention_count"] == 1
    assert boss_summer["summary"]["unknown_record_count"] == 1
    assert manager_summer["summary"] == boss_summer["summary"]


@pytest.mark.asyncio
async def test_new_task_notification_overrides_old_pending_next_context(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "teacher_wecom_map.json",
        "{\"李老师\": \"teacher2\", \"老板\": \"boss1\"}",
    )
    _write_json(
        _isolated_tuoguan_home / "wecom_whitelist.json",
        """
        {
          "known_users": ["teacher2", "boss1"],
          "allowed_users": ["teacher2"],
          "super_users": ["boss1"],
          "user_roles": {"teacher2": "teacher", "boss1": "boss"}
        }
        """,
    )
    _write_json(
        _isolated_tuoguan_home / "students.json",
        "{\"李四\": {\"teacher\": \"teacher2\", \"campus_id\": \"main\"}, \"位俊丞\": {\"teacher\": \"teacher2\", \"campus_id\": \"main\"}}",
    )
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        """
        [
          {
            "id": "task-old-b",
            "title": "位俊丞本周成长观察",
            "type": "growth_observation",
            "level": "B",
            "status": "pending",
            "student_name": "位俊丞",
            "assignee_userid": "teacher2",
            "due_at": "2026-06-23T20:00:00",
            "created_at": "2026-06-21T10:00:00"
          }
        ]
        """,
    )
    _write_json(
        _isolated_tuoguan_home / "pending_next_task_context.json",
        """
        {
          "teacher2": {
            "user_id": "teacher2",
            "task_id": "task-old-b",
            "task_title": "位俊丞本周成长观察",
            "task_level": "B",
            "student_name": "位俊丞",
            "source": "next_task_prompt",
            "trigger_words": ["继续", "开始"],
            "expires_at": "2099-01-01T00:00:00"
          }
        }
        """,
    )
    _write_json(
        _isolated_tuoguan_home / "pending_next_task_context.json",
        """
        {
          "teacher1": {
            "user_id": "teacher1",
            "task_id": "task-b",
            "task_title": "位俊丞本周成长观察",
            "task_level": "B",
            "student_name": "位俊丞",
            "source": "next_task_prompt",
            "trigger_words": ["继续", "开始"],
            "expires_at": "2099-01-01T00:00:00"
          }
        }
        """,
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("安排李老师明天前跟进李四午休状态，A级。", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    new_task = next(task for task in tasks if task["student_name"] == "李四")
    assert new_task["level"] == "A"
    assert new_task["title"] in {"跟进李四午休状态", "李四午休状态跟进"}
    pending = json.loads((_isolated_tuoguan_home / "pending_next_task_context.json").read_text(encoding="utf-8"))
    assert pending["teacher2"]["task_id"] == new_task["id"]
    assert pending["teacher2"]["source"] == "new_task_notification"
    assert pending["teacher2"]["student_name"] == "李四"

    adapter.send.reset_mock()
    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("开始", "teacher2", "李老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    new_task = next(task for task in tasks if task["student_name"] == "李四")
    old_task = next(task for task in tasks if task["id"] == "task-old-b")
    assert new_task["status"] == "active"
    assert old_task["status"] == "pending"
    args, _kwargs = adapter.send.await_args
    assert "李四午休状态" in args[1]
    assert "位俊丞本周成长观察" not in args[1]


@pytest.mark.asyncio
async def test_start_lists_tasks_when_multiple_open_and_no_recent_pending_next(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "students.json",
        "{\"李四\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}, \"位俊丞\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}}",
    )
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        """
        [
          {
            "id": "task-a",
            "title": "李四午休状态跟进",
            "type": "growth_observation",
            "level": "A",
            "status": "pending",
            "student_name": "李四",
            "assignee_userid": "teacher1",
            "due_at": "2026-06-23T20:00:00",
            "created_at": "2026-06-21T10:00:00"
          },
          {
            "id": "task-b",
            "title": "位俊丞本周成长观察",
            "type": "growth_observation",
            "level": "B",
            "status": "pending",
            "student_name": "位俊丞",
            "assignee_userid": "teacher1",
            "due_at": "2026-06-23T21:00:00",
            "created_at": "2026-06-21T10:01:00"
          }
        ]
        """,
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("开始", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "你当前有 2 个待处理任务" in args[1]
    assert "李四午休状态跟进" in args[1]
    assert "位俊丞本周成长观察" in args[1]


@pytest.mark.asyncio
async def test_task_commands_use_task_source_and_clear_expired_active_context(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "students.json",
        "{\"李四\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}, \"小金\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}, \"位俊丞\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}}",
    )
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        """
        [
          {
            "id": "task-li-a",
            "title": "跟进李四午休状态",
            "type": "growth_observation",
            "level": "A",
            "status": "pending",
            "student_name": "李四",
            "assignee_userid": "teacher1",
            "assigned_by_name": "金总",
            "due_at": "2026-06-23T20:00:00",
            "created_at": "2026-06-22T00:06:11"
          },
          {
            "id": "task-xiaojin-a",
            "title": "小金数学计算订正情况",
            "type": "academic_issue",
            "level": "A",
            "status": "pending",
            "student_name": "小金",
            "assignee_userid": "teacher1",
            "assigned_by_name": "金总",
            "due_at": "2026-06-23T20:00:00",
            "created_at": "2026-06-22T00:05:00"
          },
          {
            "id": "task-old-b",
            "title": "位俊丞本周成长观察",
            "type": "student_daily",
            "level": "B",
            "status": "active",
            "student_name": "位俊丞",
            "assignee_userid": "teacher1",
            "due_at": "2026-06-23T20:00:00",
            "created_at": "2026-06-22T00:00:27"
          }
        ]
        """,
    )
    _write_json(
        _isolated_tuoguan_home / "active_task_context.json",
        """
        {
          "teacher1": {
            "user_id": "teacher1",
            "task_id": "task-old-b",
            "student_name": "位俊丞",
            "task_type": "student_daily",
            "status": "processing",
            "started_at": "2026-06-22T00:07:26",
            "expires_at": "2000-01-01T00:00:00"
          }
        }
        """,
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("我的所有任务", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "【待处理 — 3个】" in args[1]
    assert "跟进李四午休状态" in args[1]
    assert "小金数学计算订正情况" in args[1]
    assert "位俊丞本周成长观察" in args[1]

    adapter.send.reset_mock()
    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("我的任务", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "【待处理 — 3个】" in args[1]
    assert "【当前正在处理】位俊丞本周成长观察" not in args[1]
    contexts = json.loads((_isolated_tuoguan_home / "active_task_context.json").read_text(encoding="utf-8"))
    assert "teacher1" not in contexts


@pytest.mark.asyncio
async def test_wecom_task_list_matches_dashboard_open_tasks(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin
    from plugins.tuoguan_core.dashboard_builder import load_dashboard_cache, refresh_dashboard_cache
    from plugins.tuoguan_core.store import TuoguanStore

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "students.json",
        "{\"李四\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}, \"位俊丞\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}}",
    )
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        """
        [
          {
            "id": "task-li-a",
            "title": "跟进李四午休状态",
            "type": "growth_observation",
            "level": "A",
            "status": "pending",
            "student_name": "李四",
            "assignee_userid": "teacher1",
            "due_at": "2026-06-23T20:00:00",
            "created_at": "2026-06-22T00:06:11"
          },
          {
            "id": "task-old-b",
            "title": "位俊丞本周成长观察",
            "type": "student_daily",
            "level": "B",
            "status": "pending",
            "student_name": "位俊丞",
            "assignee_userid": "teacher1",
            "due_at": "2026-06-23T20:00:00",
            "created_at": "2026-06-22T00:00:27"
          }
        ]
        """,
    )
    store = TuoguanStore()
    refresh_dashboard_cache(store)
    cache = load_dashboard_cache(store)
    dashboard_tasks = cache["teacher_dashboards"]["teacher1"]["open_tasks"]
    dashboard_titles = {task["title"] for task in dashboard_tasks}
    assert {"跟进李四午休状态", "位俊丞本周成长观察"} <= dashboard_titles

    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("我的所有任务", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "跟进李四午休状态" in args[1]
    assert "位俊丞本周成长观察" in args[1]


@pytest.mark.asyncio
async def test_record_feedback_uses_chinese_quality_label(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(_isolated_tuoguan_home / "students.json", "{\"李四\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}}")
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("李四今天午休时有点坐不住，我提醒后能安静下来，后半段休息状态比昨天好。", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "quality" not in args[1]
    assert "normal" not in args[1]
    assert "duplicate" not in args[1]
    assert "优质记录" in args[1] or "有效记录" in args[1]


@pytest.mark.asyncio
async def test_duplicate_record_feedback_has_precise_tip(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(_isolated_tuoguan_home / "students.json", "{\"李四\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}}")
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    first = "李四今天午餐吃饭比昨天主动，米饭基本吃完了，青菜一开始不太愿意吃，我提醒后也吃了一些，整体进餐状态比昨天好。"
    second = "李四今天午餐吃饭比昨天更主动，米饭基本吃完，青菜一开始不太愿意吃，我提醒后也吃了一些，整体进餐状态比昨天好。"

    for text in (first, second):
        result = plugin._on_pre_gateway_dispatch(
            event=_event_from(text, "teacher1", "王老师"),
            gateway=_gateway(adapter),
            session_store=SimpleNamespace(),
        )
        await asyncio_sleep()
        assert result == {"action": "skip", "reason": "tuoguan_core_handled"}

    args, _kwargs = adapter.send.await_args
    assert "不重复计入绩效" in args[1]
    assert "补一句老师处理动作、结果或后续安排" not in args[1]


@pytest.mark.asyncio
async def test_lunch_nap_task_start_uses_task_specific_prompt(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(_isolated_tuoguan_home / "students.json", "{\"李四\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}}")
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        """
        [
          {
            "id": "task-nap-a",
            "title": "跟进李四午休状态",
            "type": "growth_observation",
            "level": "A",
            "status": "pending",
            "student_name": "李四",
            "assignee_userid": "teacher1",
            "due_at": "2026-06-23T20:00:00",
            "created_at": "2026-06-22T00:06:11"
          }
        ]
        """,
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("开始", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "午休表现" in args[1]
    assert "老师采取了什么处理" in args[1]
    assert "明天是否继续观察" in args[1]
    assert "家长当前态度" not in args[1]


@pytest.mark.asyncio
async def test_safety_parent_script_polishes_minor_slips(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(_isolated_tuoguan_home / "students.json", "{\"李四\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}}")
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    plugin._on_pre_gateway_dispatch(
        event=_event_from("李四今天进教室时手指不小心被门缝夹了一下，我马上看查看了孩子手指，目前没有破皮没有出血，手指能正常活动。", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    adapter.send.reset_mock()
    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("处理1", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "我马上查看了孩子手指" in args[1]
    assert "看查看" not in args[1]


@pytest.mark.asyncio
async def test_b_level_tasks_are_collapsed_and_expandable(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    tasks = []
    students = {}
    for index in range(1, 8):
        name = f"学生{index}"
        students[name] = {"teacher": "teacher1", "campus_id": "main"}
        tasks.append(
            {
                "id": f"task-b-{index}",
                "title": f"{name}本周成长观察",
                "type": "student_daily",
                "level": "B",
                "status": "pending",
                "student_name": name,
                "assignee_userid": "teacher1",
                "due_at": "2026-06-23T20:00:00",
                "created_at": f"2026-06-22T00:0{index}:00",
            }
        )
    _write_json(_isolated_tuoguan_home / "students.json", json.dumps(students, ensure_ascii=False))
    _write_json(_isolated_tuoguan_home / "tasks.json", json.dumps(tasks, ensure_ascii=False))
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    plugin._on_pre_gateway_dispatch(
        event=_event_from("我的所有任务", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    args, _kwargs = adapter.send.await_args
    assert args[1].count("B级：") == 5
    assert "还有 2 个 B 级任务，回复“更多B级任务”查看全部" in args[1]

    adapter.send.reset_mock()
    plugin._on_pre_gateway_dispatch(
        event=_event_from("更多B级任务", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    args, _kwargs = adapter.send.await_args
    assert args[1].count("B级：") == 7


@pytest.mark.asyncio
async def test_boss_can_set_operations_focus(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("本月经营重点改为暑假续费和家长满意度", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    items = json.loads((_isolated_tuoguan_home / "operations_focus.json").read_text(encoding="utf-8"))
    assert items[0]["scope"] == "month"
    assert "续费" in items[0]["keywords"]
    assert "家长满意度" in items[0]["keywords"]
    args, _kwargs = adapter.send.await_args
    assert "已设置本月经营重点" in args[1]


@pytest.mark.asyncio
async def test_teacher_cannot_set_operations_focus(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("本月经营重点改为续费", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "经营重点只能由老板账号调整" in args[1]
    assert not (_isolated_tuoguan_home / "operations_focus.json").exists()


@pytest.mark.asyncio
async def test_teacher_cannot_manually_assign_task(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_manual_assignment_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event("安排王老师明天前跟进周温暖家长沟通，A级"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "只有店长或老板" in args[1]
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    assert tasks == []


@pytest.mark.asyncio
async def test_explicit_record_has_audit_source_and_can_be_undone(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_teacher(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "students.json",
        """
        {
          "周温暖": {"teacher": "teacher1", "campus_id": "main"}
        }
        """,
    )
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event("记录周温暖：今天数学测试拿了98分，值得表扬"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    records = json.loads((_isolated_tuoguan_home / "records.json").read_text(encoding="utf-8"))
    assert len(records) == 1
    assert records[0]["source_meta"]["message_id"] == "msg-1"
    assert records[0]["source_meta"]["raw_text"].startswith("记录周温暖")
    assert records[0]["record_evaluation"]["accepted"] is True
    assert "payroll_eligible" in records[0]["record_evaluation"]
    assert "reason_texts" in records[0]["record_evaluation"]

    plugin._on_pre_gateway_dispatch(
        event=_event("撤销上一条记录"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    records = json.loads((_isolated_tuoguan_home / "records.json").read_text(encoding="utf-8"))
    assert records == []


@pytest.mark.asyncio
async def test_non_business_text_falls_through(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_teacher(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event("今天天气怎么样"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result is None
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_dashboard_command_returns_signed_link(_isolated_tuoguan_home, monkeypatch):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_teacher(_isolated_tuoguan_home)
    monkeypatch.setenv("HERMES_TUOGUAN_DASHBOARD_BASE_URL", "https://example.test/h")
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event("看板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "https://example.test/h/tuoguan/dashboard?token=" in args[1]
    assert "记录和任务处理仍请回企业微信直接说" in args[1]
    cache = json.loads((_isolated_tuoguan_home / "dashboard_cache.json").read_text(encoding="utf-8"))
    assert cache["schema_version"] == 1
    assert "teacher1" in cache["teacher_dashboards"]


@pytest.mark.asyncio
async def test_daily_dashboard_push_sends_once_per_day(_isolated_tuoguan_home, monkeypatch):
    from plugins.tuoguan_core.daily_push import run_due_daily_dashboard_push
    from plugins.tuoguan_core.store import TuoguanStore

    _seed_payroll_users(_isolated_tuoguan_home)
    whitelist = json.loads((_isolated_tuoguan_home / "wecom_whitelist.json").read_text(encoding="utf-8"))
    whitelist["allowed_users"].append("CeShi")
    whitelist["user_roles"]["CeShi"] = "teacher"
    (_isolated_tuoguan_home / "wecom_whitelist.json").write_text(json.dumps(whitelist), encoding="utf-8")
    mapping = json.loads((_isolated_tuoguan_home / "teacher_wecom_map.json").read_text(encoding="utf-8"))
    mapping["李老师测试"] = "CeShi"
    (_isolated_tuoguan_home / "teacher_wecom_map.json").write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    (_isolated_tuoguan_home / "daily_push_config.json").write_text(
        json.dumps(
            {
                "include_user_ids": ["CeShi"],
                "exclude_user_ids": ["teacher1"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_TUOGUAN_DAILY_PUSH_ENABLED", "1")
    monkeypatch.setenv("HERMES_TUOGUAN_DASHBOARD_BASE_URL", "https://example.test/h")
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    store = TuoguanStore(_isolated_tuoguan_home)
    now = datetime(2026, 6, 21, 10, 0, 0)

    first = await run_due_daily_dashboard_push(adapter=adapter, store=store, now=now)
    second = await run_due_daily_dashboard_push(adapter=adapter, store=store, now=now)

    assert first == {"sent": 3, "failed": 0, "skipped": False}
    assert second == {"sent": 0, "failed": 0, "skipped": True}
    assert adapter.send.await_count == 3
    sent_text = "\n".join(call.args[1] for call in adapter.send.await_args_list)
    assert "Hermes 老师今日任务" in sent_text
    assert "Hermes 店长今日任务" in sent_text
    assert "Hermes 老板今日任务" in sent_text
    targets = [call.args[0] for call in adapter.send.await_args_list]
    assert "CeShi" in targets
    assert "teacher1" not in targets
    state = json.loads((_isolated_tuoguan_home / "daily_push_state.json").read_text(encoding="utf-8"))
    assert state["last_sent_date"] == "2026-06-21"


@pytest.mark.asyncio
async def test_head_bump_creates_s_task_and_notifies_supervisors(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "students.json",
        """
        {
          "周温暖": {"teacher": "teacher1", "campus_id": "main"}
        }
        """,
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("今天周温暖碰到头了，现在可能有点严重", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    assert tasks[0]["type"] == "safety_incident"
    assert tasks[0]["level"] == "S"
    sent = [call.args for call in adapter.send.await_args_list]
    assert any(args[0] == "boss1" and "S级任务老板关注" in args[1] for args in sent)
    assert any(args[0] == "manager1" and "S级任务店长关注" in args[1] for args in sent)
    assert any(args[0] == "teacher1" and "S级任务提醒" in args[1] for args in sent)
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    assert tasks[0]["last_escalation_action"] == "escalate_now"


@pytest.mark.asyncio
async def test_learning_record_with_completion_word_does_not_route_as_task(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    whitelist = json.loads((_isolated_tuoguan_home / "wecom_whitelist.json").read_text(encoding="utf-8"))
    whitelist["allowed_users"].append("teacher2")
    whitelist["user_roles"]["teacher2"] = "teacher"
    _write_json(_isolated_tuoguan_home / "wecom_whitelist.json", json.dumps(whitelist, ensure_ascii=False))
    mapping = json.loads((_isolated_tuoguan_home / "teacher_wecom_map.json").read_text(encoding="utf-8"))
    mapping["李老师"] = "teacher2"
    _write_json(_isolated_tuoguan_home / "teacher_wecom_map.json", json.dumps(mapping, ensure_ascii=False))
    _write_json(
        _isolated_tuoguan_home / "students.json",
        """
        {
          "小金": {"teacher": "teacher2", "campus_id": "main"}
        }
        """,
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    samples = [
        "小金今天数学作业完成得比较慢，计算题错了4道，我让他重新订正了一遍。",
        "小金今天作业完成质量一般，语文阅读漏了2题，我让他补完后重新检查。",
        "小金订正完成后还错1道，明天继续关注计算准确率。",
    ]
    for sample in samples:
        result = plugin._on_pre_gateway_dispatch(
            event=_event_from(sample, "teacher2", "李老师"),
            gateway=_gateway(adapter),
            session_store=SimpleNamespace(),
        )
        await asyncio_sleep()
        assert result == {"action": "skip", "reason": "tuoguan_core_handled"}

    sent_text = "\n".join(call.args[1] for call in adapter.send.await_args_list)
    assert "只有店长或老板" not in sent_text
    records = json.loads((_isolated_tuoguan_home / "records.json").read_text(encoding="utf-8"))
    assert len(records) == 3
    assert all(record["student_name"] == "小金" for record in records)
    assert all({"academic_issue", "learning_habit", "student_daily"} & set(record["record_types"]) for record in records)
    route_logs = [
        json.loads(line)
        for line in (_isolated_tuoguan_home / "semantic_route_logs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [item["model_intent"] for item in route_logs[-3:]] == ["student_record", "student_record", "student_record"]


@pytest.mark.asyncio
async def test_teacher_complete_without_active_task_does_not_create_payroll_task(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("完成了", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "当前没有正在处理的任务" in args[1] or "你当前暂无待处理任务" in args[1]
    assert "只有店长或老板" not in args[1]


@pytest.mark.asyncio
async def test_teacher_complete_with_active_task_routes_to_task_complete(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(_isolated_tuoguan_home / "students.json", "{\"小金\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}}")
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        """
        [
          {
            "id": "task-active-complete",
            "title": "小金数学订正跟进",
            "type": "academic_issue",
            "level": "A",
            "status": "active",
            "student_name": "小金",
            "assignee_userid": "teacher1",
            "due_at": "2026-06-21T20:00:00",
            "created_at": "2026-06-21T10:00:00",
            "evidence_summary": "今天已让小金重新订正计算题，订正后已检查，明天继续关注。"
          }
        ]
        """,
    )
    _write_json(
        _isolated_tuoguan_home / "active_task_context.json",
        "{\"teacher1\": {\"task_id\": \"task-active-complete\", \"student_name\": \"小金\", \"task_type\": \"academic_issue\", \"status\": \"processing\", \"expires_at\": \"2099-01-01T00:00:00\"}}",
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("完成了", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    assert tasks[0]["status"] == "completed"
    teacher_reply = next(call.args[1] for call in adapter.send.await_args_list if call.args[0] == "wwcorp:teacher1")
    assert "任务已完成" in teacher_reply


@pytest.mark.asyncio
async def test_boss_assigns_li_teacher_completion_task_not_student_record(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    whitelist = json.loads((_isolated_tuoguan_home / "wecom_whitelist.json").read_text(encoding="utf-8"))
    whitelist["allowed_users"].append("teacher2")
    whitelist["user_roles"]["teacher2"] = "teacher"
    _write_json(_isolated_tuoguan_home / "wecom_whitelist.json", json.dumps(whitelist, ensure_ascii=False))
    mapping = json.loads((_isolated_tuoguan_home / "teacher_wecom_map.json").read_text(encoding="utf-8"))
    mapping["李老师"] = "teacher2"
    _write_json(_isolated_tuoguan_home / "teacher_wecom_map.json", json.dumps(mapping, ensure_ascii=False))
    _write_json(_isolated_tuoguan_home / "students.json", "{\"小金\": {\"teacher\": \"teacher2\", \"campus_id\": \"main\"}}")
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("安排李老师完成小金数学订正跟进任务", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    assert len(tasks) == 1
    assert tasks[0]["assignee_userid"] == "teacher2"
    assert tasks[0]["student_name"] == "小金"
    assert tasks[0]["source_type"] == "manual_assignment"
    assert not (_isolated_tuoguan_home / "records.json").exists()
    route_logs = [
        json.loads(line)
        for line in (_isolated_tuoguan_home / "semantic_route_logs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert route_logs[-1]["model_intent"] == "create_task"
    assert route_logs[-1]["handler"] == "manual_task_assignment"


@pytest.mark.asyncio
async def test_safety_task_context_accepts_evidence_without_student_name(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "students.json",
        """
        {
          "小金": {"teacher": "teacher1", "campus_id": "main"}
        }
        """,
    )
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        """
        [
          {
            "id": "task-safe-xiaojin",
            "title": "小金差点摔倒安全闭环",
            "type": "safety_incident",
            "level": "S",
            "status": "pending",
            "student_name": "小金",
            "assignee_userid": "teacher1",
            "due_at": "2026-06-21T20:00:00",
            "created_at": "2026-06-21T10:00:00",
            "source_text": "小金差点摔倒"
          }
        ]
        """,
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    plugin._on_pre_gateway_dispatch(
        event=_event_from("开始", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    result = plugin._on_pre_gateway_dispatch(
        event=_event_from(
            "孩子当前没有疼痛、出血、红肿，活动正常，情绪稳定；已经提醒孩子上下楼靠右慢走；放学时已告知家长，家长表示知道了；后续这两天继续观察上下楼情况。",
            "teacher1",
            "王老师",
        ),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    task = tasks[0]
    assert task["status"] == "waiting_confirmation"
    assert task["closure_events"][-1]["action"] == "closure_ready"
    assert task["closure_fields"]["child_status"]
    assert task["closure_fields"]["action_taken"]
    assert task["closure_fields"]["parent_informed"]
    assert task["closure_fields"]["followup_plan"]
    args, _kwargs = adapter.send.await_args
    assert "闭环信息已补齐" in args[1]


@pytest.mark.asyncio
async def test_labelled_safety_closure_update_is_not_manual_assignment(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "students.json",
        """
        {
          "小金": {"teacher": "teacher1", "campus_id": "main"}
        }
        """,
    )
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        """
        [
          {
            "id": "task-safe-label",
            "title": "小金差点摔倒安全闭环",
            "type": "safety_incident",
            "level": "S",
            "status": "active",
            "student_name": "小金",
            "assignee_userid": "teacher1",
            "due_at": "2026-06-21T20:00:00",
            "created_at": "2026-06-21T10:00:00",
            "source_text": "小金差点摔倒"
          }
        ]
        """,
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from(
            "安全闭环补充：小金。孩子当前状态：没有疼痛、出血、红肿，活动正常。已采取处理：提醒上下楼靠右慢走。家长是否知情：已告知家长，家长表示知道了。后续观察安排：这两天继续观察上下楼情况。",
            "teacher1",
            "王老师",
        ),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "只有店长或老板" not in args[1]
    assert "闭环信息已补齐" in args[1]
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    assert tasks[0]["closure_fields"]["parent_informed"]


@pytest.mark.asyncio
async def test_safety_task_requires_final_complete_after_evidence_ready(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(_isolated_tuoguan_home / "students.json", "{\"小金\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}}")
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        """
        [
          {
            "id": "task-safe-final",
            "title": "小金差点摔倒安全闭环",
            "type": "safety_incident",
            "level": "S",
            "status": "waiting_confirmation",
            "student_name": "小金",
            "assignee_userid": "teacher1",
            "due_at": "2026-06-21T20:00:00",
            "created_at": "2026-06-21T10:00:00",
            "evidence_summary": "孩子当前状态正常，没有疼痛出血红肿；已提醒上下楼靠右慢走；已告知家长，家长表示知道了；后续两天继续观察。",
            "closure_fields": {
              "child_status": "状态正常",
              "action_taken": "提醒上下楼靠右慢走",
              "parent_informed": "已告知家长",
              "followup_plan": "后续两天继续观察"
            }
          }
        ]
        """,
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("完成了", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    assert tasks[0]["status"] == "completed"
    assert tasks[0]["closure_events"][-1]["action"] == "completed"


@pytest.mark.asyncio
async def test_boss_closes_specific_s_task_not_gateway_service(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(_isolated_tuoguan_home / "students.json", "{\"小金\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}}")
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        """
        [
          {
            "id": "task-safe-admin",
            "title": "小金差点摔倒安全闭环",
            "type": "safety_incident",
            "level": "S",
            "status": "active",
            "student_name": "小金",
            "assignee_userid": "teacher1",
            "assignee_role": "teacher",
            "due_at": "2026-06-21T20:00:00",
            "created_at": "2026-06-21T10:00:00",
            "source_text": "小金差点摔倒"
          }
        ]
        """,
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("关闭小金差点摔倒这个S级安全任务。原因：本次为系统测试任务，老板确认关闭。", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    sent_text = "\n".join(call.args[1] for call in adapter.send.await_args_list)
    assert "关闭 gateway" not in sent_text
    assert "已关闭任务" in sent_text
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    assert tasks[0]["status"] == "closed_by_admin"
    audit = json.loads((_isolated_tuoguan_home / "task_admin_closure_events.json").read_text(encoding="utf-8"))
    assert audit[0]["task_id"] == "task-safe-admin"


@pytest.mark.asyncio
async def test_boss_close_task_extracts_natural_reason_with_comma(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(_isolated_tuoguan_home / "students.json", "{\"小金\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}}")
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        """
        [
          {
            "id": "task-safe-natural",
            "title": "小金差点摔伤安全闭环",
            "type": "safety_incident",
            "level": "S",
            "status": "active",
            "student_name": "小金",
            "assignee_userid": "teacher1",
            "assignee_role": "teacher",
            "due_at": "2026-06-21T20:00:00",
            "created_at": "2026-06-21T10:00:00",
            "source_text": "小金差点摔伤"
          }
        ]
        """,
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("关闭小金差点摔伤这个S级任务原因，本次为系统测试任务，孩子状态已确认正常，家长已知情，后续由老师继续观察", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    sent_text = "\n".join(call.args[1] for call in adapter.send.await_args_list)
    assert "关闭任务需要写明原因" not in sent_text
    assert "gateway" not in sent_text.lower()
    assert "白名单" not in sent_text
    assert "已关闭任务" in sent_text
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    assert tasks[0]["status"] == "closed_by_admin"
    assert "本次为系统测试任务" in tasks[0]["admin_close_reason"]
    audit = json.loads((_isolated_tuoguan_home / "task_admin_closure_events.json").read_text(encoding="utf-8"))
    assert audit[0]["task_id"] == "task-safe-natural"


@pytest.mark.asyncio
async def test_boss_close_task_pending_reason_blocks_technical_context(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(_isolated_tuoguan_home / "students.json", "{\"小金\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}}")
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        """
        [
          {
            "id": "task-safe-pending-close",
            "title": "小金差点摔倒安全闭环",
            "type": "safety_incident",
            "level": "S",
            "status": "active",
            "student_name": "小金",
            "assignee_userid": "teacher1",
            "assignee_role": "teacher",
            "due_at": "2026-06-21T20:00:00",
            "created_at": "2026-06-21T10:00:00"
          }
        ]
        """,
    )
    _write_json(
        _isolated_tuoguan_home / "script_context.json",
        "{\"boss1\": {\"text\": \"gateway、CeShi、WECOM_CALLBACK_ALLOWED_USERS、白名单、重启 gateway\", \"updated_at\": \"2026-06-21T10:00:00\"}}",
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    plugin._on_pre_gateway_dispatch(
        event=_event_from("关闭小金这个S级任务", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    first = adapter.send.await_args.args[1]
    assert "还缺关闭原因" in first

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("本次为系统测试任务", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    sent_text = "\n".join(call.args[1] for call in adapter.send.await_args_list)
    assert "WECOM_CALLBACK_ALLOWED_USERS" not in sent_text
    assert "白名单" not in sent_text
    assert "已关闭任务" in sent_text
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    assert tasks[0]["status"] == "closed_by_admin"
    assert tasks[0]["admin_close_reason"] == "本次为系统测试任务"


@pytest.mark.asyncio
async def test_close_gateway_is_system_service_not_business_task(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("关闭gateway", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "系统服务关闭" in args[1]
    assert "已关闭任务" not in args[1]


@pytest.mark.asyncio
async def test_start_with_multiple_tasks_asks_teacher_to_choose(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(_isolated_tuoguan_home / "students.json", "{\"小金\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}}")
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        """
        [
          {
            "id": "task-safe-multi",
            "title": "小金差点摔倒安全闭环",
            "type": "safety_incident",
            "level": "S",
            "status": "pending",
            "student_name": "小金",
            "assignee_userid": "teacher1",
            "due_at": "2026-06-21T20:00:00",
            "created_at": "2026-06-21T10:00:00"
          },
          {
            "id": "task-math-multi",
            "title": "小金数学计算订正跟进",
            "type": "academic_issue",
            "level": "A",
            "status": "pending",
            "student_name": "小金",
            "assignee_userid": "teacher1",
            "due_at": "2026-06-21T21:00:00",
            "created_at": "2026-06-21T10:01:00"
          }
        ]
        """,
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("开始", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "你当前有 2 个待处理任务" in args[1]
    assert "建议先处理 S级" in args[1]
    assert "处理1" in args[1]
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    assert {task["status"] for task in tasks} == {"pending"}


@pytest.mark.asyncio
async def test_teacher_can_select_a_task_when_s_task_exists(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(_isolated_tuoguan_home / "students.json", "{\"小金\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}}")
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        """
        [
          {
            "id": "task-safe-select",
            "title": "小金差点摔倒安全闭环",
            "type": "safety_incident",
            "level": "S",
            "status": "pending",
            "student_name": "小金",
            "assignee_userid": "teacher1",
            "due_at": "2026-06-21T20:00:00",
            "created_at": "2026-06-21T10:00:00"
          },
          {
            "id": "task-math-select",
            "title": "小金数学计算订正跟进",
            "type": "academic_issue",
            "level": "A",
            "status": "pending",
            "student_name": "小金",
            "assignee_userid": "teacher1",
            "due_at": "2026-06-21T21:00:00",
            "created_at": "2026-06-21T10:01:00"
          }
        ]
        """,
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    plugin._on_pre_gateway_dispatch(
        event=_event_from("开始", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("处理2", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    by_id = {task["id"]: task for task in tasks}
    assert by_id["task-math-select"]["status"] == "active"
    assert by_id["task-safe-select"]["status"] == "pending"


def test_safety_record_negation_does_not_create_s_task(_isolated_tuoguan_home):
    from plugins.tuoguan_core.records import analyze_teacher_record
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(
        _isolated_tuoguan_home / "students.json",
        """
        {
          "周温暖": {"teacher": "teacher1", "campus_id": "main"}
        }
        """,
    )

    analysis = analyze_teacher_record(
        "今天周温暖吃饭正常，没有磕碰，没有不舒服。",
        TuoguanStore(),
    )

    assert analysis["level"] == "C"
    assert analysis["should_create_task"] is False


def test_flexible_record_classifier_handles_business_scenarios(_isolated_tuoguan_home):
    from plugins.tuoguan_core.records import analyze_teacher_record
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(
        _isolated_tuoguan_home / "students.json",
        """
        {
          "周温暖": {"teacher": "teacher1", "campus_id": "main"}
        }
        """,
    )
    store = TuoguanStore()

    safety = analyze_teacher_record("周温暖今天玩的时候撞到了胳膊，哭了一会儿。", store)
    assert safety["record_types"][0] == "safety_incident"
    assert safety["level"] == "S"
    assert safety["should_create_task"] is True
    assert safety["analysis_engine"] == "hybrid_rules_v2"
    assert safety["confidence"] >= 0.8
    assert safety["evidence_spans"]

    complaint = analyze_teacher_record("周温暖妈妈对效果不认可，有意见，说明天要退费。", store)
    assert "parent_complaint" in complaint["record_types"]
    assert complaint["level"] == "S"

    renewal = analyze_teacher_record("周温暖快到期了，妈妈说再考虑，觉得价格有点贵。", store)
    assert "renewal_risk" in renewal["record_types"]
    assert renewal["level"] == "A"


@pytest.mark.asyncio
async def test_boss_learning_feedback_review_flow(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("这个不是安全事件", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    pending = json.loads((_isolated_tuoguan_home / "pending_knowledge.json").read_text(encoding="utf-8"))
    candidate_id = pending[0]["id"]
    assert pending[0]["candidate_type"] == "false_positive"

    plugin._on_pre_gateway_dispatch(
        event=_event_from("待学习", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    args, _kwargs = adapter.send.await_args
    assert candidate_id in args[1]

    plugin._on_pre_gateway_dispatch(
        event=_event_from(f"批准学习 {candidate_id}", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    pending = json.loads((_isolated_tuoguan_home / "pending_knowledge.json").read_text(encoding="utf-8"))
    approved = json.loads((_isolated_tuoguan_home / "tuoguan_knowledge.json").read_text(encoding="utf-8"))
    assert pending[0]["status"] == "approved"
    assert approved[0]["source_candidate_id"] == candidate_id


def test_safety_closure_accepts_natural_teacher_wording():
    from plugins.tuoguan_core.tasks import closure_missing_fields, extract_safety_closure_fields

    task = {"type": "safety_incident"}
    evidence = (
        "孩子当前没有大碍了，已经做了处理，家长也知情，"
        "因为是在学校受伤的，不是在托管班，明天再回访。"
    )

    assert closure_missing_fields(task, evidence) == []
    fields = extract_safety_closure_fields(
        "孩子当前没有疼痛、出血、红肿，活动正常，情绪稳定；"
        "已经提醒孩子上下楼靠右慢走；"
        "放学时已告知家长，家长表示知道了；"
        "后续这两天继续观察上下楼情况。"
    )
    assert "没有疼痛" in fields["child_status"]
    assert "靠右慢走" in fields["action_taken"]
    assert "家长表示知道" in fields["parent_informed"]
    assert "继续观察" in fields["followup_plan"]


def test_completion_reply_with_missing_fields_is_saved():
    from plugins.tuoguan_core.tasks import apply_task_reply

    tasks = [
        {
            "id": "task1",
            "title": "周温暖安全风险任务",
            "type": "safety_incident",
            "level": "S",
            "status": "waiting_confirmation",
            "student_name": "周温暖",
            "assignee_userid": "teacher1",
            "due_at": "2026-06-19T12:00:00",
            "created_at": "2026-06-19T10:00:00",
            "evidence_summary": "家长已知情",
        }
    ]

    result = apply_task_reply(tasks, "teacher1", "完成了，也做了处理")

    assert result.action == "needs_closure_evidence"
    assert "完成了，也做了处理" in tasks[0]["evidence_summary"]
    assert tasks[0]["closure_events"][-1]["action"] == "needs_closure_evidence"
    assert "child_status" in tasks[0]["closure_events"][-1]["missing_fields"]


@pytest.mark.asyncio
async def test_task_reply_writes_closure_ledger(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        """
        [
          {
            "id": "task-safe",
            "title": "周温暖安全风险任务",
            "type": "safety_incident",
            "level": "S",
            "status": "waiting_confirmation",
            "student_name": "周温暖",
            "assignee_userid": "teacher1",
            "due_at": "2026-06-19T12:00:00",
            "created_at": "2026-06-19T10:00:00",
            "evidence_summary": "家长已知情，已经做了处理。"
          }
        ]
        """,
    )
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("孩子现在状态正常，没有红肿没有疼，后续继续观察。", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    assert tasks[0]["status"] == "waiting_confirmation"
    assert tasks[0]["closure_events"][-1]["action"] == "closure_ready"
    assert "回复“完成了”" in adapter.send.await_args.args[1]
    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("完成了", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    assert tasks[0]["status"] == "completed"
    ledger = json.loads((_isolated_tuoguan_home / "task_closure_events.json").read_text(encoding="utf-8"))
    assert ledger[-1]["task_id"] == "task-safe"
    assert ledger[-1]["action"] == "completed"


@pytest.mark.asyncio
async def test_continue_after_completed_task_starts_pending_next_task(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(_isolated_tuoguan_home / "students.json", "{\"小金\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}, \"张浩\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}}")
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        """
        [
          {
            "id": "task-safe-next",
            "title": "小金差点摔倒安全闭环",
            "type": "safety_incident",
            "level": "S",
            "status": "waiting_confirmation",
            "student_name": "小金",
            "assignee_userid": "teacher1",
            "due_at": "2026-06-21T20:00:00",
            "created_at": "2026-06-21T10:00:00",
            "evidence_summary": "孩子当前状态正常，没有疼痛出血红肿；已提醒上下楼靠右慢走；已告知家长，家长表示知道了；后续两天继续观察。",
            "closure_fields": {
              "child_status": "状态正常",
              "action_taken": "提醒上下楼靠右慢走",
              "parent_informed": "已告知家长",
              "followup_plan": "后续两天继续观察"
            }
          },
          {
            "id": "task-a-next",
            "title": "小金A级订正情况",
            "type": "academic_issue",
            "level": "A",
            "status": "pending",
            "student_name": "小金",
            "assignee_userid": "teacher1",
            "due_at": "2026-06-21T21:00:00",
            "created_at": "2026-06-21T10:01:00"
          }
        ]
        """,
    )
    _write_json(
        _isolated_tuoguan_home / "pending_student_confirm_context.json",
        """
        {
          "teacher1": {
            "student_name": "张浩",
            "raw_text": "张浩今天作业完成一般",
            "expires_at": "2099-01-01T00:00:00"
          }
        }
        """,
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("完成了", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    teacher_reply = next(call.args[1] for call in adapter.send.await_args_list if call.args[0] == "wwcorp:teacher1")
    assert "当前还有 1 个 A 级任务待处理，可稍后处理：小金A级订正情况" in teacher_reply
    assert "回复“继续”即可开始" in teacher_reply
    pending_next = json.loads((_isolated_tuoguan_home / "pending_next_task_context.json").read_text(encoding="utf-8"))
    assert pending_next["teacher1"]["task_id"] == "task-a-next"

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("继续", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "张浩" not in args[1]
    assert "开始处理" in args[1] or "小金A级订正情况" in args[1]
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    next_task = next(task for task in tasks if task["id"] == "task-a-next")
    assert next_task["status"] == "active"
    assert not json.loads((_isolated_tuoguan_home / "pending_next_task_context.json").read_text(encoding="utf-8"))
    student_context = json.loads((_isolated_tuoguan_home / "pending_student_confirm_context.json").read_text(encoding="utf-8"))
    assert "teacher1" not in student_context


@pytest.mark.asyncio
async def test_continue_without_pending_next_does_not_create_student_record(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(_isolated_tuoguan_home / "students.json", "{\"小金\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}}")
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("继续", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "当前没有可继续的任务" in args[1]
    assert not (_isolated_tuoguan_home / "records.json").exists()


@pytest.mark.asyncio
async def test_short_ack_without_context_does_not_create_student_record(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(_isolated_tuoguan_home / "students.json", "{\"小金\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}}")
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    for text in ("好的", "收到", "可以"):
        result = plugin._on_pre_gateway_dispatch(
            event=_event_from(text, "teacher1", "王老师"),
            gateway=_gateway(adapter),
            session_store=SimpleNamespace(),
        )
        await asyncio_sleep()
        assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
        args, _kwargs = adapter.send.await_args
        assert "没有识别到具体要处理的事项" in args[1]

    assert not (_isolated_tuoguan_home / "records.json").exists()


@pytest.mark.asyncio
async def test_natural_life_records_archive_without_record_keyword(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(_isolated_tuoguan_home / "students.json", "{\"李四\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}}")
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    samples = [
        ("李四今天午餐吃饭比昨天主动，米饭基本吃完了，青菜一开始不太愿意吃，我提醒后也吃了一些，整体进餐状态比昨天好。", "meal_care"),
        ("李四今天午休时有点坐不住，我提醒后能安静下来，后半段休息状态比昨天好。", "nap_care"),
    ]
    for text, _expected in samples:
        result = plugin._on_pre_gateway_dispatch(
            event=_event_from(text, "teacher1", "王老师"),
            gateway=_gateway(adapter),
            session_store=SimpleNamespace(),
        )
        await asyncio_sleep()
        assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
        args, _kwargs = adapter.send.await_args
        assert "没有明确写入正式记录" not in args[1]
        assert "已记录李四的情况" in args[1]
        assert "绩效" in args[1]

    records = json.loads((_isolated_tuoguan_home / "records.json").read_text(encoding="utf-8"))
    all_types = [set(record.get("record_types") or []) for record in records]
    assert any("meal_care" in item for item in all_types)
    assert any("nap_care" in item for item in all_types)


@pytest.mark.asyncio
async def test_natural_safety_event_creates_safety_task_and_sanitized_script(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(_isolated_tuoguan_home / "students.json", "{\"李四\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}}")
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from(
            "李四今天进教室时手指不小心被门缝夹了一下，我马上查看了孩子手指，目前没有破皮、没有出血，手指能正常活动，情绪也稳定。我已经提醒孩子进出门时慢一点，不要把手放在门缝附近。",
            "teacher1",
            "王老师",
        ),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    assert tasks and tasks[0]["type"] == "safety_incident"
    assert tasks[0]["level"] == "S"
    records = json.loads((_isolated_tuoguan_home / "records.json").read_text(encoding="utf-8"))
    assert "safety_incident" in records[0]["record_types"]

    plugin._on_pre_gateway_dispatch(
        event=_event_from("开始", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    args, _kwargs = adapter.send.await_args
    assert "安全记录" not in args[1]
    assert "S级任务" not in args[1]


@pytest.mark.asyncio
async def test_explicit_safety_record_script_has_no_internal_words(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(_isolated_tuoguan_home / "students.json", "{\"李四\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}}")
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    plugin._on_pre_gateway_dispatch(
        event=_event_from("安全记录：李四今天在教室走动时膝盖不小心磕到桌角，我马上查看了孩子膝盖，目前只是轻微发红，没有破皮出血，孩子走路正常，情绪稳定。我已经提醒他在教室里慢走，后续继续观察。", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    assert tasks[0]["level"] == "S"

    plugin._on_pre_gateway_dispatch(
        event=_event_from("开始", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    args, _kwargs = adapter.send.await_args
    forbidden = ["安全记录", "S级任务", "闭环", "入档", "绩效", "必达项", "任务编号", "记录编号", "系统识别", "H5", "老板端"]
    assert not any(word in args[1] for word in forbidden)
    assert "膝盖" in args[1]


@pytest.mark.asyncio
async def test_task_completion_notifies_boss_for_s_and_a_tasks(_isolated_tuoguan_home, monkeypatch):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    monkeypatch.setenv("HERMES_TUOGUAN_DASHBOARD_BASE_URL", "https://example.test/h")
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(_isolated_tuoguan_home / "students.json", "{\"李四\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}, \"小金\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}}")
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        """
        [
          {
            "id": "task-safe-complete-notify",
            "title": "李四膝盖磕碰安全闭环",
            "type": "safety_incident",
            "level": "S",
            "status": "waiting_confirmation",
            "student_name": "李四",
            "assignee_userid": "teacher1",
            "due_at": "2026-06-21T20:00:00",
            "created_at": "2026-06-21T10:00:00",
            "evidence_summary": "孩子状态稳定；已提醒慢走；已告知家长；后续继续观察。",
            "closure_fields": {
              "child_status": "状态稳定",
              "action_taken": "提醒慢走",
              "parent_informed": "已告知家长",
              "followup_plan": "后续继续观察"
            }
          },
          {
            "id": "task-a-complete-notify",
            "title": "小金家长沟通跟进",
            "type": "parent_anxiety",
            "level": "A",
            "status": "pending",
            "student_name": "小金",
            "assignee_userid": "teacher1",
            "source_type": "manual_assignment",
            "due_at": "2026-06-21T21:00:00",
            "created_at": "2026-06-21T10:01:00",
            "evidence_summary": "已和小金妈妈沟通数学计算问题，家长表示理解，明天继续观察计算准确率。"
          }
        ]
        """,
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    plugin._on_pre_gateway_dispatch(
        event=_event_from("完成了", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    sent = "\n".join(call.args[1] for call in adapter.send.await_args_list)
    assert "【任务完成】李四膝盖磕碰安全闭环" in sent
    assert "老板看板：" in sent

    plugin._on_pre_gateway_dispatch(
        event=_event_from("继续", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    plugin._on_pre_gateway_dispatch(
        event=_event_from("完成了", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    sent = "\n".join(call.args[1] for call in adapter.send.await_args_list)
    assert "【任务完成】小金家长沟通跟进" in sent
    assert "boss1" in [call.args[0] for call in adapter.send.await_args_list]


@pytest.mark.asyncio
async def test_active_a_level_learning_task_treats_followup_as_task_evidence(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(_isolated_tuoguan_home / "students.json", "{\"小金\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}}")
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        """
        [
          {
            "id": "task-a-math-evidence",
            "title": "小金数学计算题订正情况",
            "type": "academic_issue",
            "level": "A",
            "status": "pending",
            "student_name": "小金",
            "assignee_userid": "teacher1",
            "due_at": "2026-06-21T21:00:00",
            "created_at": "2026-06-21T10:01:00"
          }
        ]
        """,
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    plugin._on_pre_gateway_dispatch(
        event=_event_from("我的任务", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    args, _kwargs = adapter.send.await_args
    assert "小金数学计算题订正情况" in args[1]

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("需要", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    assert tasks[0]["status"] == "active"
    assert json.loads((_isolated_tuoguan_home / "active_task_context.json").read_text(encoding="utf-8"))["teacher1"]["task_id"] == "task-a-math-evidence"

    evidence_text = (
        "小金今天把之前错的4道计算题重新做了一遍，订正后还错1道。"
        "我又让他重新列竖式讲了一遍思路，最后能说出错因。"
        "家长这边暂未单独沟通，这次先做校内订正跟进；如果明天计算还是不稳定，再和家长同步。"
        "明天继续关注他的计算准确率。"
    )
    result = plugin._on_pre_gateway_dispatch(
        event=_event_from(evidence_text, "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "任务处理情况已记录" in args[1]
    assert "回复“完成了”" in args[1]
    assert "本次作为日常记录沉淀" not in args[1]
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    task = tasks[0]
    assert task["status"] == "active"
    assert "订正后还错1道" in task["evidence_summary"]
    fields = task["closure_fields"]
    assert "重新列竖式" in fields["action_taken"]
    assert "能说出错因" in fields["result"]
    assert "暂未单独沟通" in fields["parent_informed"]
    assert "明天" in fields["followup_plan"]
    route_logs = [
        json.loads(line)
        for line in (_isolated_tuoguan_home / "semantic_route_logs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert route_logs[-1]["model_intent"] == "task_evidence_update"
    assert not (_isolated_tuoguan_home / "records.json").exists()


@pytest.mark.asyncio
async def test_pending_a_level_learning_task_with_rich_evidence_recovers_without_active_context(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(_isolated_tuoguan_home / "students.json", "{\"小金\": {\"teacher\": \"teacher1\", \"campus_id\": \"main\"}}")
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        """
        [
          {
            "id": "task-a-math-pending-recover",
            "title": "小金数学计算题订正情况",
            "type": "academic_issue",
            "level": "A",
            "status": "pending",
            "student_name": "小金",
            "assignee_userid": "teacher1",
            "due_at": "2026-06-21T21:00:00",
            "created_at": "2026-06-21T10:01:00"
          }
        ]
        """,
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    evidence_text = (
        "小金今天把之前错的4道计算题重新做了一遍，订正后还错1道。"
        "我又让他重新列竖式讲了一遍思路，最后能说出错因。"
        "家长这边暂未单独沟通，这次先做校内订正跟进；如果明天计算还是不稳定，再和家长同步。"
        "明天继续关注他的计算准确率。"
    )

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from(evidence_text, "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "任务处理情况已记录" in args[1]
    assert "本次作为日常记录沉淀" not in args[1]
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    assert tasks[0]["status"] == "active"
    assert "订正后还错1道" in tasks[0]["evidence_summary"]
    route_logs = [
        json.loads(line)
        for line in (_isolated_tuoguan_home / "semantic_route_logs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert route_logs[-1]["model_intent"] == "task_evidence_update"
    assert not (_isolated_tuoguan_home / "records.json").exists()


@pytest.mark.asyncio
async def test_new_student_record_bypasses_existing_active_task(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "students.json",
        """
        {
          "周温暖": {"teacher": "teacher1", "campus_id": "main"},
          "王小明": {"teacher": "teacher1", "campus_id": "main"}
        }
        """,
    )
    _write_json(
        _isolated_tuoguan_home / "tasks.json",
        """
        [
          {
            "id": "task-existing",
            "title": "周温暖安全风险任务",
            "type": "safety_incident",
            "level": "S",
            "status": "waiting_confirmation",
            "student_name": "周温暖",
            "assignee_userid": "teacher1",
            "due_at": "2026-06-19T12:00:00",
            "created_at": "2026-06-19T10:00:00",
            "evidence_summary": ""
          }
        ]
        """,
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("王小明今天撞到头了，有点疼。", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    tasks = json.loads((_isolated_tuoguan_home / "tasks.json").read_text(encoding="utf-8"))
    assert len(tasks) == 2
    assert tasks[0]["student_name"] == "周温暖"
    assert tasks[0]["evidence_summary"] == ""
    assert tasks[1]["student_name"] == "王小明"
    assert tasks[1]["type"] == "safety_incident"


def test_other_platform_is_ignored(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_teacher(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event("帮助", platform=Platform.WECOM),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )

    assert result is None


@pytest.mark.asyncio
async def test_manager_can_record_teacher_payroll_event(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("王老师今天请假半天", "manager1", "赵店长"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "已记录工资事件：王老师 请假" in args[1]
    events = json.loads((_isolated_tuoguan_home / "payroll_events.json").read_text(encoding="utf-8"))
    assert events[0]["event_type"] == "attendance_leave"
    assert events[0]["target_user_id"] == "teacher1"
    assert events[0]["quantity"] == 0.5
    assert events[0]["confirmed"] is False


@pytest.mark.asyncio
async def test_boss_can_record_calendar_and_admission_payroll_events(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock())

    plugin._on_pre_gateway_dispatch(
        event=_event_from("今天因中招临时停课", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    plugin._on_pre_gateway_dispatch(
        event=_event_from("王老师确认招生2人", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    events = json.loads((_isolated_tuoguan_home / "payroll_events.json").read_text(encoding="utf-8"))
    assert [event["event_type"] for event in events] == [
        "calendar_temporary_closed",
        "admission_confirmed",
    ]
    assert events[0]["confirmed"] is True
    assert events[1]["quantity"] == 2
    assert events[1]["confirmed"] is True
    rules = json.loads((_isolated_tuoguan_home / "payroll_rules.json").read_text(encoding="utf-8"))
    assert rules["positions"]["part_time"]["per_shift_amount"] == 70


@pytest.mark.asyncio
async def test_teacher_cannot_record_payroll_event(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("王老师今天请假半天", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result is None
    adapter.send.assert_not_awaited()
    assert not (_isolated_tuoguan_home / "payroll_events.json").exists()


@pytest.mark.asyncio
async def test_teacher_can_confirm_or_question_payroll(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("工资确认无误", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "已记录你的工资确认无误" in args[1]
    items = json.loads((_isolated_tuoguan_home / "payroll_confirmations.json").read_text(encoding="utf-8"))
    assert items[0]["user_id"] == "teacher1"
    assert items[0]["status"] == "confirmed"

    plugin._on_pre_gateway_dispatch(
        event=_event_from("工资有疑问，全勤不对", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    items = json.loads((_isolated_tuoguan_home / "payroll_confirmations.json").read_text(encoding="utf-8"))
    assert items[-1]["status"] == "question"
    assert "全勤不对" in items[-1]["note"]


@pytest.mark.asyncio
async def test_boss_can_final_confirm_payroll(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("确认本月工资结算", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "已记录老板本月工资最终确认" in args[1]
    items = json.loads((_isolated_tuoguan_home / "payroll_confirmations.json").read_text(encoding="utf-8"))
    assert items[0]["user_id"] == "boss1"
    assert items[0]["status"] == "boss_final_confirmed"


@pytest.mark.asyncio
async def test_boss_can_create_payroll_settlement_snapshot(_isolated_tuoguan_home):
    import json
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "payroll_rules.json",
        """
        {
          "people": {
            "teacher1": {"name": "王老师", "position": "full_time_teacher", "admission_target": 5}
          }
        }
        """,
    )
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("生成工资结算快照", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "已生成本月工资结算快照" in args[1]
    settlements = json.loads((_isolated_tuoguan_home / "payroll_settlements.json").read_text(encoding="utf-8"))
    assert len(settlements) == 1
    assert settlements[0]["created_by"] == "boss1"
    assert settlements[0]["teacher_count"] == 1

    plugin._on_pre_gateway_dispatch(
        event=_event_from("生成工资结算快照", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    settlements = json.loads((_isolated_tuoguan_home / "payroll_settlements.json").read_text(encoding="utf-8"))
    assert len(settlements) == 1


@pytest.mark.asyncio
async def test_teacher_cannot_create_payroll_settlement_snapshot(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("生成工资结算快照", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result is None
    assert not (_isolated_tuoguan_home / "payroll_settlements.json").exists()


@pytest.mark.asyncio
async def test_boss_payroll_export_requires_settlement_snapshot(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("导出工资表", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "请老板先发送：生成工资结算快照" in args[1]
    assert not (_isolated_tuoguan_home / "exports" / "payroll").exists()


@pytest.mark.asyncio
async def test_boss_can_export_payroll_csv_after_settlement_snapshot(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "payroll_rules.json",
        """
        {
          "people": {
            "teacher1": {"name": "王老师", "position": "full_time_teacher", "admission_target": 5}
          }
        }
        """,
    )
    adapter = SimpleNamespace(send=AsyncMock())

    plugin._on_pre_gateway_dispatch(
        event=_event_from("生成工资结算快照", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("导出工资表", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "已导出本月工资表" in args[1]
    path = _isolated_tuoguan_home / "exports" / "payroll"
    exports = list(path.glob("payroll-*.csv"))
    assert len(exports) == 1
    raw = exports[0].read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    content = raw.decode("utf-8-sig")
    assert "月份,姓名,user_id,岗位,预计工资" in content
    assert "王老师,teacher1,全职老师" in content


@pytest.mark.asyncio
async def test_teacher_cannot_export_payroll_csv(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("导出工资表", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result is None
    assert not (_isolated_tuoguan_home / "exports" / "payroll").exists()


@pytest.mark.asyncio
async def test_teacher_growth_report_draft_and_parent_share_link(_isolated_tuoguan_home, monkeypatch):
    import json
    from datetime import datetime

    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_teacher(_isolated_tuoguan_home)
    monkeypatch.setenv("HERMES_TUOGUAN_DASHBOARD_BASE_URL", "https://example.test/h")
    _write_json(
        _isolated_tuoguan_home / "students.json",
        """
        {
          "周温暖": {
            "teacher": "teacher1",
            "campus_id": "main",
            "learning_goals": ["继续巩固数学计算。"]
          }
        }
        """,
    )
    _write_json(
        _isolated_tuoguan_home / "records.json",
        json.dumps(
            [
                {
                    "student_name": "周温暖",
                    "teacher": "teacher1",
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "content": "周温暖今天数学计算完成认真，错题能主动订正。",
                    "tags": ["正向成长", "数学计算弱"],
                    "record_types": ["positive_progress", "student_daily"],
                }
            ],
            ensure_ascii=False,
        ),
    )
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event("生成周温暖周报告"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "已生成周温暖周报告草稿" in args[1]
    assert "确认周温暖周报告" in args[1]
    reports = json.loads((_isolated_tuoguan_home / "growth_reports.json").read_text(encoding="utf-8"))
    assert reports[0]["status"] == "draft"
    assert reports[0]["period_type"] == "weekly"
    assert reports[0]["parent_summary"]

    plugin._on_pre_gateway_dispatch(
        event=_event("确认周温暖周报告"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    args, _kwargs = adapter.send.await_args
    assert "下面这段可以直接复制到微信发给家长" in args[1]
    assert "https://example.test/h/tuoguan/r/" in args[1]
    assert "token=" not in args[1]
    assert "【优益托管｜周温暖本周成长反馈】" in args[1]
    assert "点击查看孩子本期成长反馈" in args[1]
    assert "不包含内部记录和风险判断" not in args[1]
    assert "已审核，可转发给家长查看" not in args[1]
    reports = json.loads((_isolated_tuoguan_home / "growth_reports.json").read_text(encoding="utf-8"))
    assert reports[0]["status"] == "approved"
    assert reports[0]["approved_share_token_created_at"]
    links = json.loads((_isolated_tuoguan_home / "growth_report_links.json").read_text(encoding="utf-8"))
    assert len(links) == 1
    assert next(iter(links.values()))["report_id"] == reports[0]["id"]


@pytest.mark.asyncio
async def test_growth_report_does_not_create_empty_report(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_teacher(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "students.json",
        """
        {
          "周温暖": {"teacher": "teacher1", "campus_id": "main"}
        }
        """,
    )
    _write_json(_isolated_tuoguan_home / "records.json", "[]")
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event("生成周温暖月报告"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "暂不生成空报告" in args[1]
    assert not (_isolated_tuoguan_home / "growth_reports.json").exists()


@pytest.mark.asyncio
async def test_growth_report_understands_natural_language_teacher_requests(_isolated_tuoguan_home, monkeypatch):
    import json
    from datetime import datetime

    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_teacher(_isolated_tuoguan_home)
    monkeypatch.setenv("HERMES_TUOGUAN_DASHBOARD_BASE_URL", "https://example.test/h")
    _write_json(
        _isolated_tuoguan_home / "students.json",
        """
        {
          "周温暖": {"teacher": "teacher1", "campus_id": "main"}
        }
        """,
    )
    _write_json(
        _isolated_tuoguan_home / "records.json",
        json.dumps(
            [
                {
                    "student_name": "周温暖",
                    "teacher": "teacher1",
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "content": "周温暖今天阅读表达更主动，作业效率也比上周好。",
                    "tags": ["正向成长", "阅读理解弱"],
                    "record_types": ["positive_progress", "student_daily"],
                }
            ],
            ensure_ascii=False,
        ),
    )
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event("帮我整理一下周温暖这周发给妈妈看的表现反馈"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "已生成周温暖周报告草稿" in args[1]

    plugin._on_pre_gateway_dispatch(
        event=_event("周温暖这个周反馈没问题，可以发给家长看了"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    args, _kwargs = adapter.send.await_args
    assert "下面这段可以直接复制到微信发给家长" in args[1]
    assert "/tuoguan/r/" in args[1]
    assert "token=" not in args[1]


@pytest.mark.asyncio
async def test_growth_report_context_confirmation_accepts_simple_ok(_isolated_tuoguan_home, monkeypatch):
    import json
    from datetime import datetime

    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_teacher(_isolated_tuoguan_home)
    monkeypatch.setenv("HERMES_TUOGUAN_DASHBOARD_BASE_URL", "https://example.test/h")
    _write_json(
        _isolated_tuoguan_home / "students.json",
        """
        {
          "周温暖": {"teacher": "teacher1", "campus_id": "main"}
        }
        """,
    )
    _write_json(
        _isolated_tuoguan_home / "records.json",
        json.dumps(
            [
                {
                    "student_name": "周温暖",
                    "teacher": "teacher1",
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "content": "周温暖今天书写更认真，完成作业速度也提升了。",
                    "tags": ["正向成长"],
                    "record_types": ["positive_progress", "student_daily"],
                }
            ],
            ensure_ascii=False,
        ),
    )
    adapter = SimpleNamespace(send=AsyncMock())

    plugin._on_pre_gateway_dispatch(
        event=_event("生成周温暖周报告"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    result = plugin._on_pre_gateway_dispatch(
        event=_event("可以"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "下面这段可以直接复制到微信发给家长" in args[1]
    assert "/tuoguan/r/" in args[1]
    assert "token=" not in args[1]


@pytest.mark.asyncio
async def test_growth_report_natural_language_without_student_asks_for_name(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_teacher(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock())

    result = plugin._on_pre_gateway_dispatch(
        event=_event("帮我整理一下这周发给家长看的反馈"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    args, _kwargs = adapter.send.await_args
    assert "请带上学生姓名" in args[1]


def test_competitor_research_uses_local_public_source_fallback(_isolated_tuoguan_home, monkeypatch):
    import json
    from datetime import datetime, timezone

    from plugins.tuoguan_core.research import collect_public_research
    from plugins.tuoguan_core.store import TuoguanStore

    monkeypatch.setattr(
        "plugins.tuoguan_core.research._ddgs_search",
        lambda _query, _limit: {"success": True, "data": {"web": []}},
    )

    result = collect_public_research(
        TuoguanStore(),
        kind="competitor",
        search=lambda _query, _limit: {"success": False, "error": "No web search provider configured"},
        now=datetime(2026, 6, 20, 9, 30, tzinfo=timezone.utc),
    )

    assert result["evidence_count"] == 3
    assert all(item["query"] == "项城本地公开来源监控" for item in result["evidence"])
    assert any("hngh.org" in item["url"] for item in result["evidence"])
    assert any("used curated Xiangcheng public-source fallback" in item for item in result["errors"])
    latest = json.loads((_isolated_tuoguan_home / "competitor_research_latest.json").read_text(encoding="utf-8"))
    assert latest["evidence_count"] == 3


def test_knowledge_research_records_search_errors_and_creates_pending(_isolated_tuoguan_home, monkeypatch):
    import json
    from datetime import datetime, timezone

    from plugins.tuoguan_core.research import collect_public_research
    from plugins.tuoguan_core.store import TuoguanStore

    monkeypatch.setattr(
        "plugins.tuoguan_core.research._ddgs_search",
        lambda _query, _limit: {
            "success": True,
            "data": {
                "web": [
                    {
                        "title": "After-school parent communication improves retention",
                        "url": "https://example.test/after-school-retention",
                        "description": "After-school program parent communication and student retention case.",
                    }
                ]
            },
        },
    )

    result = collect_public_research(
        TuoguanStore(),
        kind="knowledge",
        search=lambda _query, _limit: {"success": False, "error": "No web search provider configured"},
        now=datetime(2026, 6, 20, 9, 35, tzinfo=timezone.utc),
    )

    assert result["evidence_count"] == 1
    assert result["pending_id"].startswith("pending_")
    assert any("No web search provider configured" in item for item in result["errors"])
    pending = json.loads((_isolated_tuoguan_home / "pending_knowledge.json").read_text(encoding="utf-8"))
    assert pending[0]["id"] == result["pending_id"]
    assert pending[0]["evidence"][0]["url"] == "https://example.test/after-school-retention"


@pytest.mark.asyncio
async def test_conversation_state_links_student_duplicate_from_short_reply(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin
    from plugins.tuoguan_core.conversation_state import remember_conversation_state
    from plugins.tuoguan_core.identity import IdentityService
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.summer_enrollment import bulk_import_summer_students

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    _write_json(
        _isolated_tuoguan_home / "students.json",
        json.dumps({"金小金": {"grade": "二年级", "phone": "13800000000", "teacher": "teacher1"}}, ensure_ascii=False),
    )
    store = TuoguanStore(_isolated_tuoguan_home)
    result = bulk_import_summer_students(
        store,
        [{"孩子姓名": "小金", "年级": "二年级", "暑假班分组": "一二年级组", "家长联系电话": "13800000000"}],
        actor_userid="boss1",
        actor_role="boss",
        confirmed=True,
    )
    issue_id = result["duplicate_pending"][0]["id"]
    identity = IdentityService(store).resolve("wecom_callback", "boss1", user_name="老板", chat_id="wwcorp:boss1", message_text="")
    remember_conversation_state(
        store,
        identity,
        state_type="pending_student_duplicate_confirm",
        last_system_prompt="发现疑似已有学生档案，请确认：1 关联为同一个学生 2 创建为新学生 3 暂不导入",
        expected_replies=["关联", "新建", "跳过"],
        payload={"issue_id": issue_id, "candidate_student_name": "金小金", "import_student_name": "小金"},
        source_handler="summer_import",
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    dispatch = plugin._on_pre_gateway_dispatch(
        event=_event_from("关联", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert dispatch == {"action": "skip", "reason": "tuoguan_core_handled"}
    sent = "\n".join(call.args[1] for call in adapter.send.await_args_list)
    assert "已关联为同一个学生" in sent
    issues = json.loads((_isolated_tuoguan_home / "summer_import_issues.json").read_text(encoding="utf-8"))
    assert issues[0]["status"] == "linked_existing"
    assert not json.loads((_isolated_tuoguan_home / "conversation_state.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_teacher_handoff_confirmation_uses_conversation_state(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    dispatch = plugin._on_pre_gateway_dispatch(
        event=_event_from("王老师离职，张老师从7月1日起接手王老师负责的孩子。", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    assert dispatch == {"action": "skip", "reason": "tuoguan_core_handled"}
    state = json.loads((_isolated_tuoguan_home / "conversation_state.json").read_text(encoding="utf-8"))["boss1"]
    assert state["state_type"] == "pending_teacher_handoff_confirm"

    adapter.send.reset_mock()
    dispatch = plugin._on_pre_gateway_dispatch(
        event=_event_from("确认执行", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    assert dispatch == {"action": "skip", "reason": "tuoguan_core_handled"}
    sent = "\n".join(call.args[1] for call in adapter.send.await_args_list)
    assert "已确认配置变更" in sent
    changes = json.loads((_isolated_tuoguan_home / "pending_config_changes.json").read_text(encoding="utf-8"))
    assert changes[-1]["status"] == "confirmed_pending_execution"


@pytest.mark.asyncio
async def test_weekly_feedback_state_modifies_only_current_child(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin
    from plugins.tuoguan_core.conversation_state import remember_conversation_state
    from plugins.tuoguan_core.identity import IdentityService
    from plugins.tuoguan_core.store import TuoguanStore

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    store = TuoguanStore(_isolated_tuoguan_home)
    identity = IdentityService(store).resolve("wecom_callback", "boss1", user_name="老板", chat_id="wwcorp:boss1", message_text="")
    remember_conversation_state(
        store,
        identity,
        state_type="pending_weekly_feedback_child_review",
        last_system_prompt="现在生成第 1/40 个孩子，小金反馈草稿，请确认/修改/跳过。",
        expected_replies=["确认", "修改", "跳过", "简短生成"],
        payload={"student_name": "小金", "index": 1, "total": 40},
        source_handler="weekly_feedback",
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    dispatch = plugin._on_pre_gateway_dispatch(
        event=_event_from("改得温和一点", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert dispatch == {"action": "skip", "reason": "tuoguan_core_handled"}
    sent = "\n".join(call.args[1] for call in adapter.send.await_args_list)
    assert "小金" in sent
    assert "只影响这个孩子" in sent


@pytest.mark.asyncio
async def test_low_record_child_state_accepts_short_generate(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin
    from plugins.tuoguan_core.conversation_state import remember_conversation_state
    from plugins.tuoguan_core.identity import IdentityService
    from plugins.tuoguan_core.store import TuoguanStore

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    store = TuoguanStore(_isolated_tuoguan_home)
    identity = IdentityService(store).resolve("wecom_callback", "boss1", user_name="老板", chat_id="wwcorp:boss1", message_text="")
    remember_conversation_state(
        store,
        identity,
        state_type="pending_weekly_feedback_child_review",
        last_system_prompt="小金本周记录较少，补充 / 简短生成 / 跳过。",
        expected_replies=["补充", "简短生成", "跳过"],
        payload={"student_name": "小金", "reason": "low_record"},
        source_handler="weekly_feedback",
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    dispatch = plugin._on_pre_gateway_dispatch(
        event=_event_from("简短生成", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert dispatch == {"action": "skip", "reason": "tuoguan_core_handled"}
    sent = "\n".join(call.args[1] for call in adapter.send.await_args_list)
    assert "小金" in sent
    assert "简短反馈草稿" in sent


@pytest.mark.asyncio
async def test_no_state_short_reply_does_not_execute_business_action(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_teacher(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    dispatch = plugin._on_pre_gateway_dispatch(
        event=_event_from("可以", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert dispatch == {"action": "skip", "reason": "tuoguan_core_handled"}
    sent = "\n".join(call.args[1] for call in adapter.send.await_args_list)
    assert "还没有识别到具体要处理的事项" in sent
    assert not (_isolated_tuoguan_home / "records.json").exists()


@pytest.mark.asyncio
async def test_conversation_state_is_isolated_by_user(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    plugin._on_pre_gateway_dispatch(
        event=_event_from("王老师离职，张老师从7月1日起接手王老师负责的孩子。", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    adapter.send.reset_mock()
    dispatch = plugin._on_pre_gateway_dispatch(
        event=_event_from("确认执行", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert dispatch == {"action": "skip", "reason": "tuoguan_core_handled"}
    sent = "\n".join(call.args[1] for call in adapter.send.await_args_list)
    assert "只有金总/老板账号可以确认执行" in sent
    changes = json.loads((_isolated_tuoguan_home / "pending_config_changes.json").read_text(encoding="utf-8"))
    assert changes[-1]["status"] == "waiting_confirmation"
    state = json.loads((_isolated_tuoguan_home / "conversation_state.json").read_text(encoding="utf-8"))
    assert "boss1" in state
    assert "teacher1" not in state


@pytest.mark.asyncio
async def test_expired_conversation_state_rejects_short_confirmation(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_payroll_users(_isolated_tuoguan_home)
    expired = {
        "boss1": {
            "user_id": "boss1",
            "role": "boss",
            "state_type": "pending_general_confirmation",
            "last_system_prompt": "请确认是否执行。",
            "expected_replies": ["确认"],
            "payload": {},
            "created_at": (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds"),
            "expires_at": (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"),
            "source_handler": "test",
        }
    }
    _write_json(_isolated_tuoguan_home / "conversation_state.json", json.dumps(expired, ensure_ascii=False))
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))

    dispatch = plugin._on_pre_gateway_dispatch(
        event=_event_from("确认", "boss1", "老板"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()

    assert dispatch == {"action": "skip", "reason": "tuoguan_core_handled"}
    sent = "\n".join(call.args[1] for call in adapter.send.await_args_list)
    assert "该确认已过期" in sent
    assert not json.loads((_isolated_tuoguan_home / "conversation_state.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_callback_summer_lesson_record_defaults_to_summer_program(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_summer_mode_users(_isolated_tuoguan_home)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    result = plugin._on_pre_gateway_dispatch(
        event=_event_from(
            "三四年级组数学课第1课时，课堂整体：练习完成稳定。重点孩子：暑假小明订正后能说出错因。",
            "teacher1", "王老师",
        ),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    reply = adapter.send.await_args.args[1]
    assert "已记录到 2026暑假班" in reply
    assert "计绩效" not in reply
    lessons = json.loads((_isolated_tuoguan_home / "summer_lesson_records.json").read_text(encoding="utf-8"))
    assert lessons[-1]["program_id"] == "summer_2026"


@pytest.mark.asyncio
async def test_callback_frozen_regular_record_requires_program_confirmation(_isolated_tuoguan_home):
    import plugins.tuoguan_core as plugin

    plugin._ROUTER = None
    _seed_summer_mode_users(_isolated_tuoguan_home)
    staff = json.loads((_isolated_tuoguan_home / "staff.json").read_text(encoding="utf-8"))
    staff["teacher1"] = {"role": "teacher", "program_ids": ["regular_tuoguan"]}
    _write_json(_isolated_tuoguan_home / "staff.json", json.dumps(staff, ensure_ascii=False))
    students = json.loads((_isolated_tuoguan_home / "students.json").read_text(encoding="utf-8"))
    students["托管小红"]["teacher"] = "teacher1"
    _write_json(_isolated_tuoguan_home / "students.json", json.dumps(students, ensure_ascii=False))
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    result = plugin._on_pre_gateway_dispatch(
        event=_event_from("托管小红今天作业完成认真，我提醒后又检查了一遍。", "teacher1", "王老师"),
        gateway=_gateway(adapter),
        session_store=SimpleNamespace(),
    )
    await asyncio_sleep()
    assert result == {"action": "skip", "reason": "tuoguan_core_handled"}
    assert "项目归属还不明确" in adapter.send.await_args.args[1]


async def asyncio_sleep() -> None:
    import asyncio

    await asyncio.sleep(0)
