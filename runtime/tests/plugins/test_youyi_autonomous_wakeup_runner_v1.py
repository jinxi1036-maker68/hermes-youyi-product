from __future__ import annotations

from datetime import datetime, timezone
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
        {"super_users": ["boss1"], "allowed_users": ["teacher1"], "user_roles": {"boss1": "boss", "teacher1": "teacher"}},
    )
    _write_json(tmp_path, "teacher_wecom_map.json", {"金总": "boss1", "李老师": "teacher1"})
    _write_json(tmp_path, "staff.json", {"teacher1": {"name": "李老师", "role": "teacher"}})
    _write_json(tmp_path, "students.json", {})
    _write_json(tmp_path, "tasks.json", [])
    _write_json(tmp_path, "notification_outbox.json", [])
    _write_json(tmp_path, "academic_term_state.json", {"state": "summer_transition", "label": "暑期过渡期"})
    _append_jsonl(
        tmp_path,
        "hermes_work_items.jsonl",
        [
            {
                "work_item_id": "work-1",
                "tenant_id": "youyi_tuoguan",
                "record_type": "work_item",
                "status": "waiting",
                "focus_key": "renewal:sept",
                "title": "九月续费稳定性",
                "focus_summary": "等李老师补充事实。",
                "current_waiting": {"target_user_id": "teacher1", "target_person": "李老师", "reason": "等老师回复"},
                "next_attention_at": "2026-07-28T08:00:00+00:00",
                "created_at": "2026-07-27T08:00:00+00:00",
                "updated_at": "2026-07-27T08:00:00+00:00",
                "source": {"actor_user_id": "boss1", "actor_role": "boss"},
                "auto_effects": {"forces_next_action": False, "changes_router": False},
            }
        ],
    )
    return TuoguanStore(tmp_path)


def _snapshot(tmp_path: Path) -> dict[str, str]:
    names = ["hermes_work_items.jsonl", "wakeup_requests.jsonl", "tasks.json", "notification_outbox.json"]
    return {name: (tmp_path / name).read_text(encoding="utf-8") if (tmp_path / name).exists() else "" for name in names}


def test_autonomous_wakeup_runner_is_clock_tick_only(tmp_path):
    from plugins.tuoguan_core.autonomous_wakeup_runner import run_autonomous_wakeup_once

    store = _seed_store(tmp_path)
    before = _snapshot(tmp_path)
    result = run_autonomous_wakeup_once(store, now=datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc), write_report=True)
    after = _snapshot(tmp_path)

    assert result["report_type"] == "autonomous_wakeup_runner_v1"
    assert result["read_only"] is True
    assert result["boundary"]["clock_tick_only"] is True
    assert result["boundary"]["limits_model"] is False
    assert result["boundary"]["sends_owner_messages"] is False
    assert result["boundary"]["materializes_wakeup_requests"] is False
    assert result["source_counts"]["visible_work_item_count"] == 1
    assert result["source_counts"]["due_wakeup_candidate_count"] == 1
    assert "no_wakeup_requests_created" in result["forbidden_actions_confirmed_absent"]
    assert before == after
    assert result["report_path"].endswith(".md")
    assert (tmp_path / "reports").exists()
