from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace


def _candidate(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "candidate-release"
    root.mkdir()
    home = tmp_path / "persistent-hermes-home"
    home.mkdir()
    home.chmod(0o700)
    return root, home


def test_candidate_preflight_binds_external_home_and_runs_as_service_identity(tmp_path, monkeypatch):
    from scripts import xiaoyou_candidate_preflight as preflight

    root, home = _candidate(tmp_path)
    current_uid = os.geteuid()
    current_gid = os.getegid()
    monkeypatch.setattr(preflight, "_identity", lambda *_args: (current_uid, current_gid))
    observed = {}

    def runner(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    result = preflight.run_candidate_preflight(
        release_root=root,
        runtime_home=home,
        command=["python", "-c", "print('ok')"],
        service_user="svc",
        service_group="svc",
        runner=runner,
    )

    assert result["ok"] is True
    assert observed["kwargs"]["env"]["HOME"] == str(home.resolve())
    assert observed["kwargs"]["env"]["HERMES_HOME"] == str(home.resolve())
    assert result["runtime_model"] == "version_neutral_persistent_home"
    assert "preexec_fn" not in observed["kwargs"]


def test_candidate_preflight_root_path_installs_privilege_drop(tmp_path, monkeypatch):
    from scripts import xiaoyou_candidate_preflight as preflight

    root, home = _candidate(tmp_path)
    monkeypatch.setattr(preflight, "_identity", lambda *_args: (12345, 23456))
    monkeypatch.setattr(preflight.os, "geteuid", lambda: 0)
    observed = {}

    def runner(command, **kwargs):
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    result = preflight.run_candidate_preflight(
        release_root=root,
        runtime_home=home,
        command=["python", "-V"],
        service_user="svc",
        service_group="svc",
        runner=runner,
    )

    assert result["ok"] is True
    assert callable(observed["kwargs"]["preexec_fn"])


def test_candidate_preflight_refuses_wrong_non_root_executor(tmp_path, monkeypatch):
    from scripts import xiaoyou_candidate_preflight as preflight

    root, home = _candidate(tmp_path)
    monkeypatch.setattr(preflight, "_identity", lambda *_args: (12345, 23456))
    monkeypatch.setattr(preflight.os, "geteuid", lambda: 99999)

    result = preflight.run_candidate_preflight(
        release_root=root,
        runtime_home=home,
        command=["python", "-V"],
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is False
    assert result["error"] == "executor_cannot_assume_service_identity"


def test_candidate_preflight_refuses_cwd_outside_release(tmp_path, monkeypatch):
    from scripts import xiaoyou_candidate_preflight as preflight

    root, home = _candidate(tmp_path)
    monkeypatch.setattr(
        preflight,
        "_identity",
        lambda *_args: (os.geteuid(), os.getegid()),
    )
    outside = tmp_path / "outside"
    outside.mkdir()

    result = preflight.run_candidate_preflight(
        release_root=root,
        runtime_home=home,
        command=["python", "-V"],
        service_user="svc",
        service_group="svc",
        cwd=outside,
    )

    assert result["ok"] is False
    assert result["error"] == "cwd_outside_release"


def test_candidate_preflight_refuses_release_local_home(tmp_path, monkeypatch):
    from scripts import xiaoyou_candidate_preflight as preflight

    root = tmp_path / "candidate-release"
    home = root / "home"
    home.mkdir(parents=True)
    monkeypatch.setattr(
        preflight,
        "_identity",
        lambda *_args: (os.geteuid(), os.getegid()),
    )

    result = preflight.run_candidate_preflight(
        release_root=root,
        runtime_home=home,
        command=["python", "-V"],
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is False
    assert result["error"] == "runtime_topology_invalid"
    assert "runtime_home_must_be_version_neutral" in result["topology"]["errors"]
