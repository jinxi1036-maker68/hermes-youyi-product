from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace


def test_wecom_receipt_claim_is_durable_and_concurrent(tmp_path):
    from plugins.platforms.wecom.inbound_receipts import WecomInboundReceiptStore

    path = tmp_path / "receipts.sqlite3"

    def claim_once() -> bool:
        store = WecomInboundReceiptStore(path)
        return bool(store.claim(app_name="youyi", message_id="msg-100", user_id="teacher_test")["accepted"])

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _value: claim_once(), range(16)))

    assert results.count(True) == 1
    assert results.count(False) == 15
    restarted = WecomInboundReceiptStore(path)
    assert restarted.claim(app_name="youyi", message_id="msg-100")["duplicate"] is True


def test_failed_wecom_receipt_can_be_reclaimed(tmp_path):
    from plugins.platforms.wecom.inbound_receipts import WecomInboundReceiptStore

    store = WecomInboundReceiptStore(tmp_path / "receipts.sqlite3")
    first = store.claim(app_name="youyi", message_id="msg-failed")
    assert first["accepted"] is True
    assert store.mark_failed(first["receipt_key"], "queue_failed") is True
    second = store.claim(app_name="youyi", message_id="msg-failed")
    assert second["accepted"] is True
    assert second["reclaimed"] is True
    assert store.mark_processed(second["receipt_key"], session_id="chat:teacher_test") is True
    assert store.status_counts() == {"processed": 1}
    health = store.health_snapshot()
    assert health["received_count"] == 1
    assert health["replied_count"] == 1
    assert health["reply_failed_count"] == 0


def test_wecom_receipt_exposes_processing_before_final_reply(tmp_path):
    from plugins.platforms.wecom.inbound_receipts import WecomInboundReceiptStore

    store = WecomInboundReceiptStore(tmp_path / "receipts.sqlite3")
    receipt = store.claim(app_name="youyi", message_id="msg-processing")
    assert store.mark_processing(receipt["receipt_key"]) is True
    health = store.health_snapshot()
    assert health["processing_count"] == 1
    assert store.mark_failed(receipt["receipt_key"], "provider_timeout") is True
    assert store.health_snapshot()["reply_failed_count"] == 1


def test_unfinished_wecom_payload_survives_process_restart(tmp_path):
    from plugins.platforms.wecom.inbound_receipts import WecomInboundReceiptStore

    path = tmp_path / "receipts.sqlite3"
    first_process = WecomInboundReceiptStore(path)
    claimed = first_process.claim(
        app_name="youyi",
        message_id="msg-durable-payload",
        user_id="teacher_test",
        session_id="corp:teacher_test",
        payload={"app_name": "youyi", "xml_text": "<xml><MsgId>msg-durable-payload</MsgId></xml>"},
    )
    assert claimed["accepted"] is True

    restarted_process = WecomInboundReceiptStore(path)
    recovered = restarted_process.recover_pending()

    assert len(recovered) == 1
    assert recovered[0]["receipt_key"] == claimed["receipt_key"]
    assert recovered[0]["attempt_count"] == 2
    assert recovered[0]["payload"]["app_name"] == "youyi"
    assert "msg-durable-payload" in recovered[0]["payload"]["xml_text"]
    assert restarted_process.recover_pending() == []


def test_wecom_receipt_is_finalized_after_dispatch_not_before(tmp_path):
    from plugins.platforms.wecom.callback_adapter import WecomCallbackAdapter

    adapter = WecomCallbackAdapter.__new__(WecomCallbackAdapter)
    calls: list[tuple[str, str]] = []
    adapter._inbound_receipts = SimpleNamespace(
        mark_processed=lambda key, session_id="": calls.append(("processed", key)) or True,
        mark_failed=lambda key, error: calls.append(("failed", key)) or True,
    )

    async def handled(_event):
        calls.append(("handled", "receipt-1"))

    adapter.handle_message = handled
    asyncio.run(
        adapter._dispatch_claimed_message(
            SimpleNamespace(),
            "receipt-1",
            "corp:teacher1",
        )
    )

    assert calls == [("handled", "receipt-1"), ("processed", "receipt-1")]


def test_wecom_failed_dispatch_keeps_receipt_retryable(tmp_path):
    from plugins.platforms.wecom.callback_adapter import WecomCallbackAdapter

    adapter = WecomCallbackAdapter.__new__(WecomCallbackAdapter)
    calls: list[tuple[str, str]] = []
    adapter._inbound_receipts = SimpleNamespace(
        mark_processed=lambda key, session_id="": calls.append(("processed", key)) or True,
        mark_failed=lambda key, error: calls.append(("failed", key)) or True,
    )

    async def failed(_event):
        raise RuntimeError("dispatch failed")

    adapter.handle_message = failed
    asyncio.run(
        adapter._dispatch_claimed_message(
            SimpleNamespace(),
            "receipt-2",
            "corp:teacher1",
        )
    )

    assert calls == [("failed", "receipt-2")]


