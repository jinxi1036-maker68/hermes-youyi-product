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
    assert "今天的自主工作安排" in item["content"]
    assert "老板今天先看这句话" in item["content"]
    assert "目标进展" in item["content"]
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
    assert "今晚给你交一下今天的工作日报" in result["content"]
    assert "老板先看结论" in result["content"]
    assert "目标进展" in result["content"]
    assert "等待老板确认" in result["content"]
    assert "没有把等待状态写成完成" in result["content"] or "等待" in result["content"]
    assert result["auto_effects"]["changes_salary"] is False


def test_daily_report_drain_bypasses_active_conversation_quiet_period(tmp_path, monkeypatch):
    from plugins.tuoguan_core import _ACTIVE_WECom_USERS, _drain_notification_outbox

    cn_tz = timezone(timedelta(hours=8))
    now = datetime.now().astimezone()
    monkeypatch.setenv("HERMES_TUOGUAN_DATA_DIR", str(tmp_path))
    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_json(
        tmp_path,
        "notification_outbox.json",
        [
            {
                "id": "autonomous_daily_report:20260808:evening",
                "status": "pending",
                "delivery_mode": "direct_wecom",
                "notification_type": "autonomous_daily_report",
                "action": "daily_evening_report",
                "role": "boss",
                "target_user_id": "boss1",
                "touser": "boss1",
                "content": "金总，今晚给你交一下今天的工作日报。",
                "summary": "小优每日晚间工作日报",
                "created_at": datetime(2026, 8, 8, 21, 0, tzinfo=cn_tz).isoformat(timespec="seconds"),
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
            assert metadata["idempotency_key"] == "autonomous_daily_report:20260808:evening"
            return Result()

    try:
        asyncio.run(_drain_notification_outbox(Adapter()))
    finally:
        _ACTIVE_WECom_USERS.pop("boss1", None)

    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert outbox[0]["status"] == "sent"
    assert outbox[0]["message_id"] == "msg_daily_1"


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
