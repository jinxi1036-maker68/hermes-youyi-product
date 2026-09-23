from __future__ import annotations

import sqlite3


def _seed_database(path):
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE agenda_runtime_status (
                tenant_id TEXT PRIMARY KEY,
                last_cycle_at REAL NOT NULL,
                cycle_count INTEGER NOT NULL
            );
            CREATE TABLE agenda_runtime_daily_status (
                tenant_id TEXT NOT NULL,
                local_date TEXT NOT NULL,
                cycle_count INTEGER NOT NULL,
                PRIMARY KEY(tenant_id, local_date)
            );
            CREATE TABLE agenda_runtime_audit (
                event_id TEXT PRIMARY KEY,
                observed_at REAL NOT NULL,
                event_type TEXT NOT NULL
            );
            CREATE TABLE agenda_service_tickets (
                ticket_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE work_facts (
                work_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                lease_until REAL
            );
            """
        )
        connection.execute(
            "INSERT INTO agenda_runtime_status VALUES (?,?,?)",
            ("tenant-a", 100.0, 1),
        )
        connection.execute(
            "INSERT INTO agenda_runtime_daily_status VALUES (?,?,?)",
            ("tenant-a", "2026-09-23", 1),
        )


def test_runtime_heartbeat_changes_do_not_fail_semantic_gate(tmp_path):
    from scripts.xiaoyou_agenda_semantic_gate import compare_snapshots, snapshot_database

    database = tmp_path / "agenda_work_runtime.sqlite"
    _seed_database(database)
    before = snapshot_database(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE agenda_runtime_status SET last_cycle_at=?, cycle_count=? WHERE tenant_id=?",
            (145.0, 4, "tenant-a"),
        )
        connection.execute(
            "UPDATE agenda_runtime_daily_status SET cycle_count=? WHERE tenant_id=? AND local_date=?",
            (4, "tenant-a", "2026-09-23"),
        )

    after = snapshot_database(database)
    result = compare_snapshots(before, after)

    assert result["ok"] is True
    assert result["decision"] == "pass_runtime_only_changes"
    assert result["semantic_change_count"] == 0
    assert {row["table"] for row in result["runtime_changes"]} == {
        "agenda_runtime_status",
        "agenda_runtime_daily_status",
    }


def test_ticket_or_work_state_change_fails_semantic_gate(tmp_path):
    from scripts.xiaoyou_agenda_semantic_gate import compare_snapshots, snapshot_database

    database = tmp_path / "agenda_work_runtime.sqlite"
    _seed_database(database)
    before = snapshot_database(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO agenda_service_tickets VALUES (?,?,?)",
            ("ticket-1", "issued", "2026-09-23T11:53:00+08:00"),
        )
        connection.execute(
            "INSERT INTO work_facts VALUES (?,?,?)",
            ("work-1", "leased", 12345.0),
        )

    result = compare_snapshots(before, snapshot_database(database))

    assert result["ok"] is False
    assert result["decision"] == "fail_semantic_state_changed"
    assert {row["table"] for row in result["semantic_changes"]} == {
        "agenda_service_tickets",
        "work_facts",
    }


def test_runtime_audit_is_not_silently_ignored(tmp_path):
    from scripts.xiaoyou_agenda_semantic_gate import compare_snapshots, snapshot_database

    database = tmp_path / "agenda_work_runtime.sqlite"
    _seed_database(database)
    before = snapshot_database(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO agenda_runtime_audit VALUES (?,?,?)",
            ("audit-1", 200.0, "ticket_reconciled"),
        )

    result = compare_snapshots(before, snapshot_database(database))

    assert result["ok"] is False
    assert result["semantic_changes"][0]["table"] == "agenda_runtime_audit"


def test_unknown_future_nonheartbeat_table_fails_closed(tmp_path):
    from scripts.xiaoyou_agenda_semantic_gate import compare_snapshots, snapshot_database

    database = tmp_path / "agenda_work_runtime.sqlite"
    _seed_database(database)
    before = snapshot_database(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE future_business_state (id TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO future_business_state VALUES (?,?)",
            ("future-1", "changed"),
        )

    result = compare_snapshots(before, snapshot_database(database))

    assert result["ok"] is False
    assert any(
        row["table"] == "future_business_state"
        for row in result["semantic_changes"]
    )
