from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path


CN_TZ = timezone(timedelta(hours=8))


def _write_json(path: Path, name: str, payload) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _append_jsonl(path: Path, name: str, rows: list[dict]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with (path / name).open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _store(tmp_path: Path):
    from plugins.tuoguan_core.store import TuoguanStore

    _write_json(tmp_path, "write_guard_config.json", {"enabled": True})
    _write_json(tmp_path, "wecom_whitelist.json", {"super_users": ["boss1"], "allowed_users": ["teacher1"], "user_roles": {"boss1": "boss", "teacher1": "teacher"}})
    _write_json(tmp_path, "teacher_wecom_map.json", {"机构负责人": "boss1", "示例老师": "teacher1"})
    _write_json(tmp_path, "staff.json", {"teacher1": {"name": "示例老师", "role": "teacher"}})
    _write_json(tmp_path, "students.json", {})
    _write_json(tmp_path, "tasks.json", [{"id": "closed-task", "title": "已完成", "status": "completed", "owner_user_id": "teacher1"}])
    _write_json(tmp_path, "active_task_context.json", {"teacher1": {"task_id": "closed-task"}})
    _write_json(tmp_path, "pending_next_task_context.json", {})
    _write_json(tmp_path, "model_focus.json", {})
    _write_json(tmp_path, "notification_outbox.json", [
        {"id": "candidate-1", "status": "pending", "touser": "teacher1", "task_id": "same-task", "action": "task_due", "created_at": "2026-08-25T10:00:00+08:00"},
        {"id": "candidate-2", "status": "pending", "touser": "teacher1", "task_id": "same-task", "action": "task_due", "created_at": "2026-08-25T10:01:00+08:00"},
    ])
    _write_json(tmp_path, "dashboard_cache.json", {"generated_at": "2026-08-20T10:00:00+08:00"})
    _append_jsonl(tmp_path, "reply_ledger.jsonl", [{
        "tenant_id": "example_institution",
        "message_id": "reply-1",
        "completed_at": "2026-08-25T10:05:00+08:00",
        "workstyle_adaptation": {"unverified_commitment": True},
    }])
    _append_jsonl(tmp_path, "turn_traces.jsonl", [{
        "tenant_id": "example_institution",
        "trace_id": "timeout-1",
        "completed_at": "2026-08-25T10:05:00+08:00",
        "failure_type": "provider_timeout_or_interruption",
        "final_outcome": "failed",
    }, {
        "tenant_id": "example_institution",
        "trace_id": "timeout-2",
        "completed_at": "2026-08-25T10:06:00+08:00",
        "failure_type": "provider_timeout_or_interruption",
        "final_outcome": "failed",
    }])
    return TuoguanStore(tmp_path)


def _boss():
    from plugins.tuoguan_core.models import UserIdentity

    return UserIdentity("wecom_callback", "boss1", "boss1", "机构负责人", "boss", "approved")


def test_work_commitment_reuses_authoritative_work_item_without_task_or_outbox(tmp_path):
    from plugins.tuoguan_core.digital_employee_state import (
        query_work_commitments,
        submit_work_commitment,
        update_work_commitment,
    )
    from plugins.tuoguan_core.write_guard import authorized_system_write

    store = _store(tmp_path)
    with authorized_system_write(store.data_dir, job_name="test_commitment", allowed_files={"hermes_work_items.jsonl"}):
        created = submit_work_commitment(
            store,
            identity=_boss(),
            focus_key="student-record-rule",
            title="整理学生记录制度依据",
            commitment_statement="我来先整理现有记录制度依据。",
            deliverable="一份待审核的事实清单",
            next_action="核对现有记录账本和老板已确认规则",
            operation_id="commitment-1",
            source_message_id="message-1",
            evidence_requirement="内部账本和老板确认原话",
        )
    assert created["ok"] is True
    item = created["work_item"]
    assert item["work_kind"] == "conversation_commitment"
    assert item["commitment_stage"] == "captured"
    assert (tmp_path / "notification_outbox.json").read_text(encoding="utf-8").count("candidate-") == 2
    assert len(json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8"))) == 1

    with authorized_system_write(store.data_dir, job_name="test_commitment_update", allowed_files={"hermes_work_items.jsonl"}):
        completed = update_work_commitment(
            store,
            identity=_boss(),
            operation_id="commitment-2",
            work_item_id=item["work_item_id"],
            commitment_stage="completed",
            progress_evidence=[{"source": "record_ledger", "reference": "records.json"}],
        )
    assert completed["ok"] is True
    commitments = query_work_commitments(store, identity=_boss(), include_closed=True)
    assert commitments["items"][0]["commitment_stage"] == "completed"
    assert commitments["items"][0]["status"] == "closed"


def test_supervision_repairs_only_derived_state_and_unsent_duplicates(tmp_path):
    from plugins.tuoguan_core.supervision_runner import run_supervision_once

    store = _store(tmp_path)
    now = datetime(2026, 8, 25, 10, 10, tzinfo=CN_TZ)
    result = run_supervision_once(store, now=now, apply_repairs=True, write_report=False)

    assert result["ok"] is True
    categories = {row["category"] for row in result["findings"]}
    assert {"commitment_without_receipt", "stale_context_projection", "duplicate_unsent_candidate", "model_consecutive_timeout"} <= categories
    outbox = json.loads((tmp_path / "notification_outbox.json").read_text(encoding="utf-8"))
    assert [row["status"] for row in outbox].count("superseded") == 1
    assert json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8"))[0]["status"] == "completed"
    assert json.loads((tmp_path / "active_task_context.json").read_text(encoding="utf-8")) == {}
    repairs = (tmp_path / "supervision_repairs.jsonl").read_text(encoding="utf-8")
    assert "supersede_duplicate_unsent_candidate" in repairs
    assert "rebuild_context_projection" in repairs


def test_supervision_detects_task_delivery_and_closed_coaching_without_changing_tasks(tmp_path):
    from plugins.tuoguan_core.supervision_runner import run_supervision_once

    store = _store(tmp_path)
    _write_json(tmp_path, "tasks.json", [{
        "id": "recent-open-no-delivery",
        "title": "联系家长",
        "status": "active",
        "owner_user_id": "teacher1",
        "created_at": "2026-08-25T10:00:00+08:00",
    }, {
        "id": "closed-coach-open",
        "title": "已完成沟通",
        "status": "completed",
        "owner_user_id": "teacher1",
        "coach_stage": "collecting_evidence",
    }])
    _write_json(tmp_path, "notification_outbox.json", [])

    result = run_supervision_once(
        store,
        now=datetime(2026, 8, 25, 10, 10, tzinfo=CN_TZ),
        apply_repairs=False,
        write_report=False,
    )

    categories = {row["category"] for row in result["findings"]}
    assert "task_created_without_notification_receipt" in categories
    assert "closed_task_coach_projection_open" in categories
    tasks = json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8"))
    assert tasks[1]["coach_stage"] == "collecting_evidence"


def test_supervision_accepts_legacy_naive_ledger_timestamps(tmp_path):
    from plugins.tuoguan_core.supervision_runner import run_supervision_once

    store = _store(tmp_path)
    _append_jsonl(tmp_path, "reply_ledger.jsonl", [{
        "tenant_id": "example_institution",
        "message_id": "legacy-reply",
        "completed_at": "2026-08-25T10:06:00",
        "workstyle_adaptation": {"unverified_commitment": True},
    }])

    result = run_supervision_once(
        store,
        now=datetime(2026, 8, 25, 10, 10, tzinfo=CN_TZ),
        apply_repairs=False,
        write_report=False,
    )

    assert result["ok"] is True
    assert any(row["category"] == "commitment_without_receipt" for row in result["findings"])


def test_supervision_accepts_legacy_naive_dashboard_timestamp(tmp_path):
    from plugins.tuoguan_core.supervision_runner import run_supervision_once

    store = _store(tmp_path)
    _write_json(tmp_path, "dashboard_cache.json", {"generated_at": "2026-08-25T10:00:00"})

    result = run_supervision_once(
        store,
        now=datetime(2026, 8, 25, 10, 10, tzinfo=CN_TZ),
        apply_repairs=False,
        write_report=False,
    )

    assert result["ok"] is True
    assert not any(row["category"] == "dashboard_projection_stale" for row in result["findings"])


def test_supervision_closes_historical_workstyle_failure_after_preference_is_superseded(tmp_path):
    from plugins.tuoguan_core.supervision import _fold_findings
    from plugins.tuoguan_core.supervision_runner import run_supervision_once
    from plugins.tuoguan_core.workstyle_profiles import WORKSTYLE_EVENTS_FILE, submit_person_workstyle_preference
    from plugins.tuoguan_core.write_guard import authorized_system_write

    store = _store(tmp_path)
    now = datetime(2026, 8, 25, 10, 10, tzinfo=CN_TZ)
    with authorized_system_write(store.data_dir, job_name="test_workstyle_failure", allowed_files={WORKSTYLE_EVENTS_FILE, "reply_ledger.jsonl"}):
        saved = submit_person_workstyle_preference(
            store,
            identity=_boss(),
            preference_type="other_low_risk",
            scope="direct_reply",
            preference_text="一次只问一个关键问题。",
            normalized_rule="一次只问一个关键问题。",
            dimension_key="interaction_pacing",
            operation_id="test-workstyle-pacing",
        )
        assert saved["ok"] is True
        store.append_jsonl_verified("reply_ledger.jsonl", {
            "tenant_id": "example_institution",
            "message_id": "reply-workstyle-failure",
            "completed_at": "2026-08-25T10:05:00+08:00",
            "workstyle_adaptation": {"application_result": {"application": {
                "target_user_id": "boss1",
                "scope": "direct_reply",
                "compliance": {"ok": False, "failures": ["interaction_pacing_multiple_questions"]},
            }}},
        })
    first = run_supervision_once(store, now=now, apply_repairs=False, write_report=False)
    assert any(row["category"] == "workstyle_saved_not_applied" for row in first["findings"])

    with authorized_system_write(store.data_dir, job_name="test_workstyle_supersede", allowed_files={WORKSTYLE_EVENTS_FILE}):
        store.append_jsonl_verified(WORKSTYLE_EVENTS_FILE, {
            "record_type": "person_workstyle_semantic_mismatch",
            "preference_id": saved["preference"]["preference_id"],
            "status": "superseded",
        })
    second = run_supervision_once(
        store,
        now=datetime(2026, 8, 25, 10, 15, tzinfo=CN_TZ),
        apply_repairs=False,
        write_report=False,
    )
    assert not any(row["category"] == "workstyle_saved_not_applied" for row in second["findings"])
    assert any(row["category"] == "workstyle_saved_not_applied" for row in second["verified_findings"])
    assert any(
        row.get("category") == "workstyle_saved_not_applied" and row.get("state") == "verified"
        for row in _fold_findings(store).values()
    )


def test_supervision_council_is_read_only_and_rejects_incomplete_advice(tmp_path):
    from plugins.tuoguan_core.supervision import run_supervision_council

    before = sorted(path.name for path in tmp_path.iterdir())
    result = run_supervision_council(
        {"tenant_id": "example_institution", "generated_at": "2026-08-25T10:00:00+08:00", "privacy": {}, "source_counts": {}, "boundary": {}},
        [{"finding_id": "finding-1", "category": "stale_context_projection", "severity": "p1", "state": "classified", "summary": "stale", "scope": "context"}],
        advisor_call=lambda role, _snapshot: {
            "problem": f"{role} found a projection issue",
            "evidence": ["finding-1"],
            "reproduction": "read-only replay",
            "root_cause": "derived state stale",
            "risk_level": "p1",
            "recommended_repair": "rebuild projection",
            "forbidden_actions": ["do not edit tasks"],
            "verification_method": "re-read projection",
            "unexpected": "discarded",
        },
    )
    assert result["status"] == "completed"
    assert len(result["advisors"]) == 3
    assert all("unexpected" not in row for row in result["advisors"])
    assert before == sorted(path.name for path in tmp_path.iterdir())


def test_supervision_records_isolated_advisory_results_without_granting_business_writes(tmp_path):
    from plugins.tuoguan_core.supervision import (
        SUPERVISION_FINDINGS_FILE,
        SUPERVISION_REPAIRS_FILE,
        SUPERVISION_RUNS_FILE,
        scan_supervision,
    )
    from plugins.tuoguan_core.write_guard import authorized_system_write

    store = _store(tmp_path)
    allowed = {SUPERVISION_RUNS_FILE, SUPERVISION_FINDINGS_FILE, SUPERVISION_REPAIRS_FILE}
    with authorized_system_write(store.data_dir, job_name="test_supervision_advisors", allowed_files=allowed):
        result = scan_supervision(
            store,
            now=datetime(2026, 8, 25, 10, 10, tzinfo=CN_TZ),
            advisor_call=lambda role, _snapshot: {
                "problem": f"{role} found a read-only inconsistency",
                "evidence": ["object-reference-only"],
                "reproduction": "read stored receipt",
                "root_cause": "derived projection",
                "risk_level": "p1",
                "recommended_repair": "do not change business facts",
                "forbidden_actions": ["no task or outbound write"],
                "verification_method": "re-read ledger",
            },
        )
    assert result["run"]["model_advisors"]["status"] == "completed"
    assert len(result["run"]["model_advisors"]["advisors"]) == 3
    assert json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8"))[0]["status"] == "completed"


def test_boss_dashboard_exposes_commitments_and_supervision_without_exposing_them_to_teachers(tmp_path):
    from plugins.tuoguan_core.dashboard_builder import build_dashboard_snapshot
    from plugins.tuoguan_core.digital_employee_state import submit_work_commitment
    from plugins.tuoguan_core.write_guard import authorized_system_write

    store = _store(tmp_path)
    with authorized_system_write(store.data_dir, job_name="test_dashboard_commitment", allowed_files={"hermes_work_items.jsonl"}):
        created = submit_work_commitment(
            store,
            identity=_boss(),
            focus_key="dashboard-contract",
            title="核验工作台状态",
            commitment_statement="我来核验看板状态。",
            deliverable="可核验的状态摘要",
            next_action="读取权威任务和缓存投影",
            operation_id="dashboard-commitment-1",
            source_message_id="message-dashboard-1",
            evidence_requirement="任务账本和缓存时间",
        )
    assert created["ok"] is True

    snapshot = build_dashboard_snapshot(store, now=datetime(2026, 8, 25, 10, 10, tzinfo=CN_TZ))
    boss = snapshot["boss_dashboard"]
    assert boss["hermes_employee"]["work_commitments"][0]["title"] == "核验工作台状态"
    assert boss["hermes_employee"]["supervision"]["read_only"] is True

    teacher = snapshot["teacher_dashboards"]["teacher1"]
    assert "work_commitments" not in teacher
    assert "supervision" not in teacher
