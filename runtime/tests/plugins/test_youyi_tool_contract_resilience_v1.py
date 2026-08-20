from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path


def _write_json(root: Path, name: str, payload) -> None:
    (root / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _seed_store(tmp_path: Path):
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(tmp_path, "write_guard_config.json", {"enabled": False})
    _write_json(
        tmp_path,
        "wecom_whitelist.json",
        {
            "super_users": ["boss1"],
            "allowed_users": ["teacher1"],
            "user_roles": {"boss1": "boss", "teacher1": "teacher"},
        },
    )
    _write_json(tmp_path, "teacher_wecom_map.json", {"金总": "boss1", "李老师": "teacher1"})
    _write_json(tmp_path, "staff.json", {"teacher1": {"name": "李老师", "role": "teacher"}})
    _write_json(
        tmp_path,
        "students.json",
        {
            "小明": {"teacher": "teacher1", "status": "active", "program_ids": ["regular_tuoguan"]},
            "小红": {"teacher": "teacher1", "status": "active", "program_ids": ["regular_tuoguan"]},
        },
    )
    _write_json(
        tmp_path,
        "tasks.json",
        [{"id": "task-1", "title": "跟进小明", "assignee_userid": "teacher1", "status": "pending", "level": "A"}],
    )
    _write_json(
        tmp_path,
        "records.json",
        [
            {
                "student_name": "小明",
                "content": "已与家长沟通今天的学习情况",
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        ],
    )
    with (tmp_path / "reply_ledger.jsonl").open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "message_id": "msg-1",
            "user_id": "teacher1",
            "role": "teacher",
            "raw_text": "我把小明的情况记录好了",
            "final_reply": "好的。",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }, ensure_ascii=False) + "\n")
    return TuoguanStore(tmp_path)


def test_observed_model_query_arguments_are_supported_without_dispatch_errors(tmp_path: Path):
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    service = TuoguanToolService(
        _seed_store(tmp_path),
        platform="wecom_callback",
        user_id="boss1",
        user_name="金总",
    )

    students = service.query_students(name="小明", limit=1)
    assert students["ok"] is True
    assert students["data"]["students"][0]["name"] == "小明"
    wrong_role = service.query_students(role="teacher")
    assert wrong_role["error"] == "wrong_tool_for_staff_query"

    tasks = service.query_tasks(assignee_user_id="teacher1", limit=1)
    assert tasks["ok"] is True
    assert tasks["data"]["task_ids"] == ["task-1"]

    weekly = service.query_weekly_record_coverage(student_name="小明", limit=1)
    parent = service.query_parent_communication_coverage(student_name="小明", limit=1)
    assert weekly["ok"] is True
    assert weekly["data"]["student_name_filter"] == "小明"
    assert parent["ok"] is True
    assert parent["data"]["covered_count"] == 1

    activity = service.query_staff_conversation_activity(teacher_name="李老师", limit=1)
    assert activity["ok"] is True
    assert activity["data"]["staff_contact_count"] == 1
    assert activity["data"]["conversations"][0]["user_id"] == "teacher1"


def test_unknown_tool_arguments_fail_cleanly_before_service_dispatch(monkeypatch):
    from plugins.tuoguan_core import tools

    class FakeService:
        def query_students(self, *, student_name: str = ""):
            return {"ok": True, "student_name": student_name}

    monkeypatch.setattr(tools, "_service", lambda _args: FakeService())
    result = json.loads(tools._handler("query_students")({"user_id": "boss1", "unknown": "value"}))

    assert result["ok"] is False
    assert result["error"] == "unsupported_arguments"
    assert result["data"]["unsupported_arguments"] == ["unknown"]


def test_missing_required_arguments_fail_cleanly_before_service_dispatch(monkeypatch):
    from plugins.tuoguan_core import tools

    class FakeService:
        def goal_workspace(self, *, action: str):
            raise AssertionError("missing required arguments must not reach the service")

    monkeypatch.setattr(tools, "_service", lambda _args: FakeService())
    result = json.loads(tools._handler("goal_workspace")({}))

    assert result["ok"] is False
    assert result["error"] == "missing_required_arguments"
    assert result["data"]["missing_required_arguments"] == ["action"]


def test_regular_tutoring_scope_does_not_turn_into_summer_scope(tmp_path: Path, monkeypatch):
    from plugins.tuoguan_core import runtime_foundation
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    store = _seed_store(tmp_path)
    students = store.read_json("students.json", {})
    students["暑假学生"] = {"teacher": "teacher1", "status": "active", "campus_id": "summer_2026"}
    store.write_json("students.json", students)
    store.write_json(
        "academic_term_state.json",
        {"service_relation_policy": "defer_until_new_term", "data_term": "previous_term"},
    )
    monkeypatch.setattr(runtime_foundation, "current_raw_text", lambda _user_id: "托管班，不是暑假班")
    service = TuoguanToolService(store, platform="wecom_callback", user_id="boss1", user_name="金总")

    result = service.query_students()

    assert result["ok"] is True
    assert result["data"]["count"] == 2
    assert result["data"]["query_scope"] == "regular"
    assert "正式托管历史名单共2名" in result["data"]["rendered_text"]
    assert "不等于已确认的新学期在读人数" in result["data"]["rendered_text"]


