#!/usr/bin/env python3
"""Run one validated XiaoU read-only Codex operation and return evidence to GitHub.

Security model:
- GitHub comments are only a control-plane queue; they never become shell code.
- Only validated READ_ONLY_INSPECTION/VERIFY commands are accepted.
- Codex runs with read-only sandbox and approval_policy=never.
- The GitHub Actions dispatch credential is withheld from the Codex process.
- The command is acknowledged only after the validated PR report is observed.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scripts.xiaoyou_ops_command_v1 import (
    CommandValidationError,
    _github_json,
    ack_command,
    poll_next_command,
)
from scripts.xiaoyou_ops_report_v1 import validate_report

REPORT_WORKFLOW = "xiaoyou-ops-report-intake.yml"
REPORT_MARKER = "<!-- xiaou-ops-report:v1 -->"
ALLOWED_ACTIONS = {"READ_ONLY_INSPECTION", "VERIFY"}


class WorkerError(RuntimeError):
    pass


@contextmanager
def _exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise WorkerError("worker_already_running") from exc
    try:
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _open_pr_numbers(repo: str) -> list[int]:
    rows = _github_json(f"https://api.github.com/repos/{repo}/pulls?state=open&per_page=50")
    if not isinstance(rows, list):
        raise WorkerError("open_pr_list_invalid")
    values: list[int] = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("number"), int):
            values.append(int(row["number"]))
    return sorted(values)


def _next_repo_command(
    *,
    repo: str,
    owner_login: str,
    state_path: Path,
) -> dict[str, Any] | None:
    for pr_number in _open_pr_numbers(repo):
        try:
            command = poll_next_command(
                repo=repo,
                pr_number=pr_number,
                owner_login=owner_login,
                state_path=state_path,
            )
        except CommandValidationError as exc:
            # Fail closed on an owner-authored malformed control-plane command.
            raise WorkerError(f"command_validation_failed:pr={pr_number}:{exc}") from exc
        if command:
            return command
    return None


def _run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _clone_candidate(repo: str, candidate_sha: str, destination: Path) -> None:
    clone = _run(
        [
            "git",
            "clone",
            "--quiet",
            "--no-checkout",
            f"https://github.com/{repo}.git",
            str(destination),
        ],
        timeout=180,
    )
    if clone.returncode != 0:
        raise WorkerError(f"candidate_clone_failed:{clone.stderr[-500:]}")

    checkout = _run(
        ["git", "checkout", "--quiet", "--detach", candidate_sha],
        cwd=destination,
        timeout=120,
    )
    if checkout.returncode != 0:
        raise WorkerError(f"candidate_checkout_failed:{checkout.stderr[-500:]}")

    verify = _run(["git", "rev-parse", "HEAD"], cwd=destination, timeout=30)
    if verify.returncode != 0 or verify.stdout.strip() != candidate_sha:
        raise WorkerError("candidate_checkout_sha_mismatch")


def _codex_env() -> dict[str, str]:
    # Never forward GitHub/report credentials or arbitrary production env vars.
    allowed = {
        "PATH",
        "HOME",
        "CODEX_HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TMPDIR",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def _build_prompt(command: dict[str, Any], *, production_repo: str) -> str:
    evidence = "\n".join(f"- {item}" for item in command["evidence_requirements"])
    return f"""You are the XiaoU server verification worker, not the code owner.

Fixed role boundary:
- You may inspect and verify only.
- You must not edit source files.
- You must not create commits, push, merge, tag, or choose the project route.
- You must not modify production data, configuration, model/provider settings, or service state.
- You must not restart or deploy anything.
- If verification would require a write or privileged mutation, return BLOCKED instead of attempting it.

Command:
- command_id: {command['command_id']}
- action: {command['action']}
- candidate_sha: {command['candidate_sha']}
- goal: {command['goal']}

Evidence required:
{evidence}

The candidate repository in the current working directory is already checked out at the exact candidate SHA.
The current production repository may be inspected read-only at: {production_repo}

