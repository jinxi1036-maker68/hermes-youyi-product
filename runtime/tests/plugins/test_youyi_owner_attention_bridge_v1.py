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
    _write_json(tmp_path, "teacher_wecom_map.json", {"金总": "boss1"})
    _write_json(tmp_path, "wecom_whitelist.json", {"super_users": ["boss1"], "user_roles": {"boss1": "boss"}})
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
                "status": "active",
                "focus_summary": "历史分析与新学期准备。",
                "created_at": "2026-07-28T08:00:00+08:00",
                "updated_at": "2026-07-28T08:00:00+08:00",
                "source": {"actor_user_id": "boss1", "actor_role": "boss"},
                "auto_effects": {"forces_next_action": False, "changes_router": False},
            }
        ],
    )
    return TuoguanStore(tmp_path)


def _base_decision() -> dict:
    return {
        "employee_summary": "续费目标需要老板确认优先抓哪类未续原因。",
        "institution_understanding": "已有续费原因分类。",
        "goal_progress_view": "缺老板优先级后才能继续整理候选。",
        "observations": [],
        "work_item_updates": [
            {
                "focus_key": "goal:sept_renewal",
                "title": "目标推进：九月份续费率更稳",
                "focus_summary": "需要老板确认优先级。",
                "status": "active",
                "update_text": "需要老板确认先抓哪类未续原因。",
                "next_actions": ["按老板确认的优先类别整理候选和话术。"],
                "blocked_by": [{"type": "missing_owner_priority", "text": "未确认优先抓哪类未续原因"}],
            }
        ],
        "questions_to_humans": [
            {
                "ask_role": "boss",
                "reason": "缺老板确认：价格、转校、等开学三类未续原因先抓哪一类。",
                "question": "请确认价格、转校、等开学三类里，今天先优先抓哪一类？",
                "urgency": "normal",
            }
        ],
        "boss_attention_candidates": [],
        "institution_fact_gaps": [],
        "value_progress_entries": [],
        "self_review": {},
        "external_actions": [],
    }


def test_owner_question_bridges_to_queued_attention(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import run_autonomous_employee_loop

    store = _seed_store(tmp_path)
    cn_tz = timezone(timedelta(hours=8))
    result = run_autonomous_employee_loop(
        store,
        now=datetime(2026, 7, 28, 10, 0, tzinfo=cn_tz),
        decision_provider=lambda _materials: _base_decision(),
    )

    assert result["ok"] is True
    assert any(row["kind"] == "owner_attention_queued" and row["ok"] for row in result["writes"])
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert len(outbox) == 1
    assert outbox[0]["notification_type"] == "autonomous_owner_attention"
    assert outbox[0]["target_user_id"] == "boss1"
    assert "卡点：" in outbox[0]["content"]
    assert "需要你确认：" in outbox[0]["content"]
    assert "确认后：" in outbox[0]["content"]

    attention_rows = [
        json.loads(line)
        for line in (tmp_path / "attention_threads.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert attention_rows[-1]["status"] == "queued"
    assert attention_rows[-1]["source_decision_summary"]
    assert "价格、转校、等开学" in json.dumps(attention_rows[-1]["needed_facts"], ensure_ascii=False)


def test_deferred_service_relation_question_does_not_bridge(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import run_autonomous_employee_loop

    decision = _base_decision()
    decision["questions_to_humans"] = [
        {
            "ask_role": "boss",
            "reason": "缺少新学期服务关系。",
            "question": "请确认新学期名单、服务类型和主责老师。",
            "urgency": "normal",
        }
    ]
    store = _seed_store(tmp_path)
    cn_tz = timezone(timedelta(hours=8))
    result = run_autonomous_employee_loop(
        store,
        now=datetime(2026, 7, 28, 10, 0, tzinfo=cn_tz),
        decision_provider=lambda _materials: decision,
        wakeup_summary={"term_state": {"service_relation_policy": "defer_until_new_term"}},
    )

    assert result["ok"] is True
    assert not any(row["kind"] == "owner_attention_queued" and row["ok"] for row in result["writes"])
    assert json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8")) == []


def test_generic_owner_attention_candidate_is_rejected(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import run_autonomous_employee_loop

    decision = _base_decision()
    decision["questions_to_humans"] = []
    decision["boss_attention_candidates"] = [
        {
            "focus_key": "goal:sept_renewal",
            "reason": "需要沟通。",
            "message": "金总，我整理好了情况。",
            "urgency": "normal",
        }
    ]
    store = _seed_store(tmp_path)
    cn_tz = timezone(timedelta(hours=8))
    result = run_autonomous_employee_loop(
        store,
        now=datetime(2026, 7, 28, 10, 0, tzinfo=cn_tz),
        decision_provider=lambda _materials: decision,
    )

    assert result["ok"] is True
    assert not any(row["kind"] == "owner_attention_queued" and row["ok"] for row in result["writes"])
    assert json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8")) == []


def test_explicit_owner_confirmation_text_bridges_to_attention(tmp_path):
    from plugins.tuoguan_core.autonomous_employee_loop import run_autonomous_employee_loop

    decision = _base_decision()
    decision["questions_to_humans"] = []
    decision["employee_summary"] = (
        "10:30白天唤醒，续费目标历史分析初稿已经形成，"
        "待老板确认后进入重点沟通准备阶段。"
    )
    decision["goal_progress_view"] = (
        "已完成未续原因分类框架，待老板确认后进入下一阶段。"
    )
    decision["work_item_updates"][0]["next_actions"] = [
        "按老板确认继续整理候选材料和话术建议，不联系老师和家长。"
    ]

    store = _seed_store(tmp_path)
    cn_tz = timezone(timedelta(hours=8))
    result = run_autonomous_employee_loop(
        store,
        now=datetime(2026, 7, 28, 10, 30, tzinfo=cn_tz),
        decision_provider=lambda _materials: decision,
    )

    assert result["ok"] is True
    assert any(row["kind"] == "owner_attention_queued" and row["ok"] for row in result["writes"])
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert len(outbox) == 1
    assert "需要你确认" in outbox[0]["content"]
    assert "是否同意我按当前分析进入下一步准备" in outbox[0]["content"]
    assert outbox[0]["auto_effects"]["sends_teacher_messages"] is False
    assert outbox[0]["auto_effects"]["sends_parent_messages"] is False