def test_wecom_multi_app_resolution_fails_closed_for_unknown_bare_user():
    from plugins.platforms.wecom.callback_adapter import WecomCallbackAdapter

    adapter = WecomCallbackAdapter.__new__(WecomCallbackAdapter)
    adapter._apps = [
        {"name": "tenant-a", "corp_id": "corp-a"},
        {"name": "tenant-b", "corp_id": "corp-b"},
    ]
    adapter._user_app_map = {}

    assert adapter._resolve_app_for_chat("teacher1") is None
    adapter._user_app_map["corp-a:teacher1"] = "tenant-a"
    assert adapter._resolve_app_for_chat("teacher1")["name"] == "tenant-a"


def test_wecom_tuoguan_import_falls_back_to_v020_dynamic_namespace(monkeypatch):
    from plugins.platforms.wecom import callback_adapter

    sentinel = object()
    requested = []

    def fake_import(name):
        requested.append(name)
        if name.startswith("plugins.tuoguan_core"):
            error = ModuleNotFoundError(name)
            error.name = "plugins.tuoguan_core"
            raise error
        return sentinel

    monkeypatch.setattr(callback_adapter.importlib, "import_module", fake_import)

    assert callback_adapter._import_tuoguan_module("dashboard_http") is sentinel
    assert requested == [
        "plugins.tuoguan_core.dashboard_http",
        "hermes_plugins.tuoguan_core.dashboard_http",
    ]


def test_completed_runtime_turn_cannot_authorize_or_leak_raw_text(tmp_path):
    from plugins.tuoguan_core.runtime_foundation import (
        begin_inbound,
        clear_runtime_state,
        current_raw_text,
        ensure_outbound_reply_recorded,
        inject_model_context,
        write_authorization_for,
    )
    from plugins.tuoguan_core.store import TuoguanStore

    clear_runtime_state()
    store = TuoguanStore(tmp_path)
    manual_context = tmp_path / "manual_context"
    manual_context.mkdir()
    (manual_context / "hermes_model_context_injection_allowlist_v1.json").write_text(
        json.dumps({"runtime_foundation": {"enabled": True}, "allowed_capability_cards": []}),
        encoding="utf-8",
    )
    begin_inbound(
        store=store,
        message_id="turn-cleanup-1",
        conversation_id="corp:boss1",
        user_id="boss1",
        role="boss",
        raw_text="把刚才任务关掉",
    )
    inject_model_context(
        session_id="session-cleanup-1",
        sender_id="boss1",
        user_message="把刚才任务关掉",
    )
    assert write_authorization_for("boss1", "cancel_task") is not None

    ensure_outbound_reply_recorded(
        store=store,
        message_id="turn-cleanup-1",
        conversation_id="corp:boss1",
        user_id="boss1",
        role="boss",
        raw_text="把刚才任务关掉",
        final_reply="我需要先用任务工具确认。",
        entered_model=True,
        session_id="session-cleanup-1",
    )

    assert current_raw_text("boss1") == ""
    assert write_authorization_for("boss1", "cancel_task") is None


