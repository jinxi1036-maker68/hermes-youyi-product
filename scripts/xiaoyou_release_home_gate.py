#!/usr/bin/env python3
"""Fail-closed runtime-home ownership gate for XiaoYou release directories."""

from __future__ import annotations

import argparse
import grp
import json
import os
from pathlib import Path
import pwd
import stat
from typing import Any


DEFAULT_SERVICE_USER = "hermes-youyi"
DEFAULT_SERVICE_GROUP = "hermes-youyi"
EXPECTED_HOME_MODE = 0o700
RUNTIME_MUTABLE_DIRS = ("sessions", "cron")


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
        return stat.S_IRWXU
    if meta.st_uid == uid:
        return (mode & stat.S_IRWXU) >> 6
    if meta.st_gid in gids:
        return (mode & stat.S_IRWXG) >> 3
    return mode & stat.S_IRWXO


def _has_permissions(path: Path, *, uid: int, gids: set[int], required: int) -> bool:
    return (_permission_bits(path, uid=uid, gids=gids) & required) == required


def _has_execute(path: Path, *, uid: int, gids: set[int]) -> bool:
    return _has_permissions(path, uid=uid, gids=gids, required=stat.S_IXUSR)


def _traversal_paths(release_root: Path) -> list[Path]:
    resolved = release_root.resolve()
    parts = resolved.parts
    paths: list[Path] = []
    current = Path(parts[0])
    paths.append(current)
    for part in parts[1:]:
        current = current / part
        paths.append(current)
    paths.append(resolved / "home")
    return paths


