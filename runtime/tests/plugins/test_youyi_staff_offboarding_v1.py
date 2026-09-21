from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path


def _write(path: Path, name: str, payload) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _seed(path: Path, *, duplicate_name: bool = False) -> None:
    staff = {
        "boss1": {"user_id": "boss1", "name": "机构负责人", "role": "boss", "status": "active"},
        "teacher1": {"user_id": "teacher1", "name": "王老师", "role": "teacher", "status": "active"},
    }
    members = [
        {"user_id": "boss1", "name": "机构负责人", "status": 1},
        {"user_id": "teacher1", "name": "王老师", "status": 1},
    ]
    mapping = {"机构负责人": "boss1", "王老师": "teacher1"}
    allowed = ["teacher1"]
    roles = {"boss1": "boss", "teacher1": "teacher"}
    if duplicate_name:
        staff["teacher2"] = {"user_id": "teacher2", "name": "王老师", "role": "teacher", "status": "active"}
        members.append({"user_id": "teacher2", "name": "王老师", "status": 1})
        mapping["另一位王老师"] = "teacher2"
        allowed.append("teacher2")
        roles["teacher2"] = "teacher"
    _write(path, "staff.json", staff)
    _write(path, "wecom_directory_cache.json", {"status": "ok", "members": members})
    _write(path, "teacher_wecom_map.json", mapping)
    _write(path, "wecom_whitelist.json", {
        "super_users": ["boss1"],
        "allowed_users": allowed,
        "manager_ids": [],
        "summer_manager_ids": [],
        "pending_users": [],
        "rejected_users": [],
        "user_roles": roles,
    })
    _write(path, "notification_outbox.json", [
        {
            "id": "pending-teacher1",
            "touser": "teacher1",
            "content": "待发送任务提醒",
            "status": "pending",
            "delivery_mode": "direct_wecom",
        },
        {
            "id": "pending-boss1",
            "touser": "boss1",
            "content": "老板提醒",
            "status": "pending",
            "delivery_mode": "direct_wecom",
        },
    ])


def _service(path: Path, user_id: str):
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    return TuoguanToolService(store=TuoguanStore(path), platform="wecom", user_id=user_id)


def test_boss_can_offboard_staff_and_preserve_history(tmp_path):
    from plugins.tuoguan_core.staff_directory import query_staff_directory
    from plugins.tuoguan_core.store import TuoguanStore

    _seed(tmp_path)
    result = _service(tmp_path, "boss1").offboard_staff(
        operation_id="offboard-message-1",
        target_name="王老师",
        reason="已经离职",
    )

    assert result["ok"] is True
    assert result["data"]["writeback_verified"] is True
    assert result["data"]["history_preserved"] is True
    assert result["data"]["wecom_directory_modified"] is False
    staff = json.loads((tmp_path / "staff.json").read_text(encoding="utf-8"))
    whitelist = json.loads((tmp_path / "wecom_whitelist.json").read_text(encoding="utf-8"))
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert staff["teacher1"]["status"] == "left"
    assert "teacher1" not in whitelist["allowed_users"]
    assert "teacher1" in whitelist["rejected_users"]
    assert next(item for item in outbox if item["id"] == "pending-teacher1")["status"] == "suppressed"
    assert next(item for item in outbox if item["id"] == "pending-boss1")["status"] == "pending"
    assert (tmp_path / "staff_offboarding_events.jsonl").exists()
    assert query_staff_directory(TuoguanStore(tmp_path), query="王老师")["result_count"] == 0
    inactive = query_staff_directory(TuoguanStore(tmp_path), query="王老师", include_inactive=True)
    assert inactive["result_count"] == 1
    assert inactive["staff"][0]["membership_status"] == "已离职停用（保留历史）"


def test_non_boss_cannot_offboard_staff(tmp_path):
    _seed(tmp_path)

    result = _service(tmp_path, "teacher1").offboard_staff(
        operation_id="teacher-offboard-attempt",
        target_name="王老师",
    )

    assert result["ok"] is False
    assert result["error"] == "permission_denied"
    staff = json.loads((tmp_path / "staff.json").read_text(encoding="utf-8"))
    assert staff["teacher1"]["status"] == "active"


def test_owner_account_is_protected_from_offboarding(tmp_path):
    _seed(tmp_path)

    result = _service(tmp_path, "boss1").offboard_staff(
        operation_id="owner-self-offboard-attempt",
        target_user_id="boss1",
    )

    assert result["ok"] is False
    assert result["error"] == "protected_owner_account"


def test_ambiguous_staff_name_does_not_change_permissions(tmp_path):
    _seed(tmp_path, duplicate_name=True)

    result = _service(tmp_path, "boss1").offboard_staff(
        operation_id="ambiguous-offboard-attempt",
        target_name="王老师",
    )

    assert result["ok"] is False
    assert result["error"] == "ambiguous_target"
    whitelist = json.loads((tmp_path / "wecom_whitelist.json").read_text(encoding="utf-8"))
    assert set(whitelist["allowed_users"]) == {"teacher1", "teacher2"}


def test_offboarded_staff_cannot_receive_new_task(tmp_path):
    _seed(tmp_path)
    boss = _service(tmp_path, "boss1")
    assert boss.offboard_staff(
        operation_id="offboard-before-task",
        target_user_id="teacher1",
    )["ok"] is True

    result = boss.create_task(
        title="离职后不应收到的任务",
        assignee_user_id="teacher1",
        operation_id="task-after-offboard",
    )

    assert result["ok"] is False
    assert result["error"] == "target_staff_inactive"
    assert not (tmp_path / "tasks.json").exists()


def test_outbox_rechecks_offboarded_recipient_before_send(tmp_path):
    from plugins.tuoguan_core import _claim_next_notification_outbox_item
    from plugins.tuoguan_core.store import TuoguanStore

    _seed(tmp_path)
    whitelist = json.loads((tmp_path / "wecom_whitelist.json").read_text(encoding="utf-8"))
    whitelist["allowed_users"] = []
    whitelist["rejected_users"] = ["teacher1"]
    _write(tmp_path, "wecom_whitelist.json", whitelist)

    claim = _claim_next_notification_outbox_item(
        TuoguanStore(tmp_path),
        excluded_task_ids=set(),
        now=datetime.now().astimezone(),
    )

    assert claim["event"] == "notification_suppressed"
    assert claim["result"] == "recipient_staff_offboarded"
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert next(item for item in outbox if item["id"] == "pending-teacher1")["status"] == "suppressed"


def test_people_facade_exposes_owner_offboarding_without_expanding_tool_count():
    from plugins.tuoguan_core.capability_facades import operation_manifest
    from plugins.tuoguan_core.tools import model_tools

    manifest = operation_manifest()
    tools = {name: schema for name, schema, _handler in model_tools("facade")}

    assert manifest["model_visible_tool_count"] == 23
    assert manifest["operations"]["offboard_staff"]["roles"] == ["boss"]
    assert manifest["operations"]["offboard_staff"]["access"] == "write"
    assert "offboard_staff" in tools["tuoguan_people"]["parameters"]["properties"]["operation"]["enum"]
