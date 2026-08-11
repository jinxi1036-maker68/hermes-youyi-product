from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor


def test_wecom_receipt_claim_is_durable_and_concurrent(tmp_path):
    from plugins.platforms.wecom.inbound_receipts import WecomInboundReceiptStore

    path = tmp_path / "receipts.sqlite3"

    def claim_once() -> bool:
        store = WecomInboundReceiptStore(path)
        return bool(store.claim(app_name="youyi", message_id="msg-100", user_id="CeShi")["accepted"])

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
    assert store.mark_processed(second["receipt_key"], session_id="chat:CeShi") is True
    assert store.status_counts() == {"processed": 1}


def test_active_work_context_is_scoped_and_evidence_only(tmp_path):
    from plugins.tuoguan_core.active_work_context import query_active_work_context, render_active_work_context
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.store import TuoguanStore

    (tmp_path / "tasks.json").write_text(
        json.dumps(
            [
                {"id": "task-teacher", "title": "跟进小金情况", "status": "active", "assignee_userid": "CeShi", "updated_at": "2026-08-11T10:00:00"},
                {"id": "task-other", "title": "其他老师任务", "status": "active", "assignee_userid": "Other", "updated_at": "2026-08-11T11:00:00"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    identity = UserIdentity(
        platform="wecom_callback",
        platform_user_id="CeShi",
        canonical_user_id="CeShi",
        person_name="李老师",
        role="teacher",
        approval_state="approved",
    )
    result = query_active_work_context(TuoguanStore(tmp_path), identity=identity)

    assert result["context_count"] == 1
    assert result["contexts"][0]["context_id"] == "task-teacher"
    assert "其他老师任务" not in render_active_work_context(result)
    assert "下一工具" not in json.dumps(result, ensure_ascii=False)


def test_short_reply_receives_active_task_evidence(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from gateway.config import Platform
    import plugins.tuoguan_core as plugin
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.runtime_foundation import clear_runtime_state
    from plugins.tuoguan_core.store import TuoguanStore

    clear_runtime_state()
    (tmp_path / "tasks.json").write_text(
        json.dumps([{"id": "task-1", "title": "确认李老师任务结果", "status": "active", "created_by_userid": "boss1"}], ensure_ascii=False),
        encoding="utf-8",
    )
    identity = UserIdentity(
        platform="wecom_callback",
        platform_user_id="boss1",
        canonical_user_id="boss1",
        person_name="金总",
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
    assert "确认李老师任务结果" in result["context"]
    assert "短回复本身不是拒绝执行的理由" in result["context"]
