#!/usr/bin/env python3
"""XiaoU controlled pull worker V2.

The worker reads trusted operation commands from GitHub, validates them,
invokes one fixed executor without a shell, validates the resulting
XIAOU_OPS_REPORT_V1, and dispatches the existing report-intake workflow.

V2 permits READ_ONLY_INSPECTION and VERIFY only.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from scripts.xiaoyou_ops_command_v1 import CommandValidationError, validate_command
from scripts.xiaoyou_ops_report_v1 import ReportValidationError, validate_report

COMMAND_MARKER = "XIAOU_OPS_COMMAND_V1"
DEFAULT_WORKFLOW = "xiaoyou-ops-report-intake.yml"
DEFAULT_POLL_SECONDS = 180
INITIAL_LOOKBACK = timedelta(hours=12)
MAX_HTTP_BYTES = 2 * 1024 * 1024
MAX_EXECUTOR_STDOUT_BYTES = 128 * 1024
_JSON_FENCE_RE = re.compile(
    r"XIAOU_OPS_COMMAND_V1\s*\n+\s*\x60\x60\x60(?:json)?\s*\n(?P<body>.*?)\n\s*\x60\x60\x60",
    re.IGNORECASE | re.DOTALL,
)


class WorkerError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerConfig:
    repository: str
    trusted_issuer: str
    executor: Path
    executor_user: str
    state_db: Path
    actions_token: str
    workflow: str = DEFAULT_WORKFLOW
    poll_seconds: int = DEFAULT_POLL_SECONDS
    executor_timeout_seconds: int = 900
    sudo_path: Path = Path("/usr/bin/sudo")


def parse_command_comment(
    body: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if COMMAND_MARKER not in body:
        raise CommandValidationError("command_marker_missing")
    match = _JSON_FENCE_RE.search(body)
    if not match:
        raise CommandValidationError("command_json_fence_missing")
    try:
        payload = json.loads(match.group("body"))
    except json.JSONDecodeError as exc:
        raise CommandValidationError("command_json_invalid") from exc
    return validate_command(payload, now=now)


def _github_json(
    url: str,
    *,
    token: str | None = None,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "xiaou-ops-worker-v2",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urlrequest.Request(url, data=data, headers=headers, method=method)
    try:
        with urlrequest.urlopen(req, timeout=20) as response:
            raw = response.read(MAX_HTTP_BYTES + 1)
            if len(raw) > MAX_HTTP_BYTES:
                raise WorkerError("github_response_too_large")
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except (urlerror.URLError, urlerror.HTTPError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerError(f"github_request_failed:{type(exc).__name__}") from exc


def list_recent_comments(repository: str, *, since: datetime) -> list[dict[str, Any]]:
    encoded_since = urlparse.quote(since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"))
    url = (
        f"https://api.github.com/repos/{repository}/issues/comments"
        f"?sort=created&direction=asc&per_page=100&since={encoded_since}"
    )
    result = _github_json(url)
    if not isinstance(result, list):
        raise WorkerError("github_comments_invalid")
    return [item for item in result if isinstance(item, dict)]


def fetch_pr(repository: str, pr_number: int) -> dict[str, Any]:
    result = _github_json(f"https://api.github.com/repos/{repository}/pulls/{pr_number}")
    if not isinstance(result, dict):
        raise WorkerError("github_pr_invalid")
    return result


def verify_github_trust(
    *,
    repository: str,
    trusted_issuer: str,
    comment: dict[str, Any],
    command: dict[str, Any],
    pr: dict[str, Any],
) -> None:
    comment_login = ((comment.get("user") or {}).get("login") or "").strip()
    if comment_login != trusted_issuer:
        raise WorkerError("untrusted_comment_author")

    issue_url = str(comment.get("issue_url") or "")
    expected_issue_suffix = f"/repos/{repository}/issues/{command['pr_number']}"
    if not issue_url.endswith(expected_issue_suffix):
        raise WorkerError("comment_pr_binding_failed")

    if pr.get("state") != "open":
        raise WorkerError("pr_not_open")

    pr_author = ((pr.get("user") or {}).get("login") or "").strip()
    if pr_author != trusted_issuer:
        raise WorkerError("untrusted_pr_author")

    base_ref = ((pr.get("base") or {}).get("ref") or "").strip()
    if base_ref != "main":
        raise WorkerError("pr_base_not_main")

    head = pr.get("head") or {}
    head_sha = str(head.get("sha") or "").strip().lower()
    if head_sha != command["candidate_sha"]:
        raise WorkerError("candidate_sha_not_current_pr_head")

    head_repo = ((head.get("repo") or {}).get("full_name") or "").strip()
    if head_repo != repository:
        raise WorkerError("fork_head_rejected")


class ReplayStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS commands (
                comment_id INTEGER PRIMARY KEY,
                command_id TEXT NOT NULL UNIQUE,
                pr_number INTEGER NOT NULL,
                candidate_sha TEXT NOT NULL,
                received_at TEXT NOT NULL,
                status TEXT NOT NULL,
                report_result TEXT
            )
            """
        )
        self.db.commit()

    def seen(self, *, comment_id: int, command_id: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM commands WHERE comment_id=? OR command_id=? LIMIT 1",
            (comment_id, command_id),
        ).fetchone()
        return row is not None

    def reserve(self, *, comment_id: int, command: dict[str, Any]) -> None:
        self.db.execute(
            """
            INSERT INTO commands(
                comment_id, command_id, pr_number, candidate_sha,
                received_at, status, report_result
            ) VALUES (?, ?, ?, ?, ?, 'RUNNING', NULL)
            """,
            (
                comment_id,
                command["command_id"],
                command["pr_number"],
                command["candidate_sha"],
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.db.commit()

    def finish(self, *, comment_id: int, status: str, report_result: str | None = None) -> None:
        self.db.execute(
            "UPDATE commands SET status=?, report_result=? WHERE comment_id=?",
            (status, report_result, comment_id),
        )
        self.db.commit()


def run_fixed_executor(config: WorkerConfig, command: dict[str, Any]) -> dict[str, Any]:
    if not config.executor.is_absolute():
        raise WorkerError("executor_path_must_be_absolute")
    if not config.executor.exists():
        raise WorkerError("executor_missing")
    if not config.sudo_path.is_absolute():
        raise WorkerError("sudo_path_must_be_absolute")
    if not config.sudo_path.exists():
        raise WorkerError("sudo_missing")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", config.executor_user):
        raise WorkerError("executor_user_invalid")

    command_bytes = (
        json.dumps(command, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")

    child_env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }

    try:
        completed = subprocess.run(
            [
                str(config.sudo_path),
                "-H",
                "-n",
                "-u",
                config.executor_user,
                "--",
                str(config.executor),
            ],
            input=command_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            timeout=config.executor_timeout_seconds,
            check=False,
            shell=False,
            env=child_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkerError("executor_timeout") from exc
    except OSError as exc:
        raise WorkerError("executor_start_failed") from exc

    stdout = completed.stdout or b""
    if len(stdout) > MAX_EXECUTOR_STDOUT_BYTES:
        raise WorkerError("executor_stdout_too_large")
    if completed.returncode != 0:
        raise WorkerError(f"executor_failed:{completed.returncode}")

    try:
        payload = json.loads(stdout.decode("utf-8"))
        report = validate_report(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ReportValidationError) as exc:
        raise WorkerError("executor_report_invalid") from exc

    if report["command_id"] != command["command_id"]:
        raise WorkerError("report_command_mismatch")
    if report["pr_number"] != command["pr_number"]:
        raise WorkerError("report_pr_mismatch")
    if report["candidate_sha"] != command["candidate_sha"]:
        raise WorkerError("report_sha_mismatch")
    if report["action"] != command["action"]:
        raise WorkerError("report_action_mismatch")
    if report["code_changed"] is not False:
        raise WorkerError("report_code_change_rejected")
    if report["production_changed"] is not False:
        raise WorkerError("report_production_change_rejected")

    return report


def dispatch_report(config: WorkerConfig, report: dict[str, Any]) -> None:
    encoded = base64.b64encode(
        json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    url = (
        f"https://api.github.com/repos/{config.repository}/actions/workflows/"
        f"{config.workflow}/dispatches"
    )
    _github_json(
        url,
        token=config.actions_token,
        method="POST",
        payload={
            "ref": "main",
            "inputs": {
                "pr_number": str(report["pr_number"]),
                "report_base64": encoded,
            },
        },
    )


def process_comment(
    config: WorkerConfig,
    store: ReplayStore,
    comment: dict[str, Any],
) -> str:
    body = str(comment.get("body") or "")
    if COMMAND_MARKER not in body:
        return "ignored"

    comment_login = ((comment.get("user") or {}).get("login") or "").strip()
    if comment_login != config.trusted_issuer:
        return "ignored"

    comment_id = comment.get("id")
    if isinstance(comment_id, bool) or not isinstance(comment_id, int) or comment_id <= 0:
        raise WorkerError("comment_id_invalid")

    command = parse_command_comment(body)
    if store.seen(comment_id=comment_id, command_id=command["command_id"]):
        return "duplicate"

    pr = fetch_pr(config.repository, command["pr_number"])
    verify_github_trust(
        repository=config.repository,
        trusted_issuer=config.trusted_issuer,
        comment=comment,
        command=command,
        pr=pr,
    )

    store.reserve(comment_id=comment_id, command=command)
    try:
        report = run_fixed_executor(config, command)
        dispatch_report(config, report)
    except Exception:
        store.finish(comment_id=comment_id, status="FAILED")
        raise

    store.finish(
        comment_id=comment_id,
        status="REPORTED",
        report_result=report["result"],
    )
    return "reported"


def run_once(config: WorkerConfig, store: ReplayStore) -> dict[str, int]:
    since = datetime.now(timezone.utc) - INITIAL_LOOKBACK
    counts = {"ignored": 0, "duplicate": 0, "reported": 0, "failed": 0}
    for comment in list_recent_comments(config.repository, since=since):
        try:
            outcome = process_comment(config, store, comment)
            counts[outcome] = counts.get(outcome, 0) + 1
        except (CommandValidationError, WorkerError):
            counts["failed"] += 1
    return counts


def _read_actions_token(path: Path) -> str:
    if not path.is_absolute():
        raise WorkerError("token_file_path_must_be_absolute")
    try:
        stat = path.stat()
    except OSError as exc:
        raise WorkerError("token_file_unavailable") from exc
    if stat.st_mode & 0o077:
        raise WorkerError("token_file_permissions_too_broad")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise WorkerError("token_file_unreadable") from exc
    if not token or len(token) > 4096:
        raise WorkerError("token_file_invalid")
    return token


def _load_config() -> WorkerConfig:
    repository = os.environ.get("XIAOU_REPOSITORY", "").strip()
    issuer = os.environ.get("XIAOU_TRUSTED_ISSUER", "").strip()
    executor = os.environ.get("XIAOU_EXECUTOR", "").strip()
    executor_user = os.environ.get("XIAOU_EXECUTOR_USER", "").strip()
    token_file = os.environ.get("XIAOU_GITHUB_ACTIONS_TOKEN_FILE", "").strip()
    state_db = os.environ.get("XIAOU_STATE_DB", "").strip()
    if not all((repository, issuer, executor, executor_user, token_file, state_db)):
        raise WorkerError("required_worker_configuration_missing")

    token = _read_actions_token(Path(token_file))

    poll_seconds = int(os.environ.get("XIAOU_POLL_SECONDS", str(DEFAULT_POLL_SECONDS)))
    if poll_seconds < 60:
        raise WorkerError("poll_interval_too_short")

    timeout = int(os.environ.get("XIAOU_EXECUTOR_TIMEOUT_SECONDS", "900"))
    if timeout <= 0 or timeout > 3600:
        raise WorkerError("executor_timeout_invalid")

    sudo_path = os.environ.get("XIAOU_SUDO_PATH", "/usr/bin/sudo").strip()
    return WorkerConfig(
        repository=repository,
        trusted_issuer=issuer,
        executor=Path(executor),
        executor_user=executor_user,
        state_db=Path(state_db),
        actions_token=token,
        poll_seconds=poll_seconds,
        executor_timeout_seconds=timeout,
        sudo_path=Path(sudo_path),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run XiaoU controlled ops pull worker V2")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    try:
        config = _load_config()
        store = ReplayStore(config.state_db)
        if args.once:
            print(json.dumps(run_once(config, store), sort_keys=True))
            return 0

        while True:
            counts = run_once(config, store)
            print(json.dumps(counts, sort_keys=True), flush=True)
            time.sleep(config.poll_seconds)
    except (OSError, ValueError, WorkerError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
