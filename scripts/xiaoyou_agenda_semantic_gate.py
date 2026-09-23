#!/usr/bin/env python3
"""Read-only semantic deployment gate for XiaoYou Agenda SQLite state.

Physical SQLite files are runtime artifacts: WAL checkpoints and scheduler
heartbeats may change database, -wal, and -shm bytes without changing any
business/work fact.  This gate snapshots logical table contents and ignores
only the two known heartbeat/statistics tables.

Every other user table is treated as semantic by default.  That makes the
gate fail closed when a deployment window creates or mutates tickets, work
facts, payloads, bindings, batches, receipts, reply jobs, leases, delivery
state, or any future non-heartbeat table.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any


SCHEMA_VERSION = "xiaoyou_agenda_semantic_gate_v1"
IGNORED_RUNTIME_TABLES = frozenset({
    "agenda_runtime_status",
    "agenda_runtime_daily_status",
})


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__blob_hex__": value.hex()}
    return value


def _connect_read_only(database: Path) -> sqlite3.Connection:
    if not database.is_file():
        raise FileNotFoundError(str(database))
    connection = sqlite3.connect(
        f"file:{database.resolve()}?mode=ro",
        uri=True,
        timeout=5.0,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _user_tables(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return [str(row["name"]) for row in rows]


def _table_snapshot(connection: sqlite3.Connection, table: str) -> dict[str, Any]:
    quoted = _quote_identifier(table)
    columns = [
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({quoted})").fetchall()
    ]
    if not columns:
        return {"row_count": 0, "digest": sha256(b"[]").hexdigest(), "columns": []}

    rows = connection.execute(f"SELECT * FROM {quoted}").fetchall()
    canonical_rows: list[str] = []
    for row in rows:
        payload = {
            column: _json_value(row[column])
            for column in columns
        }
        canonical_rows.append(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
    canonical_rows.sort()
    body = "\n".join(canonical_rows).encode("utf-8")
    return {
        "row_count": len(canonical_rows),
        "digest": sha256(body).hexdigest(),
        "columns": columns,
    }


def snapshot_database(database: str | Path) -> dict[str, Any]:
    path = Path(database).expanduser().resolve()
    with _connect_read_only(path) as connection:
        tables = {
            table: _table_snapshot(connection, table)
            for table in _user_tables(connection)
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "database": str(path),
        "captured_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "ignored_runtime_tables": sorted(IGNORED_RUNTIME_TABLES),
        "tables": tables,
    }


def compare_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_tables = before.get("tables") if isinstance(before.get("tables"), dict) else {}
    after_tables = after.get("tables") if isinstance(after.get("tables"), dict) else {}
    all_tables = sorted(set(before_tables) | set(after_tables))

    semantic_changes: list[dict[str, Any]] = []
    runtime_changes: list[dict[str, Any]] = []
    for table in all_tables:
        left = before_tables.get(table)
        right = after_tables.get(table)
        if left == right:
            continue
        change = {
            "table": table,
            "before": left,
            "after": right,
        }
        if table in IGNORED_RUNTIME_TABLES:
            runtime_changes.append(change)
        else:
            semantic_changes.append(change)

    return {
        "ok": not semantic_changes,
        "schema_version": SCHEMA_VERSION,
        "before_database": str(before.get("database") or ""),
        "after_database": str(after.get("database") or ""),
        "semantic_change_count": len(semantic_changes),
        "runtime_change_count": len(runtime_changes),
        "semantic_changes": semantic_changes,
        "runtime_changes": runtime_changes,
        "ignored_runtime_tables": sorted(IGNORED_RUNTIME_TABLES),
        "decision": (
            "pass_runtime_only_changes"
            if not semantic_changes and runtime_changes
            else "pass_no_changes"
            if not semantic_changes
            else "fail_semantic_state_changed"
        ),
    }


def _load_snapshot(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("invalid_agenda_semantic_snapshot")
    return payload


def _write_json(path: Path | None, payload: dict[str, Any]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if path is None:
        print(rendered, end="")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only semantic gate for XiaoYou Agenda SQLite deployment checks."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--database", type=Path, required=True)
    snapshot_parser.add_argument("--output", type=Path)

    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("--before", type=Path, required=True)
    compare_parser.add_argument("--database", type=Path, required=True)
    compare_parser.add_argument("--after-output", type=Path)

    args = parser.parse_args()
    try:
        if args.command == "snapshot":
            payload = snapshot_database(args.database)
            _write_json(args.output, payload)
            return 0

        before = _load_snapshot(args.before)
        after = snapshot_database(args.database)
        if args.after_output is not None:
            _write_json(args.after_output, after)
        result = compare_snapshots(before, after)
        _write_json(None, result)
        return 0 if result["ok"] else 1
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}:{exc}",
            "decision": "fail_gate_error",
        }, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
