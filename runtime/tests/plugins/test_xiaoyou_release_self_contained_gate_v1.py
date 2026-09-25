from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace


def _fixture(tmp_path: Path):
    releases = tmp_path / "releases"
    release = releases / "candidate"
    bin_dir = release / ".venv" / "bin"
    site = release / ".venv" / "lib" / "python3.11" / "site-packages"
    bin_dir.mkdir(parents=True)
    site.mkdir(parents=True)
    python = bin_dir / "python"
    console = bin_dir / "hermes"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    console.write_text("#!/bin/sh\n", encoding="utf-8")
    home = tmp_path / "persistent-home"
    home.mkdir()
    return release, home, python, console, site


def test_release_self_containment_passes_when_runtime_origins_are_candidate_local(tmp_path, monkeypatch):
    from scripts import xiaoyou_release_self_contained_gate as gate

    release, home, python, console, site = _fixture(tmp_path)
    monkeypatch.setattr(gate, "_identity", lambda *_args: (os.geteuid(), os.getegid()))

    payload = {
        "sys_executable": str(python),
        "sys_path": [str(site), "/usr/lib/python3.11"],
        "modules": {
            "hermes_cli": {"found": True, "origin": str(site / "hermes_cli/__init__.py"), "locations": []},
            "plugins.tuoguan_core": {"found": True, "origin": str(site / "plugins/tuoguan_core/__init__.py"), "locations": []},
            "plugins.platforms.wecom": {"found": True, "origin": str(site / "plugins/platforms/wecom/__init__.py"), "locations": []},
        },
    }

    def runner(_command, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    result = gate.inspect_release_self_containment(
        release_root=release,
        runtime_home=home,
        python_executable=python,
        console_executable=console,
        service_user="svc",
        service_group="svc",
        runner=runner,
    )

    assert result["ok"] is True
    assert result["sys_executable_inside_candidate"] is True
    assert result["sibling_release_sys_path_leaks"] == []
    assert all(row["inside_candidate_release"] for row in result["module_origins"].values())


def test_release_self_containment_rejects_module_from_old_release(tmp_path, monkeypatch):
    from scripts import xiaoyou_release_self_contained_gate as gate

    release, home, python, console, site = _fixture(tmp_path)
    old_site = release.parent / "old-release" / ".venv/lib/python3.11/site-packages"
    old_site.mkdir(parents=True)
    monkeypatch.setattr(gate, "_identity", lambda *_args: (os.geteuid(), os.getegid()))

    payload = {
        "sys_executable": str(python),
        "sys_path": [str(site), str(old_site)],
        "modules": {
            "hermes_cli": {"found": True, "origin": str(old_site / "hermes_cli/__init__.py"), "locations": []},
            "plugins.tuoguan_core": {"found": True, "origin": str(site / "plugins/tuoguan_core/__init__.py"), "locations": []},
            "plugins.platforms.wecom": {"found": True, "origin": str(site / "plugins/platforms/wecom/__init__.py"), "locations": []},
        },
    }

    def runner(_command, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    result = gate.inspect_release_self_containment(
        release_root=release,
        runtime_home=home,
        python_executable=python,
        console_executable=console,
        service_user="svc",
        service_group="svc",
        runner=runner,
    )

    assert result["ok"] is False
    assert result["module_origins"]["hermes_cli"]["inside_candidate_release"] is False
    assert str(old_site.resolve()) in result["sibling_release_sys_path_leaks"]


def test_release_self_containment_rejects_console_or_python_outside_candidate(tmp_path, monkeypatch):
    from scripts import xiaoyou_release_self_contained_gate as gate

    release, home, _python, _console, _site = _fixture(tmp_path)
    outside = tmp_path / "old-release"
    outside.mkdir()
    python = outside / "python"
    console = outside / "hermes"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    console.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(gate, "_identity", lambda *_args: (os.geteuid(), os.getegid()))

    result = gate.inspect_release_self_containment(
        release_root=release,
        runtime_home=home,
        python_executable=python,
        console_executable=console,
        service_user="svc",
        service_group="svc",
    )

    assert result["ok"] is False
    assert "python_outside_candidate_release" in result["errors"]
    assert "console_outside_candidate_release" in result["errors"]
