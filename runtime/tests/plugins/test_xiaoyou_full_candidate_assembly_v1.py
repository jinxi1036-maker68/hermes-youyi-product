from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace


def _core_source(tmp_path: Path) -> Path:
    core = tmp_path / "core"
    (core / "hermes_cli").mkdir(parents=True)
    (core / "hermes_cli/__init__.py").write_text("# core\n", encoding="utf-8")
    (core / "hermes_constants.py").write_text("VALUE = 1\n", encoding="utf-8")
    return core


def _release(tmp_path: Path) -> Path:
    release = tmp_path / "release"
    release.mkdir()
    return release


def test_full_candidate_plan_rejects_reused_candidate_root(tmp_path, monkeypatch):
    from scripts import xiaoyou_full_candidate_assembly as assembly

    release = _release(tmp_path)
    core = _core_source(tmp_path)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()

    monkeypatch.setattr(
        assembly,
        "verify_release_directory",
        lambda _path: {"ok": True, "release_id": "r1", "source_commit": "a" * 40},
    )

    result = assembly.plan_full_candidate(
        release_root=release,
        hermes_core_source=core,
        candidate_root=candidate,
        base_python=python,
        runtime_home=home,
    )

    assert result["ok"] is False
    assert "candidate_root_must_not_exist" in result["errors"]


def test_full_candidate_assembly_creates_clean_venv_and_installs_core_from_candidate_path(tmp_path, monkeypatch):
    from scripts import xiaoyou_full_candidate_assembly as assembly

    release = _release(tmp_path)
    core = _core_source(tmp_path)
    candidate = tmp_path / "candidate"
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    commands = []

    monkeypatch.setattr(
        assembly,
        "verify_release_directory",
        lambda _path: {"ok": True, "release_id": "r1", "source_commit": "b" * 40},
    )
    monkeypatch.setattr(
        assembly,
        "install_release",
        lambda **_kwargs: {"ok": True, "release_id": "r1", "source_commit": "b" * 40, "applied": True},
    )
    monkeypatch.setattr(
        assembly,
        "inspect_release_self_containment",
        lambda **_kwargs: {"ok": True, "install_metadata_path_leaks": []},
    )
    monkeypatch.setattr(
        assembly,
        "inspect_core_compatibility",
        lambda **_kwargs: {"ok": True},
    )

    def runner(command, **_kwargs):
        commands.append([str(item) for item in command])
        if command[1:3] == ["-m", "venv"]:
            venv = Path(command[3])
            (venv / "bin").mkdir(parents=True)
            (venv / "bin/python").write_text("#!/bin/sh\n", encoding="utf-8")
        elif "pip" in command:
            venv = candidate / ".venv/bin"
            (venv / "hermes").write_text("#!/bin/sh\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = assembly.assemble_full_candidate(
        release_root=release,
        hermes_core_source=core,
        candidate_root=candidate,
        base_python=python,
        runtime_home=home,
        service_user="svc",
        service_group="svc",
        apply=True,
        runner=runner,
    )

    assert result["ok"] is True
    assert commands[0] == [str(python.resolve()), "-m", "venv", str(candidate / ".venv")]
    pip_command = commands[1]
    assert pip_command[:4] == [str(candidate / ".venv/bin/python"), "-m", "pip", "install"]
    assert pip_command[-1] == str(candidate / "hermes-agent")
    assert result["core_install_source"] == str(candidate / "hermes-agent")
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["core_install_mode"] == "non_editable_from_candidate_local_source"
    assert manifest["production_changed"] is False
    assert not (candidate / ".xiaoyou_assembly_in_progress").exists()


def test_full_candidate_assembly_cleans_failed_candidate(tmp_path, monkeypatch):
    from scripts import xiaoyou_full_candidate_assembly as assembly

    release = _release(tmp_path)
    core = _core_source(tmp_path)
    candidate = tmp_path / "candidate"
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()

    monkeypatch.setattr(
        assembly,
        "verify_release_directory",
        lambda _path: {"ok": True, "release_id": "r1", "source_commit": "c" * 40},
    )

    def runner(command, **_kwargs):
        if command[1:3] == ["-m", "venv"]:
            return SimpleNamespace(returncode=9, stdout="", stderr="venv failed")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = assembly.assemble_full_candidate(
        release_root=release,
        hermes_core_source=core,
        candidate_root=candidate,
        base_python=python,
        runtime_home=home,
        apply=True,
        runner=runner,
    )

    assert result["ok"] is False
    assert result["error"] == "candidate_venv_create_failed"
    assert result["candidate_cleaned_after_failure"] is True
    assert not candidate.exists()


def test_full_candidate_plan_rejects_external_core_symlink(tmp_path, monkeypatch):
    from scripts import xiaoyou_full_candidate_assembly as assembly

    release = _release(tmp_path)
    core = _core_source(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("x\n", encoding="utf-8")
    os.symlink(outside, core / "linked.py")
    candidate = tmp_path / "candidate"
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()

    monkeypatch.setattr(
        assembly,
        "verify_release_directory",
        lambda _path: {"ok": True, "release_id": "r1", "source_commit": "d" * 40},
    )

    result = assembly.plan_full_candidate(
        release_root=release,
        hermes_core_source=core,
        candidate_root=candidate,
        base_python=python,
        runtime_home=home,
    )

    assert result["ok"] is False
    assert "hermes_core_external_symlink" in result["errors"]
