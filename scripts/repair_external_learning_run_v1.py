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
        "already_quarantined": bool(existing),
        "writeback_verified": False,
    }
    if not apply or existing:
        plan["writeback_verified"] = bool(existing) or not apply
        return plan

    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    correction = {
        "correction_id": f"external_research_correction_{uuid.uuid4().hex}",
        "run_id": wanted_run,
        "status": "quarantined",
        "reason": str(reason or "source_relevance_failed"),
        "candidate_count": len(candidates),
        "outbox_id": wanted_outbox,
        "created_at": stamp,
        "preserves_original_evidence": True,
    }
    allowed_files = {CORRECTIONS_FILE, OUTBOX_FILE}
    with authorized_system_write(store.data_dir, job_name="repair_external_learning_run_v1", allowed_files=allowed_files) as auth:
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
    reread = _read_jsonl(store, CORRECTIONS_FILE)
    correction_ok = any(str(row.get("correction_id") or "") == correction["correction_id"] for row in reread)
    outbox_ok = not wanted_outbox or any(
        str(row.get("id") or "") == wanted_outbox and str(row.get("content_validity") or "") == "quarantined"
        for row in store.read_json(OUTBOX_FILE, [])
        if isinstance(row, dict)
    )
    return {
        **plan,
        "dry_run": False,
        "correction_id": correction["correction_id"],
        "writeback_verified": correction_ok and outbox_ok,
        "ok": correction_ok and outbox_ok,
    }


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
