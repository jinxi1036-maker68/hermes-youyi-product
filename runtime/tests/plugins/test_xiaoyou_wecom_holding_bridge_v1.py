from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from scripts import xiaoyou_wecom_holding_bridge as holding


TOKEN = "transport-test-token"
ENCRYPTED = "encrypted-callback-payload"


def _signed_callback(
    *,
    token: str = TOKEN,
    encrypted: str = ENCRYPTED,
    timestamp: str = "1720000000",
    nonce: str = "nonce-1",
):
    payload = "".join(sorted((token, timestamp, nonce, encrypted)))
    signature = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    body = f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>".encode()
    query = {
        "msg_signature": signature,
        "timestamp": timestamp,
        "nonce": nonce,
    }
    return body, query


class FakeRelURL:
    def __init__(self, path_qs: str):
        self.raw_path_qs = path_qs


class FakeRequest:
    def __init__(
        self,
        *,
        method: str = "POST",
        body: bytes = b"",
        query: dict[str, str] | None = None,
        path_qs: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.method = method
        self._body = body
        self.query = query or {}
        if path_qs is None:
            if self.query:
                encoded = "&".join(f"{k}={v}" for k, v in self.query.items())
                path_qs = f"/wecom/callback?{encoded}"
            else:
                path_qs = "/wecom/callback"
        self.rel_url = FakeRelURL(path_qs)
        self.path = "/wecom/callback"
        self.query_string = path_qs.split("?", 1)[1] if "?" in path_qs else ""
        self.headers = headers or {"Content-Type": "application/xml"}

    async def read(self):
        return self._body


class FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"success", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {"Content-Type": "text/plain"}


class FakeClient:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.closed = False

    async def request(self, method, url, headers=None, content=b""):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "content": bytes(content),
            }
        )
        if not self.results:
            raise AssertionError("unexpected forward")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def aclose(self):
        self.closed = True


def _bridge(tmp_path: Path, *, client: FakeClient, hold: bool = False, tokens=None):
    state = tmp_path / "state"
    store = holding.HoldingStore(state / "queue.sqlite3")
    mode_file = state / "hold.json"
    if hold:
        holding._atomic_hold(mode_file, reason="test")
    bridge = holding.WeComHoldingBridge(
        callback_path="/wecom/callback",
        upstream_origin="http://127.0.0.1:8866",
        store=store,
        mode_gate=holding.HoldingModeGate(mode_file),
        tokens=list(tokens if tokens is not None else [TOKEN]),
        client=client,
        replay_poll_seconds=0.01,
        replay_max_backoff_seconds=1,
        retention_seconds=3600,
    )
    return bridge, store, mode_file


def _response_text(response) -> str:
    return bytes(response.body or b"").decode("utf-8", errors="replace")


def test_signature_verification_and_transport_key_are_stable():
    body, query = _signed_callback()

    ok, error = holding._verify_wecom_signature(
        body=body,
        query=query,
        tokens=[TOKEN],
    )

    assert ok is True
    assert error == ""
    assert holding._transport_key(body) == holding._transport_key(body)
    assert holding._transport_key(body).startswith("enc-sha256:")


def test_invalid_signature_is_rejected_when_holding(tmp_path):
    body, query = _signed_callback()
    query["msg_signature"] = "bad"
    client = FakeClient([])
    bridge, store, _mode = _bridge(tmp_path, client=client, hold=True)

    response = asyncio.run(
        bridge.handle_callback(FakeRequest(body=body, query=query))
    )

    assert response.status == 400
    assert store.counts()["total_count"] == 0
    assert client.calls == []


def test_normal_gateway_path_is_transparent_and_does_not_spool(tmp_path):
    body, query = _signed_callback()
    client = FakeClient([FakeResponse(200, b"gateway-success")])
    bridge, store, _mode = _bridge(tmp_path, client=client)

    response = asyncio.run(
        bridge.handle_callback(FakeRequest(body=body, query=query))
    )

    assert response.status == 200
    assert _response_text(response) == "gateway-success"
    assert store.counts()["total_count"] == 0
    assert len(client.calls) == 1
    assert client.calls[0]["url"].startswith("http://127.0.0.1:8866/wecom/callback?")


def test_get_verification_is_forward_only_and_never_spooled(tmp_path):
    client = FakeClient([FakeResponse(200, b"verified")])
    bridge, store, _mode = _bridge(tmp_path, client=client, hold=True)

    response = asyncio.run(
        bridge.handle_verify(
            FakeRequest(
                method="GET",
                query={"msg_signature": "x", "timestamp": "1", "nonce": "n", "echostr": "e"},
                path_qs="/wecom/callback?msg_signature=x&timestamp=1&nonce=n&echostr=e",
            )
        )
    )

    assert response.status == 200
    assert _response_text(response) == "verified"
    assert store.counts()["total_count"] == 0
    assert len(client.calls) == 1


