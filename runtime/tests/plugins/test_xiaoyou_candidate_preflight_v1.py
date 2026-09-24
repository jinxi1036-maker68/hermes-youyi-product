from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace


def _candidate(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "candidate"
    home = root / "home"
    home.mkdir(parents=True)
    home.chmod(0o700)
    return root, home


def test_candidate_preflight_sets_candidate_home_and_runs_as_service_identity(tmp_path, monkeypatch):
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
        command=["python", "-c", "print('ok')"],
        service_user="svc",
        service_group="svc",
        runner=runner,
    )

    assert result["ok"] is True
    assert observed["kwargs"]["env"]["HOME"] == str(home)
    assert observed["kwargs"]["env"]["HERMES_HOME"] == str(home)
    assert "preexec_fn" not in observed["kwargs"]


def test_candidate_preflight_root_path_installs_privilege_drop(tmp_path, monkeypatch):
    from scripts import xiaoyou_candidate_preflight as preflight

    root, _home = _candidate(tmp_path)
    monkeypatch.setattr(preflight, "_identity", lambda *_args: (12345, 23456))
    monkeypatch.setattr(preflight.os, "geteuid", lambda: 0)
    observed = {}

    def runner(command, **kwargs):
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    result = preflight.run_candidate_preflight(
        release_root=root,
        command=["python", "-V"],
        service_user="svc",
        service_group="svc",
        runner=runner,
    )

    assert result["ok"] is True
    assert callable(observed["kwargs"]["preexec_fn"])


def test_candidate_preflight_refuses_wrong_non_root_executor(tmp_path, monkeypatch):
    from scripts import xiaoyou_candidate_preflight as preflight

    root, _home = _candidate(tmp_path)
    monkeypatch.setattr(preflight, "_identity", lambda *_args: (12345, 23456))
    monkeypatch.setattr(preflight.os, "geteuid", lambda: 99999)

    result = preflight.run_candidate_preflight(
        release_root=root,
        command=["python", "-V"],
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is False
    assert result["error"] == "executor_cannot_assume_service_identity"


def test_candidate_preflight_refuses_cwd_outside_release(tmp_path, monkeypatch):
    from scripts import xiaoyou_candidate_preflight as preflight

    root, _home = _candidate(tmp_path)
    monkeypatch.setattr(
        preflight,
        "_identity",
        lambda *_args: (os.geteuid(), os.getegid()),
    )
    outside = tmp_path / "outside"
    outside.mkdir()

    result = preflight.run_candidate_preflight(
        release_root=root,
        command=["python", "-V"],
        service_user="svc",
        service_group="svc",
        cwd=outside,
    )

    assert result["ok"] is False
    assert result["error"] == "cwd_outside_release"
