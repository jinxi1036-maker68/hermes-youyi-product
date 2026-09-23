from __future__ import annotations

import pytest

from scripts.xiaoyou_ops_report_v1 import ReportValidationError, render_markdown, validate_report


SHA = "9130296098293f0aae3766619f40e1457f5a5b37"


def _report(**changes):
    payload = {
        "protocol": "XIAOU_OPS_REPORT_V1",
        "command_id": "pipeline-v1-readonly-001",
        "pr_number": 1,
        "candidate_sha": SHA,
        "action": "READ_ONLY_INSPECTION",
        "result": "PASS",
        "code_changed": False,
        "production_changed": False,
        "summary": "Read-only observation completed.",
        "evidence": {"gateway": "active"},
        "anomalies": [],
    }
    payload.update(changes)
    return payload


def test_valid_read_only_report_passes():
    result = validate_report(_report())
    assert result["candidate_sha"] == SHA
    assert result["code_changed"] is False


def test_code_change_is_always_rejected():
    with pytest.raises(ReportValidationError, match="code_changed_must_be_false"):
        validate_report(_report(code_changed=True))


def test_read_only_cannot_claim_production_change():
    with pytest.raises(ReportValidationError, match="read_only_action_cannot_change_production"):
        validate_report(_report(production_changed=True))


def test_unknown_action_fails_closed():
    with pytest.raises(ReportValidationError, match="action_invalid"):
        validate_report(_report(action="FIX_CODE"))


def test_markdown_contains_machine_marker_and_structured_evidence():
    rendered = render_markdown(_report())
    assert "<!-- xiaou-ops-report:v1 -->" in rendered
    assert "XIAOU_OPS_REPORT_V1" in rendered
    assert SHA in rendered
