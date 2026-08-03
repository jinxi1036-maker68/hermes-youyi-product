"""Idempotent business-data upgrades for the tutoring-center module."""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import datetime
from typing import Any

from .record_evaluation import evaluate_record
from .store import TuoguanStore

RECORD_EVALUATION_VERSION = "record_payroll_v3"

STUDENT_BUSINESS_DEFAULTS: dict[str, Any] = {
    "school": "",
    "grade": "",
    "enrollment_date": "",
    "fee_cycle": "",
    "renewal_due_date": "",
    "renewal_status": "unknown",
    "parent_concerns": [],
    "learning_goals": [],
    "relationship_temperature": "unknown",
}


def _normalized_record(record: dict[str, Any]) -> dict[str, Any]:
    item = deepcopy(record)
    timestamp = str(item.get("timestamp") or item.get("created_at") or "")
    item.setdefault("created_at", timestamp)
    item.setdefault(
        "record_type",
        str(item.get("type") or item.get("category") or "legacy_note"),
    )
    item.setdefault(
        "source",
        "teacher_message" if item.get("sender_id") or item.get("teacher") else "legacy_import",
    )
    item.setdefault(
        "teacher_userid",
        str(item.get("sender_id") or item.get("teacher") or ""),
    )
    item.setdefault("schema_version", 2)
    return item


def backfill_record_evaluations(
    store: TuoguanStore,
    *,
    dry_run: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Add payroll-grade evaluation evidence to legacy records."""
    students = store.read_json("students.json", {})
    records = store.read_json("records.json", [])
    if not isinstance(students, dict):
        students = {}
    if not isinstance(records, list):
        records = []

    student_names = {str(name) for name in students}
    upgraded_records: list[Any] = []
    previous_records: list[dict[str, Any]] = []
    changed = 0
    missing_evaluation = 0
    added_ids = 0
    legacy_marked = 0
    orphan_records: list[dict[str, Any]] = []
    sample_changes: list[dict[str, Any]] = []

    for index, raw_record in enumerate(records):
        if not isinstance(raw_record, dict):
            upgraded_records.append(raw_record)
            continue
        record = _normalized_record(raw_record)
        before = deepcopy(record)
        if not str(record.get("id") or "").strip():
            record["id"] = f"record_{uuid.uuid4().hex[:12]}"
            added_ids += 1
        source_meta = record.get("source_meta")
        if not isinstance(source_meta, dict):
            source_meta = {}
        if not source_meta:
            source_meta["legacy"] = True
            legacy_marked += 1
        record["source_meta"] = source_meta
        evaluation = record.get("record_evaluation")
        if (
            not isinstance(evaluation, dict)
            or str(evaluation.get("evaluation_version") or "") != RECORD_EVALUATION_VERSION
        ):
            missing_evaluation += 1
            record["record_evaluation"] = evaluate_record(record, previous_records=previous_records)
            record["record_evaluation"]["evaluated_at"] = (now or datetime.now()).isoformat(timespec="seconds")
            record["record_evaluation"]["evaluation_version"] = RECORD_EVALUATION_VERSION
        student_name = str(record.get("student_name") or record.get("student") or "").strip()
        if student_name and student_name not in student_names:
            orphan_records.append(
                {
                    "index": index,
                    "record_id": str(record.get("id") or ""),
                    "student_name": student_name,
                    "timestamp": str(record.get("timestamp") or record.get("created_at") or ""),
                    "summary": str(record.get("content") or record.get("source_text") or "")[:80],
                }
            )
        if record != before:
            changed += 1
            if len(sample_changes) < 8:
                sample_changes.append(
                    {
                        "record_id": str(record.get("id") or ""),
                        "student_name": student_name,
                        "timestamp": str(record.get("timestamp") or record.get("created_at") or ""),
                        "payroll_eligible": bool(_as_dict(record.get("record_evaluation")).get("payroll_eligible")),
                        "reason_texts": list(_as_dict(record.get("record_evaluation")).get("reason_texts") or []),
                    }
                )
        upgraded_records.append(record)
        previous_records.append(record)

    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    report = {
        "schema_version": 1,
        "dry_run": dry_run,
        "generated_at": stamp,
        "record_count": len(records),
        "changed_records": changed,
        "missing_evaluation": missing_evaluation,
        "added_ids": added_ids,
        "legacy_marked": legacy_marked,
        "orphan_record_count": len(orphan_records),
        "orphan_records": orphan_records[:50],
        "sample_changes": sample_changes,
    }
    if not dry_run and changed:
        store.write_json("records.json", upgraded_records)
    if not dry_run:
        store.write_json("record_evaluation_upgrade_state.json", report)
    return report


def upgrade_business_data(
    store: TuoguanStore,
    now: datetime | None = None,
) -> dict[str, int | str]:
    """Upgrade current data without inventing unknown business facts."""
    students = store.read_json("students.json", {})
    records = store.read_json("records.json", [])
    if not isinstance(students, dict):
        students = {}
    if not isinstance(records, list):
        records = []

    upgraded_students: dict[str, Any] = {}
    changed_students = 0
    for name, raw_profile in students.items():
        profile = deepcopy(raw_profile) if isinstance(raw_profile, dict) else {}
        before = deepcopy(profile)
        for field, default in STUDENT_BUSINESS_DEFAULTS.items():
            profile.setdefault(field, deepcopy(default))
        profile.setdefault("schema_version", 2)
        upgraded_students[str(name)] = profile
        changed_students += profile != before

    upgraded_records: list[Any] = []
    changed_records = 0
    for raw_record in records:
        if not isinstance(raw_record, dict):
            upgraded_records.append(raw_record)
            continue
        record = _normalized_record(raw_record)
        upgraded_records.append(record)
        changed_records += record != raw_record

    if changed_students:
        store.write_json("students.json", upgraded_students)
    if changed_records:
        store.write_json("records.json", upgraded_records)

    stamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    evaluation_report = backfill_record_evaluations(store, dry_run=False, now=now)
    state = {
        "schema_version": 2,
        "upgraded_at": stamp,
        "student_count": len(upgraded_students),
        "record_count": len(upgraded_records),
        "changed_students": changed_students,
        "changed_records": changed_records,
        "record_evaluation": evaluation_report,
    }
    store.write_json("business_schema_state.json", state)
    return state


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
