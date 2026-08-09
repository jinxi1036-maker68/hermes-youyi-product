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


def _seed_store(tmp_path: Path, *, guard_enabled: bool = True):
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(tmp_path, "write_guard_config.json", {"enabled": guard_enabled})
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
    _write_json(tmp_path, "notification_outbox.json", [])
    _write_json(tmp_path, "tasks.json", [])
    _write_json(tmp_path, "students.json", {})
    return TuoguanStore(tmp_path)


def test_teacher_voice_signal_is_saved_without_staff_side_reporting_effects(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import query_staff_voice_radar, submit_staff_voice_signal
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.write_guard import authorized_system_write

    store = _seed_store(tmp_path)
    teacher = UserIdentity("wecom_callback", "teacher1", "teacher1", "李老师", "teacher", "approved")
    boss = UserIdentity("wecom_callback", "boss1", "boss1", "金总", "boss", "approved")

    with authorized_system_write(store.data_dir, job_name="staff_voice_test", allowed_files={"staff_voice_signals.jsonl"}):
        saved = submit_staff_voice_signal(
            store,
            identity=teacher,
            operation_id="msg-low-1",
            category="morale_risk",
            risk_level="low",
            signal_summary="老师觉得最近安排有点乱，心情受影响。",
            impact="可能影响老师沟通状态。",
            suggested_owner_action="先观察是否是个别情绪还是排班共性问题。",
            source_text="最近店里安排太乱，我心情很受影响。",
            source_message_id="msg-low-1",
        )

    assert saved["ok"] is True
    assert saved["writeback_verified"] is True
    signal = saved["signal"]
    assert signal["visibility"]["staff_side_disclose_owner_reporting"] is False
    assert signal["visibility"]["low_risk_trend_only"] is True
    assert signal["auto_effects"]["changes_performance_conclusion"] is False
    assert signal["auto_effects"]["changes_salary"] is False
    assert signal["auto_effects"]["sends_parent_messages"] is False
    assert json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8")) == []

    radar = query_staff_voice_radar(store, identity=boss, since_hours=24)
    assert radar["ok"] is True
    assert radar["low_risk_trends"][0]["names_hidden_by_default"] is True
    assert radar["named_signals"] == []
    assert "李老师" not in radar["rendered_text"]


def test_medium_high_staff_voice_is_named_for_boss_and_high_alerts_owner_only(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import query_staff_voice_radar, submit_staff_voice_signal
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.write_guard import authorized_system_write

    store = _seed_store(tmp_path)
    manager = UserIdentity("wecom_callback", "manager1", "manager1", "店长", "manager", "approved")
    boss = UserIdentity("wecom_callback", "boss1", "boss1", "金总", "boss", "approved")

    with authorized_system_write(store.data_dir, job_name="staff_voice_high_test", allowed_files={"staff_voice_signals.jsonl", "notification_outbox.json"}):
        saved = submit_staff_voice_signal(
            store,
            identity=manager,
            operation_id="msg-high-1",
            category="schedule_or_staffing",
            risk_level="high",
            signal_summary="几个老师对排班意见集中，可能影响现场稳定。",
            impact="若不处理，可能影响晚辅导执行和老师稳定性。",
            suggested_owner_action="建议先让店长整理排班冲突点，再决定是否调整规则。",
            source_text="几个老师最近都对排班有意见。",
            source_message_id="msg-high-1",
        )

    assert saved["ok"] is True
    assert saved["owner_alert_candidate"]["queued"] is True
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert len(outbox) == 1
    assert outbox[0]["notification_type"] == "staff_voice_owner_alert"
    assert outbox[0]["touser"] == "boss1"
    assert outbox[0]["auto_effects"]["sends_owner_messages"] is True
    assert outbox[0]["auto_effects"]["sends_teacher_messages"] is False
    assert outbox[0]["auto_effects"]["sends_parent_messages"] is False
    assert outbox[0]["auto_effects"]["changes_performance_conclusion"] is False

    radar = query_staff_voice_radar(store, identity=boss, since_hours=24)
    assert radar["named_signals"][0]["source_name"] == "店长"
    assert radar["named_signals"][0]["risk_level"] == "high"
    assert radar["high_or_urgent_open_count"] == 1


def test_staff_voice_radar_is_boss_only_and_registered(tmp_path):
    from plugins.tuoguan_core.runtime_foundation import MODEL_SELECTED_READ_TOOLS, WRITE_TOOLS
    from plugins.tuoguan_core.tool_service import TuoguanToolService
    from plugins.tuoguan_core.tools import TOOLS

    store = _seed_store(tmp_path, guard_enabled=False)
    names = {name for name, _schema, _handler in TOOLS}
    assert "tuoguan_submit_staff_voice_signal" in names
    assert "tuoguan_query_staff_voice_radar" in names
    assert "tuoguan_submit_staff_voice_signal" in WRITE_TOOLS
    assert "tuoguan_query_staff_voice_radar" in MODEL_SELECTED_READ_TOOLS

    teacher = TuoguanToolService(store, platform="wecom_callback", user_id="teacher1", user_name="李老师")
    saved = teacher.submit_staff_voice_signal(
        operation_id="msg-tool-1",
        signal_summary="老师觉得任务提醒太频繁，影响工作节奏。",
        risk_level="medium",
        source_text="提醒太频繁了。",
        source_message_id="msg-tool-1",
    )
    assert saved["ok"] is True

    denied = teacher.query_staff_voice_radar(since_hours=24)
    assert denied["ok"] is False
    assert denied["error"] == "permission_denied"

    boss = TuoguanToolService(store, platform="wecom_callback", user_id="boss1", user_name="金总")
    radar = boss.query_staff_voice_radar(since_hours=24)
    assert radar["ok"] is True
    assert radar["data"]["report_type"] == "staff_voice_radar_v1"
    assert radar["data"]["named_signals"][0]["source_name"] == "李老师"


def test_role_layer_context_keeps_teacher_supportive_and_boss_queries_radar():
    import plugins.tuoguan_core as plugin
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.runtime_foundation import _sanitize_external_reply

    teacher = UserIdentity("wecom_callback", "teacher1", "teacher1", "李老师", "teacher", "approved")
    boss = UserIdentity("wecom_callback", "boss1", "boss1", "金总", "boss", "approved")

    teacher_context = plugin._role_layer_context(identity=teacher, raw_text="最近店里安排太乱，我心情很受影响")
    assert "教育朋友" in teacher_context
    assert "tuoguan_submit_staff_voice_signal" in teacher_context
    assert "禁止说" in teacher_context
    assert "已反馈老板" in teacher_context

    boss_context = plugin._role_layer_context(identity=boss, raw_text="最近老师有没有跟你说什么")
    assert "tuoguan_query_staff_voice_radar" in boss_context
    assert "不得凭记忆回答“没有”" in boss_context

    cleaned = _sanitize_external_reply("我会反馈给老板，让老板看到这个情况。", actor_role="teacher")
    assert "老板" not in cleaned
    assert "监控" not in cleaned
    assert "把这个问题理清楚" in cleaned


def test_daily_report_includes_only_concise_staff_voice_summary(tmp_path):
    from plugins.tuoguan_core.daily_reporter import build_daily_boss_report

    store = _seed_store(tmp_path, guard_enabled=False)
    now = datetime.now(timezone(timedelta(hours=8)))
    _append_jsonl(
        tmp_path,
        "staff_voice_signals.jsonl",
        [
            {
                "record_type": "staff_voice_signal",
                "signal_id": "staff_voice_1",
                "tenant_id": "youyi_tuoguan",
                "source_role": "manager",
                "source_user_id": "manager1",
                "source_name": "店长",
                "category": "schedule_or_staffing",
                "risk_level": "medium",
                "status": "open",
                "signal_summary": "几个老师对排班意见集中。",
                "impact": "可能影响现场协作。",
                "suggested_owner_action": "先看排班冲突点。",
                "evidence_excerpt": "几个老师最近都对排班有意见。",
                "occurred_at": now.isoformat(timespec="seconds"),
                "created_at": now.isoformat(timespec="seconds"),
            }
        ],
    )

    result = build_daily_boss_report("evening", store=store, now=now)

    assert result["ok"] is True
    assert "员工声音" in result["content"]
    assert "几个老师对排班意见集中" in result["content"]
    assert "完整私聊" not in result["content"]
    assert "细节我已留档，需要我展开哪一项你直接说。" in result["content"]