Do not include secrets, tokens, cookies, private keys, full environment dumps, student personal data, or staff private data in the result.
Return only the structured result required by the supplied output schema.
"""


def _run_codex(
    *,
    command: dict[str, Any],
    candidate_dir: Path,
    result_path: Path,
    schema_path: Path,
    production_repo: str,
) -> dict[str, Any]:
    prompt = _build_prompt(command, production_repo=production_repo)
    cmd = [
        "codex",
        "exec",
        "--config",
        'sandbox_mode="read-only"',
        "--config",
        'approval_policy="never"',
        "--output-schema",
        str(schema_path),
        "-o",
        str(result_path),
        prompt,
    ]
    completed = _run(
        cmd,
        cwd=candidate_dir,
        env=_codex_env(),
        timeout=1200,
    )
    if completed.returncode != 0:
        raise WorkerError(
            "codex_exec_failed:"
            + (completed.stderr.strip() or completed.stdout.strip())[-1000:]
        )
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerError("codex_result_invalid_json") from exc
    if not isinstance(result, dict):
        raise WorkerError("codex_result_must_be_object")
    return result


def _build_report(command: dict[str, Any], codex_result: dict[str, Any]) -> dict[str, Any]:
    result = str(codex_result.get("result") or "")
    if result not in {"PASS", "FAIL", "BLOCKED"}:
        raise WorkerError("codex_result_enum_invalid")

    raw_evidence = codex_result.get("evidence")
    if not isinstance(raw_evidence, list):
        raise WorkerError("codex_evidence_invalid")
    evidence: dict[str, str] = {}
    for item in raw_evidence:
        if not isinstance(item, dict):
            raise WorkerError("codex_evidence_item_invalid")
        key = str(item.get("key") or "").strip()
        value = str(item.get("value") or "").strip()
        if not key or not value:
            raise WorkerError("codex_evidence_item_invalid")
        evidence[key] = value

    anomalies = codex_result.get("anomalies")
    if not isinstance(anomalies, list) or not all(isinstance(item, str) for item in anomalies):
        raise WorkerError("codex_anomalies_invalid")

    report = {
        "protocol": "XIAOU_OPS_REPORT_V1",
        "command_id": command["command_id"],
        "pr_number": command["pr_number"],
        "candidate_sha": command["candidate_sha"],
        "action": command["action"],
        "result": result,
        "code_changed": False,
        "production_changed": False,
        "summary": str(codex_result.get("summary") or "").strip(),
        "evidence": evidence,
        "anomalies": anomalies,
    }
    return validate_report(report)


def _dispatch_report(*, repo: str, token: str, report: dict[str, Any]) -> None:
    payload = {
        "ref": "main",
        "inputs": {
            "pr_number": str(report["pr_number"]),
            "report_base64": base64.b64encode(
                json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).decode("ascii"),
        },
    }
    request = Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/{REPORT_WORKFLOW}/dispatches",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "xiaou-codex-readonly-worker-v1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            if response.status != 204:
                raise WorkerError(f"report_dispatch_unexpected_status:{response.status}")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise WorkerError(f"report_dispatch_failed:{exc.code}:{body}") from exc


def _report_seen(*, repo: str, pr_number: int, command_id: str) -> bool:
    comments = _github_json(
        f"https://api.github.com/repos/{repo}/issues/{int(pr_number)}/comments?per_page=100"
    )
    if not isinstance(comments, list):
        return False
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = str(comment.get("body") or "")
        if REPORT_MARKER in body and f'"command_id": "{command_id}"' in body:
            return True
    return False


def execute_once(
    *,
    repo: str,
    owner_login: str,
    state_path: Path,
    lock_path: Path,
    schema_path: Path,
    production_repo: str,
    actions_token: str,
) -> int:
    with _exclusive_lock(lock_path):
        command = _next_repo_command(
            repo=repo,
            owner_login=owner_login,
            state_path=state_path,
        )
        if command is None:
            return 3
        if command["action"] not in ALLOWED_ACTIONS:
            raise WorkerError("action_not_readonly_v1")

        with tempfile.TemporaryDirectory(prefix="xiaou-codex-ops-") as temp:
            root = Path(temp)
            candidate_dir = root / "candidate"
            result_path = root / "codex-result.json"
            _clone_candidate(repo, command["candidate_sha"], candidate_dir)
            codex_result = _run_codex(
                command=command,
                candidate_dir=candidate_dir,
                result_path=result_path,
                schema_path=schema_path,
                production_repo=production_repo,
            )
            report = _build_report(command, codex_result)
            _dispatch_report(repo=repo, token=actions_token, report=report)

        # A successful dispatch is not enough. Observe the validated GitHub
        # comment before acknowledging the command so failed intake can retry.
        for _ in range(12):
            if _report_seen(
                repo=repo,
                pr_number=command["pr_number"],
                command_id=command["command_id"],
            ):
                ack_command(state_path, command["command_id"])
                return 0
            time.sleep(5)
        raise WorkerError("validated_report_not_observed")


def main() -> int:
    parser = argparse.ArgumentParser(description="XiaoU read-only Codex ops worker")
    parser.add_argument("--repo", default=os.environ.get("XIAOU_REPO", ""))
    parser.add_argument("--owner-login", default=os.environ.get("XIAOU_OWNER_LOGIN", ""))
    parser.add_argument("--state-file", type=Path, default=Path(os.environ.get("XIAOU_STATE_FILE", "/var/lib/xiaoyou-ops-bridge/state.json")))
    parser.add_argument("--lock-file", type=Path, default=Path(os.environ.get("XIAOU_LOCK_FILE", "/var/lib/xiaoyou-ops-bridge/worker.lock")))
    parser.add_argument("--schema", type=Path, default=Path(os.environ.get("XIAOU_RESULT_SCHEMA", "work/ai-collaboration/XIAOU_CODEX_READONLY_RESULT_V1.schema.json")))
    parser.add_argument("--production-repo", default=os.environ.get("XIAOU_PRODUCTION_REPO", ""))
    args = parser.parse_args()

    token = os.environ.get("XIAOU_GITHUB_ACTIONS_TOKEN", "")
    if not args.repo or not args.owner_login or not args.production_repo or not token:
        print(json.dumps({"ok": False, "error": "required_worker_configuration_missing"}))
        return 2

    try:
        return execute_once(
            repo=args.repo,
            owner_login=args.owner_login,
            state_path=args.state_file,
            lock_path=args.lock_file,
            schema_path=args.schema.resolve(),
            production_repo=args.production_repo,
            actions_token=token,
        )
    except (OSError, subprocess.SubprocessError, WorkerError, CommandValidationError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
