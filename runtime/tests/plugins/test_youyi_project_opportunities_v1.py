from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys

from plugins.tuoguan_core.autonomous_employee_loop import run_autonomous_employee_loop
from plugins.tuoguan_core.capability_facades import DOMAIN_OPERATIONS, operation_manifest
from plugins.tuoguan_core.daily_reporter import build_daily_boss_report
from plugins.tuoguan_core.models import UserIdentity
from plugins.tuoguan_core.project_opportunities import (
    PROJECT_OPPORTUNITY_EVENTS_FILE,
    mark_stale_project_opportunities,
    query_project_opportunities,
    record_project_opportunity_assessment,
    review_project_opportunity,
    scan_project_opportunity_evidence,
)
from plugins.tuoguan_core.store import TuoguanStore
from plugins.tuoguan_core.tool_service import TuoguanToolService
from plugins.tuoguan_core.write_guard import authorized_system_write


NOW = datetime.fromisoformat("2026-08-23T21:30:00+08:00")


def _seed_store(tmp_path, *, student_count: int = 100, affected_count: int = 12, coverage_count: int = 70) -> TuoguanStore:
    store = TuoguanStore(tmp_path)
    students = {}
    for index in range(student_count):
        teacher = f"teacher-{index % 3 + 1}"
        students[f"学生{index:03d}"] = {
            "status": "active",
            "teacher_id": teacher,
            "program_ids": ["regular_tuoguan"],
        }
    staff = {
        f"teacher-{index}": {
            "role": "teacher",
            "status": "active",
            "program_ids": ["regular_tuoguan"],
        }
        for index in range(1, 4)
    }
    records = []
    for index in range(coverage_count):
        day_offset = index % 11
        affected = index < affected_count
        records.append({
            "id": f"record-{index}",
            "student_name": f"学生{index:03d}",
            "teacher_id": f"teacher-{index % 3 + 1}",
            "timestamp": (NOW - timedelta(days=day_offset)).isoformat(),
            "content": "数学计算基础薄弱，老师已引导订正，后续继续观察。" if affected else "今天完成作业，老师已检查，后续继续观察。",
            "record_types": ["academic_issue", "student_daily"] if affected else ["student_daily"],
            "tags": ["数学计算弱"] if affected else [],
            "record_evaluation": {"accepted": True},
        })
    store.write_json("students.json", students)
    store.write_json("staff.json", staff)
    store.write_json("records.json", records)
    store.write_json("tasks.json", [])
    store.write_json("wecom_whitelist.json", {
        "super_users": ["JinWenJie"],
        "allowed_users": ["CeShi"],
        "user_roles": {"JinWenJie": "boss", "CeShi": "teacher"},
    })
    store.write_json("teacher_wecom_map.json", {"金总": "JinWenJie", "李老师": "CeShi"})
    return store


def _math_bundle(store: TuoguanStore, *, now: datetime = NOW) -> dict:
    result = scan_project_opportunity_evidence(store, now=now, project_id="regular_tuoguan", limit=10)
    return next(item for item in result["bundles"] if item["dimension"] == "math_foundation")


def _judgement(*, complete: bool = True) -> dict:
    return {
        "worth_validating": True,
        "hypothesis": "多名学生存在共同的数学计算基础缺口。",
        "reasoning": "证据跨学生、日期和责任老师，值得先做小范围验证。",
        "missing_facts": ["抽样基线", "老师可交付方式", "家长真实需求"],
        "validation_plan": {
            "objective": "验证是否存在可交付的数学基础提升服务。" if complete else "",
            "method": "7天抽样评估并访谈责任老师。" if complete else "",
            "sample_scope": "抽样8至12名学生",
            "success_evidence": ["基线问题可复现", "老师能够稳定交付"] if complete else [],
            "estimated_days": 7,
            "requires_staff_contact": True,
        },
    }


def test_strong_internal_evidence_reaches_decision_pending(tmp_path):
    store = _seed_store(tmp_path)
    bundle = _math_bundle(store)
    assert bundle["strong_evidence"] is True
    assert bundle["coverage_rate"] >= 0.60
    assert bundle["affected_student_count"] == 12
    assert len(bundle["evidence_dates"]) >= 3
    assert bundle["teacher_count"] >= 2

    with authorized_system_write(
        store.data_dir,
        job_name="test_project_opportunity",
        allowed_files={PROJECT_OPPORTUNITY_EVENTS_FILE},
    ):
        result = record_project_opportunity_assessment(
            store,
            evidence_bundle=bundle,
            judgement=_judgement(),
            actor_user_id="system",
            operation_id="opportunity-test-1",
            now=NOW,
        )
    assert result["ok"] is True
    assert result["writeback_verified"] is True
    assert result["candidate"]["status"] == "decision_pending"
    visible = query_project_opportunities(store, now=NOW)
    assert visible["visible_count"] == 1
    assert visible["items"][0]["boundary"] == "待验证候选，不等于正式项目"