def test_hold_persists_before_ack_and_never_forwards(tmp_path):
    body, query = _signed_callback()
    client = FakeClient([])
    bridge, store, _mode = _bridge(tmp_path, client=client, hold=True)

    response = asyncio.run(
        bridge.handle_callback(FakeRequest(body=body, query=query))
    )

    assert response.status == 200
    assert _response_text(response) == "success"
    assert store.counts() == {
        "total_count": 1,
        "pending_count": 1,
        "delivering_count": 0,
        "completed_count": 0,
    }
    assert client.calls == []


def test_missing_transport_token_cannot_fake_ack_in_hold(tmp_path):
    body, query = _signed_callback()
    client = FakeClient([])
    bridge, store, _mode = _bridge(tmp_path, client=client, hold=True, tokens=[])

    response = asyncio.run(
        bridge.handle_callback(FakeRequest(body=body, query=query))
    )

    assert response.status == 503
    assert _response_text(response) != "success"
    assert store.counts()["total_count"] == 0


def test_transport_failure_falls_back_to_durable_spool_then_acks(tmp_path):
    body, query = _signed_callback()
    client = FakeClient([RuntimeError("connection refused")])
    bridge, store, _mode = _bridge(tmp_path, client=client)

    response = asyncio.run(
        bridge.handle_callback(FakeRequest(body=body, query=query))
    )

    assert response.status == 200
    assert _response_text(response) == "success"
    assert store.counts()["pending_count"] == 1
    assert len(client.calls) == 1


def test_retryable_gateway_http_status_falls_back_to_spool(tmp_path):
    body, query = _signed_callback()
    client = FakeClient([FakeResponse(503, b"unavailable")])
    bridge, store, _mode = _bridge(tmp_path, client=client)

    response = asyncio.run(
        bridge.handle_callback(FakeRequest(body=body, query=query))
    )

    assert response.status == 200
    assert store.counts()["pending_count"] == 1


def test_non_retryable_gateway_response_is_returned_not_spooled(tmp_path):
    body, query = _signed_callback()
    client = FakeClient([FakeResponse(400, b"invalid callback")])
    bridge, store, _mode = _bridge(tmp_path, client=client)

    response = asyncio.run(
        bridge.handle_callback(FakeRequest(body=body, query=query))
    )

    assert response.status == 400
    assert _response_text(response) == "invalid callback"
    assert store.counts()["total_count"] == 0


def test_persist_failure_never_returns_success_ack(tmp_path):
    body, query = _signed_callback()

    class BrokenStore:
        def lookup(self, _key):
            return None

        def persist(self, **_kwargs):
            raise sqlite3.OperationalError("disk full")

        def counts(self):
            return {
                "total_count": 0,
                "pending_count": 0,
                "delivering_count": 0,
                "completed_count": 0,
            }

    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    mode_file = state / "hold.json"
    holding._atomic_hold(mode_file, reason="test")
    bridge = holding.WeComHoldingBridge(
        callback_path="/wecom/callback",
        upstream_origin="http://127.0.0.1:8866",
        store=BrokenStore(),
        mode_gate=holding.HoldingModeGate(mode_file),
        tokens=[TOKEN],
        client=FakeClient([]),
    )

    response = asyncio.run(
        bridge.handle_callback(FakeRequest(body=body, query=query))
    )

    assert response.status == 503
    assert _response_text(response) != "success"


def test_exact_transport_duplicate_has_one_durable_row(tmp_path):
    body, query = _signed_callback()
    client = FakeClient([])
    bridge, store, _mode = _bridge(tmp_path, client=client, hold=True)
    request = FakeRequest(body=body, query=query)

    first = asyncio.run(bridge.handle_callback(request))
    second = asyncio.run(bridge.handle_callback(request))

    assert first.status == 200
    assert second.status == 200
    assert store.counts()["total_count"] == 1


def test_existing_durable_row_is_acked_without_direct_delivery(tmp_path):
    body, query = _signed_callback()
    client = FakeClient([])
    bridge, store, mode_file = _bridge(tmp_path, client=client, hold=True)
    request = FakeRequest(body=body, query=query)
    asyncio.run(bridge.handle_callback(request))
    mode_file.unlink()

    response = asyncio.run(bridge.handle_callback(request))

    assert response.status == 200
    assert client.calls == []
    assert store.counts()["total_count"] == 1


def test_replay_marks_completed_only_after_downstream_2xx(tmp_path):
    body, query = _signed_callback()
    client = FakeClient([FakeResponse(200)])
    bridge, store, _mode = _bridge(tmp_path, client=client, hold=True)
    asyncio.run(bridge.handle_callback(FakeRequest(body=body, query=query)))

    row = store.claim_next(now_epoch=10**20)
    assert row is not None
    asyncio.run(bridge._deliver_held_row(row))

    counts = store.counts()
    assert counts["completed_count"] == 1
    assert counts["pending_count"] == 0
    assert counts["delivering_count"] == 0


