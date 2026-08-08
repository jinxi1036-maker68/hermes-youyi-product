from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path


def _write_json(path: Path, name: str, payload) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_relationship_touch_candidate_is_internal_for_teacher(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import (
        query_relationship_touch_candidates,
        submit_relationship_touch_candidate,
    )
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.write_guard import authorized_system_write

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    store = TuoguanStore(tmp_path)
    identity = UserIdentity("system", "autonomous", "autonomous", "Hermes", "boss", "approved")

    with authorized_system_write(store.data_dir, job_name="relationship_touch_test", allowed_files={"relationship_touch_candidates.jsonl"}):
        result = submit_relationship_touch_candidate(
            store,
            identity=identity,
            target_role="teacher",
            target_user_id="teacher1",
            target_name="李老师",
            touch_type="care",
            message="李老师，今天辛苦了。孩子们午休还顺利吗？你随口回一句就行。",
            reason="关系经营候选，先给老师情绪价值，不是催任务。",
            value="让老师感受到 Hermes 是同事，会关心也会帮忙整理。",
            private_emotional_support=True,
            operation_id="system:test:relationship",
        )

    assert result["ok"] is True
    candidate = result["candidate"]
    assert candidate["target_role"] == "teacher"
    assert candidate["external_send_allowed"] is False
    assert candidate["auto_effects"]["sends_teacher_messages"] is False

    teacher = UserIdentity("wecom", "teacher1", "teacher1", "李老师", "teacher", "approved")
    visible = query_relationship_touch_candidates(store, identity=teacher, target_user_id="teacher1")
    assert visible["candidate_count"] == 1


def test_autonomous_loop_can_queue_boss_presence_but_not_teacher(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import run_autonomous_employee_loop
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_json(tmp_path, "wecom_whitelist.json", {"super_users": ["boss1"], "user_roles": {"boss1": "boss"}})
    _write_json(tmp_path, "teacher_wecom_map.json", {"金总": "boss1"})
    _write_json(tmp_path, "notification_outbox.json", [])
    _write_json(tmp_path, "students.json", {})
    _write_json(tmp_path, "tasks.json", [])
    _write_json(tmp_path, "academic_term_state.json", {"service_relation_policy": "defer_until_new_term"})
    store = TuoguanStore(tmp_path)
    cn_tz = timezone(timedelta(hours=8))

    def decision_provider(_materials):
        return {
            "employee_summary": "Hermes 看到今天有目标推进材料，可以给老板做一次存在感汇报，同时给老师准备一句关心候选。",
            "institution_understanding": "",
            "goal_progress_view": "",
            "observations": [],
            "work_item_updates": [],
            "questions_to_humans": [],
            "boss_attention_candidates": [],
            "relationship_touch_candidates": [
                {
                    "target_role": "boss",
                    "touch_type": "presence_report",
                    "message": "金总，我看到今天续费目标材料已经进入历史分析与沟通准备阶段。建议你不用回复，我会继续盯记录覆盖、风险分组和明天早上的推进摘要。",
                    "reason": "老板需要感受到 Hermes 在岗，同时内容有真实目标依据。",
                    "value": "增强存在感，不制造无意义打扰。",
                    "work_related": True,
                    "external_send_allowed": True,
                },
                {
                    "target_role": "teacher",
                    "target_user_id": "teacher1",
                    "touch_type": "care",
                    "message": "李老师，今天辛苦了。孩子们午休还顺利吗？你随口回一句就行。",
                    "reason": "老师同事陪伴候选，不是工作催促。",
                    "value": "让老师觉得 Hermes 是会关心人的同事。",
                    "private_emotional_support": True,
                },
            ],
            "institution_fact_gaps": [],
            "value_progress_entries": [],
            "agent_delegation_decisions": [],
            "self_review": {},
            "external_actions": [],
        }

    result = run_autonomous_employee_loop(
        store,
        now=datetime(2026, 8, 1, 10, 30, tzinfo=cn_tz),
        decision_provider=decision_provider,
    )

    assert result["ok"] is True
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert len(outbox) == 1
    assert outbox[0]["notification_type"] == "relationship_touch"
    assert outbox[0]["touser"] == "boss1"

    rows = [
        json.loads(line)
        for line in (tmp_path / "relationship_touch_candidates.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    candidates = [row for row in rows if row.get("record_type") != "relationship_touch_update"]
    assert len(candidates) == 2
    teacher_candidate = next(row for row in candidates if row["target_role"] == "teacher")
    assert teacher_candidate["external_send_allowed"] is False


def test_autonomous_loop_can_queue_teacher_and_manager_fact_requests(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import run_autonomous_employee_loop
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_json(
        tmp_path,
        "wecom_whitelist.json",
        {
            "super_users": ["boss1"],
            "allowed_users": ["teacher1", "manager1"],
            "user_roles": {"boss1": "boss", "teacher1": "teacher", "manager1": "manager"},
        },
    )
    _write_json(tmp_path, "notification_outbox.json", [])
    _write_json(tmp_path, "students.json", {})
    _write_json(tmp_path, "tasks.json", [])
    _write_json(tmp_path, "academic_term_state.json", {"service_relation_policy": "defer_until_new_term"})
    store = TuoguanStore(tmp_path)
    cn_tz = timezone(timedelta(hours=8))

    def decision_provider(_materials):
        return {
            "employee_summary": "小优发现一个老师任务结果和一个店长运营事实需要直接问归属人。",
            "institution_understanding": "",
            "goal_progress_view": "",
            "observations": [],
            "work_item_updates": [],
            "questions_to_humans": [],
            "boss_attention_candidates": [],
            "relationship_touch_candidates": [
                {
                    "target_role": "teacher",
                    "target_user_id": "teacher1",
                    "target_name": "李老师",
                    "touch_type": "record_relief",
                    "message": "李老师，老板安排给你的任务我来跟一下：请告诉我目前沟通结果是什么？如果还没开始，也请回我当前进展。",
                    "reason": "已有任务缺老师执行结果，事实归属人是李老师。",
                    "value": "让任务进展能回到上下文，不再反复打扰老板。",
                    "work_related": True,
                    "external_send_allowed": True,
                },
                {
                    "target_role": "manager",
                    "target_user_id": "manager1",
                    "target_name": "申老师",
                    "touch_type": "manager_assist",
                    "message": "申老师，我在整理今天的运营事实，请帮我确认一下老师任务执行结果是否已经收齐？缺哪位老师的反馈直接回我就行。",
                    "reason": "缺店长运营事实，事实归属人是店长。",
                    "value": "让小优能自己追运营事实，不把所有卡点都交给老板。",
                    "work_related": True,
                    "external_send_allowed": True,
                },
            ],
            "institution_fact_gaps": [],
            "value_progress_entries": [],
            "agent_delegation_decisions": [],
            "self_review": {},
            "external_actions": [],
        }

    result = run_autonomous_employee_loop(
        store,
        now=datetime(2026, 8, 1, 11, 0, tzinfo=cn_tz),
        decision_provider=decision_provider,
    )

    assert result["ok"] is True
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert [item["role"] for item in outbox] == ["teacher", "manager"]
    assert {item["touser"] for item in outbox} == {"teacher1", "manager1"}
    assert all(item["auto_effects"]["sends_parent_messages"] is False for item in outbox)
    assert outbox[0]["auto_effects"]["sends_teacher_messages"] is True
    assert outbox[1]["auto_effects"]["sends_manager_messages"] is True


def test_teacher_fact_request_cannot_be_parent_outreach_instruction(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import run_autonomous_employee_loop
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_json(
        tmp_path,
        "wecom_whitelist.json",
        {
            "super_users": ["boss1"],
            "allowed_users": ["teacher1"],
            "user_roles": {"boss1": "boss", "teacher1": "teacher"},
        },
    )
    _write_json(tmp_path, "notification_outbox.json", [])
    _write_json(tmp_path, "students.json", {})
    _write_json(tmp_path, "tasks.json", [])
    store = TuoguanStore(tmp_path)
    cn_tz = timezone(timedelta(hours=8))

    def decision_provider(_materials):
        return {
            "employee_summary": "小优错误地准备让老师主动联系家长，应被边界挡住。",
            "institution_understanding": "",
            "goal_progress_view": "",
            "observations": [],
            "work_item_updates": [],
            "questions_to_humans": [],
            "boss_attention_candidates": [],
            "relationship_touch_candidates": [
                {
                    "target_role": "teacher",
                    "target_user_id": "teacher1",
                    "touch_type": "record_relief",
                    "message": "李老师，请你现在联系家长，把这段话发给家长后告诉我结果。",
                    "reason": "这会变成对家长触达指令。",
                    "value": "不应发送。",
                    "work_related": True,
                    "external_send_allowed": True,
                }
            ],
            "institution_fact_gaps": [],
            "value_progress_entries": [],
            "agent_delegation_decisions": [],
            "self_review": {},
            "external_actions": [],
        }

    result = run_autonomous_employee_loop(
        store,
        now=datetime(2026, 8, 1, 11, 0, tzinfo=cn_tz),
        decision_provider=decision_provider,
    )

    assert result["ok"] is True
    assert json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8")) == []
