from __future__ import annotations

import asyncio
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
    _write_json(tmp_path, "wecom_whitelist.json", {"super_users": ["boss1"], "user_roles": {"boss1": "boss"}})
    _write_json(tmp_path, "teacher_wecom_map.json", {"金总": "boss1"})
    _write_json(tmp_path, "notification_outbox.json", [])
    _write_json(tmp_path, "students.json", {})
    _write_json(tmp_path, "tasks.json", [])
    _append_jsonl(
        tmp_path,
        "hermes_work_items.jsonl",
        [
            {
                "record_type": "work_item",
                "work_item_id": "work-goal-1",
                "tenant_id": "youyi_tuoguan",
                "focus_key": "goal:sept_renewal",
                "title": "目标推进：九月份续费率更稳",
                "status": "waiting",
                "focus_summary": "续费目标历史分析与新学期准备。",
                "current_waiting": {"reason": "等待老板确认优先抓哪类未续原因"},
                "next_actions": ["整理历史未续原因和候选沟通材料"],
                "next_attention_at": "2026-07-31T09:00:00+08:00",
                "created_at": "2026-07-30T08:00:00+08:00",
                "updated_at": "2026-07-30T08:00:00+08:00",
                "source": {"actor_user_id": "boss1", "actor_role": "boss"},
                "auto_effects": {"forces_next_action": False, "changes_router": False},
            }
        ],
    )
    return TuoguanStore(tmp_path)


def test_morning_report_queues_boss_only_outbox_item(tmp_path):
    from plugins.tuoguan_core.daily_reporter import queue_daily_boss_report

    store = _seed_store(tmp_path)
    cn_tz = timezone(timedelta(hours=8))
    result = queue_daily_boss_report("morning", store=store, now=datetime(2026, 7, 30, 8, 30, tzinfo=cn_tz))

    assert result["ok"] is True
    assert result["queued"] is True
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert len(outbox) == 1
    item = outbox[0]
    assert item["notification_type"] == "autonomous_daily_report"
    assert item["touser"] == "boss1"
    assert item["action"] == "daily_morning_report"
    assert "早上好" in item["content"]
    assert "今天重点" in item["content"]
    assert "目标进展" in item["content"]
    assert "细节我已留档，需要我展开哪一项你直接说。" in item["content"]
    assert "推进边界" not in item["content"]
    assert "材料来源" not in item["content"]
    assert len(item["content"]) <= 700
    assert item["auto_effects"]["sends_teacher_messages"] is False
    assert item["auto_effects"]["sends_parent_messages"] is False
    assert item["auto_effects"]["forces_next_action"] is False
    assert (tmp_path / "daily_report_runs.jsonl").exists()


def test_daily_report_is_idempotent_for_same_day_and_kind(tmp_path):
    from plugins.tuoguan_core.daily_reporter import queue_daily_boss_report

    store = _seed_store(tmp_path)
    cn_tz = timezone(timedelta(hours=8))
    first = queue_daily_boss_report("evening", store=store, now=datetime(2026, 7, 30, 21, 0, tzinfo=cn_tz))
    second = queue_daily_boss_report("evening", store=store, now=datetime(2026, 7, 30, 21, 5, tzinfo=cn_tz))

    assert first["queued"] is True
    assert second["queued"] is False
    assert second["idempotent_replay"] is True
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert len(outbox) == 1


def test_evening_report_refuses_off_schedule_delivery(tmp_path):
    from plugins.tuoguan_core.daily_reporter import queue_daily_boss_report

    store = _seed_store(tmp_path)
    cn_tz = timezone(timedelta(hours=8))
    result = queue_daily_boss_report("evening", store=store, now=datetime(2026, 8, 12, 12, 45, tzinfo=cn_tz))

    assert result["ok"] is False
    assert result["error"] == "daily_report_outside_delivery_window"
    assert json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8")) == []


def test_scheduled_report_recovers_same_day_off_schedule_delivery(tmp_path):
    from plugins.tuoguan_core.daily_reporter import queue_daily_boss_report

    store = _seed_store(tmp_path)
    cn_tz = timezone(timedelta(hours=8))
    notification_id = "autonomous_daily_report:20260812:evening"
    _write_json(
        tmp_path,
        "notification_outbox.json",
        [
            {
                "id": notification_id,
                "status": "sent",
                "notification_type": "autonomous_daily_report",
                "report_kind": "evening",
                "touser": "boss1",
                "created_at": "2026-08-12T12:45:00+08:00",
                "sent_at": "2026-08-12T12:45:05+08:00",
            }
        ],
    )

    result = queue_daily_boss_report("evening", store=store, now=datetime(2026, 8, 12, 21, 0, tzinfo=cn_tz))

    assert result["ok"] is True
    assert result["queued"] is True
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert len(outbox) == 1
    assert outbox[0]["id"] == notification_id
    assert outbox[0]["status"] == "pending"
    assert outbox[0]["requeued_after_off_schedule_delivery"] is True


