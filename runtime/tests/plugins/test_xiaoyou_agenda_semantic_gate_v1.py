from __future__ import annotations

import sqlite3


def _create_runtime_database(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(
            """
            CREATE TABLE agenda_runtime_status (
                tenant_id TEXT PRIMARY KEY,
                started_at REAL NOT NULL,
                last_cycle_at REAL NOT NULL,
                cycle_count INTEGER NOT NULL,
                last_observed INTEGER NOT NULL,
                last_admitted INTEGER NOT NULL,
                last_duplicates INTEGER NOT NULL,
                last_issued_ticket_id TEXT NOT NULL DEFAULT '',
                last_error_class TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE agenda_runtime_daily_status (
                tenant_id TEXT NOT NULL,
                local_date TEXT NOT NULL,
                first_cycle_at REAL NOT NULL,
                last_cycle_at REAL NOT NULL,
                cycle_count INTEGER NOT NULL,
                observed_facts INTEGER NOT NULL,
                admitted_facts INTEGER NOT NULL,
                duplicate_facts INTEGER NOT NULL,
                issued_ticket_count INTEGER NOT NULL,
                PRIMARY KEY(tenant_id, local_date)
            );
            CREATE TABLE agenda_service_tickets (
                ticket_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                state TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                lease_until REAL NOT NULL DEFAULT 0,
                inbox_ack_state TEXT NOT NULL DEFAULT '',
                delivery_state TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE work_facts (
                work_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                state TEXT NOT NULL,
                lease_owner TEXT,
                lease_until REAL,
                delivered_batch_id TEXT,
                delivered_at REAL
            );
            """
        )
        connection.execute(
            "INSERT INTO agenda_runtime_status VALUES (?,?,?,?,?,?,?,?,?)",
            ("tenant-a", 1.0, 1.0, 1, 0, 0, 0, "", ""),
        )
        connection.execute(
            "INSERT INTO agenda_runtime_daily_status VALUES (?,?,?,?,?,?,?,?,?)",
            ("tenant-a", "2026-09-23", 1.0, 1.0, 1, 0, 0, 0, 0),
        )


def test_runtime_heartbeat_and_wal_changes_do_not_fail_semantic_gate(tmp_path) -> None:
    from scripts.xiaoyou_agenda_semantic_gate import compare_snapshots, snapshot_database

    database = tmp_path / "agenda_work_runtime.sqlite"
    _create_runtime_database(database)
    before = snapshot_database(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE agenda_runtime_status SET last_cycle_at=?, cycle_count=cycle_count+1 WHERE tenant_id=?",
            (45.0, "tenant-a"),
        )
        connection.execute(
            "UPDATE agenda_runtime_daily_status SET last_cycle_at=?, cycle_count=cycle_count+1 "
            "WHERE tenant_id=? AND local_date=?",
            (45.0, "tenant-a", "2026-09-23"),
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    after = snapshot_database(database)
    result = compare_snapshots(before, after)

    assert result["ok"] is True
    assert result["semantic_changes_detected"] is False
    assert result["changed_semantic_tables"] == []
    assert set(result["changed_runtime_tables"]) == {
        "agenda_runtime_status",
        "agenda_runtime_daily_status",
    }
    assert result["policy"]["physical_sqlite_hash_change_is_failure"] is False


def test_runtime_status_schema_change_still_fails_closed(tmp_path) -> None:
    from scripts.xiaoyou_agenda_semantic_gate import compare_snapshots, snapshot_database

    database = tmp_path / "agenda_work_runtime.sqlite"
    _create_runtime_database(database)
    before = snapshot_database(database)

    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE agenda_runtime_status ADD COLUMN new_runtime_field TEXT NOT NULL DEFAULT ''")

    result = compare_snapshots(before, snapshot_database(database))

    assert result["ok"] is False
    assert result["runtime_schema_changes_detected"] is True
    assert result["changed_runtime_schema_tables"] == ["agenda_runtime_status"]


def test_new_ticket_fails_semantic_gate(tmp_path) -> None:
    from scripts.xiaoyou_agenda_semantic_gate import compare_snapshots, snapshot_database

    database = tmp_path / "agenda_work_runtime.sqlite"
    _create_runtime_database(database)
    before = snapshot_database(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO agenda_service_tickets(ticket_id,tenant_id,state,updated_at) VALUES (?,?,?,?)",
            ("ticket-1", "tenant-a", "issued", "2026-09-23T11:52:45+08:00"),
        )

    result = compare_snapshots(before, snapshot_database(database))

    assert result["ok"] is False
    assert result["changed_semantic_tables"] == ["agenda_service_tickets"]


def test_lease_ack_or_delivery_state_change_fails_semantic_gate(tmp_path) -> None:
    from scripts.xiaoyou_agenda_semantic_gate import compare_snapshots, snapshot_database

    database = tmp_path / "agenda_work_runtime.sqlite"
    _create_runtime_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO work_facts(work_id,tenant_id,state) VALUES (?,?,?)",
            ("work-1", "tenant-a", "ready"),
        )
    before = snapshot_database(database)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE work_facts SET state=?, lease_owner=?, lease_until=?, delivered_batch_id=?, delivered_at=? "
            "WHERE work_id=?",
            ("leased", "worker-1", 99.0, "batch-1", 100.0, "work-1"),
        )

    result = compare_snapshots(before, snapshot_database(database))

    assert result["ok"] is False
    assert result["changed_semantic_tables"] == ["work_facts"]


def test_unknown_future_application_table_is_protected_by_default(tmp_path) -> None:
    from scripts.xiaoyou_agenda_semantic_gate import compare_snapshots, snapshot_database

    database = tmp_path / "agenda_work_runtime.sqlite"
    _create_runtime_database(database)
    before = snapshot_database(database)

    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE future_delivery_truth (id TEXT PRIMARY KEY, state TEXT NOT NULL)")
        connection.execute("INSERT INTO future_delivery_truth VALUES (?,?)", ("delivery-1", "accepted"))

    result = compare_snapshots(before, snapshot_database(database))

    assert result["ok"] is False
    assert result["changed_semantic_tables"] == ["future_delivery_truth"]


def test_snapshot_contains_hashes_and_counts_but_no_business_rows(tmp_path) -> None:
    from scripts.xiaoyou_agenda_semantic_gate import snapshot_database

    database = tmp_path / "agenda_work_runtime.sqlite"
    _create_runtime_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO agenda_service_tickets(ticket_id,tenant_id,state,updated_at) VALUES (?,?,?,?)",
            ("secret-ticket-id", "tenant-a", "issued", "2026-09-23T11:52:45+08:00"),
        )

    snapshot = snapshot_database(database)
    serialized = __import__("json").dumps(snapshot, ensure_ascii=False)

    assert snapshot["read_only"] is True
    assert snapshot["contains_row_data"] is False
    assert snapshot["semantic_tables"]["agenda_service_tickets"]["row_count"] == 1
    assert "secret-ticket-id" not in serialized
