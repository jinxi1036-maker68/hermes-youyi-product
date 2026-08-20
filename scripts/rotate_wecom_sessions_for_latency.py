"""Soft-rotate oversized WeCom sessions without deleting conversation history."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any


def _digest(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:12]


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _candidate_sessions(
    connection: sqlite3.Connection,
    *,
    users: set[str],
    min_messages: int,
) -> list[dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _item in users)
    if not placeholders:
        return []
    rows = connection.execute(
        f"""
        SELECT id, user_id, session_key, message_count, started_at
        FROM sessions
        WHERE source = 'wecom_callback'
          AND user_id IN ({placeholders})
          AND archived = 0
          AND ended_at IS NULL
          AND message_count >= ?
        ORDER BY started_at
        """,
        (*sorted(users), max(1, int(min_messages))),
    ).fetchall()
    return [dict(row) for row in rows]


def _matching_routes(
    connection: sqlite3.Connection,
    *,
    session_ids: set[str],
) -> list[dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    matches: list[dict[str, Any]] = []
    for row in connection.execute("SELECT rowid, scope, session_key, entry_json FROM gateway_routing"):
        try:
            entry = json.loads(str(row["entry_json"] or "{}"))
        except (TypeError, ValueError):
            entry = {}
        if str(entry.get("session_id") or "") in session_ids:
            matches.append({
                "rowid": int(row["rowid"]),
                "scope": str(row["scope"] or ""),
                "session_key": str(row["session_key"] or ""),
            })
    return matches


def rotate(
    *,
    state_db: Path,
    sessions_json: Path,
    users: set[str],
    min_messages: int = 80,
    apply: bool = False,
) -> dict[str, Any]:
    if not state_db.exists():
        return {"ok": False, "error": "state_db_missing", "state_db": str(state_db)}
    connection = sqlite3.connect(state_db)
    try:
        candidates = _candidate_sessions(connection, users=users, min_messages=min_messages)
        session_ids = {str(row["id"]) for row in candidates}
        routes = _matching_routes(connection, session_ids=session_ids)
        mirror = _load_json(sessions_json)
        mirror_keys = [
            key for key, value in mirror.items()
            if isinstance(value, dict) and str(value.get("session_id") or "") in session_ids
        ]
        result: dict[str, Any] = {
            "ok": True,
            "apply": bool(apply),
            "candidate_count": len(candidates),
            "route_count": len(routes),
            "mirror_route_count": len(mirror_keys),
            "candidates": [
                {
                    "session_hash": _digest(str(row["id"])),
                    "user_hash": _digest(str(row["user_id"])),
                    "message_count": int(row["message_count"] or 0),
                }
                for row in candidates
            ],
            "history_deleted": False,
            "writeback_verified": not apply,
        }
        if not apply or not candidates:
            return result

        updated_mirror = dict(mirror)
        for key in mirror_keys:
            updated_mirror.pop(key, None)
        temporary: Path | None = None
        if sessions_json.exists():
            temporary = sessions_json.with_name(f".{sessions_json.name}.{os.getpid()}.tmp")
            temporary.write_text(
                json.dumps(updated_mirror, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        ended_at = time.time()
        connection.execute("BEGIN IMMEDIATE")
        for session_id in session_ids:
            connection.execute(
                """
                UPDATE sessions
                SET archived = 1, ended_at = ?, end_reason = ?
                WHERE id = ? AND archived = 0 AND ended_at IS NULL
                """,
                (ended_at, "latency_context_rotation_preserved_history", session_id),
            )
        for route in routes:
            connection.execute("DELETE FROM gateway_routing WHERE rowid = ?", (route["rowid"],))
        connection.commit()
        if temporary is not None:
            os.replace(temporary, sessions_json)

        placeholders = ",".join("?" for _item in session_ids)
        active_count = int(connection.execute(
            f"SELECT COUNT(*) FROM sessions WHERE id IN ({placeholders}) AND (archived = 0 OR ended_at IS NULL)",
            tuple(sorted(session_ids)),
        ).fetchone()[0])
        remaining_routes = _matching_routes(connection, session_ids=session_ids)
        remaining_mirror = _load_json(sessions_json)
        remaining_mirror_ids = {
            str(value.get("session_id") or "")
            for value in remaining_mirror.values()
            if isinstance(value, dict)
        }
        verified = (
            active_count == 0
            and not remaining_routes
            and not (session_ids & remaining_mirror_ids)
        )
        result.update({
            "ok": bool(verified),
            "writeback_verified": bool(verified),
            "archived_count": len(session_ids),
            "removed_route_count": len(routes),
            "removed_mirror_route_count": len(mirror_keys),
        })
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-db", required=True, type=Path)
    parser.add_argument("--sessions-json", required=True, type=Path)
    parser.add_argument("--user", action="append", required=True)
    parser.add_argument("--min-messages", type=int, default=80)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = rotate(
        state_db=args.state_db,
        sessions_json=args.sessions_json,
        users={str(value).strip() for value in args.user if str(value).strip()},
        min_messages=args.min_messages,
        apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
