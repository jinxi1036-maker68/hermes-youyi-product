from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone


def _write_json(path: Path, name: str, payload) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _append_jsonl(path: Path, name: str, rows: list[dict]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with (path / name).open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def test_owner_attention_context_anchors_current_short_reply(tmp_path):
    from plugins.tuoguan_core.__init__ import _open_owner_attention_context
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _append_jsonl(
        tmp_path,
        "attention_threads.jsonl",
        [
            {
                "record_type": "attention_thread",
                "attention_id": "attention:old",
                "focus_key": "goal:old",
                "target_user_id": "JinWenJie",
                "status": "sent",
                "question_text": "旧问题：请确认旧事项。",
                "created_at": "2026-07-31T10:00:00+08:00",
                "updated_at": "2026-07-31T10:00:00+08:00",
            },
            {
                "record_type": "attention_thread",
                "attention_id": "attention:new",
                "focus_key": "goal:sept_renewal",
                "target_user_id": "JinWenJie",
                "status": "sent",
                "question_text": "金总，是否同意我按当前分析进入下一步准备，并把需要你审核的候选材料整理出来？",
                "needed_facts": ["是否同意进入下一步准备"],
                "created_at": "2026-08-01T08:30:28+08:00",
                "updated_at": "2026-08-01T08:30:41+08:00",
            },
        ],
    )

    context = _open_owner_attention_context(
        TuoguanStore(tmp_path),
        identity=SimpleNamespace(role="boss"),
        current_message="同意，你先整理候选材料",
    )

    assert "【优益主动提问回复锚点】" in context
    assert "老板本轮原话：同意，你先整理候选材料" in context
    assert "先自主判断老板本轮原话是否在回答" in context
    assert "tuoguan_update_attention_thread" in context
    assert context.index("attention:new") < context.index("attention:old")


def test_owner_attention_context_ignores_non_boss(tmp_path):
    from plugins.tuoguan_core.__init__ import _open_owner_attention_context
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _append_jsonl(
        tmp_path,
        "attention_threads.jsonl",
        [
            {
                "record_type": "attention_thread",
                "attention_id": "attention:new",
                "focus_key": "goal:sept_renewal",
                "target_user_id": "JinWenJie",
                "status": "sent",
                "question_text": "老板问题",
                "created_at": "2026-08-01T08:30:28+08:00",
            }
        ],
    )

    context = _open_owner_attention_context(
        TuoguanStore(tmp_path),
        identity=SimpleNamespace(role="teacher"),
        current_message="同意",
    )

    assert context == ""


def test_recent_outbound_context_anchors_owner_what_does_it_mean(tmp_path):
    from plugins.tuoguan_core.__init__ import _recent_owner_outbound_context
    from plugins.tuoguan_core.store import TuoguanStore

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    _write_json(
        tmp_path,
        "notification_outbox.json",
        [
            {
                "id": "autonomous_owner_attention:20260809:goal:goal_9f25347346525253",
                "notification_type": "autonomous_owner_attention",
                "action": "owner_attention",
                "status": "sent",
                "recipient_user_id": "JinWenJie",
                "sent_at": now,
                "content": "金总，两个进度卡在同一个点：1. '她'是冯老师还是李老师？2. 沟通结果我直接看记录还是您告知？确认后我继续历史分析和准备材料。",
            }
        ],
    )

    context = _recent_owner_outbound_context(
        TuoguanStore(tmp_path),
        identity=SimpleNamespace(role="boss", canonical_user_id="JinWenJie", platform_user_id="JinWenJie"),
        current_message="什么意思",
    )

    assert "【优益最近主动外发消息锚点】" in context
    assert "老板本轮原话：什么意思" in context
    assert "两个进度卡在同一个点" in context
    assert "不要把模糊代词接到更早的旧会话" in context


def test_recent_external_learning_anchor_wins_for_it_inside_follow_up(tmp_path):
    from plugins.tuoguan_core.__init__ import _recent_owner_outbound_context
    from plugins.tuoguan_core.store import TuoguanStore

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    _write_json(
        tmp_path,
        "notification_outbox.json",
        [
            {
                "id": "external_learning_report:20260810:weekly_industry",
                "notification_type": "external_learning_report",
                "action": "external_learning_weekly_industry",
                "status": "sent",
                "recipient_user_id": "JinWenJie",
                "sent_at": now,
                "summary": "小优托管行业学习周报",
                "content": (
                    "金总，我做了一轮托管/教培行业公开学习。"
                    "我看到的公开资料：1. 托管管理学术语 2. 托管综合服务平台。"
                    "我的判断：优先把外部方法转成续费证据、家校沟通话术、老师减负素材。"
                ),
            }
        ],
    )
    _append_jsonl(
        tmp_path,
        "message_history.jsonl",
        [
            {
                "direction": "outbound",
                "canonical_user_id": "JinWenJie",
                "source": "model",
                "message_text": "金总，看板链接给您：https://example.test/dashboard",
                "created_at": (now_dt - timedelta(hours=8)).isoformat(timespec="seconds"),
            }
        ],
    )

    context = _recent_owner_outbound_context(
        TuoguanStore(tmp_path),
        identity=SimpleNamespace(role="boss", canonical_user_id="JinWenJie", platform_user_id="JinWenJie"),
        current_message="你给讲讲，它里面都具体讲了什么内容",
    )

    assert "【优益最近主动外发消息锚点】" in context
    assert "anchor_priority: latest_active_outbound_thread" in context
    assert "external_learning_report:20260810:weekly_industry" in context
    assert "它/里面/这个/这些/链接/网址/内容/讲讲" in context
    assert "不要把模糊代词接到更早的旧会话、旧看板链接" in context
    assert "托管/教培行业公开学习" in context
    assert "看板链接给您" not in context


def test_recent_outbound_context_anchors_teacher_task_created_short_question(tmp_path):
    from plugins.tuoguan_core.__init__ import _recent_owner_outbound_context
    from plugins.tuoguan_core.store import TuoguanStore

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write_json(
        tmp_path,
        "notification_outbox.json",
        [
            {
                "id": "task_123:teacher:task_created",
                "action": "task_created",
                "status": "sent",
                "role": "teacher",
                "touser": "LiLaoShi",
                "sent_at": now,
                "content": "你收到一项新任务：今天放学前反馈小金沟通结果。请直接回复处理进展。",
            }
        ],
    )

    context = _recent_owner_outbound_context(
        TuoguanStore(tmp_path),
        identity=SimpleNamespace(role="teacher", canonical_user_id="LiLaoShi", platform_user_id="LiLaoShi"),
        current_message="什么意思",
    )

    assert "【优益最近主动外发消息锚点】" in context
    assert "老师本轮原话：什么意思" in context
    assert "今天放学前反馈小金沟通结果" in context
    assert "task_created" in context
