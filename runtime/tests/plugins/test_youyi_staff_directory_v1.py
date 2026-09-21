from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path


def _write_json(path: Path, name: str, payload) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _append_jsonl(path: Path, name: str, rows: list[dict]) -> None:
    with (path / name).open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _seed_staff_directory(path: Path) -> None:
    _write_json(
        path,
        "wecom_directory_cache.json",
        {
            "status": "ok",
            "members": [
                {"user_id": "ShiLiLi", "name": "示例机构 🍋柠檬老师", "department_names": ["示例机构托管"], "status": 1},
                {"user_id": "FengJuCai", "name": "示例机构🌈🌈🌈彩虹老师", "department_names": ["示例机构托管"], "status": 1},
                {"user_id": "CuiXiaoXia", "name": "崔老师", "department_names": ["示例机构托管"], "status": 1},
                {"user_id": "owner_test", "name": "金文杰", "department_names": ["示例机构托管"], "status": 1},
            ],
        },
    )
    _write_json(
        path,
        "staff.json",
        {
            "ShiLiLi": {"name": "石老师", "role": "teacher"},
            "FengJuCai": {"name": "另一位老师", "role": "teacher"},
            "CuiXiaoXia": {"name": "崔老师", "role": "manager"},
            "owner_test": {"name": "机构负责人", "role": "boss"},
            "OldSummer": {"name": "丁老师", "role": "teacher"},
        },
    )
    _write_json(
        path,
        "teacher_wecom_map.json",
        {"柠檬老师": "ShiLiLi", "彩虹老师": "FengJuCai", "丁老师": "OldSummer"},
    )
    _write_json(
        path,
        "wecom_whitelist.json",
        {
            "super_users": ["owner_test"],
            "allowed_users": ["ShiLiLi", "FengJuCai", "CuiXiaoXia"],
            "user_roles": {
                "owner_test": "boss",
                "ShiLiLi": "teacher",
                "FengJuCai": "teacher",
                "CuiXiaoXia": "manager",
                "OldSummer": "teacher",
            },
        },
    )


def test_staff_directory_matches_emoji_wecom_name_and_alias(tmp_path):
    from plugins.tuoguan_core.staff_directory import query_staff_directory
    from plugins.tuoguan_core.store import TuoguanStore

    _seed_staff_directory(tmp_path)
    result = query_staff_directory(TuoguanStore(tmp_path), query="柠檬老师")

    assert result["ok"] is True
    assert result["result_count"] == 1
    row = result["staff"][0]
    assert row["user_id"] == "ShiLiLi"
    assert row["business_name"] == "石老师"
    assert row["directory_name"] == "示例机构 🍋柠檬老师"
    assert "柠檬老师" in row["known_aliases"]
    assert row["repair_candidate"]["needs_owner_confirmation"] is True
    assert row["repair_candidate"]["value"]["in_wecom_directory"] is True


def test_staff_directory_lists_current_roster_and_marks_missing_directory(tmp_path):
    from plugins.tuoguan_core.staff_directory import query_staff_directory
    from plugins.tuoguan_core.store import TuoguanStore

    _seed_staff_directory(tmp_path)
    result = query_staff_directory(TuoguanStore(tmp_path), role="teacher", include_inactive=True)
    ids = {row["user_id"] for row in result["staff"]}

    assert {"ShiLiLi", "FengJuCai", "OldSummer"} <= ids
    old = next(row for row in result["staff"] if row["user_id"] == "OldSummer")
    assert old["membership_status"] == "系统已授权但企业微信目录未找到"
    assert old["repair_candidate"]["subject"] == "OldSummer"
    assert "未确认前不能说已经保存" in result["rendered_text"]


def test_staff_directory_treats_broad_wecom_question_as_roster_query(tmp_path):
    from plugins.tuoguan_core.staff_directory import query_staff_directory
    from plugins.tuoguan_core.store import TuoguanStore

    _seed_staff_directory(tmp_path)
    result = query_staff_directory(TuoguanStore(tmp_path), query="企业微信里都有谁")
    ids = {row["user_id"] for row in result["staff"]}

    assert {"ShiLiLi", "FengJuCai", "CuiXiaoXia", "owner_test"} <= ids
    assert result["result_count"] >= 4


