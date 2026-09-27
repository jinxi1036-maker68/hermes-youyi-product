#!/usr/bin/env python3
"""Plan and rehearse migration from a release-bound Hermes Home to persistent runtime state."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import stat
from typing import Any, Iterable

try:
    from .xiaoyou_runtime_topology import validate_runtime_topology
except ImportError:
    from xiaoyou_runtime_topology import validate_runtime_topology


DEFAULT_SERVICE_USER = "hermes-youyi"
DEFAULT_EXCLUDED_TOP_LEVEL = {
    "workspace",
    "workspaces",
    "agenda",
    "agenda-data",
    "agenda_data",
    "plugins",
    "skills",
}
SQLITE_MAIN = "state.db"
SQLITE_TRANSIENT = {"state.db-wal", "state.db-shm"}
RECEIPT_NAME = ".xiaoyou-runtime-home-migration.json"


@dataclass(frozen=True)
class Entry:
    logical: str
    source: str
    kind: str
    mode: int
    size: int = 0


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


def _service_uid(user_name: str) -> int:
    return pwd.getpwnam(user_name).pw_uid


def _excluded(logical: Path, excluded_top: set[str]) -> bool:
    return bool(logical.parts) and logical.parts[0] in excluded_top


def _collect(
    *,
    source_root: Path,
    logical: Path,
    actual: Path,
    excluded_top: set[str],
    stack: tuple[Path, ...],
    entries: list[Entry],
    errors: list[str],
    external_links: list[dict[str, str]],
    internal_links: list[dict[str, str]],
    ephemeral_nodes: list[dict[str, str]],
) -> None:
    if _excluded(logical, excluded_top):
        return

    raw = actual
    if raw.is_symlink():
        target = raw.resolve()
        if not _inside(target, source_root):
            external_links.append({
                "logical": logical.as_posix(),
                "target": str(target),
            })
            errors.append(f"external_symlink_requires_exclusion:{logical.as_posix()}")
            return
        internal_links.append({
            "logical": logical.as_posix(),
            "target": str(target),
        })
        actual = target

    if actual.is_dir():
        resolved = actual.resolve()
        if resolved in stack:
            errors.append(f"symlink_cycle:{logical.as_posix()}")
            return
        entries.append(Entry(
            logical=logical.as_posix(),
            source=str(resolved),
            kind="directory",
            mode=stat.S_IMODE(resolved.stat().st_mode),
        ))
        next_stack = (*stack, resolved)
        for child in sorted(actual.iterdir(), key=lambda item: item.name):
            child_logical = logical / child.name
            _collect(
                source_root=source_root,
                logical=child_logical,
                actual=child,
                excluded_top=excluded_top,
                stack=next_stack,
                entries=entries,
                errors=errors,
                external_links=external_links,
                internal_links=internal_links,
                ephemeral_nodes=ephemeral_nodes,
            )
        return

    try:
        node_mode = actual.stat().st_mode
    except FileNotFoundError:
        errors.append(f"runtime_home_node_disappeared:{logical.as_posix()}")
        return

    if stat.S_ISSOCK(node_mode):
        ephemeral_nodes.append({
            "logical": logical.as_posix(),
            "source": str(actual.resolve()),
            "kind": "unix_socket",
        })
        return

    if actual.is_file():
        resolved = actual.resolve()
        entries.append(Entry(
            logical=logical.as_posix(),
            source=str(resolved),
            kind="file",
            mode=stat.S_IMODE(resolved.stat().st_mode),
            size=resolved.stat().st_size,
        ))
        return

    errors.append(f"unsupported_runtime_home_node:{logical.as_posix()}")


def inventory_runtime_home(
    *,
    source_home: Path,
    excluded_top: Iterable[str] = (),
) -> dict[str, Any]:
    source = source_home.resolve()
    if not source.is_dir():
        return {"ok": False, "error": "source_home_missing", "source_home": str(source)}
    if source_home.is_symlink():
        return {"ok": False, "error": "source_home_must_be_direct_directory"}

    excluded = set(DEFAULT_EXCLUDED_TOP_LEVEL)
    excluded.update(str(value).strip("/") for value in excluded_top if str(value).strip("/"))

    entries: list[Entry] = []
    errors: list[str] = []
    external_links: list[dict[str, str]] = []
    internal_links: list[dict[str, str]] = []
    ephemeral_nodes: list[dict[str, str]] = []

    for child in sorted(source.iterdir(), key=lambda item: item.name):
        logical = Path(child.name)
        if _excluded(logical, excluded):
            continue
        _collect(
            source_root=source,
            logical=logical,
            actual=child,
            excluded_top=excluded,
            stack=(source,),
            entries=entries,
            errors=errors,
            external_links=external_links,
            internal_links=internal_links,
            ephemeral_nodes=ephemeral_nodes,
        )

    rows = [
        {
            "logical": item.logical,
            "source": item.source,
            "kind": item.kind,
            "mode": f"{item.mode:04o}",
            "size": item.size,
        }
        for item in entries
    ]
    return {
        "ok": not errors,
        "error": "" if not errors else "runtime_home_inventory_blocked",
        "source_home": str(source),
        "excluded_top_level": sorted(excluded),
        "entry_count": len(rows),
        "entries": rows,
        "internal_symlinks_to_dereference": internal_links,
        "external_symlinks": external_links,
        "skipped_ephemeral_nodes": ephemeral_nodes,
        "skipped_ephemeral_node_count": len(ephemeral_nodes),
        "errors": errors,
        "content_read": False,
    }


def _entry_objects(inventory: dict[str, Any]) -> list[Entry]:
    rows = []
    for item in inventory.get("entries") or []:
        rows.append(Entry(
            logical=str(item["logical"]),
            source=str(item["source"]),
            kind=str(item["kind"]),
            mode=int(str(item["mode"]), 8),
            size=int(item.get("size") or 0),
        ))
    return rows


def _prepare_target(
    *,
    release_root: Path,
    target_home: Path,
    service_user: str,
) -> dict[str, Any]:
    topology = validate_runtime_topology(
        release_root=release_root,
        runtime_home=target_home,
        require_existing=False,
    )
    non_missing = [e for e in topology.get("errors", []) if e != "runtime_home_missing"]
    if non_missing:
        return {"ok": False, "error": "runtime_topology_invalid", "topology": topology}

    required_uid = _service_uid(service_user)
    current_uid = os.geteuid()
    if current_uid != required_uid:
        return {
            "ok": False,
            "error": "migration_must_run_as_service_identity",
            "current_uid": current_uid,
            "required_uid": required_uid,
        }

    if target_home.is_symlink():
        return {"ok": False, "error": "target_home_must_not_be_symlink"}
    target_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    target_home.chmod(0o700)
    return {"ok": True, "target_home": str(target_home.resolve())}


def _copy_regular_entries(entries: list[Entry], target_home: Path) -> None:
    directories = sorted(
        (item for item in entries if item.kind == "directory"),
        key=lambda row: (len(Path(row.logical).parts), row.logical),
    )
    files = sorted(
        (item for item in entries if item.kind != "directory"),
        key=lambda row: row.logical,
    )

    # Hermes intentionally locks installed trees to 0555/0444.  A repeated
    # migration must still be able to replace their state without weakening
    # the final permissions, so keep destination directories writable only
    # for the duration of the copy.
    for item in directories:
        target = target_home / Path(item.logical)
        target.mkdir(parents=True, exist_ok=True)
        target.chmod(item.mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

    for item in files:
        logical = Path(item.logical)
        target = target_home / logical
        if logical.as_posix() in {SQLITE_MAIN, *SQLITE_TRANSIENT}:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.migration-{os.getpid()}")
        temp.unlink(missing_ok=True)
        try:
            shutil.copy2(item.source, temp, follow_symlinks=True)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    for item in sorted(
        directories,
        key=lambda row: (len(Path(row.logical).parts), row.logical),
        reverse=True,
    ):
        (target_home / Path(item.logical)).chmod(item.mode)


def _sqlite_backup(source_db: Path, target_db: Path) -> dict[str, Any]:
    if not source_db.is_file():
        return {"ok": True, "copied": False}
    target_db.parent.mkdir(parents=True, exist_ok=True)
    temp = target_db.with_name(target_db.name + ".migration-tmp")
    if temp.exists():
        temp.unlink()
    source_conn = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    target_conn = sqlite3.connect(temp)
    try:
        source_conn.backup(target_conn)
        target_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        integrity = target_conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        target_conn.close()
        source_conn.close()
    if integrity != "ok":
        temp.unlink(missing_ok=True)
        return {"ok": False, "error": f"sqlite_integrity_failed:{integrity}"}
    os.replace(temp, target_db)
    for suffix in ("-wal", "-shm"):
        (target_home_file := Path(str(target_db) + suffix)).unlink(missing_ok=True)
    return {"ok": True, "copied": True, "integrity_check": "ok"}


def _prune_target(entries: list[Entry], target_home: Path) -> list[str]:
    allowed = {Path(item.logical).as_posix() for item in entries}
    allowed.add(SQLITE_MAIN)
    allowed.add(RECEIPT_NAME)
    removed: list[str] = []
    for path in sorted(target_home.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        logical = path.relative_to(target_home).as_posix()
        if logical in allowed:
            continue
        if any(value.startswith(logical + "/") for value in allowed):
            continue
        parent = path.parent
        parent_mode = stat.S_IMODE(parent.stat().st_mode)
        parent_changed = not bool(parent_mode & stat.S_IWUSR)
        if parent_changed:
            parent.chmod(parent_mode | stat.S_IWUSR)
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
                removed.append(logical)
            elif path.is_dir():
                try:
                    path.rmdir()
                    removed.append(logical)
                except OSError:
                    pass
        finally:
            if parent_changed and parent.exists():
                parent.chmod(parent_mode)
    return removed


def migrate_runtime_home(
    *,
    mode: str,
    release_root: Path,
    source_home: Path,
    target_home: Path,
    service_user: str = DEFAULT_SERVICE_USER,
    excluded_top: Iterable[str] = (),
    gateway_stopped_confirmed: bool = False,
) -> dict[str, Any]:
    inventory = inventory_runtime_home(
        source_home=source_home,
        excluded_top=excluded_top,
    )
    if mode == "plan":
        return inventory
    if not inventory.get("ok"):
        return inventory

    if mode == "finalize" and not gateway_stopped_confirmed:
        return {"ok": False, "error": "gateway_stopped_confirmation_required"}

    prepared = _prepare_target(
        release_root=release_root,
        target_home=target_home,
        service_user=service_user,
    )
    if not prepared.get("ok"):
        return prepared

    entries = _entry_objects(inventory)
    _copy_regular_entries(entries, target_home)

    source_db = source_home.resolve() / SQLITE_MAIN
    sqlite_result = _sqlite_backup(source_db, target_home / SQLITE_MAIN)
    if not sqlite_result.get("ok"):
        return sqlite_result

    removed: list[str] = []
    if mode == "finalize":
        removed = _prune_target(entries, target_home)

    receipt = {
        "schema_version": "xiaoyou_runtime_home_migration_v1",
        "mode": mode,
        "source_home": str(source_home.resolve()),
        "target_home": str(target_home.resolve()),
        "entry_count": len(entries),
        "excluded_top_level": inventory.get("excluded_top_level"),
        "internal_symlinks_dereferenced": len(inventory.get("internal_symlinks_to_dereference") or []),
        "external_symlinks": inventory.get("external_symlinks") or [],
        "skipped_ephemeral_nodes": inventory.get("skipped_ephemeral_nodes") or [],
        "sqlite": sqlite_result,
        "gateway_stopped_confirmed": bool(gateway_stopped_confirmed),
        "pruned_target_paths": removed,
    }
    (target_home / RECEIPT_NAME).write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "ok": True,
        "mode": mode,
        "source_home": str(source_home.resolve()),
        "target_home": str(target_home.resolve()),
        "entry_count": len(entries),
        "sqlite": sqlite_result,
        "receipt": str(target_home / RECEIPT_NAME),
        "production_switch_performed": False,
    }


def verify_runtime_home(
    *,
    source_home: Path,
    target_home: Path,
    excluded_top: Iterable[str] = (),
) -> dict[str, Any]:
    inventory = inventory_runtime_home(
        source_home=source_home,
        excluded_top=excluded_top,
    )
    if not inventory.get("ok"):
        return inventory
    mismatches: list[dict[str, str]] = []
    checked = 0
    for item in _entry_objects(inventory):
        if item.kind != "file":
            continue
        logical = Path(item.logical)
        if logical.as_posix() in {SQLITE_MAIN, *SQLITE_TRANSIENT}:
            continue
        target = target_home / logical
        if not target.is_file():
            mismatches.append({"path": item.logical, "error": "target_missing"})
            continue
        checked += 1
        if _sha256(Path(item.source)) != _sha256(target):
            mismatches.append({"path": item.logical, "error": "hash_mismatch"})

    source_db = source_home.resolve() / SQLITE_MAIN
    target_db = target_home / SQLITE_MAIN
    sqlite_integrity = "missing"
    if source_db.is_file() and not target_db.is_file():
        mismatches.append({"path": SQLITE_MAIN, "error": "target_missing"})
    elif target_db.is_file():
        connection = sqlite3.connect(f"file:{target_db}?mode=ro", uri=True)
        try:
            sqlite_integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            connection.close()

    ok = not mismatches and (sqlite_integrity in {"missing", "ok"})
    return {
        "ok": ok,
        "error": "" if ok else "runtime_home_verification_failed",
        "checked_file_count": checked,
        "mismatches": mismatches,
        "sqlite_integrity_check": sqlite_integrity,
        "source_home": str(source_home.resolve()),
        "target_home": str(target_home.resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan, seed, finalize, or verify a persistent Hermes Home migration.")
    parser.add_argument("mode", choices=("plan", "seed", "finalize", "verify"))
    parser.add_argument("--release-root", type=Path)
    parser.add_argument("--source-home", type=Path, required=True)
    parser.add_argument("--target-home", type=Path)
    parser.add_argument("--service-user", default=DEFAULT_SERVICE_USER)
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--gateway-stopped-confirmed", action="store_true")
    args = parser.parse_args()

    try:
        if args.mode == "plan":
            result = inventory_runtime_home(
                source_home=args.source_home,
                excluded_top=args.exclude,
            )
        elif args.mode == "verify":
            if args.target_home is None:
                result = {"ok": False, "error": "target_home_required"}
            else:
                result = verify_runtime_home(
                    source_home=args.source_home,
                    target_home=args.target_home,
                    excluded_top=args.exclude,
                )
        elif args.release_root is None or args.target_home is None:
            result = {"ok": False, "error": "release_root_and_target_home_required"}
        else:
            result = migrate_runtime_home(
                mode=args.mode,
                release_root=args.release_root,
                source_home=args.source_home,
                target_home=args.target_home,
                service_user=args.service_user,
                excluded_top=args.exclude,
                gateway_stopped_confirmed=args.gateway_stopped_confirmed,
            )
    except (OSError, RuntimeError, ValueError, sqlite3.Error, KeyError) as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
