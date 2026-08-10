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
            "allowed_users": ["teacher1", "manager1"],
            "user_roles": {"boss1": "boss", "teacher1": "teacher", "manager1": "manager"},
        },
    )
    _write_json(tmp_path, "teacher_wecom_map.json", {"金总": "boss1", "李老师": "teacher1", "店长": "manager1"})
    return TuoguanStore(tmp_path)


def test_low_risk_owner_daily_report_preference_is_saved_and_verified(tmp_path):
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    store = _seed_store(tmp_path)
    service = TuoguanToolService(store, platform="wecom_callback", user_id="boss1", user_name="金总")

    result = service.submit_person_workstyle_preference(
        preference_type="report_length",
        scope="daily_report",
        preference_text="以后晚报只说重点，别发一大堆过程。",
        normalized_rule="晚报只说重点，少说过程。",
        source_text="以后晚报只说重点，别发一大堆过程。",
        operation_id="workstyle-boss-report-1",
    )

    assert result["ok"] is True
    assert result["data"]["writeback_verified"] is True
    assert "已保存" in result["message"]
    profile = service.query_person_workstyle_profile(scope="daily_report")
    assert profile["ok"] is True
    assert profile["data"]["preference_count"] == 1
    assert profile["data"]["preferences"][0]["target_user_id"] == "boss1"
    assert profile["data"]["preferences"][0]["preference_type"] == "report_length"


def test_workstyle_preference_alias_is_saved_and_verified(tmp_path):
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    store = _seed_store(tmp_path)
    service = TuoguanToolService(store, platform="wecom_callback", user_id="boss1", user_name="金总")

    result = service.submit_person_workstyle_preference(
        preference_type="format",
        scope="daily_report",
        preference="以后汇报先说结论，再列三条重点。",
        normalized_rule="日报先说结论，再列三条重点。",
        operation_id="workstyle-boss-format-alias-1",
    )

    assert result["ok"] is True
    assert result["data"]["writeback_verified"] is True
    assert result["data"]["preference"]["preference_text"] == "以后汇报先说结论，再列三条重点。"


def test_teacher_reminder_time_preference_is_personal_not_institution_policy(tmp_path):
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    store = _seed_store(tmp_path)
    service = TuoguanToolService(store, platform="wecom_callback", user_id="teacher1", user_name="李老师")

    result = service.submit_person_workstyle_preference(
        preference_type="reminder_time",
        scope="task_followup",
        preference_text="以后下午五点后再提醒我任务。",
        normalized_rule="任务跟进尽量安排在下午五点后。",
        source_text="以后下午五点后再提醒我任务。",
        operation_id="workstyle-teacher-reminder-1",
    )

    assert result["ok"] is True
    pref = result["data"]["preference"]
    assert pref["target_user_id"] == "teacher1"
    assert pref["target_role"] == "teacher"
    assert pref["auto_effects"]["changes_router"] is False
    assert pref["auto_effects"]["forces_next_action"] is False
    assert pref["auto_effects"]["changes_institution_policy"] is False


def test_high_risk_preference_is_not_saved(tmp_path):
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    store = _seed_store(tmp_path)
    service = TuoguanToolService(store, platform="wecom_callback", user_id="boss1", user_name="金总")

    result = service.submit_person_workstyle_preference(
        preference_type="other_low_risk",
        scope="all_communication",
        preference_text="以后你可以自动联系家长并修改老师工资。",
        normalized_rule="自动联系家长并修改工资。",
        source_text="以后你可以自动联系家长并修改老师工资。",
        operation_id="workstyle-high-risk-1",
    )

    assert result["ok"] is False
    assert result["error"] == "workstyle_preference_high_risk"
    assert not (tmp_path / "person_workstyle_events.jsonl").exists()


def test_same_type_and_scope_latest_preference_wins(tmp_path):
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    store = _seed_store(tmp_path)
    service = TuoguanToolService(store, platform="wecom_callback", user_id="boss1", user_name="金总")

    first = service.submit_person_workstyle_preference(
        preference_type="report_length",
        scope="daily_report",
        preference_text="以后日报短一点。",
        normalized_rule="日报短一点。",
        operation_id="workstyle-overwrite-1",
    )
    second = service.submit_person_workstyle_preference(
        preference_type="report_length",
        scope="daily_report",
        preference_text="以后日报只保留三条重点。",
        normalized_rule="日报只保留三条重点。",
        operation_id="workstyle-overwrite-2",
    )

    assert first["ok"] is True
    assert second["ok"] is True
    profile = service.query_person_workstyle_profile(scope="daily_report")
    active = profile["data"]["preferences"]
    assert len(active) == 1
    assert active[0]["normalized_rule"] == "日报只保留三条重点。"
    assert second["data"]["preference"]["supersedes"]


def test_workstyle_tools_are_registered_in_runtime_sets():
    from plugins.tuoguan_core.runtime_foundation import MODEL_SELECTED_READ_TOOLS, WRITE_TOOLS
    from plugins.tuoguan_core.tools import TOOLS

    names = {name for name, _schema, _handler in TOOLS}
    assert "tuoguan_query_person_workstyle_profile" in names
    assert "tuoguan_submit_person_workstyle_preference" in names
    assert "tuoguan_query_workstyle_adaptation_health" in names
    assert "tuoguan_query_person_workstyle_profile" in MODEL_SELECTED_READ_TOOLS
    assert "tuoguan_query_workstyle_adaptation_health" in MODEL_SELECTED_READ_TOOLS
    assert "tuoguan_submit_person_workstyle_preference" in WRITE_TOOLS