def test_replay_failure_remains_pending(tmp_path):
    body, query = _signed_callback()
    client = FakeClient([FakeResponse(503)])
    bridge, store, _mode = _bridge(tmp_path, client=client, hold=True)
    asyncio.run(bridge.handle_callback(FakeRequest(body=body, query=query)))

    row = store.claim_next(now_epoch=10**20)
    assert row is not None
    asyncio.run(bridge._deliver_held_row(row))

    counts = store.counts()
    assert counts["completed_count"] == 0
    assert counts["pending_count"] == 1


def test_bridge_restart_recovers_delivering_row_to_pending(tmp_path):
    state = tmp_path / "state"
    db = state / "queue.sqlite3"
    store = holding.HoldingStore(db)
    body, _query = _signed_callback()
    store.persist(
        transport_key=holding._transport_key(body),
        method="POST",
        path_qs="/wecom/callback?x=1",
        content_type="application/xml",
        body=body,
    )
    assert store.claim_next(now_epoch=10**20)["status"] == "delivering"
    assert store.counts()["delivering_count"] == 1

    recovered = holding.HoldingStore(db)

    assert recovered.counts()["delivering_count"] == 0
    assert recovered.counts()["pending_count"] == 1


def test_corrupt_mode_file_fails_closed_into_hold(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    mode = state / "hold.json"
    mode.write_text("{broken", encoding="utf-8")

    snapshot = holding.HoldingModeGate(mode).snapshot()

    assert snapshot["mode"] == holding.MODE_HOLD
    assert snapshot["active"] is True
    assert snapshot["valid"] is False


def test_mode_state_and_database_are_private(tmp_path):
    state = tmp_path / "state"
    store = holding.HoldingStore(state / "queue.sqlite3")
    mode = state / "hold.json"
    holding._atomic_hold(mode, reason="test")

    assert (state.stat().st_mode & 0o777) == 0o700
    assert (store.path.stat().st_mode & 0o777) == 0o600
    assert (mode.stat().st_mode & 0o777) == 0o600


def test_forwarding_inflight_counter_returns_to_zero_on_failure(tmp_path):
    body, query = _signed_callback()
    client = FakeClient([RuntimeError("boom")])
    bridge, _store, _mode = _bridge(tmp_path, client=client)

    asyncio.run(
        bridge.handle_callback(FakeRequest(body=body, query=query))
    )

    assert bridge._inflight_forwards == 0


def test_gateway_msgid_receipt_rejects_business_duplicate(tmp_path):
    from plugins.platforms.wecom.inbound_receipts import WecomInboundReceiptStore

    store = WecomInboundReceiptStore(tmp_path / "gateway-receipts.sqlite3")

    first = store.claim(
        app_name="default",
        message_id="business-msg-1",
        user_id="user-1",
        session_id="user-1",
        payload={"app_name": "default", "xml_text": "<xml/>"},
    )
    second = store.claim(
        app_name="default",
        message_id="business-msg-1",
        user_id="user-1",
        session_id="user-1",
        payload={"app_name": "default", "xml_text": "<xml/>"},
    )

    assert first["accepted"] is True
    assert second["accepted"] is False


def test_response_lost_then_replay_can_rely_on_gateway_business_dedupe(tmp_path):
    """Transport replay is at-least-once; business execution remains once.

    Simulate a Gateway that executes MsgId once but the first bridge call loses
    its HTTP response. The replay sees the same MsgId and returns success
    without a second business execution.
    """

    body, query = _signed_callback()
    business_seen: set[str] = set()
    business_exec_count = 0

    class DedupeClient:
        def __init__(self):
            self.calls = 0

        async def request(self, _method, _url, headers=None, content=b""):
            nonlocal business_exec_count
            self.calls += 1
            message_id = "business-msg-1"
            if message_id not in business_seen:
                business_seen.add(message_id)
                business_exec_count += 1
            if self.calls == 1:
                raise RuntimeError("response lost after gateway processed")
            return FakeResponse(200, b"success")

    state = tmp_path / "state"
    store = holding.HoldingStore(state / "queue.sqlite3")
    bridge = holding.WeComHoldingBridge(
        callback_path="/wecom/callback",
        upstream_origin="http://127.0.0.1:8866",
        store=store,
        mode_gate=holding.HoldingModeGate(state / "hold.json"),
        tokens=[TOKEN],
        client=DedupeClient(),
    )

    response = asyncio.run(
        bridge.handle_callback(FakeRequest(body=body, query=query))
    )
    assert response.status == 200
    assert store.counts()["pending_count"] == 1

    row = store.claim_next(now_epoch=10**20)
    asyncio.run(bridge._deliver_held_row(row))

    assert business_exec_count == 1
    assert store.counts()["completed_count"] == 1
