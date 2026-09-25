from __future__ import annotations

import os
from pathlib import Path
import sqlite3


def _release(tmp_path: Path) -> Path:
    release = tmp_path / "releases" / "candidate"
    release.mkdir(parents=True)
    return release


def _source_home(tmp_path: Path) -> Path:
    home = tmp_path / "old-hermes-home"
    (home / "sessions").mkdir(parents=True)
    (home / "cron").mkdir()
    (home / "logs").mkdir()
    (home / "sessions" / "sessions.json").write_text('{"ok": true}\n', encoding="utf-8")
    (home / "cron" / "jobs.json").write_text('[]\n', encoding="utf-8")
    (home / "logs" / "agent.log").write_text("line\n", encoding="utf-8")
    (home / "config.yaml").write_text("model: demo\n", encoding="utf-8")
    db = sqlite3.connect(home / "state.db")
    db.execute("create table demo (id integer primary key, value text)")
    db.execute("insert into demo(value) values ('kept')")
    db.commit()
    db.close()
    return home


def test_runtime_home_inventory_blocks_unapproved_external_symlink(tmp_path):
    from scripts.xiaoyou_runtime_home_migration import inventory_runtime_home

    home = _source_home(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "other.json").write_text("{}\n", encoding="utf-8")
    (home / "mystery").symlink_to(outside, target_is_directory=True)

    result = inventory_runtime_home(source_home=home)

    assert result["ok"] is False
    assert any(error.startswith("external_symlink_requires_exclusion:mystery") for error in result["errors"])


def test_runtime_home_inventory_excludes_workspace_external_state(tmp_path):
    from scripts.xiaoyou_runtime_home_migration import inventory_runtime_home

    home = _source_home(tmp_path)
    workspace = tmp_path / "institution-workspace"
    workspace.mkdir()
    (workspace / "business.json").write_text("{}\n", encoding="utf-8")
    (home / "workspace").symlink_to(workspace, target_is_directory=True)

    result = inventory_runtime_home(source_home=home)

    assert result["ok"] is True
    assert "workspace" in result["excluded_top_level"]
    assert not any(row["logical"].startswith("workspace") for row in result["entries"])


def test_seed_migration_copies_core_state_and_sqlite_without_mutating_source(tmp_path, monkeypatch):
    from scripts import xiaoyou_runtime_home_migration as migration

    release = _release(tmp_path)
    source = _source_home(tmp_path)
    target = tmp_path / "persistent-hermes-home"
    monkeypatch.setattr(migration, "_service_uid", lambda _user: os.geteuid())

    before_sessions = (source / "sessions" / "sessions.json").read_bytes()
    before_cron = (source / "cron" / "jobs.json").read_bytes()

    result = migration.migrate_runtime_home(
        mode="seed",
        release_root=release,
        source_home=source,
        target_home=target,
        service_user="svc",
    )

    assert result["ok"] is True
    assert target.is_dir()
    assert not target.is_symlink()
    assert (target / "sessions" / "sessions.json").read_bytes() == before_sessions
    assert (target / "cron" / "jobs.json").read_bytes() == before_cron
    assert (source / "sessions" / "sessions.json").read_bytes() == before_sessions
    assert (source / "cron" / "jobs.json").read_bytes() == before_cron

    db = sqlite3.connect(target / "state.db")
    try:
        assert db.execute("select value from demo").fetchone()[0] == "kept"
        assert db.execute("pragma integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()

    verify = migration.verify_runtime_home(source_home=source, target_home=target)
    assert verify["ok"] is True


def test_internal_symlink_is_dereferenced_into_direct_target_state(tmp_path, monkeypatch):
    from scripts import xiaoyou_runtime_home_migration as migration

    release = _release(tmp_path)
    source = tmp_path / "old-hermes-home"
    shared = source / "_state" / "sessions"
    shared.mkdir(parents=True)
    (shared / "sessions.json").write_text("{}\n", encoding="utf-8")
    (source / "sessions").symlink_to(shared, target_is_directory=True)
    target = tmp_path / "persistent-hermes-home"
    monkeypatch.setattr(migration, "_service_uid", lambda _user: os.geteuid())

    result = migration.migrate_runtime_home(
        mode="seed",
        release_root=release,
        source_home=source,
        target_home=target,
        service_user="svc",
    )

    assert result["ok"] is True
    assert (target / "sessions").is_dir()
    assert not (target / "sessions").is_symlink()
    assert (target / "sessions" / "sessions.json").is_file()


def test_finalize_requires_gateway_stop_and_prunes_stale_target_state(tmp_path, monkeypatch):
    from scripts import xiaoyou_runtime_home_migration as migration

    release = _release(tmp_path)
    source = _source_home(tmp_path)
    target = tmp_path / "persistent-hermes-home"
    monkeypatch.setattr(migration, "_service_uid", lambda _user: os.geteuid())

    seed = migration.migrate_runtime_home(
        mode="seed",
        release_root=release,
        source_home=source,
        target_home=target,
        service_user="svc",
    )
    assert seed["ok"] is True
    (target / "stale.txt").write_text("stale\n", encoding="utf-8")

    blocked = migration.migrate_runtime_home(
        mode="finalize",
        release_root=release,
        source_home=source,
        target_home=target,
        service_user="svc",
        gateway_stopped_confirmed=False,
    )
    assert blocked["ok"] is False
    assert blocked["error"] == "gateway_stopped_confirmation_required"
    assert (target / "stale.txt").exists()

    final = migration.migrate_runtime_home(
        mode="finalize",
        release_root=release,
        source_home=source,
        target_home=target,
        service_user="svc",
        gateway_stopped_confirmed=True,
    )
    assert final["ok"] is True
    assert not (target / "stale.txt").exists()
    assert source.is_dir()
    assert (source / "state.db").is_file()
