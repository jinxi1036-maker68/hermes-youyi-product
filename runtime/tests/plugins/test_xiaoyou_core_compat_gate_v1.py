from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    release = tmp_path / "candidate-release"
    release.mkdir()
    python = release / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    home = tmp_path / "persistent-home"
    home.mkdir()
    return release, home, python


def test_core_compat_gate_accepts_candidate_core_with_complete_contract(tmp_path, monkeypatch):
    from scripts import xiaoyou_core_compat_gate as gate

    release, home, python = _fixture(tmp_path)
    monkeypatch.setattr(gate, "_identity", lambda *_args: (os.geteuid(), os.getegid()))
    probe = {
        "constants_path": str(release / "hermes_constants.py"),
        "test_shim_detected": False,
        "hermes_cli_found": True,
        "hermes_cli_locations": [str(release / "hermes_cli")],
        "required_symbols": ["get_default_hermes_root", "get_hermes_home"],
        "missing_symbols": [],
        "parse_errors": [],
    }

    def runner(_command, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(probe), stderr="")

    result = gate.inspect_core_compatibility(
        release_root=release,
        runtime_home=home,
        python_executable=python,
        service_user="svc",
        service_group="svc",
        runner=runner,
    )

    assert result["ok"] is True
    assert result["xiaoyou_test_shim_detected"] is False
    assert result["missing_symbols"] == []


def test_core_compat_gate_rejects_xiaoyou_test_shim_shadowing_core(tmp_path, monkeypatch):
    from scripts import xiaoyou_core_compat_gate as gate

    release, home, python = _fixture(tmp_path)
    monkeypatch.setattr(gate, "_identity", lambda *_args: (os.geteuid(), os.getegid()))
    probe = {
        "constants_path": str(release / "hermes_constants.py"),
        "test_shim_detected": True,
        "hermes_cli_found": True,
        "hermes_cli_locations": [str(release / "hermes_cli")],
        "required_symbols": ["get_default_hermes_root"],
        "missing_symbols": ["get_default_hermes_root"],
        "parse_errors": [],
    }

    def runner(_command, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(probe), stderr="")

    result = gate.inspect_core_compatibility(
        release_root=release,
        runtime_home=home,
        python_executable=python,
        service_user="svc",
        service_group="svc",
        runner=runner,
    )

    assert result["ok"] is False
    assert "xiaoyou_test_shim_shadowed_core" in result["errors"]
    assert "hermes_constants_missing_required_symbols" in result["errors"]


def test_core_compat_gate_rejects_missing_actual_hermes_cli_import(tmp_path, monkeypatch):
    from scripts import xiaoyou_core_compat_gate as gate

    release, home, python = _fixture(tmp_path)
    monkeypatch.setattr(gate, "_identity", lambda *_args: (os.geteuid(), os.getegid()))
    probe = {
        "constants_path": str(release / "hermes_constants.py"),
        "test_shim_detected": False,
        "hermes_cli_found": True,
        "hermes_cli_locations": [str(release / "hermes_cli")],
        "required_symbols": ["get_default_hermes_root", "get_hermes_home"],
        "missing_symbols": ["get_default_hermes_root"],
        "parse_errors": [],
    }

    def runner(_command, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(probe), stderr="")

    result = gate.inspect_core_compatibility(
        release_root=release,
        runtime_home=home,
        python_executable=python,
        service_user="svc",
        service_group="svc",
        runner=runner,
    )

    assert result["ok"] is False
    assert result["missing_symbols"] == ["get_default_hermes_root"]
    assert "hermes_constants_missing_required_symbols" in result["errors"]


def test_core_compat_gate_rejects_constants_from_outside_candidate(tmp_path, monkeypatch):
    from scripts import xiaoyou_core_compat_gate as gate

    release, home, python = _fixture(tmp_path)
    old = tmp_path / "old-release"
    old.mkdir()
    monkeypatch.setattr(gate, "_identity", lambda *_args: (os.geteuid(), os.getegid()))
    probe = {
        "constants_path": str(old / "hermes_constants.py"),
        "test_shim_detected": False,
        "hermes_cli_found": True,
        "hermes_cli_locations": [str(release / "hermes_cli")],
        "required_symbols": ["get_default_hermes_root"],
        "missing_symbols": [],
        "parse_errors": [],
    }

    def runner(_command, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(probe), stderr="")

    result = gate.inspect_core_compatibility(
        release_root=release,
        runtime_home=home,
        python_executable=python,
        service_user="svc",
        service_group="svc",
        runner=runner,
    )

    assert result["ok"] is False
    assert "hermes_constants_outside_candidate" in result["errors"]
