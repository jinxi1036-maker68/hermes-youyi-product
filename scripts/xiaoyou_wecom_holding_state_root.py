#!/usr/bin/env python3
"""Provision and verify the dedicated WeCom Holding Bridge state root.

The bootstrap Bridge must persist callback backlog outside Gateway HERMES_HOME.
Provisioning is a deployment-time root operation limited to exactly one state
root. Runtime writes remain owned by the unprivileged service identity.
"""

from __future__ import annotations

import argparse
import grp
import json
import os
from pathlib import Path
import pwd
import stat
from typing import Any


DEFAULT_STATE_ROOT = Path("/var/lib/hermes-youyi/wecom-holding")
DEFAULT_GATEWAY_HOME = Path("/var/lib/hermes-youyi/hermes-home")
DEFAULT_SERVICE_USER = "hermes-youyi"
DEFAULT_SERVICE_GROUP = "hermes-youyi"
EXPECTED_MODE = 0o700


def _identity(user_name: str, group_name: str) -> tuple[int, int, set[int]]:
    user = pwd.getpwnam(user_name)
    group = grp.getgrnam(group_name)
    gids = {user.pw_gid, group.gr_gid}
    for item in grp.getgrall():
        if user_name in item.gr_mem:
            gids.add(item.gr_gid)
    return user.pw_uid, group.gr_gid, gids


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _symlink_components(path: Path) -> list[str]:
    absolute = path.expanduser()
    if not absolute.is_absolute():
        return [str(absolute)]

    current = Path(absolute.anchor)
    links: list[str] = []
    for part in absolute.parts[1:]:
        current = current / part
        if not current.exists() and not current.is_symlink():
            continue
        if current.is_symlink():
            links.append(str(current))
    return links


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


def _traversal_paths(path: Path) -> list[Path]:
    resolved = path.resolve(strict=False)
    current = Path(resolved.anchor)
    rows = [current]
    for part in resolved.parts[1:]:
        current = current / part
        rows.append(current)
    return rows


