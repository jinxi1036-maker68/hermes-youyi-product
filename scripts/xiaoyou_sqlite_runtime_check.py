#!/usr/bin/env python3
"""Verify SQLite runtime compatibility without touching production databases."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any


MIN_SQLITE_VERSION = (3, 51, 3)


def _version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for item in str(value or "").split("."):
        try:
            parts.append(int(item))
        except ValueError:
            break
    return tuple(parts)


def run_sqlite_drill(*, work_dir: Path | None = None, minimum: tuple[int, ...] = MIN_SQLITE_VERSION) -> dict[str, Any]:
    sqlite_version = sqlite3.sqlite_version
    version_ok = _version_tuple(sqlite_version) >= minimum
    parent = work_dir or Path(tempfile.mkdtemp(prefix="xiaoyou-sqlite-check-"))
    parent.mkdir(parents=True, exist_ok=True)
    source = parent / "source.db"
    restored = parent / "restored.db"
    transaction_ok = False
    rollback_ok = False
    backup_ok = False
    integrity = "not_run"
    error = ""
    try:
        with sqlite3.connect(source) as connection:
            journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            connection.execute("CREATE TABLE events (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("INSERT INTO events VALUES (?, ?)", ("one", "committed"))
            connection.commit()
            transaction_ok = connection.execute("SELECT value FROM events WHERE id='one'").fetchone() == ("committed",)
            try:
                connection.execute("BEGIN")
                connection.execute("INSERT INTO events VALUES (?, ?)", ("two", "rolled_back"))
                raise RuntimeError("rollback_probe")
            except RuntimeError:
                connection.rollback()
            rollback_ok = connection.execute("SELECT COUNT(*) FROM events WHERE id='two'").fetchone()[0] == 0
            with sqlite3.connect(restored) as target:
                connection.backup(target)
        with sqlite3.connect(restored) as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            backup_ok = connection.execute("SELECT value FROM events WHERE id='one'").fetchone() == ("committed",)
    except Exception as exc:
        journal_mode = "unknown"
        error = f"{type(exc).__name__}:{exc}"
    ok = version_ok and transaction_ok and rollback_ok and backup_ok and integrity == "ok" and not error
    return {
        "ok": ok,
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "sqlite_version": sqlite_version,
        "minimum_sqlite_version": ".".join(str(item) for item in minimum),
        "version_ok": version_ok,
        "transaction_ok": transaction_ok,
        "rollback_ok": rollback_ok,
        "backup_restore_ok": backup_ok,
        "integrity_check": integrity,
        "journal_mode": journal_mode,
        "production_databases_touched": False,
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an isolated Xiaoyou SQLite compatibility drill.")
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--minimum", default="3.51.3")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    minimum = _version_tuple(args.minimum)
    result = run_sqlite_drill(work_dir=args.work_dir, minimum=minimum)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if args.strict and not result.get("ok") else 0


if __name__ == "__main__":
    raise SystemExit(main())
