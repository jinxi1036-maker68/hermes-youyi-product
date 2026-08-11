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
    focus = json.loads((tmp_path / "model_focus.json").read_text(encoding="utf-8"))
    assert focus["boss1"]["task_id"] == task_id
    assert focus["boss1"]["focus_source"] == "task_created"
    assert focus["wecom_callback:teacher1"]["task_id"] == task_id
    assert focus["wecom_callback:teacher1"]["focus_source"] == "task_created"


def test_boss_can_cancel_just_created_task_by_focus_and_clear_context(tmp_path):
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
    created = service.create_task(
        title="今天下午4:30跟小金家长沟通",
        assignee_user_id="teacher1",
        operation_id="op-create-cancel-focus",
        due_at="2026-08-09T16:30:00+08:00",
        level="B",
        student_name="小金",
    )

    assert created["ok"] is True
    cancelled = service.cancel_task(
        reason="中途取消，不用做了",
        operation_id="op-cancel-focus",
    )

    assert cancelled["ok"] is True
    assert cancelled["data"]["writeback_verified"] is True
    assert cancelled["data"]["result_action"] == "cancelled"
    saved = store.load_tasks()[0]
    assert saved["status"] == "cancelled"
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert all(item["status"] == "suppressed" for item in outbox)
    active = json.loads((tmp_path / "active_task_context.json").read_text(encoding="utf-8"))
    pending = json.loads((tmp_path / "pending_next_task_context.json").read_text(encoding="utf-8"))
    focus = json.loads((tmp_path / "model_focus.json").read_text(encoding="utf-8"))
    assert "teacher1" not in active
    assert "teacher1" not in pending
    assert all(item.get("task_id") != created["task_id"] for item in focus.values())


def test_parent_communication_manual_assignment_closes_from_natural_teacher_evidence(tmp_path):
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    store = _seed_store(tmp_path)
    boss = TuoguanToolService(
        store=store,
        platform="wecom_callback",
        user_id="boss1",
        user_name="金总",
        chat_id="boss1",
        session_key="boss1",
    )
    created = boss.create_task(
        title="小金家长沟通任务",
        assignee_user_id="teacher1",
        operation_id="op-parent-comm-create",
        due_at="2026-08-09T18:00:00+08:00",
        level="B",
        student_name="小金",
    )
    assert created["ok"] is True
    teacher = TuoguanToolService(
        store=store,
        platform="wecom_callback",
        user_id="teacher1",
        user_name="李老师",
        chat_id="teacher1",
        session_key="",
    )

    first = teacher.update_task(reply="小金妈妈说孩子最近挺好，也很感谢咱们。", operation_id="op-parent-comm-1")
    second = teacher.update_task(reply="她很满意，我下一步准备再继续跟进。", operation_id="op-parent-comm-2")

    assert first["ok"] is True
    assert first["data"]["result_action"] in {"fact_added", "completed"}
    assert second["ok"] is True
    saved = store.load_tasks()[0]
    assert saved["status"] == "completed"
    assert "下一步准备再继续跟进" in saved["evidence_summary"]


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


def test_repair_task_context_rebuilds_missing_manual_assignment_context(tmp_path):
    from plugins.tuoguan_core.repair_task_context_v1 import repair_missing_task_contexts

    store = _seed_store(tmp_path)
    _write_json(
        tmp_path,
        "tasks.json",
        [
            {
                "id": "task_manual_1",
                "type": "manual_assignment",
                "status": "active",
                "title": "请李老师明天10点汇报沟通结果",
                "level": "A",
                "assignee_userid": "teacher1",
                "created_by": "boss1",
                "due_at": "2026-08-06T10:00:00+08:00",
            }
        ],
    )
    _write_json(
        tmp_path,
        "notification_outbox.json",
        [
            {
                "id": "task_manual_1:teacher:task_created",
                "task_id": "task_manual_1",
                "action": "task_created",
                "status": "sent",
                "touser": "teacher1",
                "created_at": "2026-08-05T21:36:26+08:00",
            }
        ],
    )
    _write_json(tmp_path, "active_task_context.json", {})
    _write_json(tmp_path, "pending_next_task_context.json", {})

    result = repair_missing_task_contexts(
        store,
        now=datetime(2026, 8, 8, 10, 0, tzinfo=timezone(timedelta(hours=8))),
    )

    assert result["ok"] is True
    assert result["repaired_count"] == 1
    assert result["writeback_verified"] is True
    active = json.loads((tmp_path / "active_task_context.json").read_text(encoding="utf-8"))
    pending = json.loads((tmp_path / "pending_next_task_context.json").read_text(encoding="utf-8"))
    assert active["teacher1"]["task_id"] == "task_manual_1"
    assert active["teacher1"]["latest_outbox_id"] == "task_manual_1:teacher:task_created"
    assert pending["teacher1"]["original_owner_text"] == "请李老师明天10点汇报沟通结果"


def test_repair_task_context_skips_boss_assigned_legacy_tasks(tmp_path):
    from plugins.tuoguan_core.repair_task_context_v1 import repair_missing_task_contexts

    store = _seed_store(tmp_path)
    _write_json(
        tmp_path,
        "tasks.json",
        [
            {
                "id": "task_boss_legacy",
                "type": "manual_assignment",
                "status": "pending",
                "title": "老板自己的历史测试任务",
                "assignee_userid": "boss1",
                "created_by": "boss1",
            }
        ],
    )
    _write_json(tmp_path, "active_task_context.json", {})
    _write_json(tmp_path, "pending_next_task_context.json", {})

    result = repair_missing_task_contexts(
        store,
        now=datetime(2026, 8, 8, 10, 0, tzinfo=timezone(timedelta(hours=8))),
    )

    assert result["ok"] is True
    assert result["repaired_count"] == 0
    assert json.loads((tmp_path / "active_task_context.json").read_text(encoding="utf-8")) == {}


def test_outbox_naive_deliver_at_is_compared_in_local_timezone():
    from plugins.tuoguan_core.__init__ import _parse_outbox_datetime

    now = datetime(2026, 8, 8, 18, 0, tzinfo=timezone(timedelta(hours=8)))
    parsed = _parse_outbox_datetime("2026-08-06 08:00", now=now)

    assert parsed.tzinfo == now.tzinfo
    assert parsed < now


def test_stale_outbox_items_are_suppressed_instead_of_backfilled():
    from plugins.tuoguan_core.__init__ import _stale_outbox_failure_reason, _stale_outbox_suppression_reason

    now = datetime(2026, 8, 8, 18, 0, tzinfo=timezone(timedelta(hours=8)))

    assert _stale_outbox_failure_reason(
        {"notification_type": "autonomous_daily_report", "created_at": "2026-08-08T08:30:00+08:00"},
        now=now,
    ) == "daily_report_delivery_window_missed_after_outbox_block"
    assert _stale_outbox_suppression_reason(
        {"notification_type": "autonomous_daily_report", "created_at": "2026-08-08T08:30:00+08:00"},
        now=now,
    ) == ""
    assert _stale_outbox_suppression_reason(
        {"notification_type": "autonomous_owner_attention", "created_at": "2026-08-08T12:00:00+08:00"},
        now=now,
    ) == "stale_owner_attention_after_outbox_block"
    assert _stale_outbox_suppression_reason(
        {"action": "task_due", "created_at": "2026-08-05T21:36:26"},
        now=now,
    ) == "stale_task_notification_after_outbox_block"
