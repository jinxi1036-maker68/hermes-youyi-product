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
            target_name="示例老师",
            touch_type="care",
            message="示例老师，今天辛苦了。孩子们午休还顺利吗？你随口回一句就行。",
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

    teacher = UserIdentity("wecom", "teacher1", "teacher1", "示例老师", "teacher", "approved")
    visible = query_relationship_touch_candidates(store, identity=teacher, target_user_id="teacher1")
    assert visible["candidate_count"] == 1


def test_relationship_touch_rejects_a_salutation_for_the_wrong_wecom_target(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import submit_relationship_touch_candidate
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.write_guard import authorized_system_write

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_json(tmp_path, "teacher_wecom_map.json", {"示例老师": "teacher1", "崔老师": "manager1"})
    _write_json(tmp_path, "staff.json", {
        "teacher1": {"user_id": "teacher1", "name": "示例老师", "role": "teacher"},
        "manager1": {"user_id": "manager1", "name": "崔老师", "role": "manager"},
    })
    store = TuoguanStore(tmp_path)
    identity = UserIdentity("system", "autonomous", "autonomous", "小优", "boss", "approved")

    with authorized_system_write(store.data_dir, job_name="relationship_touch_name_guard", allowed_files={"relationship_touch_candidates.jsonl"}):
        result = submit_relationship_touch_candidate(
            store,
            identity=identity,
            target_role="teacher",
            target_user_id="teacher1",
            target_name="示例老师",
            touch_type="record_relief",
            message="崔老师，麻烦你确认一下今天的任务结果。",
            reason="测试称呼和企业微信目标必须一致。",
            work_related=True,
            operation_id="system:wrong-salutation",
        )

    assert result["ok"] is False
    assert result["error"] == "relationship_touch_target_salutation_mismatch"


def test_autonomous_loop_can_queue_boss_presence_but_not_teacher(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import run_autonomous_employee_loop
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_json(tmp_path, "wecom_whitelist.json", {"super_users": ["boss1"], "user_roles": {"boss1": "boss"}})
    _write_json(tmp_path, "teacher_wecom_map.json", {"机构负责人": "boss1"})
    _write_json(tmp_path, "notification_outbox.json", [])
    _write_json(tmp_path, "students.json", {})
    _write_json(tmp_path, "tasks.json", [])
    _write_json(
        tmp_path,
        "relationship_touch_policy.json",
        {
            "teacher": {"mode": "direct", "allowed_start": "10:00", "allowed_end": "18:30", "daily_limit": 2, "allowed_target_user_ids": ["teacher1"]},
            "manager": {"mode": "direct", "allowed_start": "10:00", "allowed_end": "18:30", "daily_limit": 1, "allowed_target_user_ids": ["manager1"]},
            "parent": {"mode": "disabled"},
        },
    )
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
                    "message": "机构负责人，我看到今天续费目标材料已经进入历史分析与沟通准备阶段。建议你不用回复，我会继续盯记录覆盖、风险分组和明天早上的推进摘要。",
                    "reason": "老板需要感受到 Hermes 在岗，同时内容有真实目标依据。",
                    "value": "增强存在感，不制造无意义打扰。",
                    "work_related": True,
                    "external_send_allowed": True,
                },
                {
                    "target_role": "teacher",
                    "target_user_id": "teacher1",
                    "touch_type": "care",
                    "message": "示例老师，今天辛苦了。孩子们午休还顺利吗？你随口回一句就行。",
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
    _write_json(
        tmp_path,
        "relationship_touch_policy.json",
        {
            "teacher": {"mode": "direct", "allowed_start": "10:00", "allowed_end": "18:30", "daily_limit": 2, "allowed_target_user_ids": ["teacher1"]},
            "manager": {"mode": "direct", "allowed_start": "10:00", "allowed_end": "18:30", "daily_limit": 1, "allowed_target_user_ids": ["manager1"]},
            "parent": {"mode": "disabled"},
        },
    )
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
                    "target_name": "示例老师",
                    "touch_type": "record_relief",
                    "message": "示例老师，老板安排给你的任务我来跟一下：请告诉我目前沟通结果是什么？如果还没开始，也请回我当前进展。",
                    "reason": "已有任务缺老师执行结果，事实归属人是示例老师。",
                    "value": "让任务进展能回到上下文，不再反复打扰老板。",
                    "work_related": True,
                    "external_send_allowed": True,
                },
                {
                    "target_role": "manager",
                    "target_user_id": "manager1",
                    "target_name": "另一位老师",
                    "touch_type": "manager_assist",
                    "message": "另一位老师，我在整理今天的运营事实，请帮我确认一下老师任务执行结果是否已经收齐？缺哪位老师的反馈直接回我就行。",
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


def test_direct_staff_policy_without_explicit_allowlist_fails_closed(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import run_autonomous_employee_loop
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_json(
        tmp_path,
        "wecom_whitelist.json",
        {"allowed_users": ["teacher1"], "user_roles": {"teacher1": "teacher"}},
    )
    _write_json(
        tmp_path,
        "relationship_touch_policy.json",
        {"teacher": {"mode": "direct", "daily_limit": 2, "allowed_target_user_ids": []}},
    )
    _write_json(tmp_path, "notification_outbox.json", [])
    _write_json(tmp_path, "students.json", {})
    _write_json(tmp_path, "tasks.json", [])

    def decision_provider(_materials):
        return {
            "employee_summary": "需要问老师一个任务事实。",
            "institution_understanding": "",
            "goal_progress_view": "",
            "observations": [],
            "work_item_updates": [],
            "questions_to_humans": [],
            "boss_attention_candidates": [],
            "relationship_touch_candidates": [{
                "target_role": "teacher",
                "target_user_id": "teacher1",
                "touch_type": "record_relief",
                "message": "老师，请帮我确认一下今天任务结果是否已经记录？",
                "reason": "缺老师任务结果。",
                "value": "补齐任务事实。",
                "work_related": True,
                "external_send_allowed": True,
            }],
            "institution_fact_gaps": [],
            "value_progress_entries": [],
            "agent_delegation_decisions": [],
            "self_review": {},
            "external_actions": [],
        }

    result = run_autonomous_employee_loop(
        TuoguanStore(tmp_path),
        now=datetime(2026, 8, 1, 11, 0, tzinfo=timezone(timedelta(hours=8))),
        decision_provider=decision_provider,
    )

    assert result["ok"] is True
    assert json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8")) == []


def test_test_mode_policy_allows_only_boss_and_li_teacher(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import run_autonomous_employee_loop
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_json(
        tmp_path,
        "wecom_whitelist.json",
        {
            "super_users": ["owner_test"],
            "allowed_users": ["teacher_test", "LiuLi", "manager1"],
            "user_roles": {"owner_test": "boss", "teacher_test": "teacher", "LiuLi": "teacher", "manager1": "manager"},
        },
    )
    _write_json(tmp_path, "teacher_wecom_map.json", {"示例老师": "teacher_test", "另一位老师": "LiuLi", "店长": "manager1"})
    _write_json(
        tmp_path,
        "relationship_touch_policy.json",
        {
            "boss": {"mode": "direct", "allowed_start": "08:00", "allowed_end": "19:00", "daily_limit": 2, "allowed_target_user_ids": ["owner_test"]},
            "teacher": {"mode": "direct", "allowed_start": "10:00", "allowed_end": "18:30", "daily_limit": 2, "allowed_target_user_ids": ["teacher_test"]},
            "manager": {"mode": "candidate", "allowed_target_user_ids": []},
            "parent": {"mode": "disabled"},
        },
    )
    _write_json(tmp_path, "notification_outbox.json", [])
    _write_json(tmp_path, "students.json", {})
    _write_json(tmp_path, "tasks.json", [])
    store = TuoguanStore(tmp_path)
    cn_tz = timezone(timedelta(hours=8))

    def decision_provider(_materials):
        return {
            "employee_summary": "小优在测试期只允许问机构负责人和示例老师。",
            "institution_understanding": "",
            "goal_progress_view": "",
            "observations": [],
            "work_item_updates": [],
            "questions_to_humans": [],
            "boss_attention_candidates": [],
            "relationship_touch_candidates": [
                {
                    "target_role": "teacher",
                    "target_user_id": "teacher_test",
                    "target_name": "示例老师",
                    "touch_type": "record_relief",
                    "message": "示例老师，我在测试主动工作能力，请帮我确认一下今天你方便让我几点问你任务记录相关事实？",
                    "reason": "测试期允许小优主动问示例老师一个具体任务记录事实。",
                    "value": "让小优自己学会按示例老师的时间偏好工作。",
                    "work_related": True,
                    "external_send_allowed": True,
                },
                {
                    "target_role": "teacher",
                    "target_user_id": "LiuLi",
                    "target_name": "另一位老师",
                    "touch_type": "record_relief",
                    "message": "另一位老师，请帮我确认一下今天学生记录有没有缺口？",
                    "reason": "非测试白名单老师，不应主动发送。",
                    "value": "不应发送。",
                    "work_related": True,
                    "external_send_allowed": True,
                },
                {
                    "target_role": "manager",
                    "target_user_id": "manager1",
                    "target_name": "店长",
                    "touch_type": "manager_assist",
                    "message": "店长，请帮我确认一下今天现场有没有任务缺口？",
                    "reason": "测试期暂不主动找店长。",
                    "value": "不应发送。",
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
        now=datetime(2026, 8, 10, 11, 0, tzinfo=cn_tz),
        decision_provider=decision_provider,
    )

    assert result["ok"] is True
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert [item["touser"] for item in outbox] == ["teacher_test"]
    assert outbox[0]["auto_effects"]["sends_teacher_messages"] is True


def test_tool_service_exposes_li_teacher_touch_policy_and_candidate_writeback(tmp_path):
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    _write_json(tmp_path, "write_guard_config.json", {"enabled": False})
    _write_json(
        tmp_path,
        "wecom_whitelist.json",
        {
            "super_users": ["owner_test"],
            "allowed_users": ["owner_test", "teacher_test", "LiuLi"],
            "user_roles": {"owner_test": "boss", "teacher_test": "teacher", "LiuLi": "teacher"},
        },
    )
    _write_json(tmp_path, "teacher_wecom_map.json", {"示例老师": "teacher_test", "另一位老师": "LiuLi"})
    _write_json(
        tmp_path,
        "relationship_touch_policy.json",
        {
            "boss": {"mode": "direct", "allowed_target_user_ids": ["owner_test"], "daily_limit": 2},
            "teacher": {"mode": "direct", "allowed_target_user_ids": ["teacher_test"], "daily_limit": 2},
            "manager": {"mode": "candidate", "allowed_target_user_ids": []},
            "parent": {"mode": "disabled"},
        },
    )
    _write_json(tmp_path, "notification_outbox.json", [])
    _write_json(tmp_path, "students.json", {})
    _write_json(tmp_path, "tasks.json", [])
    store = TuoguanStore(tmp_path)
    service = TuoguanToolService(
        store=store,
        platform="wecom_callback",
        user_id="owner_test",
        user_name="机构负责人",
        chat_id="owner_test",
        session_key="owner_test",
    )

    queried = service.query_relationship_touch_candidates(target_role="teacher")
    assert queried["ok"] is True
    assert queried["data"]["policy"]["teacher"]["allowed_target_user_ids"] == ["teacher_test"]

    allowed = service.submit_relationship_touch_candidate(
        target_role="teacher",
        target_user_id="teacher_test",
        target_name="示例老师",
        touch_type="record_relief",
        message="示例老师，我在测试主动工作能力，想确认你今天几点方便我问一个任务记录事实？",
        reason="测试期允许主动向示例老师确认具体工作事实。",
        value="验证小优可主动问事实归属人。",
        work_related=True,
        operation_id="touch-li-allowed",
    )
    blocked = service.submit_relationship_touch_candidate(
        target_role="teacher",
        target_user_id="LiuLi",
        target_name="另一位老师",
        touch_type="record_relief",
        message="另一位老师，我想确认一个学生记录事实。",
        reason="非测试白名单老师，只能留下内部候选。",
        value="验证白名单收口。",
        work_related=True,
        operation_id="touch-li-blocked",
    )

    assert allowed["ok"] is True
    assert allowed["data"]["external_send_allowed_by_policy"] is True
    assert allowed["data"]["writeback_verified"] is True
    assert blocked["ok"] is True
    assert blocked["data"]["external_send_allowed_by_policy"] is False
    rows = [
        json.loads(line)
        for line in (tmp_path / "relationship_touch_candidates.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [row["target_user_id"] for row in rows] == ["teacher_test", "LiuLi"]
    assert rows[0]["external_send_allowed"] is True
    assert rows[1]["external_send_allowed"] is False


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
                    "message": "示例老师，请你现在联系家长，把这段话发给家长后告诉我结果。",
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