def test_dry_run_does_not_write_outbox(tmp_path):
    from plugins.tuoguan_core.daily_reporter import queue_daily_boss_report

    store = _seed_store(tmp_path)
    cn_tz = timezone(timedelta(hours=8))
    result = queue_daily_boss_report("morning", store=store, now=datetime(2026, 7, 30, 8, 30, tzinfo=cn_tz), dry_run=True)

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8")) == []
    assert not (tmp_path / "daily_report_runs.jsonl").exists()


def test_evening_report_does_not_claim_waiting_as_completion(tmp_path):
    from plugins.tuoguan_core.daily_reporter import build_daily_boss_report

    store = _seed_store(tmp_path)
    cn_tz = timezone(timedelta(hours=8))
    result = build_daily_boss_report("evening", store=store, now=datetime(2026, 7, 30, 21, 0, tzinfo=cn_tz))

    assert result["ok"] is True
    assert "今晚工作重点" in result["content"]
    assert "目标进展" in result["content"]
    assert "等待老板确认" in result["content"]
    assert "没有把等待状态写成完成" in result["content"] or "等待" in result["content"]
    assert "细节我已留档，需要我展开哪一项你直接说。" in result["content"]
    assert "边界确认" not in result["content"]
    assert "材料来源" not in result["content"]
    assert len(result["content"]) <= 700
    assert result["auto_effects"]["changes_salary"] is False


def test_owner_daily_report_applies_saved_concise_workstyle(tmp_path):
    from plugins.tuoguan_core.daily_reporter import build_daily_boss_report
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.workstyle_profiles import submit_person_workstyle_preference
    from plugins.tuoguan_core.write_guard import authorized_system_write

    store = _seed_store(tmp_path)
    identity = UserIdentity(
        platform="wecom",
        platform_user_id="boss1",
        canonical_user_id="boss1",
        person_name="金总",
        role="boss",
        approval_state="approved",
    )
    with authorized_system_write(
        store.data_dir,
        job_name="test_owner_workstyle",
        allowed_files={"person_workstyle_events.jsonl"},
    ):
        saved = submit_person_workstyle_preference(
            store,
            identity=identity,
            preference_type="report_length",
            scope="daily_report",
            preference_text="以后日报只保留三条重点。",
            normalized_rule="日报只保留三条重点。",
            operation_id="daily-style-test-1",
        )

    assert saved["ok"] is True
    cn_tz = timezone(timedelta(hours=8))
    result = build_daily_boss_report("evening", store=store, now=datetime(2026, 7, 30, 21, 0, tzinfo=cn_tz))

    numbered_lines = [line for line in result["content"].splitlines() if line[:2] in {"1.", "2.", "3.", "4.", "5."}]
    assert len(numbered_lines) <= 3
    assert len(result["content"]) <= 520


