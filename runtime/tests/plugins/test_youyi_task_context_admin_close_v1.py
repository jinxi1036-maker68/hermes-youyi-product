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
    _write_json(tmp_path, "teacher_wecom_map.json", {"机构负责人": "boss1", "示例老师": "teacher1"})
    _write_json(
        tmp_path,
        "staff.json",
        {
            "boss1": {"user_id": "boss1", "name": "机构负责人", "role": "super_admin"},
            "teacher1": {"user_id": "teacher1", "name": "示例老师", "role": "teacher", "campus_ids": ["main"]},
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
        user_name="机构负责人",
        chat_id="boss1",
        session_key="boss1",
    )

    result = service.create_task(
        title="明天早上8点跟学生丙家长沟通，沟通后汇报给老板",
        assignee_user_id="teacher1",
        operation_id="op-create-task-1",
        due_at="2026-08-06T08:00:00",
        level="B",
        student_name="学生丙",
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


def test_renewal_task_persists_full_companion_contract(tmp_path):
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    store = _seed_store(tmp_path)
    service = TuoguanToolService(
        store=store,
        platform="wecom_callback",
        user_id="boss1",
        user_name="机构负责人",
        chat_id="boss1",
        session_key="boss1",
    )

    result = service.create_task(
        title="今晚8点联系李依晨家长沟通下学期续费事宜",
        assignee_user_id="teacher1",
        operation_id="op-renewal-contract",
        due_at="2026-08-13T20:00:00+08:00",
        student_name="李依晨",
    )

    assert result["ok"] is True
    task = store.load_tasks()[0]
    assert task["source_text"] == "今晚8点联系李依晨家长沟通下学期续费事宜"
    assert task["task_contract"]["task_domain"] == "renewal_conversation"
    assert task["task_contract"]["coaching_mode"] == "adaptive_companion"
    assert task["task_contract"]["completion_policy"] == "natural_confirmation"
    assert any("真实原因" in item for item in task["task_contract"]["guidance_points"])
    assert any("陪你一步一步" in item["content"] for item in json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8")))


def test_teacher_contextual_student_query_is_scoped_to_active_task(tmp_path, monkeypatch):
    from plugins.tuoguan_core import runtime_foundation
    from plugins.tuoguan_core.tasks import task_companion_context
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    store = _seed_store(tmp_path)
    _write_json(
        tmp_path,
        "students.json",
        {
            "学生丙": {"teacher": "teacher1", "phone": "old-student"},
            "李依晨": {"teacher": "teacher1", "phone": "current-student"},
        },
    )
    boss = TuoguanToolService(store=store, platform="wecom_callback", user_id="boss1", user_name="机构负责人", chat_id="boss1", session_key="boss1")
    created = boss.create_task(
        title="今晚8点联系李依晨家长沟通下学期续费事宜",
        assignee_user_id="teacher1",
        operation_id="op-current-liyichen",
        due_at="2026-08-13T20:00:00+08:00",
        student_name="李依晨",
    )
    teacher = TuoguanToolService(store=store, platform="wecom_callback", user_id="teacher1", user_name="示例老师", chat_id="teacher1", session_key="wecom_callback:teacher1")
    monkeypatch.setattr(runtime_foundation, "current_raw_text", lambda _user_id: "我怎么给他家长沟通呀")

    queried = teacher.query_students()
    assert queried["ok"] is True
    assert queried["data"]["scope_reason"] == "active_task_student"
    assert [item["name"] for item in queried["data"]["students"]] == ["李依晨"]
    assert queried["data"]["active_task"]["id"] == created["task_id"]

    context = task_companion_context(store, identity=teacher.identity, raw_text="我不知道怎么说")
    assert "任务对象：李依晨" in context
    assert "学生丙" not in context
    assert "陪伴式工作" in context


def test_parent_script_rejects_student_from_old_session(tmp_path, monkeypatch):
    from plugins.tuoguan_core import runtime_foundation
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    store = _seed_store(tmp_path)
    _write_json(tmp_path, "students.json", {"学生丙": {"teacher": "teacher1"}, "李依晨": {"teacher": "teacher1"}})
    boss = TuoguanToolService(store=store, platform="wecom_callback", user_id="boss1", user_name="机构负责人", chat_id="boss1", session_key="boss1")
    boss.create_task(
        title="今晚联系李依晨家长沟通续费",
        assignee_user_id="teacher1",
        operation_id="op-entity-guard-task",
        student_name="李依晨",
    )
    teacher = TuoguanToolService(store=store, platform="wecom_callback", user_id="teacher1", user_name="示例老师", chat_id="teacher1", session_key="wecom_callback:teacher1")
    monkeypatch.setattr(runtime_foundation, "current_raw_text", lambda _user_id: "我不知道怎么说")

    result = teacher.parent_script_context(request="帮我准备沟通内容", student_name="学生丙")

    assert result["ok"] is False
    assert result["error"] == "active_task_entity_mismatch"
    assert result["data"]["expected_student_name"] == "李依晨"


def test_active_task_result_cannot_create_duplicate_record_task(tmp_path, monkeypatch):
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    store = _seed_store(tmp_path)
    _write_json(tmp_path, "students.json", {"李依晨": {"teacher": "teacher1", "program_id": "regular_tuoguan"}})
    boss = TuoguanToolService(store=store, platform="wecom_callback", user_id="boss1", user_name="机构负责人", chat_id="boss1", session_key="boss1")
    created = boss.create_task(
        title="今晚联系李依晨家长沟通续费",
        assignee_user_id="teacher1",
        operation_id="op-no-duplicate-task",
        student_name="李依晨",
    )
    teacher = TuoguanToolService(store=store, platform="wecom_callback", user_id="teacher1", user_name="示例老师", chat_id="teacher1", session_key="wecom_callback:teacher1")
    teacher._approved = lambda: None
    monkeypatch.setattr(teacher, "_trusted_runtime_raw_text", lambda _operation: "我已经沟通过了，他家长说到开学的时候再考虑")

    result = teacher.record_student(
        student_name="李依晨",
        content="家长态度中立，需要后续跟进。",
        operation_id="op-wrong-record-tool",
    )

    assert result["ok"] is False
    assert result["error"] == "wrong_tool_for_active_task_update"
    assert result["data"]["task_id"] == created["task_id"]
    assert len(store.load_tasks()) == 1
    assert store.read_json("records.json", []) == []


def test_ordinary_renewal_contact_closes_with_parent_response_and_next_condition(tmp_path):
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    store = _seed_store(tmp_path)
    boss = TuoguanToolService(store=store, platform="wecom_callback", user_id="boss1", user_name="机构负责人", chat_id="boss1", session_key="boss1")
    created = boss.create_task(
        title="今晚联系李依晨家长沟通续费",
        assignee_user_id="teacher1",
        operation_id="op-rigorous-renewal",
        student_name="李依晨",
    )
    teacher = TuoguanToolService(store=store, platform="wecom_callback", user_id="teacher1", user_name="示例老师", chat_id="teacher1", session_key="wecom_callback:teacher1")

    first = teacher.update_task(
        task_id=created["task_id"],
        reply="我已经沟通过了，他家长说到开学的时候再考虑。",
        operation_id="op-renewal-evidence-1",
    )
    assert first["ok"] is True
    assert first["data"]["task"]["status"] == "completed"
    assert first["data"]["missing_fields"] == []
    assert "任务已完成" in first["message"]


def test_task_update_uses_live_teacher_words_not_model_enrichment(tmp_path, monkeypatch):
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    store = _seed_store(tmp_path)
    boss = TuoguanToolService(store=store, platform="wecom_callback", user_id="boss1", user_name="机构负责人", chat_id="boss1", session_key="boss1")
    created = boss.create_task(
        title="今晚联系李依晨家长沟通续费",
        assignee_user_id="teacher1",
        operation_id="op-trusted-teacher-words",
        student_name="李依晨",
    )
    teacher = TuoguanToolService(store=store, platform="wecom_callback", user_id="teacher1", user_name="示例老师", chat_id="teacher1", session_key="wecom_callback:teacher1")
    teacher_words = "我已经沟通过了，他家长说到开学的时候再考虑。"
    monkeypatch.setattr(teacher, "_trusted_runtime_raw_text", lambda _operation: teacher_words)

    result = teacher.update_task(
        task_id=created["task_id"],
        reply="家长态度中立，未拒绝；老师已解释服务价值；开学前继续跟进。",
        operation_id="op-reject-model-enrichment",
    )

    assert result["ok"] is True
    assert result["data"]["task"]["status"] == "completed"
    assert result["data"]["task"]["evidence_summary"] == teacher_words
    assert "态度中立" not in result["data"]["task"]["evidence_summary"]


def test_boss_can_cancel_just_created_task_by_focus_and_clear_context(tmp_path):
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    store = _seed_store(tmp_path)
    service = TuoguanToolService(
        store=store,
        platform="wecom_callback",
        user_id="boss1",
        user_name="机构负责人",
        chat_id="boss1",
        session_key="boss1",
    )
    created = service.create_task(
        title="今天下午4:30跟学生丙家长沟通",
        assignee_user_id="teacher1",
        operation_id="op-create-cancel-focus",
        due_at="2026-08-09T16:30:00+08:00",
        level="B",
        student_name="学生丙",
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


def test_update_task_refuses_cancel_intent_and_points_to_cancel_tool(tmp_path):
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    store = _seed_store(tmp_path)
    service = TuoguanToolService(
        store=store,
        platform="wecom_callback",
        user_id="boss1",
        user_name="机构负责人",
        chat_id="boss1",
        session_key="boss1",
    )
    created = service.create_task(
        title="今天下午4:30跟学生丙家长沟通",
        assignee_user_id="teacher1",
        operation_id="op-create-wrong-update-cancel",
        due_at="2026-08-09T16:30:00+08:00",
        level="B",
        student_name="学生丙",
    )

    result = service.update_task(
        task_id=created["task_id"],
        reply="把这个测试任务直接关掉，不用再提醒",
        operation_id="op-update-wrong-tool-cancel",
    )

    assert result["ok"] is True
    assert result["data"]["result_action"] == "wrong_tool_for_cancel_intent"
    assert result["data"]["suggested_tool"] == "tuoguan_cancel_task"
    assert result["data"]["no_write_performed"] is True
    assert result["data"]["writeback_verified"] is False
    assert result["execution_receipt"]["status"] == "clarification_required"
    saved = store.load_tasks()[0]
    assert saved["status"] == "pending"
    assert "cancelled_at" not in saved


def test_teacher_completion_uses_active_context_before_expired_model_focus(tmp_path):
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    store = _seed_store(tmp_path)
    boss = TuoguanToolService(store=store, platform="wecom_callback", user_id="boss1", user_name="机构负责人", chat_id="boss1", session_key="boss1")
    contact = boss.create_task(
        title="下午4点联系机构负责人",
        assignee_user_id="teacher1",
        operation_id="op-active-contact",
        due_at="2026-08-24T16:00:00+08:00",
    )
    old = boss.create_task(
        title="旧任务，不应被完成",
        assignee_user_id="teacher1",
        operation_id="op-old-focus",
        due_at="2026-08-20T16:00:00+08:00",
    )
    _write_json(tmp_path, "active_task_context.json", {
        "teacher1": {
            "task_id": contact["task_id"],
            "expires_at": (datetime.now().astimezone() + timedelta(hours=1)).isoformat(timespec="seconds"),
        },
    })
    _write_json(tmp_path, "model_focus.json", {
        "wecom_callback:teacher1": {
            "task_id": old["task_id"],
            "focus_source": "task_created",
            "focus_expires_at": "2026-08-20T18:00:00+08:00",
        },
    })
    teacher = TuoguanToolService(store=store, platform="wecom_callback", user_id="teacher1", user_name="示例老师", chat_id="teacher1", session_key="wecom_callback:teacher1")

    result = teacher.update_task(reply="完成了", operation_id="op-complete-active")

    tasks = {item["id"]: item for item in store.load_tasks()}
    assert result["ok"] is True
    assert result["data"]["task_id"] == contact["task_id"]
    assert result["data"]["task"]["status"] == "completed"
    assert tasks[contact["task_id"]]["status"] == "completed"
    assert tasks[old["task_id"]]["status"] == "pending"


def test_parent_communication_manual_assignment_closes_from_natural_teacher_evidence(tmp_path):
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    store = _seed_store(tmp_path)
    boss = TuoguanToolService(
        store=store,
        platform="wecom_callback",
        user_id="boss1",
        user_name="机构负责人",
        chat_id="boss1",
        session_key="boss1",
    )
    created = boss.create_task(
        title="学生丙家长沟通任务",
        assignee_user_id="teacher1",
        operation_id="op-parent-comm-create",
        due_at="2026-08-09T18:00:00+08:00",
        level="B",
        student_name="学生丙",
    )
    assert created["ok"] is True
    teacher = TuoguanToolService(
        store=store,
        platform="wecom_callback",
        user_id="teacher1",
        user_name="示例老师",
        chat_id="teacher1",
        session_key="",
    )

    first = teacher.update_task(reply="学生丙妈妈说孩子最近挺好，也很感谢咱们。", operation_id="op-parent-comm-1")
    second = teacher.update_task(reply="她很满意，我下一步准备再继续跟进。", operation_id="op-parent-comm-2")

    assert first["ok"] is True
    assert first["data"]["result_action"] in {"fact_added", "completed"}
    assert second["ok"] is True
    saved = store.load_tasks()[0]
    assert saved["status"] == "completed"
    # A normal task closes when the credible contact result arrives; a later
    # message without an active task focus is not silently attached to history.
    assert "妈妈说孩子最近挺好" in saved["evidence_summary"]


def test_boss_can_close_own_manual_assignment_and_suppress_pending_notifications(tmp_path):
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.router import TuoguanRouter

    store = _seed_store(tmp_path)
    task = {
        "id": "task_manual_1",
        "title": "明天早上8点跟学生丙家长沟通",
        "type": "manual_assignment",
        "level": "B",
        "status": "pending",
        "student_name": "学生丙",
        "assignee_userid": "teacher1",
        "assignee_name": "示例老师",
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

    identity = UserIdentity("wecom_callback", "boss1", "boss1", "机构负责人", "boss", "approved")
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
                "title": "请示例老师明天10点汇报沟通结果",
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
    assert pending["teacher1"]["original_owner_text"] == "请示例老师明天10点汇报沟通结果"


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
