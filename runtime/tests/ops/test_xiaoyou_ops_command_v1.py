from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts.xiaoyou_ops_command_v1 import CommandValidationError, validate_command


NOW = datetime(2026, 9, 23, 10, 30, tzinfo=timezone.utc)
SHA = "a" * 40


def _command(**changes):
    payload = {
        "protocol": "XIAOU_OPS_COMMAND_V1",
        "command_id": "worker-v2-test-001",
        "pr_number": 9,
        "candidate_sha": SHA,
        "action": "READ_ONLY_INSPECTION",
        "issued_at": "2026-09-23T10:00:00Z",
        "expires_at": "2026-09-23T11:00:00Z",
        "production_change_authorized": False,
        "objective": "Inspect the harmless pipeline test state.",
        "evidence_requirements": ["production SHA", "gateway service state"],
    }
    payload.update(changes)
    return payload


def test_valid_read_only_command_passes():
    result = validate_command(_command(), now=NOW)
    assert result["candidate_sha"] == SHA
    assert result["action"] == "READ_ONLY_INSPECTION"
    assert result["production_change_authorized"] is False


def test_verify_is_allowed():
    result = validate_command(_command(action="VERIFY"), now=NOW)
    assert result["action"] == "VERIFY"


def test_deploy_is_rejected_in_v2():
    with pytest.raises(CommandValidationError, match="action_not_allowed"):
        validate_command(_command(action="DEPLOY"), now=NOW)


def test_production_change_flag_is_rejected():
    with pytest.raises(CommandValidationError, match="production_change_must_be_false"):
        validate_command(_command(production_change_authorized=True), now=NOW)


def test_unknown_field_fails_closed():
    with pytest.raises(CommandValidationError, match="unknown_fields"):
        validate_command(_command(shell="rm -rf /"), now=NOW)


def test_missing_field_fails_closed():
    payload = _command()
    del payload["candidate_sha"]
    with pytest.raises(CommandValidationError, match="missing_fields"):
        validate_command(payload, now=NOW)


def test_expired_command_is_rejected():
    with pytest.raises(CommandValidationError, match="command_expired"):
        validate_command(
            _command(
                issued_at="2026-09-23T08:00:00Z",
                expires_at="2026-09-23T09:00:00Z",
            ),
            now=NOW,
        )


def test_validity_window_cannot_exceed_12_hours():
    with pytest.raises(CommandValidationError, match="validity_window_too_long"):
        validate_command(
            _command(
                issued_at="2026-09-23T00:00:00Z",
                expires_at="2026-09-23T13:00:01Z",
            ),
            now=datetime(2026, 9, 23, 1, 0, tzinfo=timezone.utc),
        )


def test_future_issue_time_beyond_skew_is_rejected():
    with pytest.raises(CommandValidationError, match="issued_at_in_future"):
        validate_command(
            _command(
                issued_at="2026-09-23T10:36:00Z",
                expires_at="2026-09-23T11:00:00Z",
            ),
            now=NOW,
        )


def test_timezone_is_required():
    with pytest.raises(CommandValidationError, match="issued_at_timezone_required"):
        validate_command(_command(issued_at="2026-09-23T10:00:00"), now=NOW)


def test_candidate_sha_is_normalized_to_lowercase():
    result = validate_command(_command(candidate_sha="A" * 40), now=NOW)
    assert result["candidate_sha"] == SHA
