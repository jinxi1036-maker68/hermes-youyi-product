from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json


BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")


def _identity(user_id: str, role: str = "teacher"):
    from plugins.tuoguan_core.models import UserIdentity

    return UserIdentity(
        platform="wecom_callback", platform_user_id=user_id, canonical_user_id=user_id,
        person_name=user_id, role=role, approval_state="approved",
    )


def test_snapshot_is_identity_scoped_and_contains_beijing_time(tmp_path):
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.work_context_snapshot import build_work_context_snapshot

    now = datetime(2026, 8, 13, 18, 0, tzinfo=BEIJING)
    (tmp_path / "tasks.json").write_text(json.dumps([
        {"id": "teacher-task", "title": "Teacher work", "status": "active", "assignee_userid": "teacher-1", "updated_at": now.isoformat()},
        {"id": "boss-task", "title": "Boss work", "status": "active", "assignee_userid": "boss-1", "updated_at": now.isoformat()},
    ]), encoding="utf-8")

    snapshot = build_work_context_snapshot(
        TuoguanStore(tmp_path), identity=_identity("teacher-1"), platform="wecom_callback",
        app_id="tenant-app", session_id="session", message_id="message", now=now,
    )

    assert snapshot["actor_user_id"] == "teacher-1"
    assert snapshot["actor_role"] == "teacher"
    assert snapshot["timezone"] == "Asia/Shanghai"
    assert snapshot["current_time"].startswith("2026-08-13T18:00:00")
    assert [item["context_id"] for item in snapshot["candidate_threads"]] == ["teacher-task"]
    assert snapshot["authoritative_object_refs"] == ({
        "object_type": "task", "object_id": "teacher-task", "source": "tasks.json",
    },)
    assert snapshot["candidate_threads"][0]["authoritative_source"] == "tasks.json"


def test_snapshot_marks_multiple_threads_without_selecting_intent(tmp_path):
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.work_context_snapshot import build_work_context_snapshot, render_work_context_snapshot

    now = datetime(2026, 8, 13, 18, 0, tzinfo=BEIJING)
    (tmp_path / "tasks.json").write_text(json.dumps([
        {"id": "task-1", "title": "First", "status": "active", "assignee_userid": "teacher-1", "updated_at": now.isoformat()},
    ]), encoding="utf-8")
    (tmp_path / "notification_outbox.json").write_text(json.dumps([
        {"id": "notice-1", "notification_type": "relationship_touch", "status": "sent", "touser": "teacher-1", "content": "Question", "sent_at": now.isoformat()},
    ]), encoding="utf-8")

    snapshot = build_work_context_snapshot(
        TuoguanStore(tmp_path), identity=_identity("teacher-1"), platform="wecom_callback",
        app_id="tenant-app", session_id="session", message_id="message", now=now,
    )
    serialized = json.dumps(snapshot, ensure_ascii=False)
    rendered = render_work_context_snapshot(snapshot)

    assert snapshot["ambiguity_state"] == "multiple_candidates_model_must_disambiguate"
    assert "next_tool" not in serialized
    assert "intent" not in serialized
    assert "模型先判断" in rendered


def test_snapshot_excludes_stale_transient_context_but_keeps_open_task(tmp_path):
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.work_context_snapshot import build_work_context_snapshot

    now = datetime(2026, 8, 13, 18, 0, tzinfo=BEIJING)
    old = now - timedelta(days=5)
    (tmp_path / "tasks.json").write_text(json.dumps([
        {"id": "old-open-task", "title": "Still open", "status": "active", "assignee_userid": "teacher-1", "updated_at": old.isoformat()},
    ]), encoding="utf-8")
    (tmp_path / "notification_outbox.json").write_text(json.dumps([
        {"id": "old-notice", "notification_type": "relationship_touch", "status": "sent", "touser": "teacher-1", "content": "Old", "sent_at": old.isoformat()},
    ]), encoding="utf-8")

    snapshot = build_work_context_snapshot(
        TuoguanStore(tmp_path), identity=_identity("teacher-1"), platform="wecom_callback",
        app_id="tenant-app", session_id="session", message_id="message", now=now,
    )
    ids = {item["context_id"] for item in snapshot["candidate_threads"]}
    assert "old-open-task" in ids
    assert "old-notice" not in ids


def test_active_context_excludes_old_relationship_touch_candidate(tmp_path):
    from plugins.tuoguan_core.active_work_context import query_active_work_context
    from plugins.tuoguan_core.store import TuoguanStore

    now = datetime(2026, 8, 24, 18, 0, tzinfo=BEIJING)
    old = now - timedelta(days=5)
    (tmp_path / "relationship_touch_candidates.jsonl").write_text(
        json.dumps({
            "record_type": "relationship_touch_candidate",
            "candidate_id": "old-touch",
            "target_role": "teacher",
            "target_user_id": "teacher-1",
            "message": "李老师，请回复一条旧问题。",
            "status": "candidate",
            "created_at": old.isoformat(),
            "updated_at": old.isoformat(),
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    result = query_active_work_context(
        TuoguanStore(tmp_path), identity=_identity("teacher-1"), now=now,
    )

    assert all(item["context_id"] != "old-touch" for item in result["contexts"])


def test_recent_tool_context_does_not_repeat_chat_text(tmp_path):
    from plugins.tuoguan_core.active_work_context import query_active_work_context
    from plugins.tuoguan_core.store import TuoguanStore

    now = datetime.now(BEIJING)
    secret = "private chat sentence"
    (tmp_path / "reply_ledger.jsonl").write_text(json.dumps({
        "ledger_id": "ledger-1", "user_id": "teacher-1", "raw_text": secret,
        "final_reply": secret, "updated_at": now.isoformat(), "used_tool_registry_entry": "tuoguan_query_tasks",
        "tool_results": [{"ok": True, "data": {"count": 1}}],
    }) + "\n", encoding="utf-8")
    result = query_active_work_context(TuoguanStore(tmp_path), identity=_identity("teacher-1"))
    serialized = json.dumps(result, ensure_ascii=False)
    assert secret not in serialized
    assert "工具=tuoguan_query_tasks" in serialized
