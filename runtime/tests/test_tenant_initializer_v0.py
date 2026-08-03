import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "tenant_initializer.py"
DEMO_PROFILE = ROOT / "work" / "commercialization" / "demo_tenant_profile.json"
FORBIDDEN = ("优益", "金总", "李老师", "JinWenJie", "youyi_tuoguan", "九月份续费率")


def run_initializer(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--tenant-profile",
            str(DEMO_PROFILE),
            "--platform-root",
            str(tmp_path),
            *extra,
        ],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )


def test_tenant_initializer_generates_clean_demo_tenant(tmp_path):
    result = run_initializer(tmp_path, "--force")
    assert result.returncode == 0, result.stderr

    tenant_root = tmp_path / "tenants" / "demo_tuoguan"
    assert tenant_root.exists()
    for rel in [
        "config/tenant_profile.json",
        "config/runtime.env",
        "data/wecom_whitelist.json",
        "data/teacher_wecom_map.json",
        "data/staff.json",
        "data/institution_operating_model.json",
        "data/academic_term_state.json",
        "memory/MEMORY.md",
        "skills/institution-facts/SKILL.md",
        "skills/institution-facts/references/institution-profile.md",
    ]:
        assert (tenant_root / rel).exists(), rel

    whitelist = json.loads((tenant_root / "data" / "wecom_whitelist.json").read_text(encoding="utf-8"))
    assert whitelist["super_users"] == ["demo_boss_001"]
    assert "demo_teacher_001" in whitelist["allowed_users"]

    staff = json.loads((tenant_root / "data" / "staff.json").read_text(encoding="utf-8"))
    assert staff["demo_boss_001"]["role"] == "boss"
    assert staff["demo_manager_001"]["role"] == "manager"
    assert staff["demo_teacher_001"]["role"] == "teacher"

    for ledger in [
        "hermes_work_items.jsonl",
        "wakeup_requests.jsonl",
        "business_events.jsonl",
        "action_executions.jsonl",
        "attention_threads.jsonl",
        "relationship_touch_candidates.jsonl",
        "daily_report_runs.jsonl",
        "hermes_employee_scorecard.jsonl",
        "value_progress_ledger.jsonl",
        "institution_fact_gap_events.jsonl",
        "industry_learning_candidates.jsonl",
        "agent_delegations.jsonl",
        "agent_delegation_results.jsonl",
    ]:
        path = tenant_root / "data" / ledger
        assert path.exists(), ledger
        assert path.read_text(encoding="utf-8") == ""

    reports = list((tenant_root / "reports" / "startup").glob("initialization-report-*.md"))
    assert len(reports) == 1
    report = reports[0].read_text(encoding="utf-8")
    assert "Tenant Initialization Report" in report
    assert "禁止携带旧机构标记：通过" in report

    output_text = "\n".join(path.read_text(encoding="utf-8") for path in tenant_root.rglob("*") if path.is_file())
    for marker in FORBIDDEN:
        assert marker not in output_text


def test_tenant_initializer_rejects_forbidden_profile(tmp_path):
    bad_profile = tmp_path / "bad_profile.json"
    data = json.loads(DEMO_PROFILE.read_text(encoding="utf-8"))
    data["tenant"]["institution_name"] = "优益旧资料"
    bad_profile.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--tenant-profile",
            str(bad_profile),
            "--platform-root",
            str(tmp_path / "out"),
        ],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert not (tmp_path / "out" / "tenants").exists()
