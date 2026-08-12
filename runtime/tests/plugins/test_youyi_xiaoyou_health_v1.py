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
            "allowed_users": ["manager1", "teacher1"],
            "user_roles": {"boss1": "boss", "manager1": "manager", "teacher1": "teacher"},
        },
    )
    _write_json(tmp_path, "teacher_wecom_map.json", {"金总": "boss1", "店长": "manager1", "李老师": "teacher1"})
    _write_json(tmp_path, "staff.json", {"manager1": {"name": "店长", "role": "manager"}, "teacher1": {"name": "李老师", "role": "teacher"}})
    _write_json(
        tmp_path,
        "notification_outbox.json",
        [
            {
                "id": "autonomous_daily_report:20260809:morning",
                "notification_type": "autonomous_daily_report",
                "status": "sent",
                "sent_at": "2026-08-09T08:02:00+08:00",
                "created_at": "2026-08-09T08:00:00+08:00",
            },
            {
                "id": "task1:teacher:task_due",
                "status": "pending",
                "action": "task_due",
                "task_id": "task1",
                "touser": "teacher1",
                "created_at": "2026-08-09T09:00:00+08:00",
            },
            {
                "id": "task1:teacher:task_due:dup",
                "status": "pending",
                "action": "task_due",
                "task_id": "task1",
                "touser": "teacher1",
                "created_at": "2026-08-09T09:05:00+08:00",
            },
            {
                "id": "owner_attention:failed",
                "status": "result_unknown",
                "notification_type": "autonomous_owner_attention",
                "created_at": "2026-08-09T10:00:00+08:00",
            },
        ],
    )
    _append_jsonl(
        tmp_path,
        "daily_report_runs.jsonl",
        [
            {
                "record_type": "daily_report_delivery_status",
                "report_kind": "morning",
                "notification_id": "autonomous_daily_report:20260809:morning",
                "delivery_status": "sent",
                "observed_at": "2026-08-09T08:02:05+08:00",
            }
        ],
    )
    _append_jsonl(
        tmp_path,
        "attention_threads.jsonl",
        [
            {
                "attention_id": "att1",
                "status": "sent",
                "target_user_id": "boss1",
                "question_text": "请确认日报是否还需要更短。",
                "created_at": "2026-08-09T08:30:00+08:00",
            }
        ],
    )
    _append_jsonl(
        tmp_path,
        "institution_fact_gap_events.jsonl",
        [
            {
                "gap_event_id": "gap1",
                "tenant_id": "youyi_tuoguan",
                "gap_key": "teacher_record_habit",
                "gap_text": "缺少李老师记录作业完成情况的习惯。",
                "ask_role": "teacher",
                "urgency": "normal",
                "created_at": "2026-08-09T09:30:00+08:00",
            }
        ],
    )
    _append_jsonl(
        tmp_path,
        "self_evolution_events.jsonl",
        [
            {
                "record_type": "self_evolution_event",
                "evolution_event_id": "evo1",
                "tenant_id": "youyi_tuoguan",
                "candidate_type": "self_correction",
                "summary": "日报只说重点。",
                "risk_level": "low",
                "status": "ready_for_application",
                "created_at": "2026-08-09T01:00:00+08:00",
            },
            {
                "record_type": "self_evolution_event",
                "evolution_event_id": "evo2",
                "tenant_id": "youyi_tuoguan",
                "candidate_type": "tool_failure_or_bug",
                "summary": "人员问题未先查目录。",
                "risk_level": "medium",
                "status": "pending_review",
                "created_at": "2026-08-09T01:10:00+08:00",
            },
            {
                "record_type": "self_evolution_event",
                "evolution_event_id": "evo3",
                "tenant_id": "youyi_tuoguan",
                "candidate_type": "tool_failure_or_bug",
                "summary": "取消任务工具授权旧失败已经修复。",
                "risk_level": "medium",
                "status": "fixed",
                "created_at": "2026-08-09T01:20:00+08:00",
            },
        ],
    )
    _append_jsonl(
        tmp_path,
        "social_market_research_runs.jsonl",
        [
            {
                "run_id": "social-run-1",
                "platform": "douyin",
                "query": "项城托管招生",
                "status": "completed",
                "created_at": "2026-08-09T10:10:00+08:00",
            }
        ],
    )
    _append_jsonl(
        tmp_path,
        "social_market_research_candidates.jsonl",
        [
            {
                "candidate_id": "social-candidate-1",
                "platform": "douyin",
                "query": "项城托管招生",
                "title": "本地同行招生短视频",
                "status": "candidate",
                "collected_at": "2026-08-09T10:12:00+08:00",
            }
        ],
    )
    _append_jsonl(
        tmp_path,
        "hermes_work_items.jsonl",
        [
            {
                "record_type": "work_item",
                "work_item_id": "work1",
                "tenant_id": "youyi_tuoguan",
                "focus_key": "goal:test",
                "title": "目标推进",
                "status": "waiting",
                "focus_summary": "等待事实。",
                "created_at": "2026-08-09T08:10:00+08:00",
            }
        ],
    )
    _append_jsonl(
        tmp_path,
        "staff_voice_signals.jsonl",
        [
            {
                "record_type": "staff_voice_signal",
                "signal_id": "staff_voice_health_1",
                "tenant_id": "youyi_tuoguan",
                "source_role": "teacher",
                "source_user_id": "teacher1",
                "source_name": "李老师",
                "category": "morale_risk",
                "risk_level": "high",
                "status": "open",
                "signal_summary": "老师表达近期安排混乱并影响心情。",
                "impact": "可能影响老师稳定和现场协作。",
                "suggested_owner_action": "建议老板先看排班事实，再决定是否调整沟通方式。",
                "occurred_at": "2026-08-09T10:30:00+08:00",
                "created_at": "2026-08-09T10:30:00+08:00",
            }
        ],
    )
    return TuoguanStore(tmp_path)


