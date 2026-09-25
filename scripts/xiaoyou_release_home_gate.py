#!/usr/bin/env python3
"""Fail-closed permission gate for a version-neutral persistent Hermes Home."""

from __future__ import annotations

import argparse
import grp
import json
import os
from pathlib import Path
import pwd
import stat
from typing import Any

try:
    from .xiaoyou_runtime_topology import validate_runtime_topology
except ImportError:
    from xiaoyou_runtime_topology import validate_runtime_topology


DEFAULT_SERVICE_USER = "hermes-youyi"
DEFAULT_SERVICE_GROUP = "hermes-youyi"
EXPECTED_HOME_MODE = 0o700
MUTABLE_RUNTIME_DIRS = ("sessions", "cron")
REQUIRED_WRITABLE_DIRS = ("logs",)
REQUIRED_WRITABLE_FILES = ("state.db",)


def _identity(user_name: str, group_name: str) -> tuple[int, int, set[int]]:
    user = pwd.getpwnam(user_name)
    group = grp.getgrnam(group_name)
    gids = {user.pw_gid, group.gr_gid}
    for item in grp.getgrall():
        if user_name in item.gr_mem:
            gids.add(item.gr_gid)
    return user.pw_uid, group.gr_gid, gids


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _permission_bits(path: Path, *, uid: int, gids: set[int]) -> int:
    meta = path.stat()
    mode = stat.S_IMODE(meta.st_mode)
    if uid == 0:
        return 0b111
    if meta.st_uid == uid:
        return (mode & stat.S_IRWXU) >> 6
    if meta.st_gid in gids:
        return (mode & stat.S_IRWXG) >> 3
    return mode & stat.S_IRWXO


def _has_permissions(path: Path, *, uid: int, gids: set[int], required: int) -> bool:
    return (_permission_bits(path, uid=uid, gids=gids) & required) == required


def _traversal_paths(path: Path) -> list[Path]:
    resolved = path.resolve()
    parts = resolved.parts
    current = Path(parts[0])
    rows = [current]
    for part in parts[1:]:
        current = current / part
        rows.append(current)
    return rows


def _inspect_top_level_symlinks(home: Path) -> tuple[bool, list[str]]:
    links = sorted(
        str(item)
        for item in home.iterdir()
        if item.is_symlink()
    )
    return not links, links


def _inspect_tree(
    *,
    root: Path,
    uid: int,
    gid: int,
    gids: set[int],
) -> tuple[bool, list[dict[str, Any]]]:
    if not root.exists():
        return True, [{"path": str(root), "exists": False, "ok": True}]
    if root.is_symlink():
        return False, [{
            "path": str(root),
            "exists": True,
            "type": "symlink",
            "ok": False,
            "error": "runtime_state_symlink_forbidden",
        }]

    rows: list[dict[str, Any]] = []
    ok = True
    candidates = [root, *sorted(root.rglob("*"))]
    for path in candidates:
        if path.is_symlink():
            rows.append({
                "path": str(path),
                "exists": True,
                "type": "symlink",
                "ok": False,
                "error": "runtime_state_symlink_forbidden",
            })
            ok = False
            continue
        meta = path.stat()
        owner_ok = meta.st_uid == uid
        group_ok = meta.st_gid == gid
        if path.is_dir():
            access_ok = _has_permissions(path, uid=uid, gids=gids, required=0b111)
            kind = "directory"
        elif path.is_file():
            access_ok = _has_permissions(path, uid=uid, gids=gids, required=0b110)
            kind = "file"
        else:
            access_ok = False
            kind = "other"
        item_ok = owner_ok and group_ok and access_ok
        rows.append({
            "path": str(path),
            "exists": True,
            "type": kind,
            "owner_uid": meta.st_uid,
            "group_gid": meta.st_gid,
            "mode": f"{stat.S_IMODE(meta.st_mode):04o}",
            "owner_ok": owner_ok,
            "group_ok": group_ok,
            "service_access_ok": access_ok,
            "ok": item_ok,
        })
        ok = ok and item_ok
    return ok, rows


