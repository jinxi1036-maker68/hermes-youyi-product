from __future__ import annotations

import os
from pathlib import Path


def _identity_for_current_process():
    uid = os.getuid()
    gid = os.getgid()
    return uid, gid, {gid}


def _topology(tmp_path: Path) -> tuple[Path, Path]:
    release = tmp_path / "candidate-release"
    release.mkdir()
    home = tmp_path / "persistent-hermes-home"
    home.mkdir()
    release.chmod(0o755)
    home.chmod(0o700)
    return release, home


def test_runtime_home_gate_passes_for_external_service_owned_home(tmp_path, monkeypatch):
    from scripts import xiaoyou_release_home_gate as gate

    release, home = _topology(tmp_path)
    monkeypatch.setattr(gate, "_identity", lambda *_args: _identity_for_current_process())

    result = gate.inspect_runtime_home(
        release_root=release,
        runtime_home=home,
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is True
    assert result["home_mode"] == "0700"
    assert result["owner_ok"] is True
    assert result["group_ok"] is True
    assert result["traversal_ok"] is True
    assert result["top_level_symlinks_ok"] is True
    assert result["secret_content_inspected"] is False


def test_runtime_home_gate_rejects_release_local_home(tmp_path, monkeypatch):
    from scripts import xiaoyou_release_home_gate as gate

    release = tmp_path / "candidate-release"
    home = release / "home"
    home.mkdir(parents=True)
    monkeypatch.setattr(gate, "_identity", lambda *_args: _identity_for_current_process())

    result = gate.inspect_runtime_home(
        release_root=release,
        runtime_home=home,
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is False
    assert result["error"] == "runtime_topology_invalid"
    assert "runtime_home_must_be_version_neutral" in result["topology"]["errors"]


def test_runtime_home_gate_rejects_top_level_symlink_to_old_release(tmp_path, monkeypatch):
    from scripts import xiaoyou_release_home_gate as gate

    release, home = _topology(tmp_path)
    old = tmp_path / "old-release-home"
    sessions = old / "sessions"
    sessions.mkdir(parents=True)
    (home / "sessions").symlink_to(sessions, target_is_directory=True)
    monkeypatch.setattr(gate, "_identity", lambda *_args: _identity_for_current_process())

    result = gate.inspect_runtime_home(
        release_root=release,
        runtime_home=home,
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is False
    assert result["top_level_symlinks_ok"] is False
    assert str(home / "sessions") in result["top_level_symlinks"]


def test_runtime_home_gate_checks_sessions_cron_state_and_logs(tmp_path, monkeypatch):
    from scripts import xiaoyou_release_home_gate as gate

    release, home = _topology(tmp_path)
    sessions = home / "sessions"
    cron = home / "cron"
    logs = home / "logs"
    sessions.mkdir()
    cron.mkdir()
    logs.mkdir()
    (sessions / "sessions.json").write_text("{}\n", encoding="utf-8")
    (cron / "jobs.json").write_text("[]\n", encoding="utf-8")
    (home / "state.db").write_bytes(b"fixture")
    for path in (sessions, cron, logs):
        path.chmod(0o700)
    for path in (sessions / "sessions.json", cron / "jobs.json", home / "state.db"):
        path.chmod(0o600)
    monkeypatch.setattr(gate, "_identity", lambda *_args: _identity_for_current_process())

    result = gate.inspect_runtime_home(
        release_root=release,
        runtime_home=home,
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is True
    assert result["mutable_state_ok"] is True
    assert result["critical_state_ok"] is True


def test_runtime_home_gate_rejects_unwritable_mutable_state(tmp_path, monkeypatch):
    from scripts import xiaoyou_release_home_gate as gate

    release, home = _topology(tmp_path)
    sessions = home / "sessions"
    sessions.mkdir()
    state = sessions / "sessions.json"
    state.write_text("{}\n", encoding="utf-8")
    sessions.chmod(0o500)
    state.chmod(0o400)
    monkeypatch.setattr(gate, "_identity", lambda *_args: _identity_for_current_process())

    result = gate.inspect_runtime_home(
        release_root=release,
        runtime_home=home,
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is False
    assert result["mutable_state_ok"] is False


def test_runtime_home_gate_rejects_overly_open_home(tmp_path, monkeypatch):
    from scripts import xiaoyou_release_home_gate as gate

    release, home = _topology(tmp_path)
    home.chmod(0o755)
    monkeypatch.setattr(gate, "_identity", lambda *_args: _identity_for_current_process())

    result = gate.inspect_runtime_home(
        release_root=release,
        runtime_home=home,
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is False
    assert result["mode_ok"] is False
    assert result["home_mode"] == "0755"


def test_runtime_home_repair_is_narrow_to_home_directory(tmp_path, monkeypatch):
    from scripts import xiaoyou_release_home_gate as gate

    release = tmp_path / "candidate-release"
    release.mkdir()
    home = tmp_path / "persistent-hermes-home"
    home.mkdir()
    home.chmod(0o755)
    preserved = home / "config.yaml"
    preserved.write_text("model: demo\n", encoding="utf-8")
    before = preserved.read_bytes()

    monkeypatch.setattr(gate, "_identity", lambda *_args: _identity_for_current_process())
    observed = []
    monkeypatch.setattr(gate.os, "chown", lambda path, uid, gid: observed.append((Path(path), uid, gid)))

    result = gate.repair_runtime_home(
        release_root=release,
        runtime_home=home,
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is True
    assert result["repair_applied"] is True
    assert result["repair_scope"] == "persistent_runtime_home_directory_only"
    assert home.stat().st_mode & 0o777 == 0o700
    assert preserved.read_bytes() == before
    assert observed == [(home.resolve(), os.getuid(), os.getgid())]


def test_runtime_home_gate_rejects_unwritable_log_file(tmp_path, monkeypatch):
    from scripts import xiaoyou_release_home_gate as gate

    release, home = _topology(tmp_path)
    logs = home / "logs"
    logs.mkdir()
    agent_log = logs / "agent.log"
    agent_log.write_text("existing\n", encoding="utf-8")
    logs.chmod(0o700)
    agent_log.chmod(0o400)
    monkeypatch.setattr(gate, "_identity", lambda *_args: _identity_for_current_process())

    result = gate.inspect_runtime_home(
        release_root=release,
        runtime_home=home,
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is False
    assert result["mutable_state_ok"] is False
    assert any(
        Path(row["path"]).name == "agent.log" and not row["ok"]
        for row in result["mutable_state"]
        if row.get("exists")
    )
