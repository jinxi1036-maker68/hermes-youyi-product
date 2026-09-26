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
    assert (release_root / "payload/runtime/plugins/platforms/wecom/http_policy.py").is_file()
    cache = release_root / "payload/scripts/__pycache__/generated.cpython-311.pyc"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b"runtime cache")
    assert verify_release_directory(release_root)["ok"] is True
    target = release_root / "payload/runtime/plugins/tuoguan_core/__init__.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    failed = verify_release_directory(release_root)
    assert failed["ok"] is False
    assert any(item["error"] == "hash_mismatch" for item in failed["mismatches"])


def test_wecom_plugin_doctor_loads_packaged_transport_policy():
    from hermes_cli.plugin_dev import doctor_plugin

    report = doctor_plugin(ROOT / "runtime/plugins/platforms/wecom")

    assert report.ok, report.format_text()


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
    scripts_target = next(item for item in plan["targets"] if item["label"] == "scripts")
    assert scripts_target["source_relative"] == "scripts"


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
    assert result["fts5_ok"] is True
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


def test_release_installer_never_targets_persistent_home(tmp_path):
    from scripts.xiaoyou_release_installer import _targets

    base = tmp_path / "release-root"
    targets = _targets(base)

    assert targets
    for _label, target, _source in targets:
        relative = target.relative_to(base)
        assert "home-proddata" not in relative.parts


def test_release_package_excludes_local_hermes_core_shims(tmp_path):
    from scripts.xiaoyou_release_package import (
        PRODUCTION_FORBIDDEN_OVERLAY_PATHS,
        build_release,
    )

    result = build_release(
        root=ROOT,
        output_dir=tmp_path,
        hermes_version="0.21.0",
        model="agnes-2.5-flash",
        require_clean=False,
        source_commit="c" * 40,
    )

    release_root = Path(result["release_root"])
    manifest = json.loads((release_root / "release_manifest.json").read_text(encoding="utf-8"))
    packaged = {row["path"] for row in manifest["files"]}

    assert "runtime/hermes_constants.py" in PRODUCTION_FORBIDDEN_OVERLAY_PATHS
    assert "runtime/hermes_constants.py" not in packaged
    assert manifest["production_overlay_policy"] == "allowlisted_xiaoyou_payload_only_no_core_shadow"
    assert "runtime/hermes_constants.py" in manifest["production_forbidden_overlay_paths"]


def test_release_package_contains_wecom_bootstrap_holding_assets(tmp_path):
    from scripts.xiaoyou_release_package import build_release

    result = build_release(
        root=ROOT,
        output_dir=tmp_path,
        hermes_version="0.21.0",
        model="agnes-2.5-flash",
        require_clean=False,
        source_commit="b" * 40,
    )

    release_root = Path(result["release_root"])
    manifest = json.loads((release_root / "release_manifest.json").read_text(encoding="utf-8"))
    packaged = {row["path"] for row in manifest["files"]}

    required = {
        "scripts/xiaoyou_wecom_holding_bridge.py",
        "deploy/config/wecom-holding-bridge.env.example",
        "deploy/systemd/xiaoyou-wecom-holding-bridge.service.example",
        "deploy/nginx/wecom-callback-holding.location.example",
        "work/commercialization/maintenance_package_v0/wecom_bootstrap_holding_bridge_v1.md",
    }

    assert required <= packaged
    assert manifest["contains_business_data"] is False
    assert manifest["contains_credentials"] is False
