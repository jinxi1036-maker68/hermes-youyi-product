from __future__ import annotations

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
            "allowed_users": ["teacher1"],
            "user_roles": {"boss1": "boss", "teacher1": "teacher"},
        },
    )
    _write_json(tmp_path, "teacher_wecom_map.json", {"机构负责人": "boss1", "示例老师": "teacher1"})
    _write_json(tmp_path, "staff.json", {"teacher1": {"user_id": "teacher1", "name": "示例老师", "role": "teacher"}})
    _write_json(tmp_path, "institution_operating_model.json", {"institution_name": "示例机构托管", "programs": {"regular_tuoguan": {"label": "正式托管"}}})
    _write_json(tmp_path, "students.json", {})
    _write_json(tmp_path, "records.json", [])
    _write_json(tmp_path, "tasks.json", [])
    _write_json(tmp_path, "notification_outbox.json", [])
    return TuoguanStore(tmp_path)


def _assert_no_router_keys(value):
    forbidden = {"intent", "next_tool", "workflow_step", "expected_reply", "model_intent"}
    if isinstance(value, dict):
        assert not (set(value.keys()) & forbidden)
        for item in value.values():
            _assert_no_router_keys(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_router_keys(item)


def test_employee_work_map_shows_known_unknown_and_fact_owners_without_routing(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import query_employee_work_map
    from plugins.tuoguan_core.models import UserIdentity

    store = _seed_store(tmp_path)
    identity = UserIdentity("wecom_callback", "boss1", "boss1", "机构负责人", "boss", "approved")

    result = query_employee_work_map(store, identity=identity, limit=10)

    assert result["ok"] is True
    assert result["read_only"] is True
    assert result["model_decides_next_action"] is True
    assert result["actions_taken"] == []
    section_keys = {item["domain_key"] for item in result["sections"]}
    assert "institution_work_map" in section_keys
    assert "organization_permissions" in section_keys
    assert result["health"]["map_section_count"] >= 6
    assert any(item["fact_owner_role"] in {"boss", "manager"} for item in result["sections"])
    assert result["fact_owner_question_queue"]["boss"] or result["fact_owner_question_queue"]["manager"]
    _assert_no_router_keys(result["sections"])
    _assert_no_router_keys(result["fact_owner_question_queue"])


def test_employee_work_map_tool_is_registered_and_permission_scoped(tmp_path):
    from plugins.tuoguan_core.tool_service import TuoguanToolService
    from plugins.tuoguan_core.tools import TOOLS

    store = _seed_store(tmp_path)
    names = {name for name, _schema, _handler in TOOLS}
    assert "tuoguan_query_employee_work_map" in names

    boss = TuoguanToolService(store, platform="wecom_callback", user_id="boss1", user_name="机构负责人")
    result = boss.query_employee_work_map(limit=10)
    assert result["ok"] is True
    assert result["data"]["report_type"] == "employee_work_map_v1"

    teacher = TuoguanToolService(store, platform="wecom_callback", user_id="teacher1", user_name="示例老师")
    denied = teacher.query_employee_work_map(limit=10)
    assert denied["ok"] is False
    assert denied["error"] == "permission_denied"
