from __future__ import annotations


def test_execution_receipt_exposes_verified_object_and_idempotency():
    from plugins.tuoguan_core.execution_receipts import build_execution_receipt

    result = build_execution_receipt(
        {"ok": True, "data": {"task_id": "task-1", "updated_at": "2026-08-13T21:00:00", "writeback_verified": True}},
        operation_id="message-1",
        operation="update_task",
    )
    receipt = result["execution_receipt"]
    assert receipt["status"] == "completed"
    assert receipt["object_type"] == "task"
    assert receipt["object_id"] == "task-1"
    assert receipt["object_version"] == "2026-08-13T21:00:00"
    assert receipt["writeback_verified"] is True
    assert result["data"]["execution_receipt"] == receipt


def test_execution_receipt_reports_real_failure_layer():
    from plugins.tuoguan_core.execution_receipts import build_execution_receipt

    result = build_execution_receipt(
        {"ok": False, "error": "wrong_tool_for_cancel_intent", "message": "use cancel"},
        operation_id="message-2",
        operation="update_task",
        idempotency_result="rejected",
    )
    receipt = result["execution_receipt"]
    assert receipt["status"] == "failed"
    assert receipt["error_layer"] == "tool_selection"
    assert receipt["error_code"] == "wrong_tool_for_cancel_intent"
    assert receipt["writeback_verified"] is False


def test_no_write_task_result_is_a_clarification_receipt_not_a_fake_completion():
    from plugins.tuoguan_core.execution_receipts import build_execution_receipt

    result = build_execution_receipt(
        {
            "ok": True,
            "data": {
                "result_action": "clarification_needed",
                "task_id": "task-should-not-be-claimed",
                "writeback_verified": True,
                "no_write_performed": True,
            },
        },
        operation_id="message-no-write",
        operation="update_task",
    )

    receipt = result["execution_receipt"]
    assert receipt["status"] == "clarification_required"
    assert receipt["object_id"] == ""
    assert receipt["writeback_verified"] is False
    assert receipt["idempotency_result"] == "not_applied"


def test_write_operation_exception_is_closed_as_failed_receipt(tmp_path, monkeypatch):
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    service = TuoguanToolService.__new__(TuoguanToolService)
    service.store = TuoguanStore(tmp_path)
    service.identity = UserIdentity(
        platform="test", platform_user_id="boss", canonical_user_id="boss",
        person_name="Boss", role="boss", approval_state="approved",
    )
    service.user_id = "boss"
    monkeypatch.setattr("plugins.tuoguan_core.write_guard.guard_enabled", lambda _path: False)

    result = service._operation("operation-1", "create_task", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    assert result["ok"] is False
    assert result["error"] == "system_error"
    assert result["execution_receipt"]["status"] == "failed"
    stored = service.store.read_json("tool_operations.json", {})
    row = stored["boss:create_task:operation-1"]
    assert row["status"] == "failed"
    assert row["result"]["execution_receipt"]["error_layer"] == "execution"


def test_replayed_operation_returns_replayed_receipt(tmp_path, monkeypatch):
    from plugins.tuoguan_core.models import UserIdentity
    from plugins.tuoguan_core.store import TuoguanStore
    from plugins.tuoguan_core.tool_service import TuoguanToolService

    service = TuoguanToolService.__new__(TuoguanToolService)
    service.store = TuoguanStore(tmp_path)
    service.identity = UserIdentity(
        platform="test", platform_user_id="boss", canonical_user_id="boss",
        person_name="Boss", role="boss", approval_state="approved",
    )
    service.user_id = "boss"
    monkeypatch.setattr("plugins.tuoguan_core.write_guard.guard_enabled", lambda _path: False)
    calls = []

    first = service._operation(
        "operation-2", "create_task",
        lambda: calls.append("executed") or {"ok": True, "data": {"task_id": "task-2", "writeback_verified": True}},
    )
    second = service._operation(
        "operation-2", "create_task",
        lambda: calls.append("executed-again") or {"ok": True, "data": {"task_id": "bad", "writeback_verified": True}},
    )

    assert first["execution_receipt"]["idempotency_result"] == "applied"
    assert second["execution_receipt"]["idempotency_result"] == "replayed"
    assert second["already_applied"] is True
    assert calls == ["executed"]


def test_concurrent_task_append_does_not_overwrite_existing_updates(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from plugins.tuoguan_core.store import TuoguanStore

    store = TuoguanStore(tmp_path)
    store.write_json("tasks.json", [{"id": "existing", "status": "pending", "title": "old"}])

    def update_existing():
        return store.update_task("existing", lambda task: {**task, "status": "completed"})

    def append_new():
        return store.append_tasks_verified([{"id": "new", "status": "pending", "title": "new"}])

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda fn: fn(), (update_existing, append_new)))

    tasks = {item["id"]: item for item in store.load_tasks()}
    assert tasks["existing"]["status"] == "completed"
    assert tasks["new"]["status"] == "pending"


def test_delivery_receipt_preserves_result_unknown_without_claiming_sent():
    from plugins.tuoguan_core.execution_receipts import delivery_execution_receipt

    receipt = delivery_execution_receipt({
        "id": "notice-1", "status": "result_unknown", "last_error": "lease_expired",
        "last_attempt_at": "2026-08-13T21:00:00",
    })
    assert receipt["status"] == "result_unknown"
    assert receipt["delivery_status"] == "result_unknown"
    assert receipt["writeback_verified"] is True
    assert receipt["error_layer"] == "execution"
