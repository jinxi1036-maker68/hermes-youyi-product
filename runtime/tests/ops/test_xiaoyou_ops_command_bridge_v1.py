from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from scripts.xiaoyou_ops_command_v1 import (
    CommandValidationError,
    ack_command,
    command_from_github_comment,
    parse_command_comment,
    validate_command,
)
from scripts.xiaoyou_codex_readonly_worker_v1 import _build_report, _codex_env


NOW = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)
SHA = "d1dee6dbcedd739849c9045e521fb1c8ce2aa901"


def _command(**changes):
    payload = {
        "protocol": "XIAOU_OPS_COMMAND_V1",
        "command_id": "pipeline-v2-readonly-001",
        "pr_number": 2,
        "candidate_sha": SHA,
        "action": "READ_ONLY_INSPECTION",
        "allow_code_change": False,
        "allow_production_change": False,
        "goal": "Inspect the candidate and current server state without writes.",
        "evidence_requirements": ["candidate SHA", "service health"],
        "issued_at": "2026-09-23T08:00:00Z",
        "expires_at": "2026-09-23T09:00:00Z",
    }
    payload.update(changes)
    return payload


def _comment(payload=None, **changes):
    payload = payload or _command()
    body = (
        "<!-- xiaou-ops-command:v1 -->\n"
        "```json\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\n```"
    )
    comment = {
        "id": 1001,
        "body": body,
        "user": {"login": "jinxi1036-maker68"},
        "author_association": "OWNER",
    }
    comment.update(changes)
    return comment


def test_valid_readonly_command_passes():
    command = validate_command(_command(), now=NOW)
    assert command["candidate_sha"] == SHA
    assert command["allow_code_change"] is False
    assert command["allow_production_change"] is False


def test_deploy_action_is_rejected_in_v1():
    with pytest.raises(CommandValidationError, match="action_not_allowed_in_v1"):
        validate_command(_command(action="DEPLOY"), now=NOW)


def test_code_change_authority_is_rejected():
    with pytest.raises(CommandValidationError, match="allow_code_change_must_be_false"):
        validate_command(_command(allow_code_change=True), now=NOW)


def test_production_change_authority_is_rejected():
    with pytest.raises(CommandValidationError, match="allow_production_change_must_be_false"):
        validate_command(_command(allow_production_change=True), now=NOW)


def test_expired_command_is_rejected():
    with pytest.raises(CommandValidationError, match="command_expired"):
        validate_command(
            _command(
                issued_at="2026-09-23T06:00:00Z",
                expires_at="2026-09-23T07:00:00Z",
            ),
            now=NOW,
        )


def test_command_lifetime_over_24h_is_rejected():
    with pytest.raises(CommandValidationError, match="command_lifetime_exceeds_24h"):
        validate_command(
            _command(
                issued_at="2026-09-23T08:00:00Z",
                expires_at="2026-09-24T09:00:01Z",
            ),
            now=NOW,
        )


def test_non_owner_comment_is_ignored():
    result = command_from_github_comment(
        _comment(user={"login": "someone-else"}, author_association="NONE"),
        owner_login="jinxi1036-maker68",
        pr_number=2,
        pr_head_sha=SHA,
        now=NOW,
    )
    assert result is None


def test_owner_command_must_match_current_pr_head():
    with pytest.raises(CommandValidationError, match="command_candidate_not_current_pr_head"):
        command_from_github_comment(
            _comment(),
            owner_login="jinxi1036-maker68",
            pr_number=2,
            pr_head_sha="a" * 40,
            now=NOW,
        )


def test_owner_command_must_match_pr_number():
    with pytest.raises(CommandValidationError, match="command_pr_mismatch"):
        command_from_github_comment(
            _comment(_command(pr_number=3)),
            owner_login="jinxi1036-maker68",
            pr_number=2,
            pr_head_sha=SHA,
            now=NOW,
        )


def test_sensitive_command_material_is_rejected():
    with pytest.raises(CommandValidationError, match="sensitive_key_rejected"):
        validate_command(_command(api_token="do-not-accept"), now=NOW)


def test_comment_parser_requires_machine_marker_and_json_block():
    body = _comment()["body"]
    parsed = parse_command_comment(body, now=NOW)
    assert parsed["command_id"] == "pipeline-v2-readonly-001"

    with pytest.raises(CommandValidationError, match="command_marker_missing"):
        parse_command_comment("```json\n{}\n```", now=NOW)


def test_ack_is_idempotent(tmp_path: Path):
    state = tmp_path / "state.json"
    ack_command(state, "pipeline-v2-readonly-001")
    ack_command(state, "pipeline-v2-readonly-001")
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["processed_command_ids"] == ["pipeline-v2-readonly-001"]


def test_codex_environment_does_not_forward_github_or_random_production_secrets(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/codex")
    monkeypatch.setenv("XIAOU_GITHUB_ACTIONS_TOKEN", "github-secret")
    monkeypatch.setenv("DATABASE_PASSWORD", "production-secret")
    env = _codex_env()
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/codex"
    assert "XIAOU_GITHUB_ACTIONS_TOKEN" not in env
    assert "DATABASE_PASSWORD" not in env


def test_worker_report_never_claims_code_or_production_change():
    command = validate_command(_command(), now=NOW)
    result = {
        "result": "PASS",
        "summary": "Read-only verification completed.",
        "evidence": [{"key": "gateway", "value": "active"}],
        "anomalies": [],
    }
    report = _build_report(command, result)
    assert report["code_changed"] is False
    assert report["production_changed"] is False
    assert report["candidate_sha"] == SHA
