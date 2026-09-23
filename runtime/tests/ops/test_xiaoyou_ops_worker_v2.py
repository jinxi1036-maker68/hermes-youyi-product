from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.xiaoyou_ops_command_v1 import CommandValidationError
from scripts.xiaoyou_ops_worker_v2 import (
    ReplayStore,
    WorkerConfig,
    WorkerError,
    parse_command_comment,
    process_comment,
    run_fixed_executor,
    verify_github_trust,
)


SHA = "b" * 40
NOW = datetime(2026, 9, 23, 10, 30, tzinfo=timezone.utc)


def _command(**changes):
    payload = {
        "protocol": "XIAOU_OPS_COMMAND_V1",
        "command_id": "worker-v2-boundary-001",
        "pr_number": 4,
        "candidate_sha": SHA,
        "action": "READ_ONLY_INSPECTION",
        "issued_at": "2026-09-23T10:00:00Z",
        "expires_at": "2026-09-23T11:00:00Z",
        "production_change_authorized": False,
        "objective": "Inspect harmless state.",
        "evidence_requirements": ["production SHA"],
    }
    payload.update(changes)
    return payload


def _comment(payload=None, *, login="trusted-owner", comment_id=101):
    body = (
        "XIAOU_OPS_COMMAND_V1\n\n"
        "```json\n"
        + json.dumps(payload or _command())
        + "\n```"
    )
    return {
        "id": comment_id,
        "body": body,
        "issue_url": "https://api.github.com/repos/acme/repo/issues/4",
        "user": {"login": login},
    }


def _pr(**changes):
    payload = {
        "state": "open",
        "user": {"login": "trusted-owner"},
        "base": {"ref": "main"},
        "head": {
            "sha": SHA,
            "repo": {"full_name": "acme/repo"},
        },
    }
    payload.update(changes)
    return payload


def test_command_comment_requires_json_fence():
    with pytest.raises(CommandValidationError, match="command_json_fence_missing"):
        parse_command_comment("XIAOU_OPS_COMMAND_V1\nnot json", now=NOW)


def test_trusted_comment_and_pr_binding_pass():
    command = parse_command_comment(_comment()["body"], now=NOW)
    verify_github_trust(
        repository="acme/repo",
        trusted_issuer="trusted-owner",
        comment=_comment(),
        command=command,
        pr=_pr(),
    )


def test_untrusted_comment_author_fails_closed():
    command = parse_command_comment(_comment()["body"], now=NOW)
    with pytest.raises(WorkerError, match="untrusted_comment_author"):
        verify_github_trust(
            repository="acme/repo",
            trusted_issuer="trusted-owner",
            comment=_comment(login="someone-else"),
            command=command,
            pr=_pr(),
        )


def test_candidate_sha_must_match_current_pr_head():
    command = parse_command_comment(_comment()["body"], now=NOW)
    with pytest.raises(WorkerError, match="candidate_sha_not_current_pr_head"):
        verify_github_trust(
            repository="acme/repo",
            trusted_issuer="trusted-owner",
            comment=_comment(),
            command=command,
            pr=_pr(head={"sha": "c" * 40, "repo": {"full_name": "acme/repo"}}),
        )


def test_fork_pr_is_rejected():
    command = parse_command_comment(_comment()["body"], now=NOW)
    with pytest.raises(WorkerError, match="fork_head_rejected"):
        verify_github_trust(
            repository="acme/repo",
            trusted_issuer="trusted-owner",
            comment=_comment(),
            command=command,
            pr=_pr(head={"sha": SHA, "repo": {"full_name": "fork/repo"}}),
        )


def test_non_main_base_is_rejected():
    command = parse_command_comment(_comment()["body"], now=NOW)
    with pytest.raises(WorkerError, match="pr_base_not_main"):
        verify_github_trust(
            repository="acme/repo",
            trusted_issuer="trusted-owner",
            comment=_comment(),
            command=command,
            pr=_pr(base={"ref": "dev"}),
        )


def test_replay_store_rejects_same_comment_or_command(tmp_path: Path):
    store = ReplayStore(tmp_path / "state.sqlite")
    command = _command()
    store.reserve(comment_id=101, command=command)
    assert store.seen(comment_id=101, command_id="new-id")
    assert store.seen(comment_id=999, command_id=command["command_id"])


def test_executor_receives_no_worker_github_token(monkeypatch, tmp_path: Path):
    executor = tmp_path / "executor"
    executor.write_text("#!/bin/true\n", encoding="utf-8")

    report = {
        "protocol": "XIAOU_OPS_REPORT_V1",
        "command_id": "worker-v2-boundary-001",
        "pr_number": 4,
        "candidate_sha": SHA,
        "action": "READ_ONLY_INSPECTION",
        "result": "PASS",
        "code_changed": False,
        "production_changed": False,
        "summary": "Read-only inspection passed.",
        "evidence": {"production_sha": SHA},
        "anomalies": [],
    }
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(report).encode("utf-8"),
            stderr=b"",
        )

    monkeypatch.setattr("scripts.xiaoyou_ops_worker_v2.subprocess.run", fake_run)

    config = WorkerConfig(
        repository="acme/repo",
        trusted_issuer="trusted-owner",
        executor=executor.resolve(),
        state_db=tmp_path / "state.sqlite",
        actions_token="must-not-reach-codex",
        executor_home=tmp_path / "codex-home",
    )
    result = run_fixed_executor(config, _command())

    assert result["result"] == "PASS"
    assert captured["kwargs"]["shell"] is False
    assert "must-not-reach-codex" not in captured["kwargs"]["env"].values()
    assert "XIAOU_GITHUB_ACTIONS_TOKEN" not in captured["kwargs"]["env"]


def test_executor_report_cannot_claim_production_change(monkeypatch, tmp_path: Path):
    executor = tmp_path / "executor"
    executor.write_text("#!/bin/true\n", encoding="utf-8")

    report = {
        "protocol": "XIAOU_OPS_REPORT_V1",
        "command_id": "worker-v2-boundary-001",
        "pr_number": 4,
        "candidate_sha": SHA,
        "action": "READ_ONLY_INSPECTION",
        "result": "PASS",
        "code_changed": False,
        "production_changed": True,
        "summary": "Invalid mutation claim.",
        "evidence": {},
        "anomalies": [],
    }

    monkeypatch.setattr(
        "scripts.xiaoyou_ops_worker_v2.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(report).encode("utf-8"),
            stderr=b"",
        ),
    )

    config = WorkerConfig(
        repository="acme/repo",
        trusted_issuer="trusted-owner",
        executor=executor.resolve(),
        state_db=tmp_path / "state.sqlite",
        actions_token="worker-token",
    )
    with pytest.raises(WorkerError, match="executor_report_invalid"):
        run_fixed_executor(config, _command())


def test_untrusted_marker_is_ignored_before_command_parsing(tmp_path: Path):
    config = WorkerConfig(
        repository="acme/repo",
        trusted_issuer="trusted-owner",
        executor=tmp_path / "missing-executor",
        state_db=tmp_path / "state.sqlite",
        actions_token="unused",
    )
    store = ReplayStore(config.state_db)
    comment = {
        "id": 555,
        "body": "XIAOU_OPS_COMMAND_V1\nmalformed attacker payload",
        "issue_url": "https://api.github.com/repos/acme/repo/issues/4",
        "user": {"login": "untrusted-user"},
    }

    assert process_comment(config, store, comment) == "ignored"
