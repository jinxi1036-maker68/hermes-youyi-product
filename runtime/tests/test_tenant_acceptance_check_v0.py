import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INITIALIZER = ROOT / "scripts" / "tenant_initializer.py"
ACCEPTANCE = ROOT / "scripts" / "tenant_acceptance_check.py"
DEMO_PROFILE = ROOT / "work" / "commercialization" / "demo_tenant_profile.json"


def generate_demo(tmp_path: Path) -> Path:
    result = subprocess.run(
        [
            sys.executable,
            str(INITIALIZER),
            "--tenant-profile",
            str(DEMO_PROFILE),
            "--platform-root",
            str(tmp_path),
            "--force",
        ],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return tmp_path / "tenants" / "demo_tuoguan"


def run_acceptance(tenant_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ACCEPTANCE),
            "--tenant-root",
            str(tenant_root),
        ],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
    )


def test_acceptance_passes_for_generated_demo(tmp_path):
    tenant_root = generate_demo(tmp_path)
    result = run_acceptance(tenant_root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STATUS:PASS" in result.stdout
    reports = list((tenant_root / "reports" / "startup").glob("tenant-acceptance-report-*.md"))
    assert reports
    report = reports[-1].read_text(encoding="utf-8")
    assert "status: `PASS`" in report


def test_acceptance_fails_when_core_file_missing(tmp_path):
    tenant_root = generate_demo(tmp_path)
    (tenant_root / "data" / "wecom_whitelist.json").unlink()
    result = run_acceptance(tenant_root)
    assert result.returncode != 0
    assert "缺少核心 JSON 文件" in result.stdout


def test_acceptance_fails_on_forbidden_marker(tmp_path):
    tenant_root = generate_demo(tmp_path)
    memory = tenant_root / "memory" / "MEMORY.md"
    memory.write_text(memory.read_text(encoding="utf-8") + "\n优益旧资料\n", encoding="utf-8")
    result = run_acceptance(tenant_root)
    assert result.returncode != 0
    assert "禁止携带旧机构标记" in result.stdout


def test_acceptance_fails_on_non_empty_ledger(tmp_path):
    tenant_root = generate_demo(tmp_path)
    (tenant_root / "data" / "hermes_work_items.jsonl").write_text('{"x":1}\n', encoding="utf-8")
    result = run_acceptance(tenant_root)
    assert result.returncode != 0
    assert "运行账本不是空白初始化" in result.stdout


def test_acceptance_fails_on_plain_secret_or_parent_auto_send(tmp_path):
    tenant_root = generate_demo(tmp_path)
    profile_path = tenant_root / "config" / "tenant_profile.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["wecom"]["secret"] = "plain-secret"
    profile["parent_communication_policy"]["auto_send_parent_messages"] = True
    profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    result = run_acceptance(tenant_root)
    assert result.returncode != 0
    assert "企业微信明文密钥字段禁止出现" in result.stdout
    assert "家长自动发送必须关闭" in result.stdout
