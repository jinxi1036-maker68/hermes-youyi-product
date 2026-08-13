from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json


CN_TZ = timezone(timedelta(hours=8))


def _write_json(root, name, value):
    (root / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _setup(root):
    _write_json(root, "write_guard_config.json", {"enabled": False})
    _write_json(
        root,
        "wecom_whitelist.json",
        {
            "super_users": ["JinWenJie"],
            "allowed_users": ["JinWenJie", "CeShi", "LiuLi", "CuiXiaoXia"],
            "user_roles": {
                "JinWenJie": "boss",
                "CeShi": "teacher",
                "LiuLi": "teacher",
                "CuiXiaoXia": "manager",
            },
        },
    )
    _write_json(
        root,
        "staff.json",
        {
            "JinWenJie": {"name": "金总", "role": "boss", "status": "active"},
            "CeShi": {"name": "李老师", "role": "teacher", "status": "active"},
            "LiuLi": {"name": "刘老师", "role": "teacher", "status": "active"},
            "CuiXiaoXia": {"name": "崔校长", "role": "manager", "status": "active"},
        },
    )
    _write_json(
        root,
        "relationship_touch_policy.json",
        {
            "boss": {"mode": "direct", "allowed_start": "08:00", "allowed_end": "19:00", "daily_limit": 2, "allowed_target_user_ids": ["JinWenJie"]},
            "teacher": {"mode": "direct", "allowed_start": "08:00", "allowed_end": "19:00", "daily_limit": 2, "allowed_target_user_ids": ["CeShi"]},
            "manager": {"mode": "candidate", "allowed_target_user_ids": []},
            "parent": {"mode": "disabled"},
        },
    )
    _write_json(root, "notification_outbox.json", [])
    _write_json(root, "tasks.json", [])
    _write_json(root, "students.json", {})
    _write_json(root, "goal_operator_goals.json", {"goals": []})


def _boss():
    from plugins.tuoguan_core.models import UserIdentity

    return UserIdentity("wecom_callback", "JinWenJie", "JinWenJie", "金总", "boss", "approved")


def _system():
    from plugins.tuoguan_core.models import UserIdentity

    return UserIdentity("system", "autonomous_employee_loop", "autonomous_employee_loop", "小优", "boss", "approved")


def _active_goal(root, goal_id="goal-renewal"):
    _write_json(
        root,
        "goal_operator_goals.json",
        {
            "goals": [
                {
                    "goal_id": goal_id,
                    "goal_type": "parent_communication_coverage",
                    "goal_text": "八月完成家校沟通并提高续费稳定性",
                    "status": "confirmed",
                    "owner_user_id": "JinWenJie",
                    "created_at": "2026-08-13T08:00:00+08:00",
                    "updated_at": "2026-08-13T08:00:00+08:00",
                }
            ]
        },
    )


def test_owner_authorization_is_structured_revocable_and_verified(tmp_path):
    from plugins.tuoguan_core.proactive_work import effective_proactive_permission, query_proactive_authorizations, submit_proactive_authorization
    from plugins.tuoguan_core.store import TuoguanStore

    _setup(tmp_path)
    store = TuoguanStore(tmp_path)
    saved = submit_proactive_authorization(
        store,
        identity=_boss(),
        operation_id="auth-1",
        subject_role="teacher",
        subject_user_ids=["CeShi"],
        action_types=["ask_work_fact", "assign_low_risk_goal_task"],
        daily_limit=2,
        rollout_stage="pilot",
        source_text="你可以主动找李老师，也可以在确认目标内安排低风险任务。",
    )
    assert saved["ok"] is True
    assert saved["writeback_verified"] is True
    auth_id = saved["authorization"]["authorization_id"]
    queried = query_proactive_authorizations(store, identity=_boss())
    assert queried["authorization_count"] == 1
    assert queried["authorizations"][0]["subject_user_ids"] == ["CeShi"]

    adjusted = submit_proactive_authorization(
        store,
        identity=_boss(),
        operation_id="auth-adjust",
        authorization_id=auth_id,
        subject_role="teacher",
        subject_user_ids=["CeShi"],
        action_types=["ask_work_fact", "assign_low_risk_goal_task"],
        daily_limit=1,
        rollout_stage="pilot",
        source_text="测试期改为每天最多一次。",
    )
    assert adjusted["writeback_verified"] is True
    assert adjusted["authorization"]["daily_limit"] == 1

    revoked = submit_proactive_authorization(
        store,
        identity=_boss(),
        operation_id="auth-2",
        subject_role="teacher",
        subject_user_ids=[],
        action_types=[],
        status="revoked",
        authorization_id=auth_id,
        source_text="停止主动联系。",
    )
    assert revoked["writeback_verified"] is True
    assert query_proactive_authorizations(store, identity=_boss())["authorization_count"] == 0
    denied = effective_proactive_permission(
        store,
        target_role="teacher",
        target_user_id="CeShi",
        action_type="ask_work_fact",
        now=datetime(2026, 8, 13, 15, 0, tzinfo=CN_TZ),
    )
    assert denied["allowed"] is False
    assert denied["reason_code"] == "formal_authorization_required"


def test_revoking_authorization_stops_queued_delivery_and_send_rechecks_employment(tmp_path):
    from plugins.tuoguan_core import _claim_next_notification_outbox_item
    from plugins.tuoguan_core.digital_employee_state import submit_relationship_touch_candidate
    from plugins.tuoguan_core.proactive_work import execute_relationship_touch, submit_proactive_authorization
    from plugins.tuoguan_core.store import TuoguanStore

    _setup(tmp_path)
    store = TuoguanStore(tmp_path)
    authorization = submit_proactive_authorization(
        store,
        identity=_boss(),
        operation_id="auth-for-delivery",
        subject_role="teacher",
        subject_user_ids=["CeShi"],
        action_types=["ask_work_fact"],
        daily_limit=2,
        effective_at="2026-08-13T08:00:00+08:00",
        rollout_stage="pilot",
        source_text="允许测试期主动询问李老师工作事实。",
    )["authorization"]
    candidate = submit_relationship_touch_candidate(
        store,
        identity=_boss(),
        target_role="teacher",
        target_user_id="CeShi",
        target_name="李老师",
        touch_type="record_relief",
        message="李老师，请告诉我今天任务执行结果？",
        reason="目标需要执行事实。",
        value="推进目标。",
        work_related=True,
        action_type="ask_work_fact",
        operation_id="auth-stop-candidate",
    )["candidate"]
    queued = execute_relationship_touch(
        store,
        identity=_boss(),
        candidate_id=candidate["candidate_id"],
        operation_id="auth-stop-execute",
        now=datetime(2026, 8, 13, 15, 0, tzinfo=CN_TZ),
    )
    assert queued["ok"] is True, queued
    assert queued["delivery_state"] == "queued"
    revoked = submit_proactive_authorization(
        store,
        identity=_boss(),
        operation_id="auth-stop-revoke",
        authorization_id=authorization["authorization_id"],
        subject_role="teacher",
        subject_user_ids=[],
        action_types=[],
        status="revoked",
        source_text="停止这项主动授权。",
    )
    assert revoked["stopped_delivery"]["suppressed"] == 1
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert outbox[0]["status"] == "suppressed"

    # A fresh environment without a formal ledger still supports legacy policy,
    # but the sender must recheck employment immediately before claiming work.
    second = tmp_path / "inactive"
    second.mkdir()
    _setup(second)
    second_store = TuoguanStore(second)
    candidate2 = submit_relationship_touch_candidate(
        second_store,
        identity=_boss(),
        target_role="teacher",
        target_user_id="CeShi",
        target_name="李老师",
        touch_type="record_relief",
        message="李老师，请确认今天的工作记录。",
        reason="补工作事实。",
        value="推进目标。",
        work_related=True,
        operation_id="inactive-candidate",
    )["candidate"]
    execute_relationship_touch(
        second_store,
        identity=_boss(),
        candidate_id=candidate2["candidate_id"],
        operation_id="inactive-execute",
        now=datetime(2026, 8, 13, 15, 0, tzinfo=CN_TZ),
    )
    staff = json.loads((second / "staff.json").read_text(encoding="utf-8"))
    staff["CeShi"]["status"] = "left"
    _write_json(second, "staff.json", staff)
    claim = _claim_next_notification_outbox_item(
        second_store,
        excluded_task_ids=set(),
        now=datetime(2026, 8, 13, 15, 1, tzinfo=CN_TZ),
    )
    assert claim["event"] == "notification_suppressed"
    assert "target_role_or_employment_invalid" in claim["result"]


def test_existing_failed_outbox_is_not_reported_as_queued(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import submit_relationship_touch_candidate
    from plugins.tuoguan_core.proactive_work import execute_relationship_touch
    from plugins.tuoguan_core.store import TuoguanStore

    _setup(tmp_path)
    store = TuoguanStore(tmp_path)
    candidate = submit_relationship_touch_candidate(
        store,
        identity=_boss(),
        target_role="teacher",
        target_user_id="CeShi",
        target_name="李老师",
        touch_type="record_relief",
        message="李老师，请确认一项工作事实。",
        reason="目标推进。",
        value="补足证据。",
        work_related=True,
        operation_id="failed-state-candidate",
    )["candidate"]
    execute_relationship_touch(
        store,
        identity=_boss(),
        candidate_id=candidate["candidate_id"],
        operation_id="failed-state-first",
        now=datetime(2026, 8, 13, 15, 0, tzinfo=CN_TZ),
    )
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    outbox[0]["status"] = "failed"
    outbox[0]["last_error"] = "simulated_delivery_failure"
    _write_json(tmp_path, "notification_outbox.json", outbox)
    replay = execute_relationship_touch(
        store,
        identity=_boss(),
        candidate_id=candidate["candidate_id"],
        operation_id="failed-state-replay",
        now=datetime(2026, 8, 13, 15, 2, tzinfo=CN_TZ),
    )
    assert replay["delivery_state"] == "failed"
    assert replay["candidate"]["status"] == "failed"


def test_staff_cannot_self_resolve_thread_and_goal_action_cannot_skip_verification(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import submit_relationship_touch_candidate
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.proactive_work import submit_goal_action, update_relationship_touch
    from plugins.tuoguan_core.store import TuoguanStore

    _setup(tmp_path)
    _active_goal(tmp_path)
    store = TuoguanStore(tmp_path)
    candidate = submit_relationship_touch_candidate(
        store,
        identity=_boss(),
        target_role="teacher",
        target_user_id="CeShi",
        target_name="李老师",
        touch_type="record_relief",
        message="李老师，请确认今天的任务结果。",
        reason="补目标证据。",
        value="推进目标。",
        work_related=True,
        operation_id="self-resolve-candidate",
    )["candidate"]
    teacher = UserIdentity("wecom_callback", "CeShi", "CeShi", "李老师", "teacher", "approved")
    denied = update_relationship_touch(
        store,
        identity=teacher,
        candidate_id=candidate["candidate_id"],
        status="resolved",
        operation_id="teacher-self-resolve",
    )
    assert denied["ok"] is False
    assert denied["error"] == "staff_can_only_submit_reply"

    forged = submit_goal_action(
        store,
        identity=_boss(),
        goal_id="goal-renewal",
        action_type="verify_evidence",
        summary="没有过程却伪造为完成。",
        operation_id="forged-resolved-action",
        status="resolved",
    )
    assert forged["ok"] is False
    assert forged["error"] == "invalid_initial_goal_action_status"


def test_existing_candidate_can_be_queued_once_with_real_state(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import submit_relationship_touch_candidate
    from plugins.tuoguan_core.proactive_work import execute_relationship_touch
    from plugins.tuoguan_core.store import TuoguanStore

    _setup(tmp_path)
    store = TuoguanStore(tmp_path)
    candidate = submit_relationship_touch_candidate(
        store,
        identity=_boss(),
        target_role="teacher",
        target_user_id="CeShi",
        target_name="李老师",
        touch_type="record_relief",
        message="李老师，我在补机构事实，请告诉我你目前负责哪些孩子的晚托服务？",
        reason="机构地图缺少李老师当前服务关系。",
        value="补齐服务关系后才能正确推进目标。",
        work_related=True,
        requires_authorization=False,
        external_send_allowed=True,
        operation_id="candidate-1",
        status="candidate",
    )["candidate"]
    now = datetime(2026, 8, 13, 15, 0, tzinfo=CN_TZ)
    queued = execute_relationship_touch(
        store,
        identity=_boss(),
        candidate_id=candidate["candidate_id"],
        operation_id="execute-1",
        now=now,
    )
    assert queued["ok"] is True
    assert queued["delivery_state"] == "queued"
    assert queued["candidate"]["status"] == "queued"
    assert len(json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))) == 1

    replay = execute_relationship_touch(
        store,
        identity=_boss(),
        candidate_id=candidate["candidate_id"],
        operation_id="execute-2",
        now=now,
    )
    assert replay["idempotent_replay"] is True
    assert len(json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))) == 1