def _service_traversal(
    path: Path,
    *,
    uid: int,
    gids: set[int],
) -> tuple[bool, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    ok = True
    for item in _traversal_paths(path):
        traversable = (
            _permission_bits(item, uid=uid, gids=gids) & 0b001
        ) == 0b001
        rows.append({
            "path": str(item),
            "mode": f"{stat.S_IMODE(item.stat().st_mode):04o}",
            "owner_uid": item.stat().st_uid,
            "group_gid": item.stat().st_gid,
            "traversable": traversable,
        })
        ok = ok and traversable
    return ok, rows


def _entry_errors(
    state_root: Path,
    *,
    uid: int,
    gid: int,
    gids: set[int],
) -> tuple[list[str], int]:
    errors: list[str] = []
    count = 0
    for item in sorted(state_root.rglob("*")):
        count += 1
        if item.is_symlink():
            errors.append(
                f"state_entry_symlink_forbidden:{item.relative_to(state_root)}"
            )
            continue
        meta = item.stat()
        if meta.st_uid != uid or meta.st_gid != gid:
            errors.append(
                f"state_entry_identity_mismatch:{item.relative_to(state_root)}"
            )
            continue
        required = 0b111 if item.is_dir() else 0b110
        if (
            _permission_bits(item, uid=uid, gids=gids) & required
        ) != required:
            errors.append(
                f"state_entry_service_access_failed:{item.relative_to(state_root)}"
            )
    return errors, count


def _boundary_errors(*, state_root: Path, gateway_home: Path) -> list[str]:
    errors: list[str] = []
    if not state_root.is_absolute():
        errors.append("state_root_must_be_absolute")
    if not gateway_home.is_absolute():
        errors.append("gateway_home_must_be_absolute")
    if errors:
        return errors

    state = state_root.resolve(strict=False)
    gateway = gateway_home.resolve(strict=False)
    if state == gateway or _inside(state, gateway) or _inside(gateway, state):
        errors.append("state_root_must_be_disjoint_from_gateway_home")

    parent = state_root.parent
    if not parent.exists():
        errors.append("state_root_parent_missing")
    elif not parent.is_dir():
        errors.append("state_root_parent_not_directory")

    links = _symlink_components(parent)
    if links:
        errors.append("state_root_parent_symlink_forbidden")

    if state_root.is_symlink():
        errors.append("state_root_symlink_forbidden")
    elif state_root.exists() and not state_root.is_dir():
        errors.append("state_root_not_directory")

    return errors


def inspect_state_root(
    *,
    state_root: Path,
    gateway_home: Path,
    service_user: str = DEFAULT_SERVICE_USER,
    service_group: str = DEFAULT_SERVICE_GROUP,
) -> dict[str, Any]:
    errors = _boundary_errors(
        state_root=state_root,
        gateway_home=gateway_home,
    )
    uid, gid, gids = _identity(service_user, service_group)

    state = state_root.resolve(strict=False)
    if not state_root.exists():
        errors.append("state_root_missing")
        return {
            "ok": False,
            "error": "holding_state_root_invalid",
            "errors": errors,
            "state_root": str(state),
            "gateway_home": str(gateway_home.resolve(strict=False)),
            "service_user": service_user,
            "service_group": service_group,
            "exists": False,
            "expected_mode": f"{EXPECTED_MODE:04o}",
            "secret_content_inspected": False,
            "payload_content_inspected": False,
        }

    if errors:
        return {
            "ok": False,
            "error": "holding_state_root_invalid",
            "errors": errors,
            "state_root": str(state),
            "gateway_home": str(gateway_home.resolve(strict=False)),
            "service_user": service_user,
            "service_group": service_group,
            "exists": True,
            "expected_mode": f"{EXPECTED_MODE:04o}",
            "secret_content_inspected": False,
            "payload_content_inspected": False,
        }

    root_meta = state_root.stat()
    owner_ok = root_meta.st_uid == uid
    group_ok = root_meta.st_gid == gid
    mode_ok = stat.S_IMODE(root_meta.st_mode) == EXPECTED_MODE
    access_ok = (
        _permission_bits(state_root, uid=uid, gids=gids) & 0b111
    ) == 0b111

    traversal_ok, traversal = _service_traversal(
        state_root.parent,
        uid=uid,
        gids=gids,
    )
    entry_errors, entry_count = _entry_errors(
        state_root,
        uid=uid,
        gid=gid,
        gids=gids,
    )

    errors.extend(entry_errors)
    if not owner_ok:
        errors.append("state_root_owner_mismatch")
    if not group_ok:
        errors.append("state_root_group_mismatch")
    if not mode_ok:
        errors.append("state_root_mode_mismatch")
    if not access_ok:
        errors.append("state_root_service_access_failed")
    if not traversal_ok:
        errors.append("state_root_parent_not_traversable_by_service")

    ok = not errors
    return {
        "ok": ok,
        "error": "" if ok else "holding_state_root_invalid",
        "errors": errors,
        "state_root": str(state_root.resolve()),
        "gateway_home": str(gateway_home.resolve(strict=False)),
        "service_user": service_user,
        "service_group": service_group,
        "exists": True,
        "owner_uid": root_meta.st_uid,
        "group_gid": root_meta.st_gid,
        "mode": f"{stat.S_IMODE(root_meta.st_mode):04o}",
        "expected_mode": f"{EXPECTED_MODE:04o}",
        "owner_ok": owner_ok,
        "group_ok": group_ok,
        "mode_ok": mode_ok,
        "service_access_ok": access_ok,
        "parent_traversal_ok": traversal_ok,
        "parent_traversal": traversal,
        "entry_count": entry_count,
        "entry_errors": entry_errors,
        "secret_content_inspected": False,
        "payload_content_inspected": False,
    }


def provision_state_root(
    *,
    state_root: Path,
    gateway_home: Path,
    service_user: str = DEFAULT_SERVICE_USER,
    service_group: str = DEFAULT_SERVICE_GROUP,
) -> dict[str, Any]:
    errors = _boundary_errors(
        state_root=state_root,
        gateway_home=gateway_home,
    )
    if errors:
        return {
            "ok": False,
            "error": "holding_state_root_boundary_failed",
            "errors": errors,
            "applied": False,
        }

    if os.geteuid() != 0:
        return {
            "ok": False,
            "error": "root_required_for_state_root_provisioning",
            "applied": False,
            "current_uid": os.geteuid(),
        }

    uid, gid, gids = _identity(service_user, service_group)

    traversal_ok, traversal = _service_traversal(
        state_root.parent,
        uid=uid,
        gids=gids,
    )
    if not traversal_ok:
        return {
            "ok": False,
            "error": "holding_state_root_parent_access_failed",
            "errors": ["state_root_parent_not_traversable_by_service"],
            "parent_traversal": traversal,
            "applied": False,
        }

    if state_root.exists():
        existing_errors, _entry_count = _entry_errors(
            state_root,
            uid=uid,
            gid=gid,
            gids=gids,
        )
        if existing_errors:
            return {
                "ok": False,
                "error": "holding_state_root_existing_state_unsafe",
                "errors": existing_errors,
                "applied": False,
                "recursive_owner_change": False,
            }

    created = False
    if not state_root.exists():
        # Deliberately no parents=True: parent creation is outside this gate.
        state_root.mkdir(mode=EXPECTED_MODE)
        created = True

    # Only mutate the exact root directory. Never recursively chown/chmod
    # existing Bridge state; unexpected child ownership already failed above.
    meta = state_root.stat()
    if meta.st_uid != uid or meta.st_gid != gid:
        os.chown(state_root, uid, gid)
    if stat.S_IMODE(state_root.stat().st_mode) != EXPECTED_MODE:
        os.chmod(state_root, EXPECTED_MODE)

    result = inspect_state_root(
        state_root=state_root,
        gateway_home=gateway_home,
        service_user=service_user,
        service_group=service_group,
    )
    result["applied"] = True
    result["created"] = created
    result["repair_scope"] = "dedicated_holding_state_root_only"
    result["recursive_owner_change"] = False
    result["parent_created"] = False
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify or minimally provision the dedicated WeCom Holding Bridge "
            "state root outside Gateway HERMES_HOME."
        )
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        default=DEFAULT_STATE_ROOT,
    )
    parser.add_argument(
        "--gateway-home",
        type=Path,
        default=DEFAULT_GATEWAY_HOME,
    )
    parser.add_argument(
        "--service-user",
        default=DEFAULT_SERVICE_USER,
    )
    parser.add_argument(
        "--service-group",
        default=DEFAULT_SERVICE_GROUP,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create/repair only the exact state-root directory; requires root.",
    )
    args = parser.parse_args()

    try:
        result = (
            provision_state_root(
                state_root=args.state_root,
                gateway_home=args.gateway_home,
                service_user=args.service_user,
                service_group=args.service_group,
            )
            if args.apply
            else inspect_state_root(
                state_root=args.state_root,
                gateway_home=args.gateway_home,
                service_user=args.service_user,
                service_group=args.service_group,
            )
        )
    except (KeyError, OSError, PermissionError) as exc:
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}:{exc}",
            "applied": False,
            "secret_content_inspected": False,
            "payload_content_inspected": False,
        }

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
