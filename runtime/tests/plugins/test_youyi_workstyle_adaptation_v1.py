from __future__ import annotations

import json
from pathlib import Path


def _write_json(path: Path, name: str, payload) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _identity(role: str = "boss"):
    from plugins.tuoguan_core.models import UserIdentity

    return UserIdentity(
        platform="wecom",
        platform_user_id=f"{role}1",
        canonical_user_id=f"{role}1",
        person_name={"boss": "机构负责人", "teacher": "示例老师", "manager": "店长"}.get(role, role),
        role=role,
        approval_state="approved",
    )


def test_workstyle_feedback_guard_auto_saves_clear_low_risk_feedback(tmp_path):
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.workstyle_profiles import (
        observe_workstyle_after_reply,
        query_person_workstyle_profile,
        query_workstyle_adaptation_health,
    )

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    store = TuoguanStore(tmp_path)
    identity = _identity("boss")

    observed = observe_workstyle_after_reply(
        store,
        identity=identity,
        raw_text="以后所有汇报先说结论，三条以内，不要发一大堆。",
        final_reply="我理解了，后续会按这个方向处理。",
        tool_calls=[],
        tool_results=[],
        ledger_id="ledger-workstyle-1",
        message_id="msg-workstyle-1",
        session_id="session-workstyle-1",
    )

    assert observed["feedback_detected"] is True
    assert observed["auto_saved"] is True
    assert observed["missed_feedback_save"] is True

    profile = query_person_workstyle_profile(store, identity=identity, scope="daily_report")
    assert profile["ok"] is True
    assert profile["preference_count"] == 1
    assert profile["preferences"][0]["dimension_key"] == "length"

    health = query_workstyle_adaptation_health(store, identity=identity)
    assert health["ok"] is True
    assert health["preference_count"] == 1
    assert health["application_count"] >= 1
    assert health["missed_feedback_save_count"] == 1

    evolution_rows = [
        json.loads(line)
        for line in (tmp_path / "self_evolution_events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any("模型没有主动调用偏好保存工具" in row["summary"] for row in evolution_rows)


def test_workstyle_feedback_guard_blocks_high_risk_autosave(tmp_path):
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.workstyle_profiles import observe_workstyle_after_reply

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    store = TuoguanStore(tmp_path)
    identity = _identity("boss")

    observed = observe_workstyle_after_reply(
        store,
        identity=identity,
        raw_text="以后自动联系家长，把优惠价格也改掉。",
        final_reply="我理解了，但这个涉及边界，需要确认。",
        tool_calls=[],
        tool_results=[],
        ledger_id="ledger-workstyle-risk",
        message_id="msg-workstyle-risk",
    )

    assert observed["feedback_detected"] is False
    assert observed["auto_saved"] is False
    assert not (tmp_path / "person_workstyle_events.jsonl").exists()


def test_institution_discussion_pacing_does_not_leak_into_normal_replies(tmp_path):
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.workstyle_profiles import (
        infer_workstyle_scope,
        resolve_workstyle_for,
        submit_person_workstyle_preference,
    )
    from plugins.tuoguan_core.write_guard import authorized_system_write

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    store = TuoguanStore(tmp_path)
    identity = _identity("boss")
    with authorized_system_write(store.data_dir, job_name="test_institution_pacing", allowed_files={"person_workstyle_events.jsonl"}):
        saved = submit_person_workstyle_preference(
            store,
            identity=identity,
            preference_type="other_low_risk",
            scope="institution_work",
            preference_text="制度讨论一项一项推进，一次只讲一个章节、只问一个关键问题。",
            normalized_rule="制度讨论一次只推进一个章节和一个关键问题。",
            operation_id="institution-pacing-1",
        )

    assert saved["ok"] is True
    assert infer_workstyle_scope("继续讨论安全制度") == "institution_work"
    assert infer_workstyle_scope("老师今天有什么任务") == "task_followup"
    assert "interaction_pacing" not in set(resolve_workstyle_for(store, identity=identity, scope="direct_reply")["applied_dimensions"])
    assert "interaction_pacing" in set(resolve_workstyle_for(store, identity=identity, scope="institution_work")["applied_dimensions"])


def test_institution_workstyle_scope_repair_preserves_history_and_repairs_only_scope(tmp_path):
    from scripts.repair_institution_workstyle_scope_v1 import (
        SOURCE_PREFERENCE_ID,
        repair_institution_workstyle_scope,
    )
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.workstyle_profiles import WORKSTYLE_EVENTS_FILE
    from plugins.tuoguan_core.write_guard import authorized_system_write

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    store = TuoguanStore(tmp_path)
    with authorized_system_write(store.data_dir, job_name="test_scope_repair_source", allowed_files={WORKSTYLE_EVENTS_FILE}):
        store.append_jsonl_verified(WORKSTYLE_EVENTS_FILE, {
            "record_type": "person_workstyle_preference",
            "preference_id": SOURCE_PREFERENCE_ID,
            "tenant_id": "example_institution",
            "target_user_id": "boss1",
            "target_name": "机构负责人",
            "target_role": "boss",
            "preference_type": "other_low_risk",
            "dimension_key": "interaction_pacing",
            "scope": "all_communication",
            "preference_text": "一次只问一个关键问题。",
            "normalized_rule": "一次只问一个关键问题。",
            "status": "active",
            "source_text": "请一次只问一个关键问题。",
            "source": "legacy_message_reference",
        })

    preview = repair_institution_workstyle_scope(store)
    assert preview["preview_verified"] is True
    repaired = repair_institution_workstyle_scope(store, apply=True)
    assert repaired["writeback_verified"] is True
    assert repaired["direct_reply_has_pacing"] is False
    assert repaired["institution_work_has_pacing"] is True
    rows = (tmp_path / WORKSTYLE_EVENTS_FILE).read_text(encoding="utf-8")
    assert SOURCE_PREFERENCE_ID in rows
    assert "person_workstyle_semantic_mismatch" in rows


def test_workstyle_dimensions_stack_and_same_dimension_replaces(tmp_path):
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.workstyle_profiles import (
        query_person_workstyle_profile,
        resolve_workstyle_for,
        submit_person_workstyle_preference,
    )
    from plugins.tuoguan_core.write_guard import authorized_system_write

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    store = TuoguanStore(tmp_path)
    identity = _identity("teacher")

    with authorized_system_write(store.data_dir, job_name="test_workstyle_stack", allowed_files={"person_workstyle_events.jsonl"}):
        first = submit_person_workstyle_preference(
            store,
            identity=identity,
            preference_type="format",
            scope="direct_reply",
            preference_text="以后回复先说结论。",
            operation_id="style-structure-1",
        )
        second = submit_person_workstyle_preference(
            store,
            identity=identity,
            preference_type="format",
            scope="direct_reply",
            preference_text="以后每段之间留空行。",
            operation_id="style-layout-1",
        )
        third = submit_person_workstyle_preference(
            store,
            identity=identity,
            preference_type="format",
            scope="direct_reply",
            preference_text="以后先说一句最终结论，再说原因。",
            operation_id="style-structure-2",
        )

    assert first["ok"] and second["ok"] and third["ok"]
    assert first["preference"]["dimension_key"] == "structure"
    assert second["preference"]["dimension_key"] == "layout"
    assert third["preference"]["dimension_key"] == "structure"
    assert first["preference"]["preference_id"] in third["preference"]["supersedes"]
    assert second["preference"]["preference_id"] not in third["preference"]["supersedes"]

    profile = query_person_workstyle_profile(store, identity=identity, scope="direct_reply")
    assert profile["preference_count"] == 2
    assert set(profile["applied_dimensions"]) == {"layout", "structure"}

    resolved = resolve_workstyle_for(store, identity=identity, scope="direct_reply")
    assert resolved["ok"] is True
    assert set(resolved["applied_dimensions"]) == {"layout", "structure"}


def test_runtime_reply_guard_records_workstyle_autosave_and_self_correction(tmp_path):
    from plugins.tuoguan_core.runtime_foundation import clear_runtime_state, ensure_outbound_reply_recorded
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    store = TuoguanStore(tmp_path)
    clear_runtime_state()

    ensure_outbound_reply_recorded(
        store=store,
        message_id="msg-runtime-style",
        conversation_id="wecom_callback:boss1",
        user_id="boss1",
        role="boss",
        raw_text="以后所有汇报先说结论，三条以内，不要发一大堆。",
        final_reply="我理解了，后续会按这个方向处理。",
        entered_model=True,
        session_id="session-runtime-style",
        route_decision="model_first",
    )

    workstyle_rows = [
        json.loads(line)
        for line in (tmp_path / "person_workstyle_events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    evolution_rows = [
        json.loads(line)
        for line in (tmp_path / "self_evolution_events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    reply_rows = [
        json.loads(line)
        for line in (tmp_path / "reply_ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert any(row.get("record_type") == "person_workstyle_preference" for row in workstyle_rows)
    assert any(row.get("record_type") == "person_workstyle_application" for row in workstyle_rows)
    assert any(row.get("candidate_type") == "self_correction" for row in evolution_rows)
    assert reply_rows[-1]["workstyle_adaptation"]["auto_saved"] is True