def inspect_runtime_home(
    *,
    release_root: Path,
    runtime_home: Path,
    service_user: str = DEFAULT_SERVICE_USER,
    service_group: str = DEFAULT_SERVICE_GROUP,
) -> dict[str, Any]:
    topology = validate_runtime_topology(
        release_root=release_root,
        runtime_home=runtime_home,
        require_existing=True,
    )
    if not topology.get("ok"):
        return {
            "ok": False,
            "error": "runtime_topology_invalid",
            "topology": topology,
            "secret_content_inspected": False,
        }

    home = runtime_home.resolve()
    uid, gid, gids = _identity(service_user, service_group)
    meta = home.stat()

    traversal: list[dict[str, Any]] = []
    traversal_ok = True
    for path in _traversal_paths(home):
        traversable = _has_permissions(path, uid=uid, gids=gids, required=0b001)
        traversal.append({
            "path": str(path),
            "owner_uid": path.stat().st_uid,
            "group_gid": path.stat().st_gid,
            "mode": f"{_mode(path):04o}",
            "traversable": traversable,
        })
        traversal_ok = traversal_ok and traversable

    owner_ok = meta.st_uid == uid
    group_ok = meta.st_gid == gid
    mode_ok = stat.S_IMODE(meta.st_mode) == EXPECTED_HOME_MODE
    top_links_ok, top_links = _inspect_top_level_symlinks(home)

    state_rows: list[dict[str, Any]] = []
    state_ok = True
    for name in MUTABLE_RUNTIME_DIRS:
        item_ok, rows = _inspect_tree(root=home / name, uid=uid, gid=gid, gids=gids)
        state_ok = state_ok and item_ok
        state_rows.extend(rows)

    critical_rows: list[dict[str, Any]] = []
    critical_ok = True
    for name in REQUIRED_WRITABLE_DIRS:
        path = home / name
        if not path.exists():
            critical_rows.append({"path": str(path), "exists": False, "ok": True})
            continue
        item_ok = (
            path.is_dir()
            and not path.is_symlink()
            and path.stat().st_uid == uid
            and path.stat().st_gid == gid
            and _has_permissions(path, uid=uid, gids=gids, required=0b111)
        )
        critical_rows.append({
            "path": str(path),
            "exists": True,
            "type": "directory" if path.is_dir() else "other",
            "mode": f"{_mode(path):04o}",
            "ok": item_ok,
        })
        critical_ok = critical_ok and item_ok

    for name in REQUIRED_WRITABLE_FILES:
        path = home / name
        if not path.exists():
            critical_rows.append({"path": str(path), "exists": False, "ok": True})
            continue
        item_ok = (
            path.is_file()
            and not path.is_symlink()
            and path.stat().st_uid == uid
            and path.stat().st_gid == gid
            and _has_permissions(path, uid=uid, gids=gids, required=0b110)
        )
        critical_rows.append({
            "path": str(path),
            "exists": True,
            "type": "file" if path.is_file() else "other",
            "mode": f"{_mode(path):04o}",
            "ok": item_ok,
        })
        critical_ok = critical_ok and item_ok

    ok = all((
        owner_ok,
        group_ok,
        mode_ok,
        traversal_ok,
        top_links_ok,
        state_ok,
        critical_ok,
    ))
    return {
        "ok": ok,
        "error": "" if ok else "runtime_home_permission_gate_failed",
        "runtime_model": "version_neutral_persistent_home",
        "release_root": str(release_root.resolve()),
        "runtime_home": str(home),
        "service_user": service_user,
        "service_group": service_group,
        "expected_home_mode": f"{EXPECTED_HOME_MODE:04o}",
        "home_owner_uid": meta.st_uid,
        "home_group_gid": meta.st_gid,
        "home_mode": f"{stat.S_IMODE(meta.st_mode):04o}",
        "owner_ok": owner_ok,
        "group_ok": group_ok,
        "mode_ok": mode_ok,
        "traversal_ok": traversal_ok,
        "traversal": traversal,
        "top_level_symlinks_ok": top_links_ok,
        "top_level_symlinks": top_links,
        "mutable_state_ok": state_ok,
        "mutable_state": state_rows,
        "critical_state_ok": critical_ok,
        "critical_state": critical_rows,
        "secret_content_inspected": False,
    }


def repair_runtime_home(
    *,
    release_root: Path,
    runtime_home: Path,
    service_user: str = DEFAULT_SERVICE_USER,
    service_group: str = DEFAULT_SERVICE_GROUP,
) -> dict[str, Any]:
    topology = validate_runtime_topology(
        release_root=release_root,
        runtime_home=runtime_home,
        require_existing=False,
    )
    errors = [e for e in topology.get("errors", []) if e != "runtime_home_missing"]
    if errors:
        return {
            "ok": False,
            "error": "runtime_topology_invalid",
            "topology": topology,
        }

    home = runtime_home.resolve()
    uid, gid, _gids = _identity(service_user, service_group)
    if runtime_home.is_symlink():
        return {"ok": False, "error": "runtime_home_must_not_be_symlink"}
    home.mkdir(mode=EXPECTED_HOME_MODE, parents=True, exist_ok=True)
    os.chown(home, uid, gid)
    os.chmod(home, EXPECTED_HOME_MODE)

    result = inspect_runtime_home(
        release_root=release_root,
        runtime_home=home,
        service_user=service_user,
        service_group=service_group,
    )
    result["repair_applied"] = True
    result["repair_scope"] = "persistent_runtime_home_directory_only"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify or narrowly repair an external persistent Hermes Home."
    )
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--runtime-home", type=Path, required=True)
    parser.add_argument("--service-user", default=DEFAULT_SERVICE_USER)
    parser.add_argument("--service-group", default=DEFAULT_SERVICE_GROUP)
    parser.add_argument("--repair", action="store_true")
    args = parser.parse_args()

    try:
        result = (
            repair_runtime_home(
                release_root=args.release_root,
                runtime_home=args.runtime_home,
                service_user=args.service_user,
                service_group=args.service_group,
            )
            if args.repair
            else inspect_runtime_home(
                release_root=args.release_root,
                runtime_home=args.runtime_home,
                service_user=args.service_user,
                service_group=args.service_group,
            )
        )
    except (KeyError, OSError, PermissionError) as exc:
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}:{exc}",
            "secret_content_inspected": False,
        }

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
