from __future__ import annotations

import os
from pathlib import Path


def _identity_for_current_process():
    uid = os.getuid()
    gid = os.getgid()
    return uid, gid, {gid}


def test_release_home_gate_passes_for_service_owned_private_home(tmp_path, monkeypatch):
    from scripts import xiaoyou_release_home_gate as gate

    root = tmp_path / "hermes-youyi-candidate"
    home = root / "home"
    home.mkdir(parents=True)
    root.chmod(0o755)
    home.chmod(0o700)
    monkeypatch.setattr(gate, "_identity", lambda *_args: _identity_for_current_process())

    result = gate.inspect_release_home(
        release_root=root,
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is True
    assert result["home_mode"] == "0700"
    assert result["owner_ok"] is True
    assert result["group_ok"] is True
    assert result["traversal_ok"] is True
    assert result["secret_content_inspected"] is False


def test_release_home_gate_rejects_root_owned_style_home_for_service_user(tmp_path, monkeypatch):
    from scripts import xiaoyou_release_home_gate as gate

    root = tmp_path / "hermes-youyi-candidate"
    home = root / "home"
    home.mkdir(parents=True)
    root.chmod(0o755)
    home.chmod(0o700)
    current_uid = os.getuid()
    current_gid = os.getgid()
    monkeypatch.setattr(
        gate,
        "_identity",
        lambda *_args: (current_uid + 10000, current_gid + 10000, {current_gid + 10000}),
    )

    result = gate.inspect_release_home(
        release_root=root,
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is False
    assert result["error"] == "runtime_home_permission_gate_failed"
    assert result["owner_ok"] is False
    assert result["traversal_ok"] is False


def test_release_home_gate_rejects_overly_open_home(tmp_path, monkeypatch):
    from scripts import xiaoyou_release_home_gate as gate

    root = tmp_path / "hermes-youyi-candidate"
    home = root / "home"
    home.mkdir(parents=True)
    root.chmod(0o755)
    home.chmod(0o755)
    monkeypatch.setattr(gate, "_identity", lambda *_args: _identity_for_current_process())

    result = gate.inspect_release_home(
        release_root=root,
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is False
    assert result["mode_ok"] is False
    assert result["home_mode"] == "0755"


def test_release_home_gate_rejects_home_symlink(tmp_path, monkeypatch):
    from scripts import xiaoyou_release_home_gate as gate

    root = tmp_path / "hermes-youyi-candidate"
    target = tmp_path / "shared-home"
    root.mkdir()
    target.mkdir()
    (root / "home").symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(gate, "_identity", lambda *_args: _identity_for_current_process())

    result = gate.inspect_release_home(
        release_root=root,
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is False
    assert result["error"] == "runtime_home_must_not_be_symlink"


def test_repair_is_narrow_to_release_home_directory(tmp_path, monkeypatch):
    from scripts import xiaoyou_release_home_gate as gate

    root = tmp_path / "hermes-youyi-candidate"
    home = root / "home"
    home.mkdir(parents=True)
    root.chmod(0o755)
    home.chmod(0o755)
    preserved = home / "workspace-link-placeholder"
    preserved.write_text("do-not-touch\n", encoding="utf-8")
    before = preserved.read_bytes()

    monkeypatch.setattr(gate, "_identity", lambda *_args: _identity_for_current_process())
    observed = []
    monkeypatch.setattr(gate.os, "chown", lambda path, uid, gid: observed.append((Path(path), uid, gid)))

    result = gate.repair_release_home(
        release_root=root,
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is True
    assert result["repair_applied"] is True
    assert result["repair_scope"] == "release_home_directory_only"
    assert home.stat().st_mode & 0o777 == 0o700
    assert preserved.read_bytes() == before
    assert observed == [(home, os.getuid(), os.getgid())]
