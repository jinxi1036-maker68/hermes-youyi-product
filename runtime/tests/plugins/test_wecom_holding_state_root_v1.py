from __future__ import annotations

import os
from pathlib import Path


def _identity(monkeypatch, module):
    uid = os.geteuid()
    gid = os.getegid()
    monkeypatch.setattr(
        module,
        "_identity",
        lambda _user, _group: (uid, gid, {gid}),
    )
    return uid, gid


def test_verify_missing_state_root_fails_closed(tmp_path, monkeypatch):
    from scripts import xiaoyou_wecom_holding_state_root as gate

    _identity(monkeypatch, gate)
    parent = tmp_path / "var"
    parent.mkdir()
    state = parent / "wecom-holding"
    gateway = parent / "hermes-home"

    result = gate.inspect_state_root(
        state_root=state,
        gateway_home=gateway,
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is False
    assert "state_root_missing" in result["errors"]


def test_provision_requires_root(tmp_path, monkeypatch):
    from scripts import xiaoyou_wecom_holding_state_root as gate

    _identity(monkeypatch, gate)
    parent = tmp_path / "var"
    parent.mkdir()
    monkeypatch.setattr(gate.os, "geteuid", lambda: 12345)

    result = gate.provision_state_root(
        state_root=parent / "wecom-holding",
        gateway_home=parent / "hermes-home",
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is False
    assert result["error"] == "root_required_for_state_root_provisioning"
    assert not (parent / "wecom-holding").exists()


def test_provision_creates_only_exact_root_with_strict_mode(
    tmp_path,
    monkeypatch,
):
    from scripts import xiaoyou_wecom_holding_state_root as gate

    uid, gid = _identity(monkeypatch, gate)
    parent = tmp_path / "var"
    parent.mkdir()
    state = parent / "wecom-holding"
    gateway = parent / "hermes-home"
    monkeypatch.setattr(gate.os, "geteuid", lambda: 0)

    result = gate.provision_state_root(
        state_root=state,
        gateway_home=gateway,
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is True
    assert result["created"] is True
    assert result["parent_created"] is False
    assert result["recursive_owner_change"] is False
    assert state.is_dir()
    assert state.stat().st_uid == uid
    assert state.stat().st_gid == gid
    assert (state.stat().st_mode & 0o777) == 0o700


def test_provision_never_creates_missing_parent(tmp_path, monkeypatch):
    from scripts import xiaoyou_wecom_holding_state_root as gate

    _identity(monkeypatch, gate)
    parent = tmp_path / "missing-parent"
    state = parent / "wecom-holding"
    monkeypatch.setattr(gate.os, "geteuid", lambda: 0)

    result = gate.provision_state_root(
        state_root=state,
        gateway_home=tmp_path / "hermes-home",
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is False
    assert "state_root_parent_missing" in result["errors"]
    assert not parent.exists()


def test_state_root_must_be_disjoint_from_gateway_home(tmp_path, monkeypatch):
    from scripts import xiaoyou_wecom_holding_state_root as gate

    _identity(monkeypatch, gate)
    gateway = tmp_path / "hermes-home"
    gateway.mkdir()
    state = gateway / "state" / "wecom-holding"

    result = gate.inspect_state_root(
        state_root=state,
        gateway_home=gateway,
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is False
    assert "state_root_must_be_disjoint_from_gateway_home" in result["errors"]


def test_state_root_symlink_is_rejected(tmp_path, monkeypatch):
    from scripts import xiaoyou_wecom_holding_state_root as gate

    _identity(monkeypatch, gate)
    parent = tmp_path / "var"
    parent.mkdir()
    actual = tmp_path / "actual"
    actual.mkdir()
    state = parent / "wecom-holding"
    state.symlink_to(actual, target_is_directory=True)

    result = gate.inspect_state_root(
        state_root=state,
        gateway_home=parent / "hermes-home",
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is False
    assert "state_root_symlink_forbidden" in result["errors"]


def test_existing_child_identity_mismatch_is_not_recursively_repaired(
    tmp_path,
    monkeypatch,
):
    from scripts import xiaoyou_wecom_holding_state_root as gate

    uid, gid = _identity(monkeypatch, gate)
    parent = tmp_path / "var"
    parent.mkdir(mode=0o755)
    state = parent / "wecom-holding"
    state.mkdir(mode=0o700)
    child = state / "existing.db"
    child.write_bytes(b"x")
    monkeypatch.setattr(gate.os, "geteuid", lambda: 0)

    original_root = state.stat()
    original_child = child.stat()
    monkeypatch.setattr(
        gate,
        "_identity",
        lambda _user, _group: (uid + 1000, gid + 1000, {gid + 1000}),
    )

    chown_calls = []
    chmod_calls = []
    monkeypatch.setattr(
        gate.os,
        "chown",
        lambda path, new_uid, new_gid: chown_calls.append(
            (str(path), new_uid, new_gid)
        ),
    )
    monkeypatch.setattr(
        gate.os,
        "chmod",
        lambda path, mode: chmod_calls.append((str(path), mode)),
    )

    result = gate.provision_state_root(
        state_root=state,
        gateway_home=parent / "hermes-home",
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is False
    assert result["applied"] is False
    assert any(
        item.startswith("state_entry_identity_mismatch:existing.db")
        for item in result["errors"]
    )
    assert chown_calls == []
    assert chmod_calls == []
    assert state.stat().st_uid == original_root.st_uid
    assert state.stat().st_gid == original_root.st_gid
    assert child.stat().st_uid == original_child.st_uid
    assert child.stat().st_gid == original_child.st_gid


def test_untraversable_parent_blocks_before_creation(tmp_path, monkeypatch):
    from scripts import xiaoyou_wecom_holding_state_root as gate

    uid = os.geteuid()
    gid = os.getegid()
    parent = tmp_path / "private-parent"
    parent.mkdir(mode=0o700)
    state = parent / "wecom-holding"

    monkeypatch.setattr(gate.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        gate,
        "_identity",
        lambda _user, _group: (uid + 1000, gid + 1000, {gid + 1000}),
    )

    result = gate.provision_state_root(
        state_root=state,
        gateway_home=tmp_path / "hermes-home",
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is False
    assert result["applied"] is False
    assert result["error"] == "holding_state_root_parent_access_failed"
    assert not state.exists()