def test_duplicate_same_student_same_day_counts_once(tmp_path):
    store = _seed_store(tmp_path, student_count=20, affected_count=0, coverage_count=12)
    records = store.read_json("records.json", [])
    for index in range(20):
        records.append({
            "id": f"duplicate-{index}",
            "student_name": "学生000",
            "teacher_id": "teacher-1",
            "timestamp": NOW.isoformat(),
            "content": "数学计算薄弱，老师已指导，后续继续观察。",
            "record_types": ["academic_issue"],
            "tags": ["数学计算弱"],
            "record_evaluation": {"accepted": True},
        })
    store.write_json("records.json", records)
    bundle = _math_bundle(store)
    assert bundle["affected_student_count"] == 1
    assert bundle["strong_evidence"] is False


def test_positive_math_progress_does_not_become_weakness(tmp_path):
    store = _seed_store(tmp_path, student_count=20, affected_count=0, coverage_count=12)
    records = store.read_json("records.json", [])
    for index in range(8):
        records.append({
            "id": f"positive-{index}",
            "student_name": f"学生{index:03d}",
            "teacher_id": f"teacher-{index % 3 + 1}",
            "timestamp": (NOW - timedelta(days=index % 4)).isoformat(),
            "content": "数学进步明显，计算正确率提高，老师已表扬，后续继续巩固。",
            "record_types": ["positive_progress", "student_daily"],
            "tags": ["正向成长"],
            "record_evaluation": {"accepted": True},
        })
    store.write_json("records.json", records)
    bundle = _math_bundle(store)
    assert bundle["affected_student_count"] == 0
    assert bundle["strong_evidence"] is False


def test_low_coverage_stays_internal_and_out_of_dashboard(tmp_path):
    store = _seed_store(tmp_path, student_count=100, affected_count=8, coverage_count=20)
    bundle = _math_bundle(store)
    assert bundle["gates"]["coverage"] is False
    with authorized_system_write(
        store.data_dir,
        job_name="test_project_opportunity",
        allowed_files={PROJECT_OPPORTUNITY_EVENTS_FILE},
    ):
        result = record_project_opportunity_assessment(
            store,
            evidence_bundle=bundle,
            judgement=_judgement(),
            actor_user_id="system",
            operation_id="opportunity-low-coverage",
            now=NOW,
        )
    assert result["candidate"]["status"] == "observing"
    assert query_project_opportunities(store, now=NOW)["visible_count"] == 0
    assert query_project_opportunities(store, now=NOW, include_internal=True)["visible_count"] == 1


def test_dismissed_candidate_obeys_30_day_cooldown(tmp_path):
    store = _seed_store(tmp_path)
    bundle = _math_bundle(store)
    with authorized_system_write(
        store.data_dir,
        job_name="test_project_opportunity",
        allowed_files={PROJECT_OPPORTUNITY_EVENTS_FILE},
    ):
        created = record_project_opportunity_assessment(
            store,
            evidence_bundle=bundle,
            judgement=_judgement(),
            actor_user_id="system",
            operation_id="create-before-dismiss",
            now=NOW,
        )
        dismissed = review_project_opportunity(
            store,
            opportunity_id=created["candidate"]["opportunity_id"],
            decision="dismiss",
            actor_user_id="JinWenJie",
            operation_id="dismiss-1",
            now=NOW,
        )
        repeated = record_project_opportunity_assessment(
            store,
            evidence_bundle=bundle,
            judgement=_judgement(),
            actor_user_id="system",
            operation_id="repeat-after-dismiss",
            now=NOW + timedelta(days=2),
        )
    assert dismissed["candidate"]["status"] == "dismissed"
    assert repeated["suppressed"] is True
    assert repeated["reason"] == "dismissed_cooldown_active"


def test_night_loop_writes_candidate_but_never_external_action(tmp_path):
    store = _seed_store(tmp_path)

    def decide(materials):
        bundle = next(
            item for item in materials["project_opportunity_evidence"]["bundles"]
            if item["dimension"] == "math_foundation"
        )
        return {
            "employee_summary": "小优已完成内部证据复核，没有执行外发。",
            "institution_understanding": "当前数据存在一个可验证的共性问题。",
            "goal_progress_view": "没有改变正式目标。",
            "project_opportunity_assessments": [{"bundle_id": bundle["bundle_id"], **_judgement()}],
            "project_opportunity_scan": {"status": "completed", "error": ""},
            "external_actions": [],
        }

    result = run_autonomous_employee_loop(store, now=NOW, decision_provider=decide, write_state=True)
    assert result["ok"] is True
    assert result["external_actions_taken"] == []
    assert any(item["kind"] == "project_opportunity_candidate" for item in result["writes"])
    second_materials = run_autonomous_employee_loop(
        store,
        now=NOW + timedelta(minutes=30),
        decision_provider=lambda materials: {
            "employee_summary": "小优复核到本晚机会扫描已经完成。",
            "project_opportunity_scan": {"status": "not_due", "error": ""},
        },
        write_state=False,
    )
    assert second_materials["materials_summary"].get("project_opportunity_scan_due_count", 0) == 0


