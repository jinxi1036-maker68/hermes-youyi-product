#!/usr/bin/env python3
"""Operate the WeCom callback maintenance drain without exposing a network control plane."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pwd
import sqlite3
import time
from typing import Any

from runtime.plugins.platforms.wecom.maintenance_drain import (
    DRAIN_STATE,
    SCHEMA_VERSION,
    WecomCallbackDrainGate,
)


DEFAULT_SERVICE_USER = "hermes-youyi"


def _service_uid(user_name: str) -> int:
    return pwd.getpwnam(user_name).pw_uid


def _require_service_identity(user_name: str) -> None:
    required = _service_uid(user_name)
    current = os.geteuid()
    if current != required:
        raise PermissionError(
            f"write operations must run as {user_name} (uid={required}); current uid={current}"
        )


def _default_drain_file(runtime_home: Path) -> Path:
    configured = str(os.getenv("HERMES_WECOM_DRAIN_FILE") or "").strip()
    return Path(configured) if configured else runtime_home / "state" / "wecom_callback_drain.json"


def _default_receipt_db(runtime_home: Path) -> Path:
    configured = str(os.getenv("HERMES_WECOM_DEDUPE_DB") or "").strip()
    return Path(configured) if configured else runtime_home / "state" / "wecom_callback_receipts.sqlite3"


def _receipt_counts(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "exists": False,
            "claimed_count": 0,
            "processing_count": 0,
            "failed_count": 0,
            "processed_count": 0,
        }

    uri = f"file:{path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN status = 'claimed' THEN 1 ELSE 0 END) AS claimed_count,
              SUM(CASE WHEN reply_status = 'processing' THEN 1 ELSE 0 END) AS processing_count,
              SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
              SUM(CASE WHEN status = 'processed' THEN 1 ELSE 0 END) AS processed_count
            FROM inbound_receipts
            """
        ).fetchone()
    finally:
        connection.close()

    return {
        "exists": True,
        "total_count": int(row["total"] or 0),
        "claimed_count": int(row["claimed_count"] or 0),
        "processing_count": int(row["processing_count"] or 0),
        "failed_count": int(row["failed_count"] or 0),
        "processed_count": int(row["processed_count"] or 0),
    }


def _atomic_enter(path: Path, *, reason: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "state": DRAIN_STATE,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "reason": str(reason or "maintenance"),
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _resume(path: Path) -> None:
    path.unlink(missing_ok=True)


def _snapshot(runtime_home: Path, drain_file: Path, receipt_db: Path) -> dict[str, Any]:
    gate = WecomCallbackDrainGate(drain_file)
    return {
        "ok": True,
        "runtime_home": str(runtime_home.resolve()),
        "drain": gate.snapshot(),
        "receipts": _receipt_counts(receipt_db),
        "secret_content_inspected": False,
        "payload_content_inspected": False,
    }


def _wait_for_quiesced(
    *,
    runtime_home: Path,
    drain_file: Path,
    receipt_db: Path,
    timeout_seconds: float,
    stable_seconds: float,
    poll_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    stable_required = max(0.0, float(stable_seconds))
    poll = max(0.05, float(poll_seconds))
    zero_since: float | None = None

    while True:
        snapshot = _snapshot(runtime_home, drain_file, receipt_db)
        drain = snapshot["drain"]
        receipts = snapshot["receipts"]

        if not drain.get("active"):
            return {
                **snapshot,
                "ok": False,
                "error": "drain_not_active",
                "quiesced": False,
            }
        if not drain.get("valid"):
            return {
                **snapshot,
                "ok": False,
                "error": "drain_state_invalid",
                "quiesced": False,
            }

        processing = int(receipts.get("processing_count") or 0)
        now = time.monotonic()
        if processing == 0:
            if zero_since is None:
                zero_since = now
            if now - zero_since >= stable_required:
                return {
                    **snapshot,
                    "ok": True,
                    "quiesced": True,
                    "stable_seconds": stable_required,
                }
        else:
            zero_since = None

        if now >= deadline:
            return {
                **snapshot,
                "ok": False,
                "error": "drain_wait_timeout",
                "quiesced": False,
            }
        time.sleep(poll)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Enter, inspect, wait for, or resume the WeCom callback maintenance drain."
    )
    parser.add_argument("action", choices=("enter", "status", "wait", "resume"))
    parser.add_argument("--runtime-home", type=Path, required=True)
    parser.add_argument("--drain-file", type=Path)
    parser.add_argument("--receipt-db", type=Path)
    parser.add_argument("--service-user", default=DEFAULT_SERVICE_USER)
    parser.add_argument("--reason", default="maintenance")
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--stable-seconds", type=float, default=1.0)
    parser.add_argument("--poll-seconds", type=float, default=0.2)
    args = parser.parse_args()

    runtime_home = args.runtime_home.resolve()
    drain_file = (args.drain_file or _default_drain_file(runtime_home)).resolve()
    receipt_db = (args.receipt_db or _default_receipt_db(runtime_home)).resolve()

    try:
        if args.action == "enter":
            _require_service_identity(args.service_user)
            _atomic_enter(drain_file, reason=args.reason)
            result = _snapshot(runtime_home, drain_file, receipt_db)
            result["action"] = "enter"
        elif args.action == "resume":
            _require_service_identity(args.service_user)
            _resume(drain_file)
            result = _snapshot(runtime_home, drain_file, receipt_db)
            result["action"] = "resume"
        elif args.action == "wait":
            result = _wait_for_quiesced(
                runtime_home=runtime_home,
                drain_file=drain_file,
                receipt_db=receipt_db,
                timeout_seconds=args.timeout_seconds,
                stable_seconds=args.stable_seconds,
                poll_seconds=args.poll_seconds,
            )
            result["action"] = "wait"
        else:
            result = _snapshot(runtime_home, drain_file, receipt_db)
            result["action"] = "status"
    except (OSError, PermissionError, sqlite3.Error, KeyError) as exc:
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}:{exc}",
            "secret_content_inspected": False,
            "payload_content_inspected": False,
        }

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
