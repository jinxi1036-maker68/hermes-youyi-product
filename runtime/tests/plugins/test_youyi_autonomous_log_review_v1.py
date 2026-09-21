from __future__ import annotations

import json
from pathlib import Path

from gateway.config import Platform


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
    (tmp_path / "manual_context").mkdir(parents=True, exist_ok=True)
    _write_json(
        tmp_path / "manual_context",
        "hermes_model_context_injection_allowlist_v1.json",
        {"runtime_foundation": {"enabled": True}, "allowed_capability_cards": []},
    )
    _write_json(
        tmp_path,
        "wecom_whitelist.json",
        {
            "super_users": ["boss1"],
            "allowed_users": ["teacher1", "manager1"],
            "user_roles": {"boss1": "boss", "teacher1": "teacher", "manager1": "manager"},
        },
    )
    _write_json(tmp_path, "teacher_wecom_map.json", {"机构负责人": "boss1", "示例老师": "teacher1", "王店长": "manager1"})
    _write_json(
        tmp_path,
        "staff.json",
        {
            "teacher1": {"name": "示例老师", "role": "teacher", "campus_ids": ["main"], "program_ids": ["regular_tuoguan"]},
            "manager1": {"name": "王店长", "role": "manager", "campus_ids": ["main"], "program_ids": ["regular_tuoguan"]},
        },
    )
    _write_json(tmp_path, "students.json", {})
    _write_json(tmp_path, "tasks.json", [])
    _write_json(tmp_path, "notification_outbox.json", [])
    _append_jsonl(
        tmp_path,
        "reply_ledger.jsonl",
        [
            {
                "ledger_id": "ledger-goal-1",
                "timestamp": "2026-07-27T10:00:00+08:00",
                "user_id": "boss1",
                "raw_text": "刚才那个目标你现在推进到哪一步了？还在等什么？",
                "final_reply": "我看到现在还在等示例老师回复，会先核验最新事实。",
                "tool_calls": [{"tool": "tuoguan_query_hermes_work_items"}],
                "tool_results": [{"tool": "tuoguan_query_hermes_work_items", "ok": True}],
            },
            {
                "ledger_id": "ledger-guard-1",
                "timestamp": "2026-07-27T10:05:00+08:00",
                "user_id": "boss1",
                "raw_text": "系统校验未通过到底是啥原因嘛",
                "final_reply": "系统校验未通过。",
                "guard_result": "unauthorized_write_blocked",
                "tool_results": [{"tool": "tuoguan_submit_hermes_work_item", "ok": False, "error": "unauthorized_write_blocked"}],
            },
            {
                "ledger_id": "ledger-chat-1",
                "timestamp": "2026-07-27T10:10:00+08:00",
                "user_id": "boss1",
                "raw_text": "在线吗",
                "final_reply": "在的。",
            },
        ],
    )
    _append_jsonl(
        tmp_path,
        "gray_observations.jsonl",
        [
            {
                "observation_id": "gray-1",
                "scenario_id": "autonomous_result_unknown_recovery",
                "outcome": "issue",
                "observation_text": "自主工作等待恢复不稳定，结果未知后没先核验。",
                "created_at": "2026-07-27T10:15:00+08:00",
                "actor_user_id": "boss1",
            }
        ],
    )
    return TuoguanStore(tmp_path)


def _service(store, user_id: str):
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    return TuoguanToolService(
        store=store,
        platform=Platform.WECOM_CALLBACK.value,
        user_id=user_id,
        user_name="",
        chat_id=f"wwcorp:{user_id}",
        session_key=f"session:{user_id}",
    )


def _file_snapshot(tmp_path: Path) -> dict[str, str]:
    names = [
        "reply_ledger.jsonl",
        "gray_observations.jsonl",
        "hermes_work_items.jsonl",
        "wakeup_requests.jsonl",
        "learning_candidates.jsonl",
        "tasks.json",
        "notification_outbox.json",
    ]
    return {name: (tmp_path / name).read_text(encoding="utf-8") if (tmp_path / name).exists() else "" for name in names}


def test_autonomous_log_review_registered_readonly_not_router():
    from plugins.tuoguan_core.runtime_foundation import MODEL_SELECTED_READ_TOOLS, WRITE_TOOLS
    from plugins.tuoguan_core.tools import TOOLS

    names = {name for name, _schema, _handler in TOOLS}
    assert "tuoguan_generate_autonomous_log_review" in names
    assert "tuoguan_generate_autonomous_log_review" in MODEL_SELECTED_READ_TOOLS
    assert "tuoguan_generate_autonomous_log_review" not in WRITE_TOOLS
    descriptions = "\n".join(str(schema.get("description") or "") for name, schema, _handler in TOOLS if name == "tuoguan_generate_autonomous_log_review")
    for fragment in ["关键词", "用户说", "必须调用", "model_intent", "next_tool", "workflow_step", "expected_reply"]:
        assert fragment not in descriptions


def test_autonomous_log_review_classifies_logs_and_is_read_only(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import generate_autonomous_log_review

    store = _seed_store(tmp_path)
    before = _file_snapshot(tmp_path)
    result = generate_autonomous_log_review(store, identity=_service(store, "boss1").identity, limit=20)
    after = _file_snapshot(tmp_path)

    assert result["ok"] is True
    assert result["read_only"] is True
    assert result["ledger_scanned_count"] == 3
    assert result["related_turn_count"] == 2
    assert result["gray_observation_count"] == 1
    assert result["issue_candidate_count"] == 3
    assert result["boundary"]["limits_model"] is False
    assert result["boundary"]["updates_handbook"] is False
    assert result["boundary"]["creates_learning_candidates"] is False
    assert result["boundary"]["patches_tools"] is False
    assert result["boundary"]["changes_permissions"] is False
    assert result["remaining_stages_after_this"][0]["stage_id"] == "owner_decision_and_small_rollout"
    assert before == after

    candidate_types = {item["candidate_type"] for item in result["candidates"]}
    assert "autonomous_work_candidate" in candidate_types
    assert "permission_boundary_candidate" in candidate_types


def test_autonomous_log_review_tool_permission_for_teacher(tmp_path):
    store = _seed_store(tmp_path)
    result = _service(store, "teacher1").generate_autonomous_log_review(limit=20)

    assert result["ok"] is False
    assert result["error"] == "permission_denied"
