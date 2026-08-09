from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path


def _write_json(path: Path, name: str, payload) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _append_jsonl(path: Path, name: str, rows: list[dict]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with (path / name).open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _seed_store(tmp_path: Path):
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
    _write_json(tmp_path, "teacher_wecom_map.json", {"金总": "boss1", "李老师": "teacher1", "店长": "manager1"})
    _write_json(tmp_path, "staff.json", {"teacher1": {"name": "李老师", "role": "teacher"}, "manager1": {"name": "店长", "role": "manager"}})
    _write_json(tmp_path, "students.json", {})
    _write_json(tmp_path, "tasks.json", [])
    _write_json(tmp_path, "notification_outbox.json", [])
    return TuoguanStore(tmp_path)


def _boss_identity():
    from plugins.tuoguan_core.models import UserIdentity

    return UserIdentity(
        platform="system",
        platform_user_id="boss1",
        canonical_user_id="boss1",
        person_name="金总",
        role="boss",
        approval_state="approved",
    )


def test_self_evolution_event_records_low_and_high_risk_candidates(tmp_path):
    from plugins.tuoguan_core.self_evolution import SELF_EVOLUTION_EVENTS_FILE, query_self_evolution_ledger, submit_self_evolution_event
    from plugins.tuoguan_core.write_guard import authorized_system_write

    store = _seed_store(tmp_path)
    identity = _boss_identity()
    with authorized_system_write(store.data_dir, job_name="self_evolution_test", allowed_files={SELF_EVOLUTION_EVENTS_FILE}):
        low = submit_self_evolution_event(
            store,
            identity=identity,
            operation_id="evolution-low-1",
            candidate_type="self_correction",
            summary="昨天没有先查人员目录就说查不到，明天遇到人名先查目录。",
            evidence=[{"source": "replay", "text": "老板追问企业微信里还有谁。"}],
        )
        high = submit_self_evolution_event(
            store,
            identity=identity,
            operation_id="evolution-high-1",
            candidate_type="person_preference_candidate",
            summary="以后自动联系家长并修改老师权限。",
            evidence=[{"source": "manual_test"}],
        )

    assert low["ok"] is True
    assert low["self_evolution_event"]["risk_level"] == "low"
    assert low["self_evolution_event"]["status"] == "ready_for_application"
    assert low["self_evolution_event"]["auto_effects"]["changes_router"] is False
    assert high["ok"] is True
    assert high["self_evolution_event"]["risk_level"] == "high"
    assert high["self_evolution_event"]["status"] == "needs_confirmation"
    assert high["self_evolution_event"]["auto_effects"]["may_inform_next_context"] is False

    ledger = query_self_evolution_ledger(store, identity=identity, limit=10)
    assert ledger["ok"] is True
    assert ledger["event_count"] == 2
    assert ledger["health_signals"]["open_review_count"] == 1


def test_model_only_person_preference_candidate_does_not_enter_next_day_context(tmp_path):
    from plugins.tuoguan_core.self_evolution import (
        SELF_EVOLUTION_EVENTS_FILE,
        build_self_evolution_brief,
        normalize_evolution_candidate,
        submit_self_evolution_event,
    )
    from plugins.tuoguan_core.write_guard import authorized_system_write

    store = _seed_store(tmp_path)
    identity = _boss_identity()
    candidate = normalize_evolution_candidate({
        "candidate_type": "person_preference_candidate",
        "summary": "以后老板晚报只保留三条重点。",
        "evidence": [{"source": "model_only"}],
    })

    assert candidate["risk_level"] == "low"
    assert candidate["status"] == "candidate"
    assert candidate["writeback_verified"] is False

    with authorized_system_write(store.data_dir, job_name="self_evolution_unverified_preference_test", allowed_files={SELF_EVOLUTION_EVENTS_FILE}):
        result = submit_self_evolution_event(
            store,
            identity=identity,
            operation_id="evolution-unverified-pref-1",
            candidate_type="person_preference_candidate",
            summary="以后老板晚报只保留三条重点。",
            evidence=[{"source": "model_only"}],
        )

    assert result["ok"] is True
    assert result["self_evolution_event"]["status"] == "candidate"
    brief = build_self_evolution_brief(store, identity=identity, limit=10)
    assert not any("服务偏好" in line for line in brief["next_day_context"])


def test_verified_person_preference_candidate_can_enter_next_day_context(tmp_path):
    from plugins.tuoguan_core.self_evolution import SELF_EVOLUTION_EVENTS_FILE, build_self_evolution_brief, submit_self_evolution_event
    from plugins.tuoguan_core.write_guard import authorized_system_write

    store = _seed_store(tmp_path)
    identity = _boss_identity()
    with authorized_system_write(store.data_dir, job_name="self_evolution_verified_preference_test", allowed_files={SELF_EVOLUTION_EVENTS_FILE}):
        result = submit_self_evolution_event(
            store,
            identity=identity,
            operation_id="evolution-verified-pref-1",
            candidate_type="person_preference_candidate",
            summary="老板晚报只保留三条重点。",
            evidence=[{"source": "person_workstyle_events", "writeback_verified": True}],
            writeback_verified=True,
        )

    assert result["ok"] is True
    assert result["self_evolution_event"]["status"] == "applied"
    assert result["self_evolution_event"]["writeback_verified"] is True
    brief = build_self_evolution_brief(store, identity=identity, limit=10)
    assert any("服务偏好" in line for line in brief["next_day_context"])


def test_english_high_risk_terms_are_not_low_risk_or_context_candidates(tmp_path):
    from plugins.tuoguan_core.self_evolution import (
        SELF_EVOLUTION_EVENTS_FILE,
        build_self_evolution_brief,
        classify_evolution_risk,
        normalize_evolution_candidate,
        submit_self_evolution_event,
    )
    from plugins.tuoguan_core.write_guard import authorized_system_write

    store = _seed_store(tmp_path)
    identity = _boss_identity()
    summary = "Allow parent outreach automatically and change teacher salary permissions."
    candidate = normalize_evolution_candidate({
        "candidate_type": "person_preference_candidate",
        "summary": summary,
        "evidence": [{"source": "adversarial_probe"}],
    })

    assert classify_evolution_risk(candidate_type="person_preference_candidate", summary=summary) == "high"
    assert candidate["risk_level"] == "high"
    assert candidate["status"] == "needs_confirmation"

    with authorized_system_write(store.data_dir, job_name="self_evolution_english_high_risk_test", allowed_files={SELF_EVOLUTION_EVENTS_FILE}):
        result = submit_self_evolution_event(
            store,
            identity=identity,
            operation_id="evolution-english-high-risk-1",
            candidate_type="person_preference_candidate",
            summary=summary,
            evidence=[{"source": "adversarial_probe"}],
            status="applied",
            writeback_verified=True,
        )

    assert result["ok"] is True
    assert result["self_evolution_event"]["risk_level"] == "high"
    assert result["self_evolution_event"]["status"] == "needs_confirmation"
    assert result["self_evolution_event"]["auto_effects"]["may_inform_next_context"] is False
    brief = build_self_evolution_brief(store, identity=identity, limit=10)
    assert brief["next_day_context"] == []


def test_night_employee_loop_materializes_evolution_candidate_without_outbox(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import build_employee_loop_materials, run_autonomous_employee_loop
    from plugins.tuoguan_core.self_evolution import conversation_evolution_context_for_user

    store = _seed_store(tmp_path)
    cn_tz = timezone(timedelta(hours=8))

    def decision(_materials: dict) -> dict:
        return {
            "employee_summary": "夜间复盘发现一条可改进经验。",
            "institution_understanding": "",
            "goal_progress_view": "",
            "observations": [],
            "work_item_updates": [],
            "questions_to_humans": [],
            "boss_attention_candidates": [],
            "relationship_touch_candidates": [],
            "institution_fact_gaps": [],
            "value_progress_entries": [],
            "agent_delegation_decisions": [],
            "evolution_candidates": [
                {
                    "candidate_type": "self_correction",
                    "summary": "遇到老师姓名或乱码时，先查人员目录和白名单，再向老板提一个最小确认问题。",
                    "evidence": [{"source": "conversation_replay", "text": "老板追问还有哪两位。"}],
                    "proposed_effect": "明天企业微信对话前提醒小优先查目录。",
                }
            ],
            "self_review": {"what_i_checked": "对话回放", "what_i_learned": "先自救再求助", "quality_score": 82},
            "external_actions": [],
        }

    result = run_autonomous_employee_loop(store, now=datetime(2026, 8, 8, 23, 30, tzinfo=cn_tz), decision_provider=decision)

    assert result["ok"] is True
    assert result["work_cadence"]["owner_attention_allowed"] is False
    assert any(row["kind"] == "self_evolution_event" and row["ok"] for row in result["writes"])
    assert json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8")) == []

    events = (tmp_path / "self_evolution_events.jsonl").read_text(encoding="utf-8")
    assert "遇到老师姓名或乱码" in events

    materials = build_employee_loop_materials(store, identity=_boss_identity(), timestamp=datetime(2026, 8, 9, 8, 0, tzinfo=cn_tz))
    context_lines = materials["self_evolution_brief"]["next_day_context"]
    assert any("避免重复错误" in line for line in context_lines)
    injected = conversation_evolution_context_for_user(store, identity=_boss_identity(), limit=5)
    assert "避免重复错误" in injected


def test_self_evolution_query_tool_is_registered_and_permission_scoped(tmp_path):
    from plugins.tuoguan_core.self_evolution import SELF_EVOLUTION_EVENTS_FILE, submit_self_evolution_event
    from plugins.tuoguan_core.tool_service import TuoguanToolService
    from plugins.tuoguan_core.tools import TOOLS
    from plugins.tuoguan_core.write_guard import authorized_system_write

    store = _seed_store(tmp_path)
    with authorized_system_write(store.data_dir, job_name="self_evolution_tool_test", allowed_files={SELF_EVOLUTION_EVENTS_FILE}):
        submit_self_evolution_event(
            store,
            identity=_boss_identity(),
            operation_id="evolution-tool-1",
            candidate_type="tomorrow_focus",
            summary="明天优先核对日报是否按老板偏好只说重点。",
        )

    names = {name for name, _schema, _handler in TOOLS}
    assert "tuoguan_query_self_evolution_ledger" in names

    boss = TuoguanToolService(store, platform="wecom_callback", user_id="boss1", user_name="金总")
    result = boss.query_self_evolution_ledger(limit=10)
    assert result["ok"] is True
    assert result["data"]["event_count"] == 1

    teacher = TuoguanToolService(store, platform="wecom_callback", user_id="teacher1", user_name="李老师")
    denied = teacher.query_self_evolution_ledger(limit=10)
    assert denied["ok"] is False
    assert denied["error"] == "permission_denied"