def test_xiaoyou_health_summarizes_read_only_operating_signals(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import query_xiaoyou_health
    from plugins.tuoguan_core.models import UserIdentity

    store = _seed_store(tmp_path)
    before_outbox = (tmp_path / "notification_outbox.json").read_text(encoding="utf-8")
    identity = UserIdentity("wecom_callback", "boss1", "boss1", "金总", "boss", "approved")

    result = query_xiaoyou_health(store, identity=identity, now_at="2026-08-09T11:00:00+08:00")

    assert result["ok"] is True
    assert result["read_only"] is True
    assert result["status"] == "attention_needed"
    assert result["daily_reports"]["sent_last_24h"] is True
    assert result["proactive_work"]["outbox"]["failed_or_unknown_count"] == 1
    assert result["proactive_work"]["outbox"]["repeated_task_reminder_candidate_count"] == 1
    assert result["fact_gaps"]["candidate_count"] == 1
    assert result["fact_gaps"]["by_ask_role"]["teacher"] == 1
    assert result["staff_voice"]["available"] is True
    assert result["staff_voice"]["high_or_urgent_open_count"] == 1
    assert result["evolution"]["next_day_context_count"] == 1
    assert result["evolution"]["pending_review_count"] == 1
    assert result["evolution"]["tool_failure_candidate_count"] == 1
    assert result["evolution"]["fixed_or_verified_failure_count"] == 1
    assert result["evolution"]["tool_failure_status_counts"]["fixed"] == 1
    assert result["market_learning"]["run_count_last_24h"] == 1
    assert result["market_learning"]["candidate_count_last_24h"] == 1
    assert result["market_learning"]["latest_status"] == "completed"
    assert result["actions_taken"] == []
    assert result["boundary"]["sends_messages"] is False
    assert (tmp_path / "notification_outbox.json").read_text(encoding="utf-8") == before_outbox


def test_xiaoyou_health_does_not_count_off_schedule_daily_report_as_delivery():
    from plugins.tuoguan_core.digital_employee_state import _xiaoyou_daily_report_health

    rows = [
        {
            "id": "autonomous_daily_report:20260812:evening:deploy_misfire_124550",
            "notification_type": "autonomous_daily_report",
            "status": "sent",
            "sent_at": "2026-08-12T12:45:52+08:00",
        }
    ]
    runs = [
        {
            "record_type": "daily_report_delivery_status",
            "report_kind": "evening",
            "notification_id": "autonomous_daily_report:20260812:evening",
            "delivery_status": "sent",
            "sent_at": "2026-08-12T12:45:52+08:00",
        }
    ]

    health = _xiaoyou_daily_report_health(rows, runs, 0.0)

    assert health["sent_last_24h"] is False
    assert health["sent_count_last_24h"] == 0
    assert health["by_kind"]["evening"]["status"] == "missing"


def test_xiaoyou_health_tool_is_registered_and_permission_scoped(tmp_path):
    from plugins.tuoguan_core.runtime_foundation import MODEL_SELECTED_READ_TOOLS
    from plugins.tuoguan_core.tool_service import TuoguanToolService
    from plugins.tuoguan_core.tools import TOOLS

    store = _seed_store(tmp_path)
    names = {name for name, _schema, _handler in TOOLS}
    assert "tuoguan_query_xiaoyou_health" in names
    assert "tuoguan_query_xiaoyou_health" in MODEL_SELECTED_READ_TOOLS

    boss = TuoguanToolService(store, platform="wecom_callback", user_id="boss1", user_name="金总")
    result = boss.query_xiaoyou_health(now_at="2026-08-09T11:00:00+08:00")
    assert result["ok"] is True
    assert result["data"]["report_type"] == "xiaoyou_health_v1"

    teacher = TuoguanToolService(store, platform="wecom_callback", user_id="teacher1", user_name="李老师")
    denied = teacher.query_xiaoyou_health(now_at="2026-08-09T11:00:00+08:00")
    assert denied["ok"] is False
    assert denied["error"] == "permission_denied"


def test_xiaoyou_health_exposes_real_autonomous_loop_failure(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import query_xiaoyou_health
    from plugins.tuoguan_core.models import UserIdentity

    store = _seed_store(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "autonomous-wakeup-v1-20260809-103000.json").write_text(
        json.dumps(
            {
                "ok": False,
                "status": "degraded",
                "generated_at": "2026-08-09T10:30:00+08:00",
                "failure_stage": "employee_loop_model_decision_failed",
                "failure_message": "all_model_providers_failed:diagnosis",
                "source_counts": {"employee_loop_write_count": 0},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    identity = UserIdentity("wecom_callback", "boss1", "boss1", "金总", "boss", "approved")

    result = query_xiaoyou_health(store, identity=identity, now_at="2026-08-09T11:00:00+08:00")

    loop = result["proactive_work"]["autonomous_loop"]
    assert loop["failed_count_last_24h"] == 1
    assert loop["latest_status"] == "degraded"
    assert loop["latest_failure_stage"] == "employee_loop_model_decision_failed"
    assert any("自主员工循环失败 1 次" in issue for issue in result["issues"])