def test_simple_student_count_and_dashboard_use_authoritative_tool_rendering():
    from plugins.tuoguan_core.runtime_foundation import _authoritative_read_reply

    student_item = {
        "raw_text": "现在托管班有多少孩子，不是暑假班",
        "tool_calls": [{"tool": "tuoguan_query_students"}],
        "tool_results": [{
            "ok": True,
            "data": {
                "rendered_text": "正式托管历史名单共122名学生。当前处于新学期过渡期。",
            },
        }],
    }
    dashboard_item = {
        "raw_text": "把看板链接发给我",
        "tool_results": [{
            "ok": True,
            "data": {
                "legacy_tool": "tuoguan_dashboard_link",
                "rendered_text": "这是你的老板端托管 AI 看板链接：https://example.test/dashboard?token=signed",
            },
        }],
    }

    assert _authoritative_read_reply(student_item).startswith("正式托管历史名单共122名")
    assert "https://example.test/dashboard" in _authoritative_read_reply(dashboard_item)


def test_core_contract_requires_direct_visible_tool_calls():
    from plugins.tuoguan_core import _xiaoyou_core_skill_context
    from plugins.tuoguan_core.models import UserIdentity

    identity = UserIdentity("wecom_callback", "boss1", "boss1", "金总", "boss", "approved")
    context = _xiaoyou_core_skill_context(identity=identity)

    assert "必须直接调用该工具" in context
    assert "禁止再套用 tool_call" in context
    assert "operation_id 使用当前消息 id" in context


def test_trusted_session_identity_is_not_a_model_required_argument():
    from plugins.tuoguan_core.tools import (
        TUOGUAN_QUERY_STAFF_DIRECTORY_SCHEMA,
        TUOGUAN_RECORD_STUDENT_SCHEMA,
    )

    query_required = TUOGUAN_QUERY_STAFF_DIRECTORY_SCHEMA["parameters"]["required"]
    write_required = TUOGUAN_RECORD_STUDENT_SCHEMA["parameters"]["required"]

    assert "user_id" not in query_required
    assert "user_id" not in write_required
    assert "operation_id" not in write_required
    assert "student_name" in write_required
    assert "content" in write_required


def test_write_handler_injects_trusted_message_id(monkeypatch):
    from plugins.tuoguan_core import tools

    observed = {}

    class FakeService:
        def record_student(self, *, student_name: str, content: str, operation_id: str):
            observed["operation_id"] = operation_id
            return {"ok": True}

    monkeypatch.setattr(tools, "_service", lambda _args: FakeService())
    monkeypatch.setattr(
        tools,
        "get_session_env",
        lambda key, default="": "msg-trusted-1" if key == "HERMES_SESSION_MESSAGE_ID" else default,
    )

    result = json.loads(tools._handler("record_student")({"student_name": "小明", "content": "今天进步明显"}))

    assert result["ok"] is True
    assert observed["operation_id"] == "msg-trusted-1"


def test_outbox_worker_units_use_version_neutral_runtime():
    root = Path(__file__).resolve().parents[3]
    service = (root / "systemd" / "hermes-youyi-notification-outbox.service").read_text(encoding="utf-8")
    path_unit = (root / "systemd" / "hermes-youyi-notification-outbox.path").read_text(encoding="utf-8")
    timer = (root / "systemd" / "hermes-youyi-notification-outbox.timer").read_text(encoding="utf-8")

    assert "/opt/hermes-youyi-current/.venv/bin/python" in service
    assert "notification_outbox_runner" in service
    assert "PathChanged=/opt/hermes-youyi/data/tuoguan-data/notification_outbox.json" in path_unit
    assert "OnUnitInactiveSec=60s" in timer


def test_v020_autonomous_dropin_restores_model_led_loop():
    root = Path(__file__).resolve().parents[3]
    content = (root / "systemd" / "hermes-youyi-autonomous-employee-020.conf").read_text(encoding="utf-8")

    assert "HERMES_AUTONOMOUS_EMPLOYEE_LOOP=1" in content
    assert "HERMES_AUTONOMOUS_WAKEUP_MATERIALIZE_LEDGER=1" in content
    assert "HERMES_MULTI_AGENT_SHADOW=1" in content
