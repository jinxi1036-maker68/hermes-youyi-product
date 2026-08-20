#!/usr/bin/env python3
"""Quarantine one invalid public-learning run without deleting audit history."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from typing import Any
import uuid

from plugins.tuoguan_core.store import TuoguanStore
from plugins.tuoguan_core.write_guard import authorized_system_write


CORRECTIONS_FILE = "external_research_corrections.jsonl"
OUTBOX_FILE = "notification_outbox.json"
PENDING_KNOWLEDGE_FILE = "pending_knowledge.json"
LATEST_RESEARCH_FILES = ("knowledge_research_latest.json", "competitor_research_latest.json")


def _read_jsonl(store: TuoguanStore, filename: str) -> list[dict[str, Any]]:
    path = store.path_for(filename)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def quarantine_external_learning_run(
    *,
    store: TuoguanStore,
    run_id: str,
    outbox_id: str = "",
    reason: str = "source_relevance_failed",
    apply: bool = False,
) -> dict[str, Any]:
    wanted_run = str(run_id or "").strip()
    wanted_outbox = str(outbox_id or "").strip()
    if not wanted_run:
        return {"ok": False, "error": "run_id_required", "writeback_verified": False}
    runs = _read_jsonl(store, "external_research_runs.jsonl")
    matching_runs = [row for row in runs if str(row.get("run_id") or "") == wanted_run]
    if not matching_runs:
        return {"ok": False, "error": "run_not_found", "run_id": wanted_run, "writeback_verified": False}
    candidates = [
        row
        for row in _read_jsonl(store, "industry_learning_candidates.jsonl")
        if str(((row.get("source") or {}).get("source_message_id") if isinstance(row.get("source"), dict) else "") or row.get("source_message_id") or "") == wanted_run
    ]
    outbox = store.read_json(OUTBOX_FILE, [])
    outbox_rows = outbox if isinstance(outbox, list) else []
    matching_outbox = [row for row in outbox_rows if isinstance(row, dict) and str(row.get("id") or "") == wanted_outbox] if wanted_outbox else []
    queries = {
        str(query or "").strip()
        for run in matching_runs
        for query in (run.get("queries") if isinstance(run.get("queries"), list) else [])
        if str(query or "").strip()
    }
    run_minutes = {
        str(run.get("created_at") or "")[:16]
        for run in matching_runs
        if str(run.get("created_at") or "")
    }
    pending_rows = store.read_json(PENDING_KNOWLEDGE_FILE, [])
    if not isinstance(pending_rows, list):
        pending_rows = []
    matching_pending = [
        row for row in pending_rows
        if isinstance(row, dict) and _matches_run_material(row, queries=queries, run_minutes=run_minutes)
    ]
    matching_latest_files = []
    for filename in LATEST_RESEARCH_FILES:
        latest = store.read_json(filename, {})
        if isinstance(latest, dict) and _matches_run_material(latest, queries=queries, run_minutes=run_minutes):
            matching_latest_files.append(filename)
    existing = [
        row for row in _read_jsonl(store, CORRECTIONS_FILE)
        if str(row.get("run_id") or "") == wanted_run
        and str(row.get("status") or "") == "quarantined"
    ]
    plan = {
        "ok": True,
        "dry_run": not apply,
        "run_id": wanted_run,
        "outbox_id": wanted_outbox,
        "reason": str(reason or "source_relevance_failed"),
        "matched_run_count": len(matching_runs),
        "matched_candidate_count": len(candidates),
        "matched_outbox_count": len(matching_outbox),
        "matched_pending_count": len(matching_pending),
        "matched_latest_count": len(matching_latest_files),
        "already_quarantined": bool(existing),
        "writeback_verified": False,
    }
    if not apply:
        plan["writeback_verified"] = True
        return plan

    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    correction = dict(existing[-1]) if existing else {
            "correction_id": f"external_research_correction_{uuid.uuid4().hex}",
            "run_id": wanted_run,
            "status": "quarantined",
            "reason": str(reason or "source_relevance_failed"),
            "candidate_count": len(candidates),
            "outbox_id": wanted_outbox,
            "created_at": stamp,
            "preserves_original_evidence": True,
        }
    allowed_files = {CORRECTIONS_FILE, OUTBOX_FILE, PENDING_KNOWLEDGE_FILE, *LATEST_RESEARCH_FILES}
    with authorized_system_write(store.data_dir, job_name="repair_external_learning_run_v1", allowed_files=allowed_files) as auth:
        if not existing:
            correction.update({
                "operation_id": auth.operation_id,
                "ledger_id": auth.ledger_id,
                "audit_id": auth.audit_id,
            })
            store.append_jsonl_verified(CORRECTIONS_FILE, correction)
        if wanted_outbox:
            def mark_invalid(value: Any) -> list[dict[str, Any]]:
                rows = value if isinstance(value, list) else []
                for row in rows:
                    if isinstance(row, dict) and str(row.get("id") or "") == wanted_outbox:
                        row.update({
                            "content_validity": "quarantined",
                            "validity_reason": correction["reason"],
                            "validity_correction_id": correction["correction_id"],
                            "validity_corrected_at": stamp,
                        })
                return rows
            store.update_json(OUTBOX_FILE, [], mark_invalid)
        if matching_pending:
            matching_ids = {str(row.get("id") or "") for row in matching_pending}

            def quarantine_pending(value: Any) -> list[dict[str, Any]]:
                rows = value if isinstance(value, list) else []
                for row in rows:
                    if isinstance(row, dict) and str(row.get("id") or "") in matching_ids:
                        row.update({
                            "status": "quarantined",
                            "content_validity": "quarantined",
                            "validity_reason": correction["reason"],
                            "validity_correction_id": correction["correction_id"],
                            "validity_corrected_at": stamp,
                        })
                return rows

            store.update_json(PENDING_KNOWLEDGE_FILE, [], quarantine_pending)
        for filename in matching_latest_files:
            def quarantine_latest(value: Any) -> dict[str, Any]:
                row = value if isinstance(value, dict) else {}
                row.update({
                    "content_validity": "quarantined",
                    "active_evidence_count": 0,
                    "validity_reason": correction["reason"],
                    "validity_correction_id": correction["correction_id"],
                    "validity_corrected_at": stamp,
                })
                return row

            store.update_json(filename, {}, quarantine_latest)
    reread = _read_jsonl(store, CORRECTIONS_FILE)
    correction_ok = any(str(row.get("correction_id") or "") == correction["correction_id"] for row in reread)
    outbox_ok = not wanted_outbox or any(
        str(row.get("id") or "") == wanted_outbox and str(row.get("content_validity") or "") == "quarantined"
        for row in store.read_json(OUTBOX_FILE, [])
        if isinstance(row, dict)
    )
    pending_after = store.read_json(PENDING_KNOWLEDGE_FILE, [])
    matching_pending_ids = {str(item.get("id") or "") for item in matching_pending}
    pending_after_matches = [
        row for row in pending_after
        if isinstance(row, dict) and str(row.get("id") or "") in matching_pending_ids
    ]
    pending_ok = len(pending_after_matches) == len(matching_pending) and all(
        str(row.get("status") or "") == "quarantined"
        for row in pending_after_matches
    )
    latest_ok = all(
        str((store.read_json(filename, {}) or {}).get("content_validity") or "") == "quarantined"
        for filename in matching_latest_files
    )
    return {
        **plan,
        "dry_run": False,
        "correction_id": correction["correction_id"],
        "writeback_verified": correction_ok and outbox_ok and pending_ok and latest_ok,
        "ok": correction_ok and outbox_ok and pending_ok and latest_ok,
    }


def _matches_run_material(row: dict[str, Any], *, queries: set[str], run_minutes: set[str]) -> bool:
    row_queries = {str(row.get("query") or "").strip()}
    raw_queries = row.get("queries") if isinstance(row.get("queries"), list) else []
    row_queries.update(str(item or "").strip() for item in raw_queries)
    evidence = row.get("evidence") if isinstance(row.get("evidence"), list) else []
    row_queries.update(str(item.get("query") or "").strip() for item in evidence if isinstance(item, dict))
    row_queries.discard("")
    if queries and not row_queries.intersection(queries):
        return False
    timestamps = {
        str(row.get("created_at") or "")[:16],
        str(row.get("updated_at") or "")[:16],
    }
    timestamps.update(
        str(item.get("collected_at") or "")[:16]
        for item in evidence
        if isinstance(item, dict)
    )
    timestamps.discard("")
    return not run_minutes or bool(timestamps.intersection(run_minutes))


def main() -> int:
    parser = argparse.ArgumentParser(description="Append a quarantine correction for one invalid external-learning run.")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--outbox-id", default="")
    parser.add_argument("--reason", default="source_relevance_failed")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = quarantine_external_learning_run(
        store=TuoguanStore(args.data_dir),
        run_id=args.run_id,
        outbox_id=args.outbox_id,
        reason=args.reason,
        apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
