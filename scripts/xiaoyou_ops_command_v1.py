#!/usr/bin/env python3
"""Validate XIAOU_OPS_COMMAND_V1 commands.

This module validates protocol shape and time bounds only. Repository/PR trust
checks are performed separately by the worker against live GitHub metadata.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any

PROTOCOL = "XIAOU_OPS_COMMAND_V1"
ALLOWED_ACTIONS = {"READ_ONLY_INSPECTION", "VERIFY"}
ALLOWED_FIELDS = {
    "protocol",
    "command_id",
    "pr_number",
    "candidate_sha",
    "action",
    "issued_at",
    "expires_at",
    "production_change_authorized",
    "objective",
    "evidence_requirements",
}
REQUIRED_FIELDS = ALLOWED_FIELDS
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_COMMAND_RE = re.compile(r"^[A-Za-z0-9._:-]{3,120}$")
MAX_COMMAND_BYTES = 32 * 1024
MAX_OBJECTIVE_CHARS = 2000
MAX_EVIDENCE_ITEMS = 20
MAX_EVIDENCE_ITEM_CHARS = 500
MAX_VALIDITY = timedelta(hours=12)
MAX_CLOCK_SKEW = timedelta(minutes=5)


class CommandValidationError(ValueError):
    pass


def _parse_time(value: Any, key: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise CommandValidationError(f"{key}_required")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CommandValidationError(f"{key}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CommandValidationError(f"{key}_timezone_required")
    return parsed.astimezone(timezone.utc)


def validate_command(payload: Any, *, now: datetime | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CommandValidationError("command_must_be_object")

    keys = set(payload)
    unknown = keys - ALLOWED_FIELDS
    missing = REQUIRED_FIELDS - keys
    if unknown:
        raise CommandValidationError("unknown_fields:" + ",".join(sorted(unknown)))
    if missing:
        raise CommandValidationError("missing_fields:" + ",".join(sorted(missing)))

    if payload["protocol"] != PROTOCOL:
        raise CommandValidationError("unsupported_protocol")

    command_id = payload["command_id"]
    if not isinstance(command_id, str) or not _COMMAND_RE.fullmatch(command_id.strip()):
        raise CommandValidationError("command_id_invalid")
    command_id = command_id.strip()

    pr_number = payload["pr_number"]
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        raise CommandValidationError("pr_number_invalid")

    candidate_sha = payload["candidate_sha"]
    if not isinstance(candidate_sha, str) or not _SHA_RE.fullmatch(candidate_sha.strip()):
        raise CommandValidationError("candidate_sha_invalid")
    candidate_sha = candidate_sha.strip().lower()

    action = payload["action"]
    if action not in ALLOWED_ACTIONS:
        raise CommandValidationError("action_not_allowed")

    if payload["production_change_authorized"] is not False:
        raise CommandValidationError("production_change_must_be_false")

    objective = payload["objective"]
    if not isinstance(objective, str) or not objective.strip():
        raise CommandValidationError("objective_required")
    objective = objective.strip()
    if len(objective) > MAX_OBJECTIVE_CHARS:
        raise CommandValidationError("objective_too_long")

    evidence = payload["evidence_requirements"]
    if (
        not isinstance(evidence, list)
        or len(evidence) > MAX_EVIDENCE_ITEMS
        or not all(isinstance(item, str) and item.strip() for item in evidence)
    ):
        raise CommandValidationError("evidence_requirements_invalid")
    evidence = [item.strip() for item in evidence]
    if any(len(item) > MAX_EVIDENCE_ITEM_CHARS for item in evidence):
        raise CommandValidationError("evidence_requirement_too_long")

    issued_at = _parse_time(payload["issued_at"], "issued_at")
    expires_at = _parse_time(payload["expires_at"], "expires_at")
    if expires_at <= issued_at:
        raise CommandValidationError("expiry_must_follow_issue")
    if expires_at - issued_at > MAX_VALIDITY:
        raise CommandValidationError("validity_window_too_long")

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if issued_at > current + MAX_CLOCK_SKEW:
        raise CommandValidationError("issued_at_in_future")
    if expires_at < current:
        raise CommandValidationError("command_expired")

    return {
        "protocol": PROTOCOL,
        "command_id": command_id,
        "pr_number": pr_number,
        "candidate_sha": candidate_sha,
        "action": action,
        "issued_at": issued_at.isoformat().replace("+00:00", "Z"),
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        "production_change_authorized": False,
        "objective": objective,
        "evidence_requirements": evidence,
    }


def load_command(path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    raw = path.read_bytes()
    if len(raw) > MAX_COMMAND_BYTES:
        raise CommandValidationError("command_too_large")
    return validate_command(json.loads(raw.decode("utf-8")), now=now)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate XIAOU_OPS_COMMAND_V1")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        command = load_command(args.input)
        rendered = json.dumps(command, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
        return 0
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, CommandValidationError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
