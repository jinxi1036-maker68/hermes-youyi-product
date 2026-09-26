from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import socket


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


def test_runtime_home_inventory_excludes_release_code_directories(tmp_path):
    from scripts.xiaoyou_runtime_home_migration import inventory_runtime_home

    home = _source_home(tmp_path)
    (home / "plugins").mkdir()
    (home / "plugins" / "legacy_plugin.py").write_text("VALUE = 1\n", encoding="utf-8")
    (home / "skills").mkdir()
    (home / "skills" / "legacy_skill.txt").write_text("legacy\n", encoding="utf-8")

    result = inventory_runtime_home(source_home=home)

    assert result["ok"] is True
    assert "plugins" in result["excluded_top_level"]
    assert "skills" in result["excluded_top_level"]
    assert not any(row["logical"].startswith("plugins") for row in result["entries"])
    assert not any(row["logical"].startswith("skills") for row in result["entries"])


def test_runtime_home_verify_requires_state_database_when_source_has_one(tmp_path):
    from scripts.xiaoyou_runtime_home_migration import verify_runtime_home

    source = _source_home(tmp_path)
    target = tmp_path / "persistent-hermes-home"
    target.mkdir()
    (target / "sessions").mkdir()
    (target / "sessions" / "sessions.json").write_bytes(
        (source / "sessions" / "sessions.json").read_bytes()
    )
    (target / "cron").mkdir()
    (target / "cron" / "jobs.json").write_bytes(
        (source / "cron" / "jobs.json").read_bytes()
    )
    (target / "logs").mkdir()
    (target / "logs" / "agent.log").write_bytes(
        (source / "logs" / "agent.log").read_bytes()
    )
    (target / "config.yaml").write_bytes((source / "config.yaml").read_bytes())

    result = verify_runtime_home(source_home=source, target_home=target)

    assert result["ok"] is False
    assert {"path": "state.db", "error": "target_missing"} in result["mismatches"]


def test_runtime_home_inventory_skips_unix_socket_as_ephemeral_state(tmp_path):
    from scripts.xiaoyou_runtime_home_migration import inventory_runtime_home

    home = _source_home(tmp_path)
    state_dir = home / "state"
    state_dir.mkdir()
    socket_path = state_dir / "gateway.loop-tick.fixture.sock"

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(socket_path))
        result = inventory_runtime_home(source_home=home)
    finally:
        sock.close()

    assert result["ok"] is True
    assert result["skipped_ephemeral_node_count"] == 1
    assert result["skipped_ephemeral_nodes"] == [{
        "logical": "state/gateway.loop-tick.fixture.sock",
        "source": str(socket_path.resolve()),
        "kind": "unix_socket",
    }]
    assert not any(
        row["logical"] == "state/gateway.loop-tick.fixture.sock"
        for row in result["entries"]
    )


def test_runtime_home_seed_does_not_copy_unix_socket(tmp_path, monkeypatch):
    from scripts import xiaoyou_runtime_home_migration as migration

    release = _release(tmp_path)
    source = _source_home(tmp_path)
    state_dir = source / "state"
    state_dir.mkdir()
    socket_path = state_dir / "gateway.loop-tick.fixture.sock"
    target = tmp_path / "persistent-hermes-home"
    monkeypatch.setattr(migration, "_service_uid", lambda _user: os.geteuid())

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(socket_path))
        result = migration.migrate_runtime_home(
            mode="seed",
            release_root=release,
            source_home=source,
            target_home=target,
            service_user="svc",
        )
    finally:
        sock.close()

    assert result["ok"] is True
    assert not (target / "state" / "gateway.loop-tick.fixture.sock").exists()


def test_runtime_home_inventory_still_blocks_unknown_special_node(tmp_path):
    from scripts.xiaoyou_runtime_home_migration import inventory_runtime_home

    if not hasattr(os, "mkfifo"):
        return

    home = _source_home(tmp_path)
    special = home / "state"
    special.mkdir()
    fifo = special / "unexpected.pipe"
    os.mkfifo(fifo)

    result = inventory_runtime_home(source_home=home)

    assert result["ok"] is False
    assert "unsupported_runtime_home_node:state/unexpected.pipe" in result["errors"]



def test_runtime_home_finalize_cannot_prune_external_holding_state(
    tmp_path,
    monkeypatch,
):
    from plugins.platforms.wecom import holding_bridge as hb
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

    monkeypatch.setenv("HERMES_HOME", str(target))
    monkeypatch.delenv("XIAOYOU_WECOM_HOLDING_STATE_ROOT", raising=False)
    monkeypatch.delenv("XIAOYOU_WECOM_HOLDING_DB", raising=False)
    monkeypatch.delenv("XIAOYOU_WECOM_HOLDING_MODE_FILE", raising=False)

    bridge = hb.build_bridge_from_env()
    expected_root = target.parent / hb.DEFAULT_STATE_DIR_NAME
    assert bridge.store.path.parent == expected_root
    assert expected_root != target
    assert not expected_root.is_relative_to(target)

    held = hb.make_held_request(
        method="POST",
        raw_path=(
            "/wecom/callback?msg_signature=sig"
            "&timestamp=100&nonce=n"
        ),
        headers={"Content-Type": "text/xml"},
        body=b"<xml><Encrypt>held</Encrypt></xml>",
    )
    staged = bridge.store.persist_pending(held)
    assert staged["created"] is True
    bridge.mode_gate.path.parent.mkdir(parents=True, exist_ok=True)
    bridge.mode_gate.path.write_text(
        json.dumps(
            {
                "schema_version": hb.MODE_SCHEMA_VERSION,
                "state": hb.HOLD_STATE,
            }
        ),
        encoding="utf-8",
    )
    db_before = bridge.store.path.read_bytes()
    mode_before = bridge.mode_gate.path.read_bytes()

    final = migration.migrate_runtime_home(
        mode="finalize",
        release_root=release,
        source_home=source,
        target_home=target,
        service_user="svc",
        gateway_stopped_confirmed=True,
    )

    assert final["ok"] is True
    assert bridge.store.path.read_bytes() == db_before
    assert bridge.mode_gate.path.read_bytes() == mode_before
    assert hb.HoldingStore(bridge.store.path).counts()["pending"] == 1
