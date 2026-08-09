from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path


def _write_json(path: Path, name: str, payload) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _seed_store(tmp_path: Path, *, guard_enabled: bool = False):
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(tmp_path, "write_guard_config.json", {"enabled": guard_enabled})
    _write_json(
        tmp_path,
        "wecom_whitelist.json",
        {
            "super_users": ["boss1"],
            "allowed_users": ["manager1", "teacher1"],
            "user_roles": {"boss1": "boss", "manager1": "manager", "teacher1": "teacher"},
        },
    )
    _write_json(tmp_path, "teacher_wecom_map.json", {"金总": "boss1", "店长": "manager1", "李老师": "teacher1"})
    _write_json(
        tmp_path,
        "staff.json",
        {
            "manager1": {"user_id": "manager1", "name": "店长", "role": "manager"},
            "teacher1": {"user_id": "teacher1", "name": "李老师", "role": "teacher"},
        },
    )
    _write_json(tmp_path, "students.json", {})
    _write_json(tmp_path, "tasks.json", [])
    _write_json(tmp_path, "notification_outbox.json", [])
    return TuoguanStore(tmp_path)


def _boss_identity():
    from plugins.tuoguan_core.models import UserIdentity

    return UserIdentity("wecom_callback", "boss1", "boss1", "金总", "boss", "approved")


def test_fact_gap_candidate_tool_is_registered_and_permission_scoped(tmp_path):
    from plugins.tuoguan_core.tool_service import TuoguanToolService
    from plugins.tuoguan_core.tools import TOOLS

    store = _seed_store(tmp_path)
    names = {name for name, _schema, _handler in TOOLS}
    assert "tuoguan_query_fact_gap_candidates" in names
    assert "tuoguan_submit_fact_gap_candidate" in names

    boss = TuoguanToolService(store, platform="wecom_callback", user_id="boss1", user_name="金总")
    assert boss.query_fact_gap_candidates()["ok"] is True

    teacher = TuoguanToolService(store, platform="wecom_callback", user_id="teacher1", user_name="李老师")
    denied = teacher.query_fact_gap_candidates()
    assert denied["ok"] is False
    assert denied["error"] == "permission_denied"


def test_fact_gap_candidate_writeback_keeps_outbox_empty(tmp_path):
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    store = _seed_store(tmp_path)
    boss = TuoguanToolService(store, platform="wecom_callback", user_id="boss1", user_name="金总")

    result = boss.submit_fact_gap_candidate(
        gap_key="teacher_record_habit",
        gap_text="还不了解李老师日常记录学生作业完成情况的习惯。",
        fact_owner_role="teacher",
        operation_id="fact-gap-candidate-1",
        suggested_question="李老师，你平时每天什么时候记录学生作业完成情况？",
        target_user_id="teacher1",
        target_name="李老师",
        impact="影响小优判断什么时候跟进老师最不打扰。",
        urgency="normal",
        source_text="老板要求小优主动了解每位老师的工作习惯。",
    )

    assert result["ok"] is True
    assert result["data"]["writeback_verified"] is True
    assert result["data"]["auto_effects"]["sends_messages"] is False
    assert result["data"]["auto_effects"]["creates_tasks"] is False
    assert json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8")) == []

    rows = [
        json.loads(line)
        for line in (tmp_path / "institution_fact_gap_events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["ask_role"] == "teacher"
    assert rows[0]["auto_effects"]["sends_messages"] is False
    assert rows[0]["related_objects"][-1]["candidate_kind"] == "fact_gap_candidate"
    assert rows[0]["related_objects"][-1]["suggested_question"].startswith("李老师")

    queried = boss.query_fact_gap_candidates(ask_role="teacher", limit=10)
    assert queried["ok"] is True
    assert queried["data"]["candidate_count"] == 1
    assert queried["data"]["by_ask_role"]["teacher"] == 1
    assert queried["data"]["boundary"]["model_decides_whether_to_ask"] is True
    assert queried["data"]["boundary"]["sends_messages"] is False


def test_night_loop_materializes_fact_gap_candidate_without_sending(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import run_autonomous_employee_loop
    from plugins.tuoguan_core.digital_employee_state import query_fact_gap_candidates

    store = _seed_store(tmp_path, guard_enabled=True)
    cn_tz = timezone(timedelta(hours=8))

    def decision(_materials: dict) -> dict:
        return {
            "employee_summary": "夜间复盘发现：小优还不了解老师记录习惯，需要白天按时机判断是否问老师。",
            "institution_understanding": "当前已知人员角色，但缺少李老师记录作业完成情况的工作习惯。",
            "goal_progress_view": "",
            "observations": [],
            "work_item_updates": [],
            "questions_to_humans": [],
            "boss_attention_candidates": [],
            "relationship_touch_candidates": [],
            "institution_fact_gaps": [
                {
                    "gap_key": "teacher_record_habit",
                    "gap_text": "缺少李老师每天什么时候记录学生作业完成情况的习惯。",
                    "ask_role": "teacher",
                    "target_user_id": "teacher1",
                    "target_name": "李老师",
                    "suggested_question": "李老师，你一般几点记录学生作业完成情况？",
                    "impact": "影响小优选择低打扰跟进时间。",
                    "urgency": "normal",
                }
            ],
            "value_progress_entries": [],
            "agent_delegation_decisions": [],
            "evolution_candidates": [],
            "self_review": {"what_i_checked": "老师工作习惯缺口", "quality_score": 85},
            "external_actions": [],
        }

    result = run_autonomous_employee_loop(store, now=datetime(2026, 8, 8, 23, 30, tzinfo=cn_tz), decision_provider=decision)

    assert result["ok"] is True
    assert result["work_cadence"]["owner_attention_allowed"] is False
    assert any(row["kind"] == "fact_gap_candidate" and row["ok"] for row in result["writes"])
    assert json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8")) == []

    queried = query_fact_gap_candidates(store, identity=_boss_identity(), ask_role="teacher", limit=10)
    assert queried["ok"] is True
    assert queried["candidate_count"] == 1
    candidate = queried["candidates"][0]
    assert candidate["status"] == "candidate"
    assert candidate["model_decides_whether_to_ask"] is True
    assert candidate["related_objects"][-1]["target_user_id"] == "teacher1"
    assert candidate["related_objects"][-1]["external_send_allowed"] is False
