#!/usr/bin/env python3
"""Prepare or explicitly apply the reviewed WeCom identity-authority cutover.

This is an operator-only deployment step.  It never calls Hermes, sends a
message, changes the old directory, or guesses a conflicting lifecycle fact.
Without ``--apply`` it is a read-only preflight.  Applying it creates the new
Personnel Governance aggregate as the authoritative source for inbound role
and lifecycle decisions; the old WeCom files remain preserved as historical
reference and transport/display compatibility data.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = REPO_ROOT / "runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from plugins.tuoguan_core.personnel_identity_authority import (  # noqa: E402
    DATA_FILE,
    IdentityAuthorityError,
    build_legacy_identity_authority_bootstrap,
    validate_runtime_identity_document,
)
from plugins.tuoguan_core.store import TuoguanStore  # noqa: E402
from plugins.tuoguan_core.write_guard import authorized_system_write  # noqa: E402


def _stamp(value: Any, timestamp: str) -> Any:
    if isinstance(value, dict):
        return {key: _stamp(item, timestamp) for key, item in value.items()}
    if isinstance(value, list):
        return [_stamp(item, timestamp) for item in value]
    return timestamp if value == "migration_pending_apply" else value


def _summary(report: dict[str, Any], *, applied: bool) -> dict[str, Any]:
    return {
        "tenant_id": report.get("tenant_id"),
        "approved_identity_count": report.get("approved_identity_count"),
        "pending_identity_count": report.get("pending_identity_count"),
        "rejected_identity_count": report.get("rejected_identity_count"),
        "active_boss_count": report.get("active_boss_count"),
        "safe_to_apply": bool(report.get("safe_to_apply")),
        "blockers": list(report.get("blockers") or []),
        "applied": applied,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, help="Institution Workspace data directory")
    parser.add_argument("--tenant-id", required=True, help="Server-configured tenant identifier")
    parser.add_argument("--apply", action="store_true", help="Write only after a clean preflight")
    args = parser.parse_args()

    store = TuoguanStore(Path(args.data_dir).expanduser().resolve())
    existing = store.read_json(DATA_FILE, {})
    document, report = build_legacy_identity_authority_bootstrap(
        tenant_id=args.tenant_id,
        whitelist=store.read_json("wecom_whitelist.json", {}),
        staff=store.read_json("staff.json", {}),
        existing_governance=existing,
    )
    if not args.apply:
        print(json.dumps(_summary(report, applied=False), ensure_ascii=False, sort_keys=True))
        return 0 if report.get("safe_to_apply") else 2
    if not report.get("safe_to_apply"):
        print(json.dumps(_summary(report, applied=False), ensure_ascii=False, sort_keys=True))
        return 2

    timestamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    candidate = _stamp(document, timestamp)
    try:
        validate_runtime_identity_document(candidate)
    except IdentityAuthorityError as exc:
        print(json.dumps({"safe_to_apply": False, "blockers": [str(exc)], "applied": False}, ensure_ascii=False))
        return 2
    # No overwrite: an existing aggregate requires its own audited
    # reconciliation, never an accidental bootstrap replacement.
    if isinstance(existing, dict) and existing:
        print(json.dumps({"safe_to_apply": False, "blockers": ["existing_governance_requires_explicit_reconciliation"], "applied": False}, ensure_ascii=False))
        return 2
    with authorized_system_write(store.data_dir, job_name="apply_personnel_identity_authority", allowed_files={DATA_FILE}) as write:
        store.write_json(DATA_FILE, candidate)
        persisted = store.read_json(DATA_FILE, {})
        validate_runtime_identity_document(persisted)
        if persisted != candidate:
            raise RuntimeError("identity_authority_writeback_failed")
    payload = _summary(report, applied=True)
    payload.update({"writeback_verified": True, "operation_id": write.operation_id})
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