def test_queued_daily_report_records_workstyle_and_evolution_application_evidence(tmp_path):
    from plugins.tuoguan_core.daily_reporter import queue_daily_boss_report
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.self_evolution import SELF_EVOLUTION_EVENTS_FILE, submit_self_evolution_event
    from plugins.tuoguan_core.workstyle_profiles import submit_person_workstyle_preference
    from plugins.tuoguan_core.write_guard import authorized_system_write

    store = _seed_store(tmp_path)
    identity = UserIdentity("wecom", "boss1", "boss1", "金总", "boss", "approved")
    with authorized_system_write(
        store.data_dir,
        job_name="test_daily_report_application_evidence",
        allowed_files={"person_workstyle_events.jsonl", SELF_EVOLUTION_EVENTS_FILE},
    ):
        preference = submit_person_workstyle_preference(
            store,
            identity=identity,
            preference_type="report_length",
            scope="daily_report",
            preference_text="以后日报只保留三条重点。",
            normalized_rule="日报只保留三条重点。",
            operation_id="daily-application-pref",
        )
        evolution = submit_self_evolution_event(
            store,
            identity=identity,
            operation_id="daily-application-evolution",
            candidate_type="self_correction",
            summary="老板日报只保留三条重点。",
            evidence=[{"source": "owner_feedback", "text": "老板要求日报只说重点。"}],
            target_store="person_workstyle_events.jsonl",
            applies_to_user_id="boss1",
            applies_to_scope="daily_report",
        )
    assert preference["ok"] is True
    assert evolution["ok"] is True

    queued = queue_daily_boss_report(
        "morning",
        store=store,
        now=datetime(2026, 8, 13, 8, 30, tzinfo=timezone(timedelta(hours=8))),
    )

    assert queued["queued"] is True
    workstyle_rows = [json.loads(line) for line in (tmp_path / "person_workstyle_events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(row.get("record_type") == "person_workstyle_application" for row in workstyle_rows)
    evolution_rows = [json.loads(line) for line in (tmp_path / SELF_EVOLUTION_EVENTS_FILE).read_text(encoding="utf-8").splitlines()]
    assert evolution_rows[-1]["status"] == "verified"
    run = json.loads((tmp_path / "daily_report_runs.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert run["workstyle_application_verified"] is True
    assert run["self_evolution_application"]["verified_count"] == 1


def test_daily_report_keeps_concise_rule_when_spacing_feedback_arrives_later(tmp_path):
    from plugins.tuoguan_core.daily_reporter import build_daily_boss_report
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.workstyle_profiles import daily_report_style_for_owner, submit_person_workstyle_preference
    from plugins.tuoguan_core.write_guard import authorized_system_write

    store = _seed_store(tmp_path)
    identity = UserIdentity("wecom", "boss1", "boss1", "金总", "boss", "approved")
    with authorized_system_write(
        store.data_dir,
        job_name="test_owner_report_format_feedback",
        allowed_files={"person_workstyle_events.jsonl"},
    ):
        first = submit_person_workstyle_preference(
            store,
            identity=identity,
            preference_type="format",
            scope="daily_report",
            preference_text="早晚报精简为3-5行，每条一行。早上：正常/异常+需确认+今日重点。晚上：完成X件+异常有/无+明日计划。不出现内部术语。",
            normalized_rule="早晚报精简为3-5行，每条一行。早上：正常/异常+需确认+今日重点。晚上：完成X件+异常有/无+明日计划。不出现内部术语。",
            operation_id="daily-style-format-1",
        )
        second = submit_person_workstyle_preference(
            store,
            identity=identity,
            preference_type="format",
            scope="daily_report",
            preference_text="汇报要段落分明、每条之间留空行，文字不要拥挤堆叠在一起。",
            normalized_rule="汇报要段落分明、每条之间留空行，文字不要拥挤堆叠在一起。",
            operation_id="daily-style-format-2",
        )

    assert first["ok"] is True
    assert second["ok"] is True
    assert first["preference"]["preference_id"] not in second["preference"]["supersedes"]
    assert first["preference"]["dimension_key"] == "length"
    assert second["preference"]["dimension_key"] == "layout"

    style = daily_report_style_for_owner(store, "boss1")
    assert style["report_length"] == "ultra_concise"
    assert style["layout"] == "spaced_sections"
    assert len(style["active_preferences"]) == 2
    assert len(style["applied_preferences"]) == 2

    cn_tz = timezone(timedelta(hours=8))
    report = build_daily_boss_report("morning", store=store, now=datetime(2026, 8, 10, 8, 30, tzinfo=cn_tz))

    assert report["ok"] is True
    assert "状态：" in report["content"]
    assert "需确认：" in report["content"]
    assert "今日重点：" in report["content"]
    assert "\n\n需确认：" in report["content"]
    assert "[{" not in report["content"]
    assert "goal_id" not in report["content"]
    assert "status waiting" not in report["content"]
    assert "状态 waiting" not in report["content"]
    assert "2026-08-" not in report["content"]
    assert "等待 " not in report["content"]
    assert len(report["content"]) <= 520


def test_daily_report_applies_next_day_self_evolution_context(tmp_path):
    from plugins.tuoguan_core.daily_reporter import build_daily_boss_report
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.self_evolution import SELF_EVOLUTION_EVENTS_FILE, submit_self_evolution_event
    from plugins.tuoguan_core.write_guard import authorized_system_write

    store = _seed_store(tmp_path)
    identity = UserIdentity("system", "boss1", "boss1", "金总", "boss", "approved")
    with authorized_system_write(
        store.data_dir,
        job_name="test_daily_report_self_evolution",
        allowed_files={SELF_EVOLUTION_EVENTS_FILE},
    ):
        saved = submit_self_evolution_event(
            store,
            identity=identity,
            operation_id="daily-report-evolution-1",
            candidate_type="self_correction",
                summary="昨天老板嫌晚报太长，今天日报只放重点和异常，不展开过程。",
                evidence=[{"source": "owner_feedback", "text": "老板要求日报只说重点。"}],
                applies_to_role="boss",
                applies_to_scope="daily_report",
                occurred_at="2026-08-08T23:00:00+08:00",
            )

    assert saved["ok"] is True
    cn_tz = timezone(timedelta(hours=8))
    report = build_daily_boss_report("morning", store=store, now=datetime(2026, 8, 9, 8, 0, tzinfo=cn_tz))

    assert report["ok"] is True
    assert report["source_counts"]["self_evolution_next_day_count"] == 1
    assert "今天带入" in report["content"]
    assert "避免重复错误" in report["content"]
    assert "日报只放重点" in report["content"]


def test_morning_report_does_not_relabel_yesterday_relative_time_as_today(tmp_path):
    from plugins.tuoguan_core.daily_reporter import build_daily_boss_report

    store = _seed_store(tmp_path)
    _append_jsonl(
        tmp_path,
        "self_evolution_events.jsonl",
        [
            {
                "record_type": "self_evolution_event",
                "evolution_event_id": "relative-time-report",
                "semantic_fingerprint": "relative-time-report",
                "tenant_id": "youyi_tuoguan",
                "candidate_type": "self_correction",
                    "summary": "今日21:01老板反问后，应主动追问具体缺口。",
                    "evidence": [{"source": "conversation_replay", "text": "老板21:01反问当前事项。"}],
                "risk_level": "low",
                "status": "ready_for_application",
                "applies_to_role": "boss",
                "applies_to_scope": "daily_report",
                "created_at": "2026-08-12T23:10:00+08:00",
            }
        ],
    )

    report = build_daily_boss_report(
        "morning",
        store=store,
        now=datetime(2026, 8, 13, 8, 30, tzinfo=timezone(timedelta(hours=8))),
    )

    assert report["ok"] is True
    assert "今日21:01" not in report["content"]
    assert "8月12日21:01" in report["content"]


def test_ultra_report_limit_keeps_complete_sentence():
    from plugins.tuoguan_core.daily_reporter import _first_safe_report_text

    result = _first_safe_report_text(
        [
            "今天带入：避免重复错误：老板反问但未提供信息时，应主动追问具体缺口，而非等待。"
            "8月11日21:01老板反问，但主工作项仍停滞，下一次应主动明确提问。"
        ],
        "无新增重点。",
        limit=58,
    )

    assert result == "今天带入：避免重复错误：老板反问但未提供信息时，应主动追问具体缺口，而非等待。"
    assert "…" not in result


def test_daily_report_does_not_surface_stale_owner_attention_as_today_focus(tmp_path):
    from plugins.tuoguan_core.daily_reporter import build_daily_boss_report

    store = _seed_store(tmp_path)
    _append_jsonl(
        tmp_path,
        "attention_threads.jsonl",
        [
            {
                "record_type": "attention_thread",
                "attention_id": "attention_old_li",
                "focus_key": "report:xiaojin_parent_comm_20260806",
                "question_text": "昨晚您说要明天10点汇报李老师沟通结果，需要确认吗？",
                "status": "queued",
                "target_user_id": "boss1",
                "created_at": "2026-08-06T21:30:00+08:00",
                "updated_at": "2026-08-06T21:30:00+08:00",
            },
            {
                "record_type": "attention_thread",
                "attention_id": "attention_recent",
                "focus_key": "market:today",
                "question_text": "今天新采集到一条本地市场观察，是否需要展开？",
                "status": "queued",
                "target_user_id": "boss1",
                "created_at": "2026-08-11T07:30:00+08:00",
                "updated_at": "2026-08-11T07:30:00+08:00",
            },
        ],
    )
    cn_tz = timezone(timedelta(hours=8))
    report = build_daily_boss_report("morning", store=store, now=datetime(2026, 8, 11, 8, 30, tzinfo=cn_tz))

    assert report["ok"] is True
    assert "昨晚" not in report["content"]
    assert "李老师沟通结果" not in report["content"]
    assert "明天10点" not in report["content"]
    assert "本地市场观察" in report["content"]
    assert report["source_counts"]["open_attention_count"] == 1
    assert len(report["content"]) <= 700


def test_daily_report_does_not_render_empty_json_detail(tmp_path):
    from plugins.tuoguan_core.daily_reporter import build_daily_boss_report

    store = _seed_store(tmp_path)
    _append_jsonl(
        tmp_path,
        "hermes_work_items.jsonl",
        [
            {
                "record_type": "work_item",
                "work_item_id": "work-empty-json",
                "tenant_id": "youyi_tuoguan",
                "focus_key": "goal:empty_json",
                "title": "已合并到主工作项，不再独立推进。",
                "status": "active",
                "current_phase": {},
                "current_waiting": {},
                "next_actions": [{}],
                "created_at": "2026-08-12T08:00:00+08:00",
                "updated_at": "2026-08-12T08:00:00+08:00",
            }
        ],
    )
    cn_tz = timezone(timedelta(hours=8))
    report = build_daily_boss_report("morning", store=store, now=datetime(2026, 8, 12, 8, 30, tzinfo=cn_tz))

    assert report["ok"] is True
    assert "{}" not in report["content"]
    assert "今天先看 。" not in report["content"]


def test_daily_report_excludes_old_active_item_from_today_focus(tmp_path):
    from plugins.tuoguan_core.daily_reporter import build_daily_boss_report

    store = _seed_store(tmp_path)
    _append_jsonl(
        tmp_path,
        "hermes_work_items.jsonl",
        [
            {
                "record_type": "work_item",
                "work_item_id": "work-old-active",
                "tenant_id": "youyi_tuoguan",
                "focus_key": "historical:old_active",
                "title": "五天前的待确认事项",
                "focus_summary": "五天前曾经需要确认，当前没有新证据。",
                "status": "active",
                "created_at": "2026-08-07T08:00:00+08:00",
                "updated_at": "2026-08-07T08:00:00+08:00",
            }
        ],
    )

    report = build_daily_boss_report(
        "morning",
        store=store,
        now=datetime(2026, 8, 13, 8, 30, tzinfo=timezone(timedelta(hours=8))),
    )

    assert report["ok"] is True
    assert "五天前的待确认事项" not in report["content"]
    assert "五天前曾经需要确认" not in report["content"]
    assert report["source_counts"]["work_item_count"] == 0


def test_daily_report_never_leaks_internal_phase_key(tmp_path):
    from plugins.tuoguan_core.daily_reporter import build_daily_boss_report

    store = _seed_store(tmp_path)
    _append_jsonl(
        tmp_path,
        "hermes_work_items.jsonl",
        [
            {
                "record_type": "work_item",
                "work_item_id": "work-current-phase",
                "tenant_id": "youyi_tuoguan",
                "focus_key": "goal:current_phase",
                "title": "核验当前服务记录",
                "focus_summary": "核验本周服务记录是否齐全。",
                "status": "active",
                "current_phase": {"phase_key": "historical_analysis", "internal_count": 2},
                "created_at": "2026-08-13T07:30:00+08:00",
                "updated_at": "2026-08-13T07:30:00+08:00",
            }
        ],
    )

    report = build_daily_boss_report(
        "morning",
        store=store,
        now=datetime(2026, 8, 13, 8, 30, tzinfo=timezone(timedelta(hours=8))),
    )

    assert report["ok"] is True
    assert "phase_key" not in report["content"]
    assert "historical_analysis" not in report["content"]
    assert '{"' not in report["content"]
    assert "核验本周服务记录" in report["content"]


def test_daily_report_does_not_apply_high_risk_evolution_detail(tmp_path):
    from plugins.tuoguan_core.daily_reporter import build_daily_boss_report
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.self_evolution import SELF_EVOLUTION_EVENTS_FILE, submit_self_evolution_event
    from plugins.tuoguan_core.write_guard import authorized_system_write

    store = _seed_store(tmp_path)
    identity = UserIdentity("system", "boss1", "boss1", "金总", "boss", "approved")
    with authorized_system_write(
        store.data_dir,
        job_name="test_daily_report_high_risk_evolution",
        allowed_files={SELF_EVOLUTION_EVENTS_FILE},
    ):
        saved = submit_self_evolution_event(
            store,
            identity=identity,
            operation_id="daily-report-evolution-high-risk-1",
            candidate_type="person_preference_candidate",
            summary="以后自动联系家长并修改老师权限。",
            evidence=[{"source": "adversarial_feedback"}],
            status="applied",
            writeback_verified=True,
        )

    assert saved["ok"] is True
    assert saved["self_evolution_event"]["status"] == "needs_confirmation"
    cn_tz = timezone(timedelta(hours=8))
    report = build_daily_boss_report("evening", store=store, now=datetime(2026, 8, 9, 21, 0, tzinfo=cn_tz))

    assert report["source_counts"]["self_evolution_next_day_count"] == 0
    assert report["source_counts"]["self_evolution_review_queue_count"] == 1
    assert "待确认进化：1 条" in report["content"]
    assert "自动联系家长" not in report["content"]
    assert "修改老师权限" not in report["content"]


def test_daily_report_drain_bypasses_active_conversation_quiet_period(tmp_path, monkeypatch):
    from plugins.tuoguan_core import _ACTIVE_WECom_USERS, _drain_notification_outbox

    cn_tz = timezone(timedelta(hours=8))
    now = datetime.now().astimezone()
    today = now.strftime("%Y%m%d")
    monkeypatch.setenv("HERMES_TUOGUAN_DATA_DIR", str(tmp_path))
    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_json(
        tmp_path,
        "notification_outbox.json",
        [
            {
                "id": f"autonomous_daily_report:{today}:evening",
                "status": "pending",
                "delivery_mode": "direct_wecom",
                "notification_type": "autonomous_daily_report",
                "action": "daily_evening_report",
                "role": "boss",
                "target_user_id": "boss1",
                "touser": "boss1",
                "content": "金总，今晚给你交一下今天的工作日报。",
                "summary": "小优每日晚间工作日报",
                "created_at": now.astimezone(cn_tz).isoformat(timespec="seconds"),
                "attempt_count": 0,
            }
        ],
    )
    _ACTIVE_WECom_USERS["boss1"] = now

    class Result:
        success = True
        message_id = "msg_daily_1"
        error = ""

    class Adapter:
        async def send(self, target, content, metadata=None):
            assert target == "boss1"
            assert metadata["idempotency_key"] == f"autonomous_daily_report:{today}:evening"
            return Result()

    try:
        asyncio.run(_drain_notification_outbox(Adapter()))
    finally:
        _ACTIVE_WECom_USERS.pop("boss1", None)

    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert outbox[0]["status"] == "sent"
    assert outbox[0]["message_id"] == "msg_daily_1"


def test_non_daily_drain_tolerates_naive_active_conversation_timestamp(tmp_path, monkeypatch):
    from plugins.tuoguan_core import _ACTIVE_WECom_USERS, _drain_notification_outbox

    monkeypatch.setenv("HERMES_TUOGUAN_DATA_DIR", str(tmp_path))
    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_json(
        tmp_path,
        "notification_outbox.json",
        [
            {
                "id": "task_reminder_1",
                "status": "pending",
                "delivery_mode": "direct_wecom",
                "notification_type": "task_notification",
                "action": "task_due",
                "target_user_id": "teacher1",
                "touser": "teacher1",
                "content": "李老师，这条任务需要回执。",
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "attempt_count": 0,
            }
        ],
    )
    _ACTIVE_WECom_USERS["teacher1"] = datetime.now()

    class Adapter:
        async def send(self, target, content, metadata=None):
            raise AssertionError("active teacher conversation should keep non-daily reminder pending")

    try:
        asyncio.run(_drain_notification_outbox(Adapter()))
    finally:
        _ACTIVE_WECom_USERS.pop("teacher1", None)

    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert outbox[0]["status"] == "pending"


def test_stale_daily_report_becomes_visible_failure_not_suppressed(tmp_path, monkeypatch):
    from plugins.tuoguan_core import _drain_notification_outbox

    monkeypatch.setenv("HERMES_TUOGUAN_DATA_DIR", str(tmp_path))
    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_json(
        tmp_path,
        "notification_outbox.json",
        [
            {
                "id": "autonomous_daily_report:20260808:morning",
                "status": "pending",
                "delivery_mode": "direct_wecom",
                "notification_type": "autonomous_daily_report",
                "action": "daily_morning_report",
                "role": "boss",
                "target_user_id": "boss1",
                "touser": "boss1",
                "content": "金总，早上好，这是今天的自主工作安排。",
                "summary": "小优每日早间工作安排",
                "created_at": "2026-08-08T08:30:00+08:00",
                "attempt_count": 0,
            }
        ],
    )

    class Adapter:
        async def send(self, target, content, metadata=None):
            raise AssertionError("stale daily reports should not be backfilled")

    asyncio.run(_drain_notification_outbox(Adapter()))

    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert outbox[0]["status"] == "failed"
    assert outbox[0]["last_error"] == "daily_report_delivery_window_missed_after_outbox_block"
