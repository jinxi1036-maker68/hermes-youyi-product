from __future__ import annotations

import json


def _write_json(root, name, value):
    (root / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _write_jsonl(root, name, rows):
    (root / name).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def test_repair_is_append_only_and_converts_authorization_semantics(tmp_path):
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.workstyle_profiles import query_person_workstyle_profile
    from scripts.repair_proactive_employee_state_v1 import apply_plan, build_plan

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_json(tmp_path, "notification_outbox.json", [])
    _write_json(tmp_path, "tasks.json", [])
    _write_json(tmp_path, "students.json", {})
    _write_json(
        tmp_path,
        "goal_operator_goals.json",
        {"goals": [{"goal_id": "goal1", "status": "confirmed", "goal_text": "提高续费稳定性", "owner_user_id": "JinWenJie"}]},
    )
    _write_json(
        tmp_path,
        "wecom_whitelist.json",
        {
            "super_users": ["JinWenJie"],
            "allowed_users": ["JinWenJie", "CeShi"],
            "user_roles": {"JinWenJie": "boss", "CeShi": "teacher"},
        },
    )
    _write_jsonl(
        tmp_path,
        "relationship_touch_candidates.jsonl",
        [{
            "candidate_id": "relationship_touch_old",
            "target_role": "teacher",
            "target_user_id": "CeShi",
            "message": "李老师，请确认一项记录。",
            "reason": "旧候选",
            "status": "candidate",
            "created_at": "2026-08-13T15:09:00+08:00",
        }],
    )
    _write_jsonl(
        tmp_path,
        "person_workstyle_events.jsonl",
        [{
            "record_type": "person_workstyle_preference",
            "preference_id": "pref_wrong_tone",
            "target_user_id": "JinWenJie",
            "scope": "all_communication",
            "dimension_key": "tone",
            "status": "active",
            "normalized_rule": "小优自主决定找谁和什么时候找",
            "created_at": "2026-08-13T15:09:00+08:00",
        }],
    )
    _write_jsonl(
        tmp_path,
        "hermes_work_items.jsonl",
        [{
            "record_type": "work_item",
            "work_item_id": "work1",
            "focus_key": "goal:goal1",
            "title": "目标推进",
            "focus_summary": "旧等待",
            "status": "active",
            "current_waiting": {"reason": "等待李老师全名和看板获取方式"},
            "created_at": "2026-08-13T12:00:00+08:00",
        }],
    )
    _write_jsonl(
        tmp_path,
        "self_evolution_events.jsonl",
        [{
            "record_type": "self_evolution_event",
            "evolution_event_id": "evolution_without_evidence",
            "semantic_fingerprint": "fingerprint_without_evidence",
            "candidate_type": "tomorrow_focus",
            "summary": "明天去问店长旧名单。",
            "evidence": [],
            "status": "ready_for_application",
            "source": {"actor_user_id": "autonomous_employee_loop"},
            "created_at": "2026-08-13T19:00:00+08:00",
        }],
    )
    store = TuoguanStore(tmp_path)
    plan = build_plan(store)
    assert plan["stale_relationship_touch_ids"] == ["relationship_touch_old"]
    assert plan["semantic_mismatch_preference_ids"] == ["pref_wrong_tone"]
    assert plan["stale_work_focus_keys"] == ["goal:goal1"]
    assert len(plan["authorization_additions"]) == 2
    assert plan["goal_action_seed_goal_ids"] == ["goal1"]
    assert plan["unsupported_evolution_event_ids"] == ["evolution_without_evidence"]

    result = apply_plan(store, plan)
    assert result["ok"] is True
    touch_rows = [json.loads(line) for line in (tmp_path / "relationship_touch_candidates.jsonl").read_text(encoding="utf-8").splitlines()]
    assert touch_rows[0]["status"] == "candidate"
    assert touch_rows[-1]["status"] == "superseded"
    preference = query_person_workstyle_profile(
        store,
        identity=UserIdentity("wecom_callback", "JinWenJie", "JinWenJie", "金总", "boss", "approved"),
        target_user_id="JinWenJie",
    )
    assert preference["preference_count"] == 0
    auth_rows = (tmp_path / "proactive_authorizations.jsonl").read_text(encoding="utf-8")
    assert "CeShi" in auth_rows and "JinWenJie" in auth_rows
    action_rows = (tmp_path / "goal_actions.jsonl").read_text(encoding="utf-8")
    assert "goal1" in action_rows
    assert "query_internal_data" in action_rows
    evolution_rows = [json.loads(line) for line in (tmp_path / "self_evolution_events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert evolution_rows[0]["status"] == "ready_for_application"
    assert evolution_rows[-1]["status"] == "superseded"
    assert evolution_rows[-1]["invalidation_reason"] == "夜间候选缺少当前事实证据，不能进入次日应用。"
    assert build_plan(store)["unsupported_evolution_event_ids"] == []