def test_daily_report_mentions_new_candidate_without_claiming_launch(tmp_path):
    store = _seed_store(tmp_path)
    bundle = _math_bundle(store)
    with authorized_system_write(
        store.data_dir,
        job_name="test_project_opportunity",
        allowed_files={PROJECT_OPPORTUNITY_EVENTS_FILE},
    ):
        record_project_opportunity_assessment(
            store,
            evidence_bundle=bundle,
            judgement=_judgement(),
            actor_user_id="system",
            operation_id="opportunity-for-report",
            now=NOW,
        )
    report = build_daily_boss_report("evening", store=store, now=NOW)
    assert "新项目机会" in report["content"]
    assert "尚未立项" in report["content"]
    assert report["project_opportunity_id"]


def test_capability_surface_stays_at_23_tools():
    manifest = operation_manifest()
    assert manifest["model_visible_tool_count"] == 23
    assert "query_project_opportunities" in DOMAIN_OPERATIONS["learning"]
    assert "review_project_opportunity" in DOMAIN_OPERATIONS["learning"]


def test_tools_are_owner_only_and_review_has_verified_receipt(tmp_path):
    store = _seed_store(tmp_path)
    bundle = _math_bundle(store)
    with authorized_system_write(
        store.data_dir,
        job_name="test_project_opportunity",
        allowed_files={PROJECT_OPPORTUNITY_EVENTS_FILE},
    ):
        created = record_project_opportunity_assessment(
            store,
            evidence_bundle=bundle,
            judgement=_judgement(),
            actor_user_id="system",
            operation_id="tool-visible-opportunity",
            now=NOW,
        )
    boss = TuoguanToolService(store, platform="wecom_callback", user_id="JinWenJie", user_name="金总")
    teacher = TuoguanToolService(store, platform="wecom_callback", user_id="CeShi", user_name="李老师")
    assert boss.query_project_opportunities()["ok"] is True
    denied = teacher.query_project_opportunities()
    assert denied["ok"] is False
    assert denied["error"] == "permission_denied"
    reviewed = boss.review_project_opportunity(
        opportunity_id=created["candidate"]["opportunity_id"],
        decision="defer",
        operation_id="boss-defer-opportunity",
    )
    assert reviewed["ok"] is True
    assert reviewed["execution_receipt"]["writeback_verified"] is True
    assert reviewed["data"]["candidate"]["status"] == "deferred"


def test_boss_dashboard_uses_new_projection_and_empties_legacy_field(tmp_path):
    from plugins.tuoguan_core.dashboard_builder import build_dashboard_snapshot

    store = _seed_store(tmp_path)
    bundle = _math_bundle(store)
    with authorized_system_write(
        store.data_dir,
        job_name="test_project_opportunity",
        allowed_files={PROJECT_OPPORTUNITY_EVENTS_FILE},
    ):
        record_project_opportunity_assessment(
            store,
            evidence_bundle=bundle,
            judgement=_judgement(),
            actor_user_id="system",
            operation_id="dashboard-opportunity",
            now=NOW,
        )
    snapshot = build_dashboard_snapshot(store, now=NOW)
    boss = snapshot["boss_dashboard"]
    assert boss["opportunities"] == []
    assert boss["project_opportunities"]["visible_count"] == 1
    assert boss["project_opportunities"]["items"][0]["title"] == "数学基础提升"


def test_read_only_cli_hides_people_and_never_writes_candidate_ledger(tmp_path):
    store = _seed_store(tmp_path)
    root = Path(__file__).resolve().parents[3]
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "project_opportunity_scan.py"),
            "--data-dir",
            str(store.data_dir),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["mode"] == "read_only_dry_run"
    assert result["writes_performed"] is False
    assert result["strong_bundle_count"] >= 1
    assert "affected_student_names" not in completed.stdout
    assert "teacher_ids" not in completed.stdout
    assert "学生000" not in completed.stdout
    assert not (store.data_dir / PROJECT_OPPORTUNITY_EVENTS_FILE).exists()


def test_candidate_without_new_evidence_becomes_stale(tmp_path):
    store = _seed_store(tmp_path)
    old_now = NOW - timedelta(days=31)
    bundle = _math_bundle(store)
    bundle["latest_evidence_at"] = old_now.isoformat(timespec="seconds")
    with authorized_system_write(
        store.data_dir,
        job_name="test_project_opportunity",
        allowed_files={PROJECT_OPPORTUNITY_EVENTS_FILE},
    ):
        created = record_project_opportunity_assessment(
            store,
            evidence_bundle=bundle,
            judgement=_judgement(),
            actor_user_id="system",
            operation_id="old-opportunity",
            now=old_now,
        )
        stale = mark_stale_project_opportunities(store, operation_id="stale-scan", now=NOW)
    assert created["ok"] is True
    assert stale["ok"] is True
    assert stale["stale_count"] == 1
    result = query_project_opportunities(store, now=NOW, include_internal=True)
    assert result["items"][0]["status"] == "stale"
