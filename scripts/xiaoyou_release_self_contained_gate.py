#!/usr/bin/env python3
"""Fail closed when a candidate release resolves runtime code from an older release."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import grp
import subprocess
from typing import Any, Callable


DEFAULT_SERVICE_USER = "hermes-youyi"
DEFAULT_SERVICE_GROUP = "hermes-youyi"
DEFAULT_MODULES = (
    "hermes_cli",
    "plugins.tuoguan_core",
    "plugins.agenda_service_work",
    "plugins.platforms.wecom",
    "plugins.reply_recovery",
    "plugins.robot_poc",
)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _identity(user_name: str, group_name: str) -> tuple[int, int]:
    return pwd.getpwnam(user_name).pw_uid, grp.getgrnam(group_name).gr_gid


def _preexec(user_name: str, uid: int, gid: int) -> Callable[[], None]:
    def drop_privileges() -> None:
        os.initgroups(user_name, gid)
        os.setgid(gid)
        os.setuid(uid)
    return drop_privileges


def _probe_code(module_names: list[str]) -> str:
    return (
        "import importlib.util, json, os, sys\n"
        f"names={module_names!r}\n"
        "rows={}\n"
        "for name in names:\n"
        "    spec=importlib.util.find_spec(name)\n"
        "    if spec is None:\n"
        "        rows[name]={'found':False}\n"
        "        continue\n"
        "    locs=list(spec.submodule_search_locations or [])\n"
        "    rows[name]={'found':True,'origin':spec.origin,'locations':locs}\n"
        "print(json.dumps({'sys_executable':sys.executable,'sys_path':sys.path,'modules':rows}))\n"
    )


def inspect_release_self_containment(
    *,
    release_root: Path,
    runtime_home: Path,
    python_executable: Path,
    console_executable: Path,
    module_names: list[str] | None = None,
    service_user: str = DEFAULT_SERVICE_USER,
    service_group: str = DEFAULT_SERVICE_GROUP,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    root = release_root.resolve()
    home = runtime_home.resolve()
    python_path = python_executable.resolve()
    console_path = console_executable.resolve()

    errors: list[str] = []
    if not root.is_dir():
        errors.append("release_root_missing")
    if not home.is_dir():
        errors.append("runtime_home_missing")
    if not _inside(python_path, root):
        errors.append("python_outside_candidate_release")
    if not _inside(console_path, root):
        errors.append("console_outside_candidate_release")
    if not python_path.is_file():
        errors.append("python_missing")
    if not console_path.is_file():
        errors.append("console_missing")
    if errors:
        return {"ok": False, "error": "release_self_containment_failed", "errors": errors}

    names = list(module_names or DEFAULT_MODULES)
    uid, gid = _identity(service_user, service_group)
    current_uid = os.geteuid()
    if current_uid not in {0, uid}:
        return {
            "ok": False,
            "error": "executor_cannot_assume_service_identity",
            "current_uid": current_uid,
            "required_uid": uid,
        }

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["HERMES_HOME"] = str(home)
    kwargs: dict[str, Any] = {
        "cwd": root,
        "env": env,
        "text": True,
        "capture_output": True,
        "check": False,
    }
    if current_uid == 0:
        kwargs["preexec_fn"] = _preexec(service_user, uid, gid)

    completed = runner(
        [str(python_path), "-c", _probe_code(names)],
        **kwargs,
    )
    if completed.returncode != 0:
        return {
            "ok": False,
            "error": "module_origin_probe_failed",
            "returncode": completed.returncode,
            "stderr_tail": str(completed.stderr or "")[-2000:],
        }

    try:
        probe = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "module_origin_probe_invalid_json"}

    origins: dict[str, dict[str, Any]] = {}
    module_ok = True
    for name, row in (probe.get("modules") or {}).items():
        found = bool(row.get("found"))
        candidates = []
        if row.get("origin") and row.get("origin") not in {"built-in", "frozen"}:
            candidates.append(Path(row["origin"]))
        candidates.extend(Path(value) for value in row.get("locations") or [])
        inside = found and bool(candidates) and all(_inside(path, root) for path in candidates)
        origins[name] = {
            "found": found,
            "paths": [str(path.resolve()) for path in candidates],
            "inside_candidate_release": inside,
        }
        module_ok = module_ok and inside

    sys_executable = Path(str(probe.get("sys_executable") or "")).resolve()
    executable_ok = _inside(sys_executable, root)

    sibling_release_leaks: list[str] = []
    parent = root.parent
    sibling_release_roots = [
        item.resolve()
        for item in parent.iterdir()
        if item.is_dir()
        and item.resolve() != root
        and item.name.startswith("hermes-youyi-")
    ]
    for value in probe.get("sys_path") or []:
        if not value:
            continue
        path = Path(value)
        if not path.is_absolute():
            continue
        resolved = path.resolve()
        if resolved == root or _inside(resolved, root):
            continue
        if any(resolved == sibling or _inside(resolved, sibling) for sibling in sibling_release_roots):
            sibling_release_leaks.append(str(resolved))

    ok = module_ok and executable_ok and not sibling_release_leaks
    return {
        "ok": ok,
        "error": "" if ok else "release_self_containment_failed",
        "release_root": str(root),
        "runtime_home": str(home),
        "python_executable": str(python_path),
        "console_executable": str(console_path),
        "resolved_sys_executable": str(sys_executable),
        "sys_executable_inside_candidate": executable_ok,
        "module_origins": origins,
        "sibling_release_sys_path_leaks": sibling_release_leaks,
        "runtime_model": "self_contained_release_with_external_home",
        "secret_content_inspected": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify candidate runtime code resolves only from the candidate release.")
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--runtime-home", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--console", type=Path, required=True)
    parser.add_argument("--module", action="append", default=[])
    parser.add_argument("--service-user", default=DEFAULT_SERVICE_USER)
    parser.add_argument("--service-group", default=DEFAULT_SERVICE_GROUP)
    args = parser.parse_args()

    try:
        result = inspect_release_self_containment(
            release_root=args.release_root,
            runtime_home=args.runtime_home,
            python_executable=args.python,
            console_executable=args.console,
            module_names=args.module or None,
            service_user=args.service_user,
            service_group=args.service_group,
        )
    except (KeyError, OSError, PermissionError) as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
