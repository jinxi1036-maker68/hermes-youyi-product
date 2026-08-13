from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_release_package_is_versioned_and_tamper_evident(tmp_path):
    from scripts.xiaoyou_release_package import build_release, verify_release_directory

    result = build_release(
        root=ROOT,
        output_dir=tmp_path,
        hermes_version="0.20.0",
        model="agnes-2.5-flash",
        require_clean=False,
        source_commit="f" * 40,
    )

    assert result["ok"] is True
    release_root = Path(result["release_root"])
    verification = verify_release_directory(release_root)
    assert verification["ok"] is True
    assert verification["manifest"]["contains_business_data"] is False
    assert verification["manifest"]["contains_credentials"] is False
    target = release_root / "payload/runtime/plugins/tuoguan_core/__init__.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    failed = verify_release_directory(release_root)
    assert failed["ok"] is False
    assert any(item["error"] == "hash_mismatch" for item in failed["mismatches"])


def test_release_installer_blocks_unknown_production_modules(tmp_path):
    from scripts.xiaoyou_release_installer import plan_install
    from scripts.xiaoyou_release_package import build_release

    built = build_release(
        root=ROOT,
        output_dir=tmp_path / "dist",
        hermes_version="0.20.0",
        model="agnes-2.5-flash",
        require_clean=False,
        source_commit="e" * 40,
    )
    base = tmp_path / "base"
    runtime = base / "runtime/plugins/tuoguan_core"
    runtime.mkdir(parents=True)
    (runtime / "unknown_history_module.py").write_text("VALUE = 1\n", encoding="utf-8")

    plan = plan_install(release_root=Path(built["release_root"]), base=base)

    assert plan["ok"] is False
    assert plan["error"] == "unknown_existing_modules"
    assert plan["unknown_existing_file_count"] == 1
    assert plan["applied"] is False


def test_release_installer_rolls_back_partial_link_failure(tmp_path, monkeypatch):
    from scripts import xiaoyou_release_installer as installer
    from scripts.xiaoyou_release_package import build_release

    built = build_release(
        root=ROOT,
        output_dir=tmp_path / "dist",
        hermes_version="0.20.0",
        model="agnes-2.5-flash",
        require_clean=False,
        source_commit="d" * 40,
    )
    base = tmp_path / "base"
    core_runtime = base / "runtime/plugins/tuoguan_core"
    core_home = base / "home-proddata/plugins/tuoguan_core"
    wecom_runtime = base / "runtime/plugins/platforms/wecom"
    for path, filename in (
        (core_runtime, "__init__.py"),
        (core_home, "__init__.py"),
        (wecom_runtime, "callback_adapter.py"),
    ):
        path.mkdir(parents=True)
        (path / filename).write_text("original\n", encoding="utf-8")
    calls = {"count": 0}

    def fail_second_link(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("injected_link_failure")

    monkeypatch.setattr(installer.os, "symlink", fail_second_link)

    result = installer.install_release(release_root=Path(built["release_root"]), base=base, apply=True)

    assert result["ok"] is False
    assert result["error"] == "install_failed_rolled_back"
    assert result["rollback_succeeded"] is True
    assert (core_runtime / "__init__.py").read_text(encoding="utf-8") == "original\n"
    assert (core_home / "__init__.py").read_text(encoding="utf-8") == "original\n"


def test_sqlite_runtime_drill_checks_transaction_rollback_and_restore(tmp_path):
    from scripts.xiaoyou_sqlite_runtime_check import run_sqlite_drill

    result = run_sqlite_drill(work_dir=tmp_path, minimum=(0, 0, 0))

    assert result["ok"] is True
    assert result["transaction_ok"] is True
    assert result["rollback_ok"] is True
    assert result["backup_restore_ok"] is True
    assert result["integrity_check"] == "ok"
    assert result["production_databases_touched"] is False


def test_backup_restore_drill_is_hash_verified_and_ephemeral(tmp_path):
    from scripts.xiaoyou_backup_restore_drill import run_backup_restore_drill

    tenant = tmp_path / "tenant"
    (tenant / "config").mkdir(parents=True)
    (tenant / "data").mkdir()
    (tenant / "memory").mkdir()
    (tenant / "config/runtime.env").write_text("HERMES_TENANT_ID=demo\n", encoding="utf-8")
    (tenant / "data/tasks.json").write_text("[]\n", encoding="utf-8")
    (tenant / "memory/MEMORY.md").write_text("# Demo\n", encoding="utf-8")
    work = tmp_path / "drill"

    result = run_backup_restore_drill(source=tenant, work_dir=work, keep_artifacts=False)

    assert result["ok"] is True
    assert result["file_count"] == 3
    assert result["hash_mismatches"] == []
    assert result["production_modified"] is False
    assert result["offsite_backup"]["enabled"] is False
    assert not Path(result["archive"]).exists()
    assert json.loads(Path(result["report"]).read_text(encoding="utf-8"))["ok"] is True


def test_non_youyi_tenant_gate_covers_identity_task_proactive_report_and_dashboard():
    from scripts.xiaoyou_non_youyi_tenant_gate import run_gate

    result = run_gate()

    assert result["ok"] is True
    assert result["pollution_hits"] == []
    probe = result["capability_probe"]
    assert probe["tenant_id"] == "demo_tuoguan"
    assert probe["boss"]["role"] == "boss"
    assert probe["teacher"]["role"] == "teacher"
    assert probe["task_writeback_verified"] is True
    assert probe["proactive_authorization_count"] == 1
    assert probe["capability_domain_count"] == 12
    assert probe["health_read_only"] is True


def test_systemd_templates_use_version_neutral_current_path():
    stale: list[str] = []
    for path in (ROOT / "systemd").glob("*.service"):
        text = path.read_text(encoding="utf-8")
        if "hermes-youyi-upgrade-0.19.0" in text or "runtime.gateway.019.env" in text:
            stale.append(path.name)
    assert stale == []
