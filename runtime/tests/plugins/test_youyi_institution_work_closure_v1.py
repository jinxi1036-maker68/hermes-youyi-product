from __future__ import annotations

import json
from pathlib import Path


def _json(path: Path, name: str, payload: object) -> None:
    (path / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _store(tmp_path: Path):
    from plugins.tuoguan_core.store import TuoguanStore

    # This module test exercises the pure append-only state machine. Runtime
    # authorization is covered through ToolService and is intentionally not
    # bypassed in production.
    _json(tmp_path, "write_guard_config.json", {"enabled": False})
    _json(tmp_path, "wecom_whitelist.json", {
        "super_users": ["JinWenJie"],
        "allowed_users": ["CeShi"],
        "user_roles": {"JinWenJie": "boss", "CeShi": "teacher"},
    })
    _json(tmp_path, "teacher_wecom_map.json", {"金总": "JinWenJie", "李老师": "CeShi"})
    _json(tmp_path, "staff.json", {
        "JinWenJie": {"name": "金总", "role": "boss", "status": "active"},
        "CeShi": {"name": "李老师", "role": "teacher", "status": "active"},
    })
    _json(tmp_path, "tasks.json", [])
    _json(tmp_path, "notification_outbox.json", [])
    return TuoguanStore(tmp_path)


def _boss():
    from plugins.tuoguan_core.models import UserIdentity

    return UserIdentity("test", "JinWenJie", "JinWenJie", "金总", "boss", "approved")


def _safety_chain(store, *, content_source: str = "owner-message-content", authorization_source: str = "owner-message-authorization"):
    from plugins.tuoguan_core.digital_employee_state import advance_institution_work

    discovered = advance_institution_work(
        store,
        identity=_boss(),
        action="discover",
        operation_id="discover-safety",
        focus_key="institution:safety_management_policy",
        title="优益托管安全管理制度",
        summary="需要把现有安全做法整理为可复核草案。",
        evidence=[{"source_kind": "internal_confirmed", "summary": "家长在机构门口接孩子；发生伤情需通知家长。"}],
        source_message_id="owner-message-discover",
    )
    assert discovered["ok"] and discovered["writeback_verified"]
    work_item = discovered["work_item"]
    drafted = advance_institution_work(
        store,
        identity=_boss(),
        action="save_draft",
        operation_id="draft-safety",
        work_item_id=work_item["work_item_id"],
        artifact_title="优益托管安全管理制度 V0.1",
        artifact_content="家长在机构门口接孩子；发生伤情需通知家长。留样时长待专业核验。",
        pending_items=["留样时长和标准待官方来源或专业人员核验"],
        source_message_id="owner-message-draft",
    )
    assert drafted["ok"] and drafted["writeback_verified"]
    version_id = drafted["artifact"]["version_id"]
    submitted = advance_institution_work(
        store,
        identity=_boss(),
        action="submit_for_review",
        operation_id="submit-safety",
        work_item_id=work_item["work_item_id"],
        artifact_version_id=version_id,
        source_message_id="owner-message-submit",
    )
    assert submitted["ok"]
    approved = advance_institution_work(
        store,
        identity=_boss(),
        action="review_content",
        operation_id="content-safety",
        work_item_id=work_item["work_item_id"],
        artifact_version_id=version_id,
        decision="approved",
        source_message_id=content_source,
    )
    assert approved["ok"] and approved["work_item"]["institution_stage"] == "awaiting_implementation_authorization"
    return work_item["work_item_id"], version_id, authorization_source


def test_institution_work_requires_two_owner_decisions_and_real_execution_link(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import advance_institution_work, query_institution_work

    store = _store(tmp_path)
    work_item_id, version_id, authorization_source = _safety_chain(store)

    same_turn = advance_institution_work(
        store,
        identity=_boss(),
        action="authorize_implementation",
        operation_id="wrong-same-owner-turn",
        work_item_id=work_item_id,
        artifact_version_id=version_id,
        source_message_id="owner-message-content",
    )
    assert same_turn["ok"] is False
    assert same_turn["error"] == "separate_owner_authorization_required"

    authorization = advance_institution_work(
        store,
        identity=_boss(),
        action="authorize_implementation",
        operation_id="authorize-safety",
        work_item_id=work_item_id,
        artifact_version_id=version_id,
        implementation_scope={"targets": ["CeShi"], "boundary": "仅测试号"},
        source_message_id=authorization_source,
    )
    assert authorization["ok"] and authorization["writeback_verified"]
    assert authorization["work_item"]["institution_stage"] == "implementing"

    linked = advance_institution_work(
        store,
        identity=_boss(),
        action="link_execution",
        operation_id="link-safety",
        work_item_id=work_item_id,
        artifact_version_id=version_id,
        execution_link={"kind": "task", "task_id": "task-test", "delivery_status": "sent"},
        source_message_id="owner-message-link",
    )
    assert linked["ok"] and linked["writeback_verified"]
    assert linked["execution_link"]["task_id"] == "task-test"
    stored = query_institution_work(store, identity=_boss(), focus_key="institution:safety_management_policy")
    item = stored["items"][0]
    assert len(item["owner_decisions"]) == 2
    assert item["owner_decisions"][0]["decision_type"] == "content_approval"
    assert item["owner_decisions"][1]["decision_type"] == "implementation_authorization"
    assert item["execution_links"][0]["delivery_status"] == "sent"


def test_institution_draft_cannot_promote_unsupported_standard_without_pending_marker(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import advance_institution_work

    store = _store(tmp_path)
    discovered = advance_institution_work(
        store, identity=_boss(), action="discover", operation_id="discover-food",
        focus_key="institution:food_policy", title="食品安全流程", summary="需要整理食品留样流程。",
    )
    refused = advance_institution_work(
        store, identity=_boss(), action="save_draft", operation_id="draft-food",
        work_item_id=discovered["work_item"]["work_item_id"], artifact_title="食品流程 V0.1",
        artifact_content="食品留样48小时，每份125克。",
    )
    assert refused["ok"] is False
    assert refused["error"] == "unsupported_claims_require_pending_items"


def test_atomic_draft_submission_enters_content_review_without_a_second_write(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import advance_institution_work

    store = _store(tmp_path)
    discovered = advance_institution_work(
        store,
        identity=_boss(),
        action="discover",
        operation_id="discover-record-policy",
        focus_key="institution:student_record_policy",
        title="学生记录制度",
        summary="需要整理学生记录的最小规范。",
    )
    drafted = advance_institution_work(
        store,
        identity=_boss(),
        action="save_draft",
        operation_id="draft-record-policy",
        work_item_id=discovered["work_item"]["work_item_id"],
        artifact_title="学生记录制度 V0.1",
        artifact_content="记录范围和频率待老板审核。",
        submit_for_review=True,
        source_message_id="owner-draft-record-policy",
    )

    assert drafted["ok"] and drafted["writeback_verified"]
    assert drafted["artifact"]["status"] == "awaiting_review"
    assert drafted["work_item"]["institution_stage"] == "awaiting_content_approval"
    assert drafted["work_item"]["current_waiting"]["decision_type"] == "content_approval"


def test_institution_drafts_are_hidden_from_teacher_until_effective(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import advance_institution_work, query_institution_work
    from plugins.tuoguan_core.models import UserIdentity

    store = _store(tmp_path)
    _safety_chain(store)
    teacher = UserIdentity("test", "CeShi", "CeShi", "李老师", "teacher", "approved")
    assert query_institution_work(store, identity=teacher)["items"] == []


def test_institution_drafts_do_not_leak_through_generic_work_or_manager_dashboard(tmp_path):
    from plugins.tuoguan_core.dashboard_builder import build_dashboard_snapshot
    from plugins.tuoguan_core.digital_employee_state import query_hermes_work_items
    from plugins.tuoguan_core.models import UserIdentity

    store = _store(tmp_path)
    _safety_chain(store)
    _json(tmp_path, "staff.json", {
        "JinWenJie": {"name": "金总", "role": "boss", "status": "active"},
        "CeShi": {"name": "李老师", "role": "teacher", "status": "active"},
        "manager1": {"name": "崔老师", "role": "manager", "status": "active"},
    })
    manager = UserIdentity("test", "manager1", "manager1", "崔老师", "manager", "approved")

    assert query_hermes_work_items(store, identity=_boss())["items"] == []
    assert query_hermes_work_items(store, identity=manager)["items"] == []

    snapshot = build_dashboard_snapshot(store)
    assert "manager1" in snapshot["manager_dashboards"]
    manager_text = json.dumps(snapshot["manager_dashboards"].get("manager1", {}), ensure_ascii=False)
    teacher_text = json.dumps(snapshot["teacher_dashboards"].get("CeShi", {}), ensure_ascii=False)
    boss_text = json.dumps(snapshot["boss_dashboard"], ensure_ascii=False)

    assert "优益托管安全管理制度 V0.1" not in manager_text
    assert "优益托管安全管理制度 V0.1" not in teacher_text
    assert "优益托管安全管理制度 V0.1" in boss_text


def test_interaction_pacing_is_its_own_workstyle_dimension():
    from plugins.tuoguan_core.workstyle_profiles import _check_reply_compliance, _dimension_for_preference

    assert _dimension_for_preference("other_low_risk", "direct_reply", "一项一项说，一次只问一个问题") == "interaction_pacing"
    preference = {"dimension_key": "interaction_pacing", "normalized_rule": "制度讨论一次只推进一个章节和一个关键问题。"}
    assert _check_reply_compliance("第一章：接送。\n第二章：餐食。\n还要确认接送吗？还要确认餐食吗？", [preference])["ok"] is False
    assert _check_reply_compliance("这次先看接送这一项：家长是否仍在门口接孩子？", [preference])["ok"] is True


def test_institution_claim_guard_needs_matching_stage_receipt():
    from plugins.tuoguan_core.runtime_foundation import _sanitize_external_reply

    unrelated_write = _sanitize_external_reply(
        "安全制度已确认并已经落实。", verified_state_change=True, used_trusted_tool=True,
    )
    awaiting_authorization = _sanitize_external_reply(
        "安全制度已确认并已经落实。", verified_state_change=True, used_trusted_tool=True,
        institution_commitment_state="awaiting_implementation_authorization",
    )

    assert "不能" in unrelated_write
    assert awaiting_authorization == "内容已确认，仍待单独授权落实。"


def test_institution_claim_guard_preserves_honest_uncertainty_and_blocks_future_promise():
    from plugins.tuoguan_core.runtime_foundation import _sanitize_external_reply

    honest = _sanitize_external_reply("学生记录制度尚未确认，仍待老板审核。")
    promised = _sanitize_external_reply("8月31日我会自动开启并发给全体老师。")

    assert honest == "学生记录制度尚未确认，仍待老板审核。"
    assert "没有形成可验证的执行安排" in promised
