#!/usr/bin/env python3
"""Read-only semantic deployment gate for the XiaoYou Agenda SQLite runtime.

The Agenda runtime intentionally updates heartbeat/status tables while the
service is healthy.  SQLite file bytes, mtimes, WAL size, and SHM contents are
therefore not stable deployment invariants.

This gate snapshots logical table state instead.  The two aggregate runtime
status tables are allowed to change.  Every other application table is
protected by default, including future tables that are not known yet, so new
tickets, work facts, bindings, leases, acknowledgements, deliveries, payloads,
audits, or schema changes still fail closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any


SNAPSHOT_SCHEMA = "xiaoyou_agenda_semantic_snapshot_v1"
ALLOWED_RUNTIME_TABLES = frozenset({
    "agenda_runtime_status",
    "agenda_runtime_daily_status",
})


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__bytes_hex__": value.hex()}
    if value is None or isinstance(value, (str, int, float)):
        return value
    return str(value)


def _table_summary(connection: sqlite3.Connection, table: str, schema_sql: str) -> dict[str, Any]:
    cursor = connection.execute(f"SELECT * FROM {_quote_identifier(table)}")
    columns = [str(item[0]) for item in (cursor.description or ())]
    rows = []
    for raw in cursor.fetchall():
        row = {
            columns[index]: _json_value(raw[index])
            for index in range(len(columns))
        }
        rows.append(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    rows.sort()
    digest = hashlib.sha256()
    digest.update(str(schema_sql or "").encode("utf-8"))
    digest.update(b"\n")
    for row in rows:
        digest.update(row.encode("utf-8"))
        digest.update(b"\n")
    return {
        "row_count": len(rows),
        "digest": digest.hexdigest(),
        "schema_digest": hashlib.sha256(str(schema_sql or "").encode("utf-8")).hexdigest(),
    }


def snapshot_database(database: str | Path) -> dict[str, Any]:
    """Capture a content-free logical snapshot of one Agenda database.

    The connection is opened read-only and query-only.  No checkpoint, pragma
    mutation, schema migration, or business write is performed.
    """

    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"agenda_database_missing:{path}")

    uri = f"file:{path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        tables = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        summaries: dict[str, dict[str, Any]] = {}
        for item in tables:
            name = str(item["name"])
            summaries[name] = _table_summary(connection, name, str(item["sql"] or ""))

    runtime = {
        name: summary
        for name, summary in summaries.items()
        if name in ALLOWED_RUNTIME_TABLES
    }
    semantic = {
        name: summary
        for name, summary in summaries.items()
        if name not in ALLOWED_RUNTIME_TABLES
    }
    return {
        "schema_version": SNAPSHOT_SCHEMA,
        "database_name": path.name,
        "allowed_runtime_tables": sorted(ALLOWED_RUNTIME_TABLES),
        "runtime_tables": runtime,
        "semantic_tables": semantic,
        "semantic_table_count": len(semantic),
        "runtime_table_count": len(runtime),
        "contains_row_data": False,
        "read_only": True,
    }


def _changed_tables(
    before: dict[str, Any],
    after: dict[str, Any],
    section: str,
) -> list[str]:
    left = before.get(section) if isinstance(before.get(section), dict) else {}
    right = after.get(section) if isinstance(after.get(section), dict) else {}
    names = sorted(set(left) | set(right))
    return [name for name in names if left.get(name) != right.get(name)]


def compare_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    if str(before.get("schema_version") or "") != SNAPSHOT_SCHEMA:
        raise ValueError("before_snapshot_schema_invalid")
    if str(after.get("schema_version") or "") != SNAPSHOT_SCHEMA:
        raise ValueError("after_snapshot_schema_invalid")

    semantic_changes = _changed_tables(before, after, "semantic_tables")
    runtime_changes = _changed_tables(before, after, "runtime_tables")
    return {
        "ok": not semantic_changes,
        "gate": "agenda_semantic_deployment_gate_v1",
        "semantic_changes_detected": bool(semantic_changes),
        "changed_semantic_tables": semantic_changes,
        "allowed_runtime_changes_detected": bool(runtime_changes),
        "changed_runtime_tables": runtime_changes,
        "ignored_physical_files": [
            "agenda_work_runtime.sqlite",
            "agenda_work_runtime.sqlite-wal",
            "agenda_work_runtime.sqlite-shm",
        ],
        "policy": {
            "physical_sqlite_hash_change_is_failure": False,
            "runtime_status_table_change_is_failure": False,
            "all_other_application_table_change_is_failure": True,
        },
    }


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"snapshot_not_object:{path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only semantic deployment gate for Agenda SQLite.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--database", type=Path, required=True)
    snapshot_parser.add_argument("--output", type=Path, required=True)

    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("--before", type=Path, required=True)
    compare_parser.add_argument("--after", type=Path, required=True)
    compare_parser.add_argument("--strict", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            result = snapshot_database(args.database)
            _write_json(args.output, result)
            print(json.dumps({
                "ok": True,
                "output": str(args.output),
                "semantic_table_count": result["semantic_table_count"],
                "runtime_table_count": result["runtime_table_count"],
                "read_only": True,
            }, ensure_ascii=False))
            return 0

        result = compare_snapshots(_load_json(args.before), _load_json(args.after))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 1 if args.strict and not result["ok"] else 0
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}:{exc}",
        }, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
