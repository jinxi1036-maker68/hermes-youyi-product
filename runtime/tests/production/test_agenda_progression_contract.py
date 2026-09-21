"""Focused regressions for Agenda progression and Reply Truth boundaries.

These tests deliberately exercise contracts rather than a fixed business
sentence: the Agenda service gets a model-selectable, source-scoped progress
tool, failed writes remain result-unknown, and runtime audit fields never
survive the user-facing sanitizer.
"""

from tuoguan_core.agenda_service_policy import ALLOWED_TOOL_METHODS
from tuoguan_core.execution_receipts import build_execution_receipt
from tuoguan_core.runtime_foundation import _redact_internal_observability_fields
from tuoguan_core.tool_service import TuoguanToolService
from tuoguan_core.tools import agenda_task_tools


def test_agenda_progress_tool_is_explicit_and_source_scoped() -> None:
    names = {name for name, _schema, _handler in agenda_task_tools()}
    assert "agenda_task_contact_current_task_party" in names
    assert "contact_current_task_party" in ALLOWED_TOOL_METHODS


def test_unknown_failed_write_stays_unknown_when_replayed() -> None:
    original = {
        "ok": False,
        "error": "system_error",
        "data": {},
        "execution_receipt": {
            "status": "failed",
            "writeback_verified": False,
            "idempotency_result": "result_unknown",
            "operation_id": "op-test",
        },
    }
    replayed = build_execution_receipt(
        original,
        operation_id="op-test",
        operation="agenda_contact_current_task_party",
        idempotency_result="replayed",
    )
    assert replayed["ok"] is False
    assert replayed["already_applied"] is False
    assert replayed["idempotency_replay_observed"] is True
    assert replayed["execution_receipt"]["idempotency_result"] == "result_unknown"
    assert replayed["execution_receipt"]["status"] == "failed"


def test_runtime_identifiers_and_states_are_not_external_reply_text() -> None:
    source = (
        "task_id=`task_123` receipt_id=receipt_456 operation_id=op_789\n"
        "writeback_verified=true\n"
        "agenda_service durable_reply_staged system_error\n"
        "任务仍待责任人提供进展。"
    )
    rendered, changed = _redact_internal_observability_fields(source)
    assert changed is True
    assert "task_123" not in rendered
    assert "receipt_456" not in rendered
    assert "operation_id" not in rendered
    assert "writeback_verified" not in rendered
    assert "agenda_service" not in rendered
    assert "任务仍待责任人提供进展。" in rendered


def test_legacy_unverified_system_error_projects_to_result_unknown() -> None:
    legacy = {
        "ok": False,
        "error": "system_error",
        "data": {},
        "execution_receipt": {
            "status": "failed",
            "writeback_verified": False,
            "idempotency_result": "applied",
            "error_code": "system_error",
        },
    }
    projected = TuoguanToolService._normalize_unverified_exception_truth(legacy)
    assert projected["execution_receipt"]["idempotency_result"] == "result_unknown"
    assert projected["already_applied"] is False
