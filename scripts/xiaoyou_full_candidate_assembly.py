#!/usr/bin/env python3
"""Assemble one clean, self-contained XiaoYou/Hermes full candidate.

The builder is deliberately fail-closed:
- candidate_root must not already exist;
- Hermes Core is copied into the candidate and installed from that exact path;
- a brand-new venv is created for every candidate;
- XiaoYou release links are installed only after Core install succeeds;
- existing self-contained/Core compatibility gates must pass before success.

It never mutates the active production release.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable

try:
    from .xiaoyou_release_package import verify_release_directory
    from .xiaoyou_release_installer import install_release
    from .xiaoyou_release_self_contained_gate import inspect_release_self_containment
    from .xiaoyou_core_compat_gate import inspect_core_compatibility
except ImportError:
    from xiaoyou_release_package import verify_release_directory
    from xiaoyou_release_installer import install_release
    from xiaoyou_release_self_contained_gate import inspect_release_self_containment
    from xiaoyou_core_compat_gate import inspect_core_compatibility


CORE_EXCLUDES = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "data",
    "logs",
    "backups",
}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _core_tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in CORE_EXCLUDES for part in relative.parts):
            continue
        if path.is_symlink():
            digest.update(relative.as_posix().encode())
            digest.update(b"\\0L\\0")
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            digest.update(relative.as_posix().encode())
            digest.update(b"\\0F\\0")
            digest.update(_sha256(path).encode())
    return digest.hexdigest()


def _external_symlinks(root: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_symlink():
            continue
        resolved = path.resolve()
        if not _inside(resolved, root):
            rows.append({
                "path": str(path),
                "target": os.readlink(path),
                "resolved": str(resolved),
            })
    return rows


def _ignore_core(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in CORE_EXCLUDES}


def plan_full_candidate(
    *,
    release_root: Path,
    hermes_core_source: Path,
    candidate_root: Path,
    base_python: Path,
    runtime_home: Path,
) -> dict[str, Any]:
    release = verify_release_directory(release_root)
    errors: list[str] = []
    if not release.get("ok"):
        errors.append("xiaoyou_release_invalid")

    core = hermes_core_source.resolve()
    target = candidate_root.absolute()
    python = base_python.resolve()
    home = runtime_home.resolve()

    if not core.is_dir():
        errors.append("hermes_core_source_missing")
    else:
        if not (core / "hermes_cli").is_dir():
            errors.append("hermes_cli_source_missing")
        if not (core / "hermes_constants.py").is_file():
            errors.append("hermes_constants_source_missing")
        if _external_symlinks(core):
            errors.append("hermes_core_external_symlink")

    if candidate_root.exists() or candidate_root.is_symlink():
        errors.append("candidate_root_must_not_exist")
    if not python.is_file():
        errors.append("base_python_missing")
    if _inside(target, core) or _inside(core, target):
        errors.append("candidate_and_core_source_overlap")
    if _inside(home, target) or _inside(target, home):
        errors.append("runtime_home_must_be_external")

    return {
        "ok": not errors,
        "error": "" if not errors else "full_candidate_plan_failed",
        "errors": errors,
        "release_id": str(release.get("release_id") or ""),
        "source_commit": str(release.get("source_commit") or ""),
        "release_root": str(release_root.resolve()),
        "hermes_core_source": str(core),
        "hermes_core_tree_sha256": _core_tree_digest(core) if core.is_dir() else "",
        "candidate_root": str(target),
        "base_python": str(python),
        "runtime_home": str(home),
        "applied": False,
        "production_changed": False,
    }


def _fail_and_cleanup(candidate_root: Path, result: dict[str, Any], *, preserve_failed: bool) -> dict[str, Any]:
    if candidate_root.exists() and not preserve_failed:
        shutil.rmtree(candidate_root, ignore_errors=True)
    result["candidate_cleaned_after_failure"] = not candidate_root.exists()
    result["production_changed"] = False
    return result


def assemble_full_candidate(
    *,
    release_root: Path,
    hermes_core_source: Path,
    candidate_root: Path,
    base_python: Path,
    runtime_home: Path,
    service_user: str = "hermes-youyi",
    service_group: str = "hermes-youyi",
    apply: bool = False,
    preserve_failed: bool = False,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    plan = plan_full_candidate(
        release_root=release_root,
        hermes_core_source=hermes_core_source,
        candidate_root=candidate_root,
        base_python=base_python,
        runtime_home=runtime_home,
    )
    if not plan.get("ok") or not apply:
        return plan

    target = candidate_root.absolute()
    core_source = hermes_core_source.resolve()
    try:
        target.mkdir(mode=0o755, parents=True, exist_ok=False)
        (target / ".xiaoyou_assembly_in_progress").write_text(
            datetime.now(timezone.utc).isoformat(timespec="seconds") + "\\n",
            encoding="utf-8",
        )

        core_target = target / "hermes-agent"
        shutil.copytree(
            core_source,
            core_target,
            symlinks=True,
            ignore=_ignore_core,
        )

        venv_cmd = [str(base_python.resolve()), "-m", "venv", str(target / ".venv")]
        created = runner(venv_cmd, text=True, capture_output=True, check=False)
        if created.returncode != 0:
            return _fail_and_cleanup(target, {
                **plan,
                "ok": False,
                "error": "candidate_venv_create_failed",
                "returncode": created.returncode,
                "stderr_tail": str(created.stderr or "")[-2000:],
            }, preserve_failed=preserve_failed)

        candidate_python = target / ".venv/bin/python"
        candidate_console = target / ".venv/bin/hermes"
        if not candidate_python.is_file():
            return _fail_and_cleanup(target, {
                **plan,
                "ok": False,
                "error": "candidate_python_missing_after_venv",
            }, preserve_failed=preserve_failed)

        install_cmd = [
            str(candidate_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            str(core_target),
        ]
        installed = runner(install_cmd, cwd=target, text=True, capture_output=True, check=False)
        if installed.returncode != 0:
            return _fail_and_cleanup(target, {
                **plan,
                "ok": False,
                "error": "candidate_core_install_failed",
                "returncode": installed.returncode,
                "stderr_tail": str(installed.stderr or "")[-2000:],
            }, preserve_failed=preserve_failed)
        if not candidate_console.is_file():
            return _fail_and_cleanup(target, {
                **plan,
                "ok": False,
                "error": "candidate_console_missing_after_core_install",
            }, preserve_failed=preserve_failed)

        overlay = install_release(
            release_root=release_root,
            base=target,
            apply=True,
        )
        if not overlay.get("ok"):
            return _fail_and_cleanup(target, {
                **plan,
                "ok": False,
                "error": "xiaoyou_overlay_install_failed",
                "overlay": overlay,
            }, preserve_failed=preserve_failed)

        self_contained = inspect_release_self_containment(
            release_root=target,
            runtime_home=runtime_home,
            python_executable=candidate_python,
            console_executable=candidate_console,
            service_user=service_user,
            service_group=service_group,
        )
        if not self_contained.get("ok"):
            return _fail_and_cleanup(target, {
                **plan,
                "ok": False,
                "error": "candidate_self_containment_failed",
                "self_contained": self_contained,
            }, preserve_failed=preserve_failed)

        core_compat = inspect_core_compatibility(
            release_root=target,
            runtime_home=runtime_home,
            python_executable=candidate_python,
            service_user=service_user,
            service_group=service_group,
        )
        if not core_compat.get("ok"):
            return _fail_and_cleanup(target, {
                **plan,
                "ok": False,
                "error": "candidate_core_compatibility_failed",
                "core_compatibility": core_compat,
            }, preserve_failed=preserve_failed)

        marker = target / ".xiaoyou_assembly_in_progress"
        marker.unlink(missing_ok=True)
        manifest = {
            "schema_version": "xiaoyou_full_candidate_assembly_v1",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "candidate_root": str(target),
            "xiaoyou_release_id": str(plan.get("release_id") or ""),
            "xiaoyou_source_commit": str(plan.get("source_commit") or ""),
            "hermes_core_source": str(core_source),
            "hermes_core_tree_sha256": str(plan.get("hermes_core_tree_sha256") or ""),
            "candidate_python": str(candidate_python),
            "candidate_console": str(candidate_console),
            "runtime_home": str(runtime_home.resolve()),
            "core_install_mode": "non_editable_from_candidate_local_source",
            "production_changed": False,
        }
        manifest_path = target / "full_candidate_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\\n",
            encoding="utf-8",
        )

        return {
            **plan,
            "ok": True,
            "error": "",
            "applied": True,
            "candidate_python": str(candidate_python),
            "candidate_console": str(candidate_console),
            "core_install_source": str(core_target),
            "overlay": {
                "release_id": overlay.get("release_id"),
                "source_commit": overlay.get("source_commit"),
                "applied": overlay.get("applied"),
            },
            "self_contained": self_contained,
            "core_compatibility": core_compat,
            "manifest": str(manifest_path),
            "production_changed": False,
        }
    except (OSError, ValueError) as exc:
        return _fail_and_cleanup(target, {
            **plan,
            "ok": False,
            "error": f"{type(exc).__name__}:{exc}",
        }, preserve_failed=preserve_failed)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build one clean self-contained XiaoYou/Hermes full candidate.")
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--hermes-core-source", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--base-python", type=Path, required=True)
    parser.add_argument("--runtime-home", type=Path, required=True)
    parser.add_argument("--service-user", default="hermes-youyi")
    parser.add_argument("--service-group", default="hermes-youyi")
    parser.add_argument("--preserve-failed", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = assemble_full_candidate(
        release_root=args.release_root,
        hermes_core_source=args.hermes_core_source,
        candidate_root=args.candidate_root,
        base_python=args.base_python,
        runtime_home=args.runtime_home,
        service_user=args.service_user,
        service_group=args.service_group,
        apply=args.apply,
        preserve_failed=args.preserve_failed,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