def test_non_rollout_teacher_and_parent_instruction_are_blocked(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import submit_relationship_touch_candidate
    from plugins.tuoguan_core.proactive_work import execute_relationship_touch
    from plugins.tuoguan_core.store import TuoguanStore

    _setup(tmp_path)
    store = TuoguanStore(tmp_path)
    outside = submit_relationship_touch_candidate(
        store,
        identity=_boss(),
        target_role="teacher",
        target_user_id="LiuLi",
        touch_type="record_relief",
        message="刘老师，请告诉我你今天的学生服务记录是否已经补齐？",
        reason="补一项工作事实。",
        value="目标推进。",
        work_related=True,
        operation_id="outside",
    )["candidate"]
    denied = execute_relationship_touch(
        store,
        identity=_boss(),
        candidate_id=outside["candidate_id"],
        operation_id="outside-execute",
        now=datetime(2026, 8, 13, 15, 0, tzinfo=CN_TZ),
    )
    assert denied["ok"] is False
    assert denied["error"] == "target_not_in_rollout_allowlist"

    unsafe = submit_relationship_touch_candidate(
        store,
        identity=_boss(),
        target_role="teacher",
        target_user_id="CeShi",
        touch_type="record_relief",
        message="李老师，请你现在联系家长，把这段话发给家长后告诉我结果。",
        reason="错误的代发请求。",
        value="不应执行。",
        work_related=True,
        operation_id="unsafe",
    )
    assert unsafe["ok"] is False or execute_relationship_touch(
        store,
        identity=_boss(),
        candidate_id=str((unsafe.get("candidate") or {}).get("candidate_id") or "missing"),
        operation_id="unsafe-execute",
        now=datetime(2026, 8, 13, 15, 0, tzinfo=CN_TZ),
    )["ok"] is False


def test_goal_action_resumes_into_outreach_and_reply_updates_goal(tmp_path):
    from plugins.tuoguan_core.proactive_work import (
        execute_goal_action_decision,
        query_goal_actions,
        submit_goal_action,
        update_relationship_touch,
    )
    from plugins.tuoguan_core.store import TuoguanStore

    _setup(tmp_path)
    _active_goal(tmp_path)
    _write_json(tmp_path, "students.json", {
        "王同学": {
            "campus_id": "main",
            "program_id": "regular_tuoguan",
            "service_mode": "evening_only",
            "teacher": "CeShi",
            "evening_teacher_user_id": "CeShi",
            "status": "active",
        },
        "赵同学": {
            "campus_id": "main",
            "program_id": "regular_tuoguan",
            "service_mode": "evening_only",
            "teacher": "LiuLi",
            "evening_teacher_user_id": "LiuLi",
            "status": "active",
        },
    })
    store = TuoguanStore(tmp_path)
    action = submit_goal_action(
        store,
        identity=_boss(),
        goal_id="goal-renewal",
        action_type="ask_staff_fact",
        summary="请李老师确认王同学家长当前态度和下一步安排。",
        target_role="teacher",
        target_user_id="CeShi",
        target_name="李老师",
        student_names=["王同学"],
        evidence_requirement="家长态度和下一步安排都要明确。",
        operation_id="goal-action-1",
    )["goal_action"]
    executed = execute_goal_action_decision(
        store,
        identity=_system(),
        goal_action_id=action["goal_action_id"],
        decision="execute",
        operation_id="goal-action-execute",
        message="李老师，请告诉我王同学家长目前是什么态度，下一步准备怎么跟进？",
        decision_reason="这是目标当前缺失的关键事实。",
        now=datetime(2026, 8, 13, 15, 0, tzinfo=CN_TZ),
    )
    assert executed["ok"] is True
    assert executed["delivery_state"] == "queued"
    assert executed["goal_action"]["status"] == "waiting_reply"

    candidate_id = executed["candidate"]["candidate_id"]
    reply = update_relationship_touch(
        store,
        identity=_boss(),
        candidate_id=candidate_id,
        status="replied_sufficient",
        operation_id="reply-1",
        reply_text="家长认可目前服务，月底前继续保持每周反馈。",
        evidence_complete=True,
    )
    assert reply["writeback_verified"] is True
    queried = query_goal_actions(store, identity=_boss(), goal_id="goal-renewal", include_closed=True)
    assert queried["goal_actions"][0]["status"] == "replied_sufficient"
    open_after_reply = query_goal_actions(store, identity=_boss(), goal_id="goal-renewal")
    assert open_after_reply["goal_actions"][0]["status"] == "replied_sufficient"
    verified = execute_goal_action_decision(
        store,
        identity=_system(),
        goal_action_id=action["goal_action_id"],
        decision="execute",
        operation_id="verify-reply-1",
        decision_reason="老师回复同时包含家长态度和下一步安排，满足本行动证据要求。",
        now=datetime(2026, 8, 13, 15, 10, tzinfo=CN_TZ),
    )
    assert verified["writeback_verified"] is True
    assert verified["goal_action"]["status"] == "resolved"
    assert verified["relationship_updates"][0]["candidate"]["status"] == "resolved"
    goals = json.loads((tmp_path / "goal_operator_goals.json").read_text(encoding="utf-8"))
    goal = next(item for item in goals["goals"] if item["goal_id"] == "goal-renewal")
    assert goal["teacher_updates"][0]["student_name"] == "王同学"
    assert "家长认可" in goal["teacher_updates"][0]["update_text"]


def test_night_wakeup_never_executes_existing_touch_or_goal_action(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import run_autonomous_employee_loop
    from plugins.tuoguan_core.digital_employee_state import submit_relationship_touch_candidate
    from plugins.tuoguan_core.proactive_work import submit_goal_action
    from plugins.tuoguan_core.store import TuoguanStore

    _setup(tmp_path)
    _active_goal(tmp_path)
    store = TuoguanStore(tmp_path)
    candidate = submit_relationship_touch_candidate(
        store,
        identity=_boss(),
        target_role="teacher", target_user_id="CeShi", target_name="李老师",
        touch_type="record_relief", message="李老师，请确认一项任务结果。", reason="目标缺事实。",
        value="推进目标。", work_related=True, operation_id="night-candidate",
    )["candidate"]
    action = submit_goal_action(
        store, identity=_boss(), goal_id="goal-renewal", action_type="ask_staff_fact",
        summary="夜间不得执行的事实请求。", target_role="teacher", target_user_id="CeShi",
        evidence_requirement="老师真实回复。", operation_id="night-action",
    )["goal_action"]

    def decision_provider(_materials):
        return {
            "employee_summary": "小优夜间只复盘。",
            "institution_understanding": "",
            "goal_progress_view": "",
            "observations": [], "work_item_updates": [], "questions_to_humans": [],
            "boss_attention_candidates": [], "relationship_touch_candidates": [],
            "relationship_touch_executions": [{"candidate_id": candidate["candidate_id"], "decision_reason": "不应执行"}],
            "goal_action_decisions": [{"goal_action_id": action["goal_action_id"], "decision": "execute", "message": "不应发送"}],
            "institution_fact_gaps": [], "value_progress_entries": [], "agent_delegation_decisions": [],
            "evolution_candidates": [], "self_review": {}, "external_actions": [],
        }

    result = run_autonomous_employee_loop(
        store,
        now=datetime(2026, 8, 13, 23, 30, tzinfo=CN_TZ),
        decision_provider=decision_provider,
    )
    assert result["ok"] is True
    assert json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8")) == []


def test_internal_goal_action_reads_real_evidence_before_model_verification(tmp_path):
    from plugins.tuoguan_core.proactive_work import execute_goal_action_decision, submit_goal_action
    from plugins.tuoguan_core.store import TuoguanStore

    _setup(tmp_path)
    _active_goal(tmp_path)
    (tmp_path / "goal_evidence.jsonl").write_text(
        json.dumps({"goal_id": "another-goal", "evidence_id": "other-1"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    store = TuoguanStore(tmp_path)
    action = submit_goal_action(
        store,
        identity=_boss(),
        goal_id="goal-renewal",
        action_type="query_internal_data",
        summary="核对当前事实基线和第一项真实缺口。",
        evidence_requirement="必须列明来源、已知、未知和事实归属人。",
        operation_id="internal-read-plan",
    )["goal_action"]

    read_result = execute_goal_action_decision(
        store,
        identity=_system(),
        goal_action_id=action["goal_action_id"],
        decision="execute",
        operation_id="internal-read-execute",
        decision_reason="先读取内部可信材料。",
        now=datetime(2026, 8, 13, 18, 0, tzinfo=CN_TZ),
    )

    assert read_result["ok"] is True
    assert read_result["writeback_verified"] is True
    assert read_result["goal_action"]["status"] == "replied_sufficient"
    assert read_result["evidence_snapshot"]["read_only"] is True
    assert "goal_operator_goals.json" in read_result["evidence_snapshot"]["sources"]
    assert "内部事实反查" in read_result["goal_action"]["last_result"]
    assert "目标证据=0条" in read_result["goal_action"]["last_result"]
    assert json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8")) == []

    verified = execute_goal_action_decision(
        store,
        identity=_system(),
        goal_action_id=action["goal_action_id"],
        decision="execute",
        operation_id="internal-read-verify",
        decision_reason="反查来源和数据口径完整，足以完成当前基线核对行动。",
        now=datetime(2026, 8, 13, 18, 30, tzinfo=CN_TZ),
    )
    assert verified["ok"] is True
    assert verified["goal_action"]["status"] == "resolved"

    unsupported = submit_goal_action(
        store,
        identity=_boss(),
        goal_id="goal-renewal",
        action_type="fill_institution_fact",
        summary="填写尚未确认的机构事实。",
        operation_id="unsupported-internal-plan",
    )["goal_action"]
    rejected = execute_goal_action_decision(
        store,
        identity=_system(),
        goal_action_id=unsupported["goal_action_id"],
        decision="execute",
        operation_id="unsupported-internal-execute",
        now=datetime(2026, 8, 13, 18, 40, tzinfo=CN_TZ),
    )
    assert rejected["ok"] is False
    assert rejected["error"] == "goal_action_requires_specific_evidence_tool"


def test_daytime_model_can_persist_next_goal_action_without_executing_it(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import run_autonomous_employee_loop
    from plugins.tuoguan_core.proactive_work import query_goal_actions
    from plugins.tuoguan_core.store import TuoguanStore

    _setup(tmp_path)
    _active_goal(tmp_path)
    store = TuoguanStore(tmp_path)

    def decision_provider(_materials):
        return {
            "employee_summary": "小优，优益托管机构数字员工。",
            "institution_understanding": "先核对目标事实，不把旧名单当成当前事实。",
            "goal_progress_view": "目标已确认，下一步只保存低风险事实核对行动。",
            "observations": [], "work_item_updates": [], "questions_to_humans": [],
            "boss_attention_candidates": [], "relationship_touch_candidates": [],
            "relationship_touch_executions": [], "goal_action_decisions": [],
            "goal_action_submissions": [{
                "goal_id": "goal-renewal",
                "action_type": "query_internal_data",
                "summary": "读取续费目标当前内部证据。",
                "evidence_requirement": "列出目标状态、证据和第一项缺口。",
                "decision_reason": "先取数再判断是否找人。",
            }],
            "institution_fact_gaps": [], "value_progress_entries": [],
            "agent_delegation_decisions": [], "evolution_candidates": [],
            "self_review": {}, "external_actions": [],
        }

    result = run_autonomous_employee_loop(
        store,
        now=datetime(2026, 8, 13, 15, 0, tzinfo=CN_TZ),
        decision_provider=decision_provider,
    )

    assert result["ok"] is True
    actions = query_goal_actions(store, identity=_boss(), goal_id="goal-renewal")
    assert actions["goal_action_count"] == 1
    assert actions["goal_actions"][0]["status"] == "planned"
    assert result["external_actions_taken"] == []


def test_failed_goal_execution_marks_autonomous_cycle_degraded(tmp_path, monkeypatch):
    import plugins.tuoguan_core.autonomous_employee_loop as loop
    from plugins.tuoguan_core.store import TuoguanStore

    _setup(tmp_path)
    _active_goal(tmp_path)
    store = TuoguanStore(tmp_path)

    monkeypatch.setattr(
        loop,
        "execute_goal_action_decision",
        lambda *args, **kwargs: {"ok": False, "error": "writeback_failed", "message": "反查失败"},
    )

    def decision_provider(_materials):
        return {
            "employee_summary": "小优，优益托管机构数字员工。",
            "institution_understanding": "目标需要继续推进。",
            "goal_progress_view": "存在到期行动。",
            "observations": [], "work_item_updates": [], "questions_to_humans": [],
            "boss_attention_candidates": [], "relationship_touch_candidates": [],
            "relationship_touch_executions": [], "goal_action_submissions": [],
            "goal_action_decisions": [{"goal_action_id": "action-1", "decision": "execute"}],
            "institution_fact_gaps": [], "value_progress_entries": [],
            "agent_delegation_decisions": [], "evolution_candidates": [],
            "self_review": {}, "external_actions": [],
        }

    result = loop.run_autonomous_employee_loop(
        store,
        now=datetime(2026, 8, 13, 15, 0, tzinfo=CN_TZ),
        decision_provider=decision_provider,
    )
    assert result["ok"] is False
    assert result["error"] == "employee_loop_action_execution_failed"
    assert result["external_actions_taken"] == []


def test_confirmed_goal_can_create_only_bounded_low_risk_teacher_task(tmp_path):
    from plugins.tuoguan_core.proactive_work import execute_goal_action_decision, query_goal_actions, submit_goal_action
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    _setup(tmp_path)
    _active_goal(tmp_path)
    _write_json(tmp_path, "students.json", {
        "王同学": {
            "campus_id": "main",
            "program_id": "regular_tuoguan",
            "service_mode": "evening_only",
            "teacher": "CeShi",
            "evening_teacher_user_id": "CeShi",
            "status": "active",
        },
        "赵同学": {
            "campus_id": "main",
            "program_id": "regular_tuoguan",
            "service_mode": "evening_only",
            "teacher": "LiuLi",
            "evening_teacher_user_id": "LiuLi",
            "status": "active",
        },
    })
    store = TuoguanStore(tmp_path)
    action = submit_goal_action(
        store,
        identity=_boss(),
        goal_id="goal-renewal",
        action_type="create_low_risk_task",
        summary="请完成王同学本周家校沟通并反馈真实结果。",
        target_role="teacher",
        target_user_id="CeShi",
        target_name="李老师",
        student_names=["王同学"],
        evidence_requirement="家长态度、孩子当前情况和下一步安排。",
        operation_id="low-risk-action",
    )["goal_action"]
    result = execute_goal_action_decision(
        store,
        identity=_system(),
        goal_action_id=action["goal_action_id"],
        decision="execute",
        operation_id="low-risk-execute",
        decision_reason="责任关系明确，属于老板已确认目标。",
        now=datetime(2026, 8, 13, 15, 0, tzinfo=CN_TZ),
    )
    assert result["ok"] is True
    assert result["delivery_state"] == "queued"
    tasks = json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8"))
    assert tasks[0]["goal_id"] == "goal-renewal"
    assert tasks[0]["goal_action_id"] == action["goal_action_id"]
    assert tasks[0]["created_autonomously_within_goal"] is True
    assert "家长态度" in tasks[0]["evidence_requirement"]
    active_context = json.loads((tmp_path / "active_task_context.json").read_text(encoding="utf-8"))
    pending_context = json.loads((tmp_path / "pending_next_task_context.json").read_text(encoding="utf-8"))
    model_focus = json.loads((tmp_path / "model_focus.json").read_text(encoding="utf-8"))
    assert active_context["CeShi"]["task_id"] == tasks[0]["id"]
    assert pending_context["CeShi"]["goal_action_id"] == action["goal_action_id"]
    assert model_focus["wecom_callback:CeShi"]["task_id"] == tasks[0]["id"]

    teacher_service = TuoguanToolService(
        store=store,
        platform="wecom_callback",
        user_id="CeShi",
        user_name="李老师",
        chat_id="CeShi",
        session_key="wecom_callback:CeShi",
    )
    completed = teacher_service.update_task(
        reply="我已经和王同学妈妈沟通过了，家长表示认可，后续我会继续每周跟进。",
        operation_id="teacher-completes-goal-task",
    )
    assert completed["ok"] is True
    assert completed["data"]["task"]["status"] == "completed"
    assert completed["data"]["goal_action_update"]["writeback_verified"] is True
    synced = query_goal_actions(store, identity=_boss(), goal_id="goal-renewal", include_closed=True)
    synced_action = next(item for item in synced["goal_actions"] if item["goal_action_id"] == action["goal_action_id"])
    assert synced_action["status"] == "replied_sufficient"

    wrong_teacher_action = submit_goal_action(
        store,
        identity=_boss(),
        goal_id="goal-renewal",
        action_type="create_low_risk_task",
        summary="请完成赵同学本周家校沟通并反馈真实结果。",
        target_role="teacher",
        target_user_id="CeShi",
        target_name="李老师",
        student_names=["赵同学"],
        evidence_requirement="家长态度和下一步安排。",
        operation_id="wrong-teacher-action",
    )["goal_action"]
    wrong_teacher = execute_goal_action_decision(
        store,
        identity=_system(),
        goal_action_id=wrong_teacher_action["goal_action_id"],
        decision="execute",
        operation_id="wrong-teacher-execute",
        decision_reason="模型错误选择了责任老师。",
        now=datetime(2026, 8, 14, 15, 0, tzinfo=CN_TZ),
    )
    assert wrong_teacher["ok"] is False
    assert wrong_teacher["error"] == "responsible_teacher_mismatch"

    high_risk = submit_goal_action(
        store,
        identity=_boss(),
        goal_id="goal-renewal",
        action_type="create_low_risk_task",
        summary="根据结果调整李老师工资和绩效。",
        target_role="teacher",
        target_user_id="CeShi",
        operation_id="high-risk-action",
    )
    assert high_risk["ok"] is False
    assert high_risk["error"] == "high_risk_goal_action_requires_confirmation"


def test_two_low_frequency_followups_then_escalate_to_manager_plan(tmp_path):
    from plugins.tuoguan_core.proactive_work import execute_goal_action_decision, query_goal_actions, submit_goal_action
    from plugins.tuoguan_core.store import TuoguanStore

    _setup(tmp_path)
    _active_goal(tmp_path)
    store = TuoguanStore(tmp_path)
    action = submit_goal_action(
        store,
        identity=_boss(),
        goal_id="goal-renewal",
        action_type="ask_staff_fact",
        summary="请李老师确认王同学家长沟通结果。",
        target_role="teacher",
        target_user_id="CeShi",
        target_name="李老师",
        evidence_requirement="家长态度和下一步安排。",
        operation_id="retry-action",
    )["goal_action"]
    messages = [
        "李老师，请告诉我王同学家长目前是什么态度和下一步安排？",
        "李老师，我只补一个缺口：王同学家长目前是什么态度？",
        "李老师，再确认最后一点：王同学下一步准备什么时候跟进？",
    ]
    for index, message in enumerate(messages):
        result = execute_goal_action_decision(
            store,
            identity=_system(),
            goal_action_id=action["goal_action_id"],
            decision="execute",
            operation_id=f"retry-execute-{index}",
            message=message,
            decision_reason="仍缺少目标证据，调整为更简洁问法。",
            now=datetime(2026, 8, 13 + index, 15, 0, tzinfo=CN_TZ),
        )
        assert result["ok"] is True
    current = query_goal_actions(store, identity=_boss(), goal_id="goal-renewal", include_closed=True)
    original = next(item for item in current["goal_actions"] if item["goal_action_id"] == action["goal_action_id"])
    assert original["retry_count"] == 2

    escalated = execute_goal_action_decision(
        store,
        identity=_system(),
        goal_action_id=action["goal_action_id"],
        decision="execute",
        operation_id="retry-escalate",
        message="这条不应再发给李老师。",
        decision_reason="两次低频追问仍没有完整事实。",
        now=datetime(2026, 8, 16, 15, 0, tzinfo=CN_TZ),
    )
    assert escalated["ok"] is True
    assert escalated["escalation"] == "manager_planned"
    assert escalated["goal_action"]["status"] == "escalated"
    assert escalated["manager_goal_action"]["target_user_id"] == "CuiXiaoXia"


def test_outreach_honesty_guard_distinguishes_candidate_queue_and_sent():
    from plugins.tuoguan_core.runtime_foundation import _sanitize_external_reply

    candidate = _sanitize_external_reply("我现在就去找李老师。", verified_state_change=True, used_trusted_tool=True, outreach_state="candidate")
    assert "尚未进入发送队列" in candidate
    queued = _sanitize_external_reply("我已经通知李老师了。", verified_state_change=True, used_trusted_tool=True, outreach_state="queued")
    assert "只有入队回执" in queued
    sent = _sanitize_external_reply("我已经通知李老师了。", verified_state_change=True, used_trusted_tool=True, outreach_state="sent")
    assert sent == "我已经通知李老师了。"


def test_public_tools_expose_authorization_execution_and_goal_actions(tmp_path):
    from plugins.tuoguan_core.runtime_foundation import MODEL_SELECTED_READ_TOOLS, WRITE_TOOLS
    from plugins.tuoguan_core.tool_service import TuoguanToolService
    from plugins.tuoguan_core.tools import TOOLS
    from plugins.tuoguan_core.store import TuoguanStore

    _setup(tmp_path)
    names = {name for name, _schema, _handler in TOOLS}
    expected = {
        "tuoguan_query_proactive_authorizations",
        "tuoguan_submit_proactive_authorization",
        "tuoguan_execute_relationship_touch",
        "tuoguan_update_relationship_touch",
        "tuoguan_query_goal_actions",
        "tuoguan_submit_goal_action",
    }
    assert expected <= names
    assert {"tuoguan_query_proactive_authorizations", "tuoguan_query_goal_actions"} <= MODEL_SELECTED_READ_TOOLS
    assert expected - MODEL_SELECTED_READ_TOOLS <= WRITE_TOOLS

    service = TuoguanToolService(
        store=TuoguanStore(tmp_path),
        platform="wecom_callback",
        user_id="JinWenJie",
        user_name="金总",
        chat_id="JinWenJie",
        session_key="JinWenJie",
    )
    result = service.submit_relationship_touch_candidate(
        target_role="teacher",
        target_user_id="CeShi",
        target_name="李老师",
        touch_type="record_relief",
        message="李老师，请告诉我今天任务执行结果和还缺少的一个事实？",
        reason="测试同一工具内候选到入队闭环。",
        value="避免只保存候选不执行。",
        work_related=True,
        execute_if_authorized=True,
        operation_id="composite-touch",
    )
    assert result["ok"] is True
    assert result["data"]["execution"]["delivery_state"] == "queued"
