from __future__ import annotations

from datetime import datetime
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


def _seed_dashboard_store(tmp_path: Path):
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_json(tmp_path, "teacher_wecom_map.json", {"李老师": "teacher1", "金总": "JinWenJie"})
    _write_json(tmp_path, "wecom_whitelist.json", {"super_users": ["JinWenJie"], "allowed_users": ["teacher1"], "user_roles": {"JinWenJie": "boss", "teacher1": "teacher"}})
    _write_json(tmp_path, "students.json", {"小明": {"teacher": "teacher1", "status": "active", "program_ids": ["regular_tuoguan"]}})
    _write_json(tmp_path, "tasks.json", [])
    _write_json(tmp_path, "task_closure_events.json", [])
    _write_json(tmp_path, "notification_outbox.json", [
        {
            "id": "autonomous_daily_report:20260730:evening",
            "notification_type": "autonomous_daily_report",
            "status": "sent",
            "sent_at": "2026-07-30T21:00:52+08:00",
            "created_at": "2026-07-30T21:00:07+08:00",
        }
    ])
    _write_json(tmp_path, "payroll_rules.json", {"people": {"teacher1": {"name": "李老师", "position": "part_time"}}})
    _write_json(tmp_path, "records.json", [
        {
            "student_name": "小明",
            "teacher": "teacher1",
            "content": "小明今天数学计算进步明显，老师让他先讲思路再做订正，明天继续观察。",
            "created_at": "2026-07-30T10:00:00+08:00",
            "record_evaluation": {"accepted": True, "payroll_eligible": True, "quality_level": "quality", "score": 12},
            "tags": ["数学", "进步"],
        }
    ])
    _append_jsonl(tmp_path, "hermes_work_items.jsonl", [
        {
            "record_type": "work_item",
            "work_item_id": "work1",
            "focus_key": "goal:sept_renewal",
            "title": "目标推进：九月份续费率更稳",
            "status": "active",
            "focus_summary": "候选材料预览",
            "next_actions": ["向老板展示候选材料预览"],
            "created_at": "2026-07-30T09:00:00+08:00",
        }
    ])
    _append_jsonl(tmp_path, "attention_threads.jsonl", [
        {
            "attention_id": "att1",
            "focus_key": "goal:sept_renewal",
            "target_user_id": "JinWenJie",
            "question_text": "请确认优先抓价格、转校、等开学哪一类。",
            "status": "sent",
            "created_at": "2026-07-30T11:00:00+08:00",
        }
    ])
    _append_jsonl(tmp_path, "value_progress_ledger.jsonl", [
        {
            "subject": "续费目标候选材料整理",
            "discovered": "老板要求先看三类未续原因。",
            "hermes_action": "整理候选材料。",
            "outcome": "等待老板预览确认。",
            "created_at": "2026-07-30T12:00:00+08:00",
        }
    ])
    _append_jsonl(tmp_path, "daily_report_runs.jsonl", [
        {"report_kind": "evening", "queued_at": "2026-07-30T21:00:07+08:00", "created_at": "2026-07-30T21:00:07+08:00"}
    ])
    return TuoguanStore(tmp_path)


def test_dashboard_v2_adds_hermes_role_blocks(tmp_path):
    from plugins.tuoguan_core.dashboard_builder import build_dashboard_snapshot

    store = _seed_dashboard_store(tmp_path)
    snapshot = build_dashboard_snapshot(store, now=datetime(2026, 7, 30, 21, 5))

    boss = snapshot["boss_dashboard"]
    teacher = snapshot["teacher_dashboards"]["teacher1"]
    assert "hermes_employee" in boss
    assert boss["hermes_employee"]["current_focus"]["title"] == "目标推进：九月份续费率更稳"
    assert boss["hermes_employee"]["focus_brief"]["title"] == "九月份续费率更稳"
    assert len(boss["hermes_employee"]["decision_brief"]) == 1
    assert len(boss["hermes_employee"]["value_entries"]) <= 2
    assert boss["hermes_employee"]["daily_reports"]["evening"]["status"] == "sent"
    assert boss["hermes_employee"]["value_entries"]
    assert "hermes_companion" in teacher
    assert "hermes_performance_tree" in teacher
    assert teacher["hermes_performance_tree"]["cap_amount"] == 200
    assert teacher["hermes_performance_tree"]["estimated_amount"] >= 0
    assert "扣" not in json.dumps(teacher["hermes_performance_tree"], ensure_ascii=False)
    public_payload = json.dumps(
        {
            "boss_headline": boss["hermes_employee"]["headline"],
            "boss_boundary": boss["hermes_employee"]["boundary_note"],
            "teacher_headline": teacher["hermes_companion"]["headline"],
            "teacher_growth": teacher["hermes_performance_tree"]["growth_text"],
        },
        ensure_ascii=False,
    )
    assert "小优" in public_payload
    assert "Hermes" not in public_payload


def test_dashboard_v2_frontend_uses_frozen_workbench_v1():
    from plugins.tuoguan_core.dashboard_http import _DASHBOARD_HTML

    assert "<title>小优工作台</title>" in _DASHBOARD_HTML
    assert "正在读取小优工作台数据" in _DASHBOARD_HTML
    assert "我的教学与服务工作" in _DASHBOARD_HTML
    assert "门店运营与协作" in _DASHBOARD_HTML
    assert "经营决策与小优进展" in _DASHBOARD_HTML
    for label in ("今日", "学生", "我的积累", "今日现场", "团队", "任务", "决策", "经营", "小优"):
        assert label in _DASHBOARD_HTML
    assert "新项目机会" in _DASHBOARD_HTML
    assert "当前没有达到展示门槛的新项目机会" in _DASHBOARD_HTML
    assert "复制问题，回企业微信问小优" in _DASHBOARD_HTML
    assert "/tuoguan/api/me" in _DASHBOARD_HTML
    assert "/tuoguan/api/teacher" in _DASHBOARD_HTML
    assert "/tuoguan/api/boss" in _DASHBOARD_HTML
    assert 'summer_2026:"暑假项目"' in _DASHBOARD_HTML
    assert 'value="summer_2026"' in _DASHBOARD_HTML
    assert "summer_care_2026" not in _DASHBOARD_HTML
    assert "托管 AI 看板" not in _DASHBOARD_HTML
    assert "小优老师成长助手" not in _DASHBOARD_HTML
    assert "绩效树" not in _DASHBOARD_HTML
    assert "工资估算" not in _DASHBOARD_HTML
    assert "href=" not in _DASHBOARD_HTML
