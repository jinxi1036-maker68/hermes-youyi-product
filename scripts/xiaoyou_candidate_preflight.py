#!/usr/bin/env python3
"""Run candidate release preflight commands under the Gateway service identity."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import grp
import subprocess
from typing import Any, Callable

try:
    from .xiaoyou_runtime_topology import validate_runtime_topology
except ImportError:
    from xiaoyou_runtime_topology import validate_runtime_topology


DEFAULT_SERVICE_USER = "hermes-youyi"
DEFAULT_SERVICE_GROUP = "hermes-youyi"


def _identity(user_name: str, group_name: str) -> tuple[int, int]:
    user = pwd.getpwnam(user_name)
    group = grp.getgrnam(group_name)
    return user.pw_uid, group.gr_gid


def _preexec(user_name: str, uid: int, gid: int) -> Callable[[], None]:
    def drop_privileges() -> None:
        os.initgroups(user_name, gid)
        os.setgid(gid)
        os.setuid(uid)
    return drop_privileges


def run_candidate_preflight(
    *,
    release_root: Path,
    runtime_home: Path,
    command: list[str],
    service_user: str = DEFAULT_SERVICE_USER,
    service_group: str = DEFAULT_SERVICE_GROUP,
    cwd: Path | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    root = release_root.resolve()
    home = runtime_home.resolve()

    topology = validate_runtime_topology(
        release_root=root,
        runtime_home=home,
        require_existing=True,
    )
    if not topology.get("ok"):
        return {
            "ok": False,
            "error": "runtime_topology_invalid",
            "topology": topology,
        }
    if not command:
        return {"ok": False, "error": "command_missing"}

    uid, gid = _identity(service_user, service_group)
    current_uid = os.geteuid()
    if current_uid not in {0, uid}:
        return {
            "ok": False,
            "error": "executor_cannot_assume_service_identity",
            "current_uid": current_uid,
            "required_uid": uid,
        }

    workdir = (cwd or root).resolve()
    try:
        workdir.relative_to(root)
    except ValueError:
        return {"ok": False, "error": "cwd_outside_release", "cwd": str(workdir)}
    if not workdir.is_dir():
        return {"ok": False, "error": "cwd_missing", "cwd": str(workdir)}

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["HERMES_HOME"] = str(home)

    kwargs: dict[str, Any] = {
        "cwd": workdir,
        "env": env,
        "check": False,
    }
    if current_uid == 0:
        kwargs["preexec_fn"] = _preexec(service_user, uid, gid)

    completed = runner(command, **kwargs)
    return {
        "ok": completed.returncode == 0,
        "error": "" if completed.returncode == 0 else "candidate_preflight_failed",
        "returncode": completed.returncode,
        "release_root": str(root),
        "runtime_home": str(home),
        "cwd": str(workdir),
        "service_user": service_user,
        "service_group": service_group,
        "effective_identity_enforced": True,
        "runtime_model": "version_neutral_persistent_home",
        "secret_content_inspected": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Execute a candidate preflight command against an external persistent Hermes Home."
    )
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--runtime-home", type=Path, required=True)
    parser.add_argument("--service-user", default=DEFAULT_SERVICE_USER)
    parser.add_argument("--service-group", default=DEFAULT_SERVICE_GROUP)
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]

    try:
        result = run_candidate_preflight(
            release_root=args.release_root,
            runtime_home=args.runtime_home,
            command=command,
            service_user=args.service_user,
            service_group=args.service_group,
            cwd=args.cwd,
        )
    except (KeyError, OSError, PermissionError) as exc:
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}:{exc}",
            "effective_identity_enforced": False,
            "secret_content_inspected": False,
        }

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
