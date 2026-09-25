from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace


def test_drain_gate_is_open_without_file(tmp_path):
    from plugins.platforms.wecom.maintenance_drain import WecomCallbackDrainGate

    gate = WecomCallbackDrainGate(tmp_path / "drain.json")

    assert gate.snapshot()["active"] is False
    assert gate.is_draining() is False


def test_drain_gate_is_fail_closed_for_corrupt_file(tmp_path):
    from plugins.platforms.wecom.maintenance_drain import WecomCallbackDrainGate

    path = tmp_path / "drain.json"
    path.write_text("{broken", encoding="utf-8")

    snapshot = WecomCallbackDrainGate(path).snapshot()

    assert snapshot["active"] is True
    assert snapshot["valid"] is False
    assert snapshot["error"] == "drain_state_unreadable"


def test_drain_gate_waits_until_file_is_removed(tmp_path):
    from plugins.platforms.wecom.maintenance_drain import (
        DRAIN_STATE,
        SCHEMA_VERSION,
        WecomCallbackDrainGate,
    )

    path = tmp_path / "drain.json"
    path.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "state": DRAIN_STATE}),
        encoding="utf-8",
    )
    gate = WecomCallbackDrainGate(path)
    events: list[str] = []

    async def scenario():
        async def waiter():
            await gate.wait_until_open(poll_seconds=0.01)
            events.append("opened")

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.03)
        assert events == []
        path.unlink()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())
    assert events == ["opened"]


