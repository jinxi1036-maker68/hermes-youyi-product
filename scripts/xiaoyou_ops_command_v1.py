#!/usr/bin/env python3
"""Validate and poll XiaoU ChatGPT -> Codex operation commands.

The GitHub PR comment is the control-plane record. This module never executes
Codex, deploys production, or mutates product data. It only accepts commands
that are authored by the repository owner, bound to the current open PR head,
time-limited, and restricted to the V1 read-only action set.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any
from urllib.request import Request, urlopen

PROTOCOL = "XIAOU_OPS_COMMAND_V1"
MARKER = "<!-- xiaou-ops-command:v1 -->"
ALLOWED_ACTIONS = {"READ_ONLY_INSPECTION", "VERIFY"}
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_COMMAND_RE = re.compile(r"^[A-Za-z0-9._:-]{3,120}$")
_COMMENT_RE = re.compile(
    r"<!--\s*xiaou-ops-command:v1\s*-->\s*```json\s*(\{.*?\})\s*```",
    re.IGNORECASE | re.DOTALL,
)
_FORBIDDEN_KEY_PARTS = {
    "authorization",
    "cookie",
    "credential",
    "api_key",
    "apikey",
    "password",
    "passwd",
    "private_key",
    "secret",
    "token",
}
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(r"\b(?:ghp_|github_pat_|sk-)[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


class CommandValidationError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any, key: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise CommandValidationError(f"{key}_required")
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise CommandValidationError(f"{key}_invalid") from exc
    if parsed.tzinfo is None:
        raise CommandValidationError(f"{key}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _require_text(payload: dict[str, Any], key: str, *, max_len: int = 2000) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CommandValidationError(f"{key}_required")
    value = value.strip()
    if len(value) > max_len:
        raise CommandValidationError(f"{key}_too_long")
    return value


def _reject_sensitive_material(value: Any, *, path: str = "command") -> None:
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key or "").strip().lower()
            if any(part in key for part in _FORBIDDEN_KEY_PARTS):
                raise CommandValidationError(f"sensitive_key_rejected:{path}.{raw_key}")
            _reject_sensitive_material(child, path=f"{path}.{raw_key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_material(child, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        for pattern in _SECRET_PATTERNS:
            if pattern.search(value):
                raise CommandValidationError(f"sensitive_value_rejected:{path}")


def validate_command(payload: Any, *, now: datetime | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CommandValidationError("command_must_be_object")

    _reject_sensitive_material(payload)

    if _require_text(payload, "protocol", max_len=64) != PROTOCOL:
        raise CommandValidationError("unsupported_protocol")

    command_id = _require_text(payload, "command_id", max_len=120)
    if not _COMMAND_RE.fullmatch(command_id):
        raise CommandValidationError("command_id_invalid")

    pr_number = payload.get("pr_number")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        raise CommandValidationError("pr_number_invalid")

    candidate_sha = _require_text(payload, "candidate_sha", max_len=40).lower()
    if not _SHA_RE.fullmatch(candidate_sha):
        raise CommandValidationError("candidate_sha_invalid")

    action = _require_text(payload, "action", max_len=64)
    if action not in ALLOWED_ACTIONS:
        raise CommandValidationError("action_not_allowed_in_v1")

    if payload.get("allow_code_change") is not False:
        raise CommandValidationError("allow_code_change_must_be_false")
    if payload.get("allow_production_change") is not False:
        raise CommandValidationError("allow_production_change_must_be_false")

    goal = _require_text(payload, "goal")

    evidence = payload.get("evidence_requirements")
    if not isinstance(evidence, list) or not evidence or len(evidence) > 20:
        raise CommandValidationError("evidence_requirements_invalid")
    cleaned_evidence: list[str] = []
    for item in evidence:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > 500:
            raise CommandValidationError("evidence_requirement_invalid")
        cleaned_evidence.append(item.strip())

    issued_at = _parse_time(payload.get("issued_at"), "issued_at")
    expires_at = _parse_time(payload.get("expires_at"), "expires_at")
    reference_now = (now or _now()).astimezone(timezone.utc)
    if expires_at <= issued_at:
        raise CommandValidationError("expires_at_must_follow_issued_at")
    if expires_at - issued_at > timedelta(hours=24):
        raise CommandValidationError("command_lifetime_exceeds_24h")
    if expires_at <= reference_now:
        raise CommandValidationError("command_expired")
    if issued_at > reference_now + timedelta(minutes=5):
        raise CommandValidationError("issued_at_in_future")

    return {
        "protocol": PROTOCOL,
        "command_id": command_id,
        "pr_number": pr_number,
        "candidate_sha": candidate_sha,
        "action": action,
        "allow_code_change": False,
        "allow_production_change": False,
        "goal": goal,
        "evidence_requirements": cleaned_evidence,
        "issued_at": issued_at.isoformat().replace("+00:00", "Z"),
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
    }


def parse_command_comment(body: str, *, now: datetime | None = None) -> dict[str, Any]:
    if MARKER not in str(body or ""):
        raise CommandValidationError("command_marker_missing")
    match = _COMMENT_RE.search(str(body or ""))
    if not match:
        raise CommandValidationError("command_json_block_missing")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise CommandValidationError("command_json_invalid") from exc
    return validate_command(payload, now=now)


def command_from_github_comment(
    comment: dict[str, Any],
    *,
    owner_login: str,
    pr_number: int,
    pr_head_sha: str,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    body = str(comment.get("body") or "")
    if MARKER not in body:
        return None

    user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
    login = str(user.get("login") or "")
    association = str(comment.get("author_association") or "").upper()
    if login != owner_login or association != "OWNER":
        return None

    command = parse_command_comment(body, now=now)
    if command["pr_number"] != int(pr_number):
        raise CommandValidationError("command_pr_mismatch")
    if command["candidate_sha"] != str(pr_head_sha or "").lower():
        raise CommandValidationError("command_candidate_not_current_pr_head")

    comment_id = comment.get("id")
    if isinstance(comment_id, bool) or not isinstance(comment_id, int) or comment_id <= 0:
        raise CommandValidationError("github_comment_id_invalid")
    command["source_comment_id"] = comment_id
    command["source_author_login"] = login
    return command


def _github_json(url: str) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "xiaou-ops-command-bridge-v1",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    read_token = os.environ.get("XIAOU_GITHUB_READ_TOKEN", "").strip()
    if read_token:
        headers["Authorization"] = f"Bearer {read_token}"
    req = Request(url, headers=headers)
    with urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"processed_command_ids": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommandValidationError("state_file_invalid") from exc
    values = raw.get("processed_command_ids") if isinstance(raw, dict) else None
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise CommandValidationError("state_file_invalid")
    return {"processed_command_ids": values}


def ack_command(path: Path, command_id: str) -> None:
    state = _read_state(path)
    values = list(dict.fromkeys([*state["processed_command_ids"], str(command_id)]))[-500:]
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps({"processed_command_ids": values}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def poll_next_command(
    *,
    repo: str,
    pr_number: int,
    owner_login: str,
    state_path: Path,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise CommandValidationError("repo_invalid")

    pr = _github_json(f"https://api.github.com/repos/{repo}/pulls/{int(pr_number)}")
    if str(pr.get("state") or "") != "open":
        raise CommandValidationError("target_pr_not_open")
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    pr_head_sha = str(head.get("sha") or "").lower()
    if not _SHA_RE.fullmatch(pr_head_sha):
        raise CommandValidationError("target_pr_head_invalid")

    comments = _github_json(
        f"https://api.github.com/repos/{repo}/issues/{int(pr_number)}/comments?per_page=100"
    )
    if not isinstance(comments, list):
        raise CommandValidationError("github_comments_invalid")

    processed = set(_read_state(state_path)["processed_command_ids"])
    for comment in sorted(
        (item for item in comments if isinstance(item, dict)),
        key=lambda item: int(item.get("id") or 0),
    ):
        command = command_from_github_comment(
            comment,
            owner_login=owner_login,
            pr_number=pr_number,
            pr_head_sha=pr_head_sha,
            now=now,
        )
        if command and command["command_id"] not in processed:
            return command
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="XiaoU GitHub -> Codex command bridge")
    sub = parser.add_subparsers(dest="mode", required=True)

    poll = sub.add_parser("poll")
    poll.add_argument("--repo", required=True)
    poll.add_argument("--pr", type=int, required=True)
    poll.add_argument("--owner-login", required=True)
    poll.add_argument("--state-file", type=Path, required=True)
    poll.add_argument("--output", type=Path)

    ack = sub.add_parser("ack")
    ack.add_argument("--state-file", type=Path, required=True)
    ack.add_argument("--command-id", required=True)

    validate = sub.add_parser("validate-file")
    validate.add_argument("--input", type=Path, required=True)

    args = parser.parse_args()
    try:
        if args.mode == "poll":
            command = poll_next_command(
                repo=args.repo,
                pr_number=args.pr,
                owner_login=args.owner_login,
                state_path=args.state_file,
            )
            if command is None:
                return 3
            body = json.dumps(command, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(body, encoding="utf-8")
            else:
                print(body, end="")
            return 0
        if args.mode == "ack":
            ack_command(args.state_file, args.command_id)
            return 0
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        print(json.dumps(validate_command(payload), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, json.JSONDecodeError, CommandValidationError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
