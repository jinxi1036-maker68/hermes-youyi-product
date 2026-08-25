from __future__ import annotations

import json
from pathlib import Path
import sqlite3


def _seed(state_db: Path, sessions_json: Path) -> None:
    connection = sqlite3.connect(state_db)
    connection.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, user_id TEXT, session_key TEXT,
            message_count INTEGER, started_at REAL, archived INTEGER,
            ended_at REAL, end_reason TEXT, input_tokens INTEGER,
            handoff_state TEXT, handoff_platform TEXT
        );
        CREATE TABLE gateway_routing (
            scope TEXT, session_key TEXT, entry_json TEXT, updated_at REAL
        );
        """
    )
    rows = [
        ("boss-long", "wecom_callback", "JinWenJie", "key:boss", 129, 1.0, 0, None, None, 3000, None, None),
        ("teacher-long", "wecom_callback", "CeShi", "key:teacher", 145, 2.0, 0, None, None, 40000, None, None),
        ("teacher-short", "wecom_callback", "CeShi", "key:short", 10, 3.0, 0, None, None, 1000, None, None),
        ("other-long", "wecom_callback", "OtherTeacher", "key:other", 200, 4.0, 0, None, None, 90000, None, None),
    ]
    connection.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    for session_id, session_key in (("boss-long", "key:boss"), ("teacher-long", "key:teacher"), ("teacher-short", "key:short"), ("other-long", "key:other")):
        connection.execute(
            "INSERT INTO gateway_routing VALUES (?,?,?,?)",
            ("scope", session_key, json.dumps({"session_id": session_id, "last_prompt_tokens": {"boss-long": 34000, "teacher-long": 40000, "teacher-short": 1000, "other-long": 96000}[session_id]}), 1.0),
        )
    connection.commit()
    connection.close()
    sessions_json.write_text(
        json.dumps({
            "key:boss": {"session_id": "boss-long"},
            "key:teacher": {"session_id": "teacher-long"},
            "key:short": {"session_id": "teacher-short"},
            "key:other": {"session_id": "other-long"},
        }),
        encoding="utf-8",
    )


def test_rotation_is_dry_run_then_soft_archives_only_target_long_sessions(tmp_path: Path):
    from scripts.rotate_wecom_sessions_for_latency import rotate

    state_db = tmp_path / "state.db"
    sessions_json = tmp_path / "sessions.json"
    _seed(state_db, sessions_json)

    dry = rotate(
        state_db=state_db,
        sessions_json=sessions_json,
        users={"JinWenJie", "CeShi"},
        min_messages=80,
        apply=False,
    )
    assert dry["candidate_count"] == 2
    assert dry["history_deleted"] is False

    applied = rotate(
        state_db=state_db,
        sessions_json=sessions_json,
        users={"JinWenJie", "CeShi"},
        min_messages=80,
        apply=True,
    )
    assert applied["ok"] is True
    assert applied["writeback_verified"] is True
    assert applied["archived_count"] == 2
    assert applied["handoff_recorded"] is True

    connection = sqlite3.connect(state_db)
    states = dict(connection.execute("SELECT id, archived FROM sessions"))
    route_ids = {
        json.loads(row[0])["session_id"]
        for row in connection.execute("SELECT entry_json FROM gateway_routing")
    }
    message_counts = dict(connection.execute("SELECT id, message_count FROM sessions"))
    handoff_states = dict(connection.execute("SELECT id, handoff_state FROM sessions"))
    connection.close()
    assert states["boss-long"] == 1
    assert states["teacher-long"] == 1
    assert states["teacher-short"] == 0
    assert states["other-long"] == 0
    assert route_ids == {"teacher-short", "other-long"}
    assert message_counts["boss-long"] == 129
    assert message_counts["teacher-long"] == 145
    assert handoff_states["boss-long"] == "trusted_state_projection_ready"
    mirror = json.loads(sessions_json.read_text(encoding="utf-8"))
    assert set(mirror) == {"key:short", "key:other"}


def test_rotation_uses_live_prompt_threshold_even_before_message_count(tmp_path: Path):
    from scripts.rotate_wecom_sessions_for_latency import rotate

    state_db = tmp_path / "state.db"
    sessions_json = tmp_path / "sessions.json"
    _seed(state_db, sessions_json)
    result = rotate(
        state_db=state_db,
        sessions_json=sessions_json,
        users={"CeShi"},
        min_messages=999,
        max_input_tokens=32000,
        apply=False,
    )

    assert result["candidate_count"] == 1
    assert result["candidates"][0]["last_prompt_tokens"] == 40000


def test_rotation_never_uses_cumulative_input_tokens_as_a_live_context_measure(tmp_path: Path):
    from scripts.rotate_wecom_sessions_for_latency import rotate

    state_db = tmp_path / "state.db"
    sessions_json = tmp_path / "sessions.json"
    _seed(state_db, sessions_json)
    connection = sqlite3.connect(state_db)
    connection.execute("UPDATE sessions SET input_tokens = ? WHERE id = ?", (999_999, "teacher-short"))
    connection.commit()
    connection.close()

    result = rotate(
        state_db=state_db,
        sessions_json=sessions_json,
        users={"CeShi"},
        min_messages=999,
        max_input_tokens=32_000,
        apply=False,
    )

    assert result["candidate_count"] == 1
    assert result["candidates"][0]["last_prompt_tokens"] == 40000