def test_staff_directory_infers_teacher_role_from_broad_teacher_question(tmp_path):
    from plugins.tuoguan_core.staff_directory import query_staff_directory
    from plugins.tuoguan_core.store import TuoguanStore

    _seed_staff_directory(tmp_path)
    result = query_staff_directory(TuoguanStore(tmp_path), query="系统里挂的这些老师都是正确的吗")

    assert result["role"] == "teacher"
    assert result["staff"]
    assert {row["role"] for row in result["staff"]} == {"teacher"}


def test_staff_directory_uses_confirmed_operational_fact_alias(tmp_path):
    from plugins.tuoguan_core.staff_directory import query_staff_directory
    from plugins.tuoguan_core.store import TuoguanStore

    _seed_staff_directory(tmp_path)
    _write_json(
        tmp_path,
        "operational_facts.json",
        {
            "facts": [
                {
                    "fact_id": "fact_staff_1",
                    "fact_type": "staff_directory_repair",
                    "subject": "FengJuCai",
                    "scope": "staff_directory",
                    "status": "active",
                    "value": {
                        "business_name": "彩虹老师",
                        "aliases": ["另一位老师", "彩虹老师", "FengJuCai"],
                    },
                }
            ]
        },
    )

    result = query_staff_directory(TuoguanStore(tmp_path), query="彩虹")
    row = result["staff"][0]

    assert row["user_id"] == "FengJuCai"
    assert row["business_name"] == "彩虹老师"
    assert any(evidence["source"] == "operational_facts.json" for evidence in row["source_evidence"])


def test_staff_directory_tool_is_registered_and_model_selected_read_safe():
    from plugins.tuoguan_core.runtime_foundation import MODEL_SELECTED_READ_TOOLS
    from plugins.tuoguan_core.tools import TOOLS

    tool_names = {name for name, _schema, _handler in TOOLS}

    assert "tuoguan_query_staff_directory" in tool_names
    assert "tuoguan_query_staff_directory" in MODEL_SELECTED_READ_TOOLS


def test_unverified_read_and_save_claims_are_sanitized_without_tool():
    from plugins.tuoguan_core.runtime_foundation import _sanitize_external_reply

    reply = "小优查了，系统里显示这些老师都正确，已经确认并保存了，以后就按这个来理解。"
    sanitized = _sanitize_external_reply(reply, verified_state_change=False, used_trusted_tool=False)

    assert "小优查了" not in sanitized
    assert "系统里显示" not in sanitized
    assert "已经确认并保存" not in sanitized
    assert "以后就按这个来理解" not in sanitized
    assert "当前上下文" in sanitized


def test_daily_report_health_flags_unverified_staff_directory_deflection(tmp_path):
    from plugins.tuoguan_core.daily_reporter import _proactivity_health
    from plugins.tuoguan_core.store import TuoguanStore

    now = datetime.now().astimezone()
    _write_json(tmp_path, "notification_outbox.json", [])
    _append_jsonl(
        tmp_path,
        "reply_ledger.jsonl",
        [
            {
                "created_at": (now - timedelta(minutes=20)).isoformat(timespec="seconds"),
                "completed_at": (now - timedelta(minutes=20)).isoformat(timespec="seconds"),
                "raw_text": "那这个乱码能不能解决？",
                "final_reply": "小优查了，但系统里显示名字都是乱码，需要技术处理一下。",
                "tool_calls": [],
                "used_tool_registry_entry": "",
            }
        ],
    )

    issues = _proactivity_health(TuoguanStore(tmp_path), now)

    assert any("未查工具却声称已查/已保存" in item for item in issues)
    assert any("人员/企业微信问题疑似未先查目录" in item for item in issues)


def test_runtime_records_tool_failure_evolution_for_unrescued_staff_deflection(tmp_path):
    from plugins.tuoguan_core.runtime_foundation import ensure_outbound_reply_recorded
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    store = TuoguanStore(tmp_path)

    ensure_outbound_reply_recorded(
        store=store,
        message_id="msg-staff-deflection-1",
        conversation_id="conv-boss",
        user_id="boss1",
        role="boss",
        raw_text="企业微信乱码能不能解决？还有哪两位老师你自己不会查吗？",
        final_reply="这个小优查不到，需要技术处理一下。",
        entered_model=True,
        route_decision="model_first",
        reply_owner="model",
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "self_evolution_events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row["candidate_type"] == "tool_failure_or_bug"
    assert row["status"] == "pending_review"
    assert "人员/企业微信/业务事实问题未先自救" in row["summary"]
    assert row["auto_effects"]["may_inform_next_context"] is False
    assert row["auto_effects"]["changes_router"] is False
    assert row["evidence"][0]["source"] == "reply_ledger"