def _inspect_mutable_runtime_state(
    *,
    home: Path,
    uid: int,
    gid: int,
    gids: set[int],
) -> tuple[bool, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    ok = True
    for name in RUNTIME_MUTABLE_DIRS:
        root = home / name
        if not root.exists():
            rows.append({"path": str(root), "exists": False, "ok": True})
            continue
        if root.is_symlink():
            rows.append({
                "path": str(root),
                "exists": True,
                "type": "symlink",
                "ok": False,
                "error": "mutable_runtime_path_must_not_be_symlink",
            })
            ok = False
            continue
        candidates = [root, *sorted(root.rglob("*"))]
        for path in candidates:
            if path.is_symlink():
                rows.append({
                    "path": str(path),
                    "exists": True,
                    "type": "symlink",
                    "ok": False,
                    "error": "mutable_runtime_path_must_not_be_symlink",
                })
                ok = False
                continue
            meta = path.stat()
            mode = stat.S_IMODE(meta.st_mode)
            owner_ok = meta.st_uid == uid
            group_ok = meta.st_gid == gid
            if path.is_dir():
                access_ok = _has_permissions(
                    path,
                    uid=uid,
                    gids=gids,
                    required=stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
                )
                path_type = "directory"
            elif path.is_file():
                access_ok = _has_permissions(
                    path,
                    uid=uid,
                    gids=gids,
                    required=stat.S_IRUSR | stat.S_IWUSR,
                )
                path_type = "file"
            else:
                access_ok = False
                path_type = "other"
            item_ok = owner_ok and group_ok and access_ok
            rows.append({
                "path": str(path),
                "exists": True,
                "type": path_type,
                "owner_uid": meta.st_uid,
                "group_gid": meta.st_gid,
                "mode": f"{mode:04o}",
                "owner_ok": owner_ok,
                "group_ok": group_ok,
                "service_access_ok": access_ok,
                "ok": item_ok,
            })
            ok = ok and item_ok
    return ok, rows


def inspect_release_home(
    *,
    release_root: Path,
    service_user: str = DEFAULT_SERVICE_USER,
    service_group: str = DEFAULT_SERVICE_GROUP,
) -> dict[str, Any]:
    root = release_root.resolve()
    uid, gid, gids = _identity(service_user, service_group)
    if not root.is_dir():
        return {"ok": False, "error": "release_root_missing", "release_root": str(root)}
    if release_root.is_symlink():
        return {"ok": False, "error": "release_root_must_not_be_symlink", "release_root": str(root)}

    home = root / "home"
    if not home.exists():
        return {
            "ok": False,
            "error": "runtime_home_missing",
            "release_root": str(root),
            "home": str(home),
        }
    if home.is_symlink():
        return {
            "ok": False,
            "error": "runtime_home_must_not_be_symlink",
            "release_root": str(root),
            "home": str(home),
        }
    if not home.is_dir():
        return {
            "ok": False,
            "error": "runtime_home_not_directory",
            "release_root": str(root),
            "home": str(home),
        }

    home_stat = home.stat()
    traversal: list[dict[str, Any]] = []
    traversal_ok = True
    for path in _traversal_paths(root):
        if not path.exists():
            traversal.append({"path": str(path), "exists": False, "traversable": False})
            traversal_ok = False
            continue
        traversable = _has_execute(path, uid=uid, gids=gids)
        traversal.append(
            {
                "path": str(path),
                "exists": True,
                "owner_uid": path.stat().st_uid,
                "group_gid": path.stat().st_gid,
                "mode": f"{_mode(path):04o}",
                "traversable": traversable,
            }
        )
        traversal_ok = traversal_ok and traversable

    owner_ok = home_stat.st_uid == uid
    group_ok = home_stat.st_gid == gid
    mode_ok = stat.S_IMODE(home_stat.st_mode) == EXPECTED_HOME_MODE
    mutable_state_ok, mutable_state = _inspect_mutable_runtime_state(
        home=home,
        uid=uid,
        gid=gid,
        gids=gids,
    )
    ok = owner_ok and group_ok and mode_ok and traversal_ok and mutable_state_ok
    return {
        "ok": ok,
        "error": "" if ok else "runtime_home_permission_gate_failed",
        "release_root": str(root),
        "home": str(home),
        "service_user": service_user,
        "service_group": service_group,
        "expected_home_mode": f"{EXPECTED_HOME_MODE:04o}",
        "home_owner_uid": home_stat.st_uid,
        "home_group_gid": home_stat.st_gid,
        "home_mode": f"{stat.S_IMODE(home_stat.st_mode):04o}",
        "owner_ok": owner_ok,
        "group_ok": group_ok,
        "mode_ok": mode_ok,
        "traversal_ok": traversal_ok,
        "traversal": traversal,
        "mutable_runtime_dirs": list(RUNTIME_MUTABLE_DIRS),
        "mutable_state_ok": mutable_state_ok,
        "mutable_state": mutable_state,
        "secret_content_inspected": False,
    }


def repair_release_home(
    *,
    release_root: Path,
    service_user: str = DEFAULT_SERVICE_USER,
    service_group: str = DEFAULT_SERVICE_GROUP,
) -> dict[str, Any]:
    root = release_root.resolve()
    if not root.is_dir():
        return {"ok": False, "error": "release_root_missing", "release_root": str(root)}
    if release_root.is_symlink():
        return {"ok": False, "error": "release_root_must_not_be_symlink", "release_root": str(root)}

    uid, gid, _gids = _identity(service_user, service_group)
    home = root / "home"
    if home.is_symlink():
        return {"ok": False, "error": "runtime_home_must_not_be_symlink", "home": str(home)}
    home.mkdir(mode=EXPECTED_HOME_MODE, exist_ok=True)
    if not home.is_dir():
        return {"ok": False, "error": "runtime_home_not_directory", "home": str(home)}

    os.chown(home, uid, gid)
    os.chmod(home, EXPECTED_HOME_MODE)
    result = inspect_release_home(
        release_root=root,
        service_user=service_user,
        service_group=service_group,
    )
    result["repair_applied"] = True
    result["repair_scope"] = "release_home_directory_only"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify or narrowly repair the service-owned runtime home for a XiaoYou release."
    )
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--service-user", default=DEFAULT_SERVICE_USER)
    parser.add_argument("--service-group", default=DEFAULT_SERVICE_GROUP)
    parser.add_argument("--repair", action="store_true")
    args = parser.parse_args()

    try:
        result = (
            repair_release_home(
                release_root=args.release_root,
                service_user=args.service_user,
                service_group=args.service_group,
            )
            if args.repair
            else inspect_release_home(
                release_root=args.release_root,
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