def _receipt_db(path: Path, *, processing: int = 0, claimed_waiting: int = 0) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE inbound_receipts (
                receipt_key TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                reply_status TEXT NOT NULL
            )
            """
        )
        for index in range(processing):
            connection.execute(
                "INSERT INTO inbound_receipts VALUES (?, 'claimed', 'processing')",
                (f"processing-{index}",),
            )
        for index in range(claimed_waiting):
            connection.execute(
                "INSERT INTO inbound_receipts VALUES (?, 'claimed', 'received')",
                (f"waiting-{index}",),
            )
        connection.commit()
    finally:
        connection.close()


def test_drain_controller_waits_for_processing_zero_but_allows_claimed_backlog(tmp_path):
    from scripts import xiaoyou_wecom_callback_drain as controller

    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    drain = state / "wecom_callback_drain.json"
    receipt_db = state / "wecom_callback_receipts.sqlite3"
    controller._atomic_enter(drain, reason="test")
    _receipt_db(receipt_db, processing=0, claimed_waiting=3)

    result = controller._wait_for_quiesced(
        runtime_home=home,
        drain_file=drain,
        receipt_db=receipt_db,
        timeout_seconds=1,
        stable_seconds=0,
        poll_seconds=0.01,
    )

    assert result["ok"] is True
    assert result["quiesced"] is True
    assert result["receipts"]["processing_count"] == 0
    assert result["receipts"]["claimed_count"] == 3


def test_drain_controller_refuses_quiesced_while_processing_exists(tmp_path):
    from scripts import xiaoyou_wecom_callback_drain as controller

    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    drain = state / "wecom_callback_drain.json"
    receipt_db = state / "wecom_callback_receipts.sqlite3"
    controller._atomic_enter(drain, reason="test")
    _receipt_db(receipt_db, processing=1)

    result = controller._wait_for_quiesced(
        runtime_home=home,
        drain_file=drain,
        receipt_db=receipt_db,
        timeout_seconds=0.05,
        stable_seconds=0,
        poll_seconds=0.01,
    )

    assert result["ok"] is False
    assert result["error"] == "drain_wait_timeout"
    assert result["receipts"]["processing_count"] == 1


def test_callback_poller_pauses_dispatch_and_marks_processing_before_handle(tmp_path):
    from plugins.platforms.wecom.callback_adapter import WecomCallbackAdapter
    from plugins.platforms.wecom.maintenance_drain import (
        DRAIN_STATE,
        SCHEMA_VERSION,
        WecomCallbackDrainGate,
    )

    drain_path = tmp_path / "drain.json"
    drain_path.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "state": DRAIN_STATE}),
        encoding="utf-8",
    )
    order: list[str] = []

    class Receipts:
        def mark_processing(self, key):
            order.append(f"processing:{key}")
            return True

        def mark_failed(self, key, error):
            order.append(f"failed:{key}:{error}")
            return True

        def mark_processed(self, key, *, session_id=""):
            order.append(f"processed:{key}:{session_id}")
            return True

    adapter = WecomCallbackAdapter.__new__(WecomCallbackAdapter)
    adapter._message_queue = asyncio.Queue()
    adapter._background_tasks = set()
    adapter._inbound_receipts = Receipts()
    adapter._drain_gate = WecomCallbackDrainGate(drain_path)

    async def handle_message(_event):
        order.append("handle")

    adapter.handle_message = handle_message

    async def scenario():
        await adapter._message_queue.put((object(), "receipt-1", "session-1"))
        poller = asyncio.create_task(adapter._poll_loop())
        try:
            await asyncio.sleep(0.04)
            assert order == []
            drain_path.unlink()

            deadline = asyncio.get_running_loop().time() + 1
            while "processed:receipt-1:session-1" not in order:
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError(f"dispatch did not finish: {order}")
                await asyncio.sleep(0.01)
        finally:
            poller.cancel()
            try:
                await poller
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())

    assert order == [
        "processing:receipt-1",
        "handle",
        "processed:receipt-1:session-1",
    ]


def test_callback_http_intake_still_claims_and_acks_while_drain_active(tmp_path):
    from plugins.platforms.wecom.callback_adapter import WecomCallbackAdapter
    from plugins.platforms.wecom.maintenance_drain import (
        DRAIN_STATE,
        SCHEMA_VERSION,
        WecomCallbackDrainGate,
    )

    drain_path = tmp_path / "drain.json"
    drain_path.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "state": DRAIN_STATE}),
        encoding="utf-8",
    )
    claimed: list[dict] = []

    class Receipts:
        def claim(self, **kwargs):
            claimed.append(kwargs)
            return {"accepted": True, "receipt_key": "receipt-1"}

        def mark_failed(self, *_args, **_kwargs):
            raise AssertionError("receipt should not fail")

    class Request:
        query = {"msg_signature": "sig", "timestamp": "1", "nonce": "n"}

        async def read(self):
            return b"<xml/>"

    adapter = WecomCallbackAdapter.__new__(WecomCallbackAdapter)
    adapter._apps = [{
        "name": "default",
        "corp_id": "corp",
        "agent_id": "1",
    }]
    adapter._inbound_receipts = Receipts()
    adapter._message_queue = asyncio.Queue()
    adapter._user_app_map = {}
    adapter._drain_gate = WecomCallbackDrainGate(drain_path)
    adapter._decrypt_request = lambda *_args, **_kwargs: "<decrypted/>"
    source = SimpleNamespace(user_id="user-1", chat_id="user-1")
    event = SimpleNamespace(message_id="msg-1", source=source)
    adapter._build_event = lambda *_args, **_kwargs: event

    response = asyncio.run(adapter._handle_callback(Request()))

    assert response.status == 200
    assert response.text == "success"
    assert len(claimed) == 1
    assert claimed[0]["message_id"] == "msg-1"
    queued_event, receipt_key, session_id = adapter._message_queue.get_nowait()
    assert queued_event is event
    assert receipt_key == "receipt-1"
    assert session_id == "user-1"


def test_claimed_callback_is_immediately_recoverable_by_new_process(tmp_path):
    from plugins.platforms.wecom.inbound_receipts import WecomInboundReceiptStore

    path = tmp_path / "wecom_callback_receipts.sqlite3"
    old_process = WecomInboundReceiptStore(path)
    claim = old_process.claim(
        app_name="default",
        message_id="msg-drain-1",
        user_id="user-1",
        session_id="user-1",
        payload={"app_name": "default", "xml_text": "<decrypted/>"},
    )
    assert claim["accepted"] is True

    new_process = WecomInboundReceiptStore(path)
    recovered = new_process.recover_pending()

    assert len(recovered) == 1
    assert recovered[0]["message_id"] == "msg-drain-1"
    assert recovered[0]["payload"] == {
        "app_name": "default",
        "xml_text": "<decrypted/>",
    }
    assert recovered[0]["attempt_count"] == 2