def test_active_work_context_is_scoped_and_evidence_only(tmp_path):
    from plugins.tuoguan_core.active_work_context import query_active_work_context, render_active_work_context
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.store import TuoguanStore

    (tmp_path / "tasks.json").write_text(
        json.dumps(
            [
                {"id": "task-teacher", "title": "跟进学生丙情况", "status": "active", "assignee_userid": "teacher_test", "updated_at": "2026-08-11T10:00:00"},
                {"id": "task-other", "title": "其他老师任务", "status": "active", "assignee_userid": "Other", "updated_at": "2026-08-11T11:00:00"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    identity = UserIdentity(
        platform="wecom_callback",
        platform_user_id="teacher_test",
        canonical_user_id="teacher_test",
        person_name="示例老师",
        role="teacher",
        approval_state="approved",
    )
    result = query_active_work_context(TuoguanStore(tmp_path), identity=identity)

    assert result["context_count"] == 1
    assert result["contexts"][0]["context_id"] == "task-teacher"
    assert "其他老师任务" not in render_active_work_context(result)
    assert "下一工具" not in json.dumps(result, ensure_ascii=False)


def test_active_work_context_unifies_outbound_daily_market_and_tool_evidence(tmp_path):
    from plugins.tuoguan_core.active_work_context import query_active_work_context, render_active_work_context
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.store import TuoguanStore

    now = datetime.now().astimezone()
    recent = (now - timedelta(minutes=20)).isoformat(timespec="seconds")
    (tmp_path / "notification_outbox.json").write_text(
        json.dumps(
            [
                {
                    "id": "owner_attention_recent",
                    "notification_type": "autonomous_owner_attention",
                    "status": "sent",
                    "touser": "boss1",
                    "summary": "金钟两个进度卡在同一个点，需要老板确认。",
                    "sent_at": recent,
                },
                {
                    "id": "daily_recent",
                    "notification_type": "autonomous_daily_report",
                    "status": "sent",
                    "touser": "boss1",
                    "summary": "小优早报",
                    "sent_at": recent,
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "reply_ledger.jsonl").write_text(
        json.dumps(
            {
                "ledger_id": "ledger-tool-1",
                "user_id": "boss1",
                "raw_text": "关闭学生丙任务",
                "final_reply": "已关闭任务。",
                "used_tool_registry_entry": "tuoguan_cancel_task",
                "writeback_verified": True,
                "completed_at": recent,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "social_market_research_candidates.jsonl").write_text(
        json.dumps(
                {
                    "candidate_id": "market-1",
                    "platform": "douyin",
                    "query": "项城托管招生",
                    "source_id": "video-market-1",
                    "url": "https://www.douyin.com/video/video-market-1",
                    "title": "项城托管招生短视频观察",
                    "evidence_level": "platform_observation",
                    "status": "candidate",
                "collected_at": recent,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    identity = UserIdentity("wecom_callback", "boss1", "boss1", "机构负责人", "boss", "approved")

    result = query_active_work_context(TuoguanStore(tmp_path), identity=identity, limit=6)
    types = {item["context_type"] for item in result["contexts"]}
    rendered = render_active_work_context(result)

    assert {"recent_outbound", "daily_report", "recent_tool_result", "social_market_research"} <= types
    assert "当前时间=" in rendered
    assert "金钟两个进度卡在同一个点" in rendered
    assert "tuoguan_cancel_task" in rendered


def test_short_reply_receives_active_task_evidence(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from gateway.config import Platform
    import plugins.tuoguan_core as plugin
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.runtime_foundation import clear_runtime_state
    from plugins.tuoguan_core.store import TuoguanStore

    clear_runtime_state()
    (tmp_path / "tasks.json").write_text(
        json.dumps([{"id": "task-1", "title": "确认示例老师任务结果", "status": "active", "created_by_userid": "boss1"}], ensure_ascii=False),
        encoding="utf-8",
    )
    identity = UserIdentity(
        platform="wecom_callback",
        platform_user_id="boss1",
        canonical_user_id="boss1",
        person_name="机构负责人",
        role="boss",
        approval_state="approved",
    )
    store = TuoguanStore(tmp_path)
    fake_router = SimpleNamespace(store=store, identities=SimpleNamespace(resolve=lambda *args, **kwargs: identity))
    monkeypatch.setattr(plugin, "_router", lambda: fake_router)

    result = plugin._on_pre_llm_call(
        platform=Platform.WECOM_CALLBACK,
        sender_id="boss1",
        session_id="session-short",
        turn_id="turn-short",
        user_message="可以",
    )

    assert result is not None
    assert "当前活动工作线程" in result["context"]
    assert "确认示例老师任务结果" in result["context"]
    assert "短回复本身不是拒绝执行的理由" in result["context"]


def test_short_question_and_cancel_reply_receive_recent_outbound_anchor(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    from gateway.config import Platform
    import plugins.tuoguan_core as plugin
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.runtime_foundation import clear_runtime_state
    from plugins.tuoguan_core.store import TuoguanStore

    clear_runtime_state()
    recent = (datetime.now().astimezone() - timedelta(minutes=10)).isoformat(timespec="seconds")
    (tmp_path / "notification_outbox.json").write_text(
        json.dumps(
            [
                {
                    "id": "owner_attention_recent",
                    "notification_type": "autonomous_owner_attention",
                    "status": "sent",
                    "touser": "boss1",
                    "summary": "金钟两个进度卡在同一个点，需要老板确认。",
                    "sent_at": recent,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    identity = UserIdentity("wecom_callback", "boss1", "boss1", "机构负责人", "boss", "approved")
    store = TuoguanStore(tmp_path)
    fake_router = SimpleNamespace(store=store, identities=SimpleNamespace(resolve=lambda *args, **kwargs: identity))
    monkeypatch.setattr(plugin, "_router", lambda: fake_router)

    question = plugin._on_pre_llm_call(
        platform=Platform.WECOM_CALLBACK,
        sender_id="boss1",
        session_id="session-short-question",
        turn_id="turn-short-question",
        user_message="什么意思",
    )
    cancel = plugin._on_pre_llm_call(
        platform=Platform.WECOM_CALLBACK,
        sender_id="boss1",
        session_id="session-short-cancel",
        turn_id="turn-short-cancel",
        user_message="关掉",
    )

    assert question is not None and "金钟两个进度卡在同一个点" in question["context"]
    assert "什么意思/这个/展开" in question["context"]
    assert cancel is not None and "金钟两个进度卡在同一个点" in cancel["context"]
    assert "tuoguan_cancel_task" in cancel["context"]
