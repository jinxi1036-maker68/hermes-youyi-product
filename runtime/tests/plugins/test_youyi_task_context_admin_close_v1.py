from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path


def _write_json(path: Path, name: str, payload) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _seed_store(tmp_path: Path):
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(tmp_path, "write_guard_config.json", {"enabled": False})
    _write_json(
        tmp_path,
        "wecom_whitelist.json",
        {
            "super_users": ["boss1"],
            "allowed_users": ["boss1", "teacher1"],
            "user_roles": {"boss1": "boss", "teacher1": "teacher"},
        },
    )
    _write_json(tmp_path, "teacher_wecom_map.json", {"金总": "boss1", "李老师": "teacher1"})
    _write_json(
        tmp_path,
        "staff.json",
        {
            "boss1": {"user_id": "boss1", "name": "金总", "role": "super_admin"},
            "teacher1": {"user_id": "teacher1", "name": "李老师", "role": "teacher", "campus_ids": ["main"]},
        },
    )
    _write_json(tmp_path, "students.json", {})
    _write_json(tmp_path, "tasks.json", [])
    _write_json(tmp_path, "notification_outbox.json", [])
    return TuoguanStore(tmp_path)


def test_model_created_task_persists_teacher_context_for_natural_completion(tmp_path):
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    store = _seed_store(tmp_path)
    service = TuoguanToolService(
        store=store,
        platform="wecom_callback",
        user_id="boss1",
        user_name="金总",
        chat_id="boss1",
        session_key="boss1",
    )

    result = service.create_task(
        title="明天早上8点跟小金家长沟通，沟通后汇报给老板",
        assignee_user_id="teacher1",
        operation_id="op-create-task-1",
        due_at="2026-08-06T08:00:00",
        level="B",
        student_name="小金",
    )

    assert result["ok"] is True
    task_id = result["task_id"]
    active = json.loads((tmp_path / "active_task_context.json").read_text(encoding="utf-8"))
    assert active["teacher1"]["task_id"] == task_id
    assert active["teacher1"]["expires_at"] > "2026-08-05T22:00:00"
    pending = json.loads((tmp_path / "pending_next_task_context.json").read_text(encoding="utf-8"))
    assert pending["teacher1"]["source"] == "new_task_notification"


def test_boss_can_close_own_manual_assignment_and_suppress_pending_notifications(tmp_path):
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.router import TuoguanRouter

    store = _seed_store(tmp_path)
    task = {
        "id": "task_manual_1",
        "title": "明天早上8点跟小金家长沟通",
        "type": "manual_assignment",
        "level": "B",
        "status": "pending",
        "student_name": "小金",
        "assignee_userid": "teacher1",
        "assignee_name": "李老师",
        "assignee_role": "teacher",
        "created_by": "boss1",
        "created_at": datetime(2026, 8, 5, 21, 30).isoformat(timespec="seconds"),
        "updated_at": datetime(2026, 8, 5, 21, 30).isoformat(timespec="seconds"),
    }
    _write_json(tmp_path, "tasks.json", [task])
    _write_json(
        tmp_path,
        "model_focus.json",
        {"agent:main:wecom_callback:dm:corp:boss1": {"task_id": "task_manual_1", "updated_at": "2026-08-05T22:05:57"}},
    )
    _write_json(
        tmp_path,
        "notification_outbox.json",
        [
            {
                "id": "task_manual_1:teacher:task_created",
                "task_id": "task_manual_1",
                "role": "teacher",
                "touser": "teacher1",
                "action": "task_created",
                "status": "pending",
                "delivery_mode": "direct_wecom",
                "content": "你收到一项新任务",
            }
        ],
    )

    identity = UserIdentity("wecom_callback", "boss1", "boss1", "金总", "boss", "approved")
    reply = TuoguanRouter(store)._route_admin_close_task(identity, "这个任务闭关了吧，原因：刚才安排错了")

    assert reply is not None
    assert "已关闭任务" in reply.reply
    saved = store.load_tasks()[0]
    assert saved["status"] == "closed_by_admin"
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert outbox[0]["status"] == "suppressed"
    assert outbox[0]["suppressed_reason"] == "task_closed_by_supervisor"


def test_outbox_naive_deliver_at_is_compared_in_local_timezone():
    from plugins.tuoguan_core.__init__ import _parse_outbox_datetime

    now = datetime(2026, 8, 8, 18, 0, tzinfo=timezone(timedelta(hours=8)))
    parsed = _parse_outbox_datetime("2026-08-06 08:00", now=now)

    assert parsed.tzinfo == now.tzinfo
    assert parsed < now


def test_stale_outbox_items_are_suppressed_instead_of_backfilled():
    from plugins.tuoguan_core.__init__ import _stale_outbox_suppression_reason

    now = datetime(2026, 8, 8, 18, 0, tzinfo=timezone(timedelta(hours=8)))

    assert _stale_outbox_suppression_reason(
        {"notification_type": "autonomous_daily_report", "created_at": "2026-08-08T08:30:00+08:00"},
        now=now,
    ) == "stale_daily_report_after_outbox_block"
    assert _stale_outbox_suppression_reason(
        {"notification_type": "autonomous_owner_attention", "created_at": "2026-08-08T12:00:00+08:00"},
        now=now,
    ) == "stale_owner_attention_after_outbox_block"
    assert _stale_outbox_suppression_reason(
        {"action": "task_due", "created_at": "2026-08-05T21:36:26"},
        now=now,
    ) == "stale_task_notification_after_outbox_block"
