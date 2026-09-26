from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx

from plugins.platforms.wecom import holding_bridge as hb


def _raw_path(tag="1"):
    return (
        f"/wecom/callback?msg_signature=sig-{tag}"
        f"&timestamp=100&nonce=n-{tag}"
    )


def _body(tag="1"):
    return f"<xml><Encrypt>{tag}</Encrypt></xml>".encode()


def _headers():
    return {
        "Content-Type": "text/xml",
        "Authorization": "must-not-forward",
    }


def _run(coro):
    return asyncio.run(coro)


def _bridge(
    tmp_path,
    handler,
    *,
    hold=False,
    db_name="holding.sqlite3",
):
    mode = tmp_path / "holding-mode.json"
    if hold:
        mode.write_text(
            json.dumps(
                {
                    "schema_version": hb.MODE_SCHEMA_VERSION,
                    "state": hb.HOLD_STATE,
                }
            ),
            encoding="utf-8",
        )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=1.0,
    )
    bridge = hb.WecomHoldingBridge(
        upstream_base="http://127.0.0.1:19090",
        store=hb.HoldingStore(tmp_path / db_name),
        mode_gate=hb.HoldingModeGate(mode),
        http_client=client,
        replay_interval_seconds=999,
    )
    return bridge, client, mode


def test_transport_fingerprint_is_stable_across_query_order():
    body = _body()
    a = hb.transport_fingerprint(
        method="POST",
        raw_path=(
            "/wecom/callback?nonce=n&timestamp=1"
            "&msg_signature=s"
        ),
        body=body,
    )
    b = hb.transport_fingerprint(
        method="post",
        raw_path=(
            "/wecom/callback?msg_signature=s&nonce=n"
            "&timestamp=1"
        ),
        body=body,
    )
    assert a == b


def test_get_verification_is_transparent_and_never_queued(tmp_path):
    seen = []

    def handler(request):
        seen.append(
            (
                request.method,
                request.url.raw_path,
                request.content,
            )
        )
        return httpx.Response(
            200,
            text="verified",
            headers={"content-type": "text/plain"},
        )

    bridge, client, _ = _bridge(
        tmp_path,
        handler,
        hold=True,
    )

    async def scenario():
        await bridge.start()
        try:
            result = await bridge.handle_get(
                raw_path=(
                    "/wecom/callback?echostr=x&msg_signature=s"
                    "&timestamp=1&nonce=n"
                ),
                headers={},
            )
        finally:
            await bridge.close()
            await client.aclose()
        return result

    result = _run(scenario())
    assert result.route == "FORWARD"
    assert result.status == 200
    assert result.body == b"verified"
    assert bridge.store.counts() == {
        "pending": 0,
        "completed": 0,
    }
    assert seen[0][0] == "GET"


def test_forward_success_is_transparent_after_durable_stage(tmp_path):
    seen_headers = {}

    def handler(request):
        seen_headers.update(request.headers)
        return httpx.Response(
            202,
            content=b"success",
            headers={"content-type": "text/plain"},
        )

    bridge, client, _ = _bridge(
        tmp_path,
        handler,
    )

    async def scenario():
        await bridge.start()
        try:
            return await bridge.handle_post(
                raw_path=_raw_path(),
                headers=_headers(),
                body=_body(),
            )
        finally:
            await bridge.close()
            await client.aclose()

    result = _run(scenario())
    assert result.route == "FORWARD"
    assert result.status == 202
    assert result.body == b"success"
    assert bridge.store.counts() == {
        "pending": 0,
        "completed": 1,
    }
    assert "authorization" not in seen_headers
    assert seen_headers.get("content-type") == "text/xml"


def test_completed_tombstone_does_not_retain_transport_payload(tmp_path):
    def handler(_request):
        return httpx.Response(200, text="success")

    bridge, client, _ = _bridge(
        tmp_path,
        handler,
    )

    async def scenario():
        await bridge.start()
        try:
            await bridge.handle_post(
                raw_path=_raw_path(),
                headers=_headers(),
                body=_body(),
            )
        finally:
            await bridge.close()
            await client.aclose()

    _run(scenario())
    with bridge.store._connect() as connection:
        row = connection.execute(
            "SELECT state, raw_path, headers_json, body "
            "FROM held_callbacks"
        ).fetchone()

    assert row["state"] == "completed"
    assert row["raw_path"] == ""
    assert row["headers_json"] == "{}"
    assert bytes(row["body"]) == b""


def test_hold_persists_before_ack_and_duplicate_is_not_forwarded(
    tmp_path,
):
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="upstream")

    bridge, client, _ = _bridge(
        tmp_path,
        handler,
        hold=True,
    )

    async def scenario():
        await bridge.start()
        try:
            first = await bridge.handle_post(
                raw_path=_raw_path(),
                headers=_headers(),
                body=_body(),
            )
            second = await bridge.handle_post(
                raw_path=_raw_path(),
                headers=_headers(),
                body=_body(),
            )
            return first, second
        finally:
            await bridge.close()
            await client.aclose()

    first, second = _run(scenario())
    assert first.route == "HOLD"
    assert first.status == 200
    assert first.body == b"success"
    assert second.route == "DUPLICATE"
    assert second.status == 200
    assert bridge.store.counts() == {
        "pending": 1,
        "completed": 0,
    }
    assert calls == 0


def test_transport_failure_uses_already_durable_row_then_acks(
    tmp_path,
):
    def handler(request):
        raise httpx.ConnectError(
            "down",
            request=request,
        )

    bridge, client, _ = _bridge(
        tmp_path,
        handler,
    )

    async def scenario():
        await bridge.start()
        try:
            return await bridge.handle_post(
                raw_path=_raw_path(),
                headers=_headers(),
                body=_body(),
            )
        finally:
            await bridge.close()
            await client.aclose()

    result = _run(scenario())
    assert result.route == "FALLBACK"
    assert result.status == 200
    assert result.body == b"success"
    assert bridge.store.counts()["pending"] == 1


def test_5xx_falls_back_but_4xx_remains_transparent(tmp_path):
    statuses = iter([503, 403])

    def handler(_request):
        return httpx.Response(
            next(statuses),
            text="upstream",
        )

    bridge, client, _ = _bridge(
        tmp_path,
        handler,
    )

    async def scenario():
        await bridge.start()
        try:
            a = await bridge.handle_post(
                raw_path=_raw_path("a"),
                headers=_headers(),
                body=_body("a"),
            )
            b = await bridge.handle_post(
                raw_path=_raw_path("b"),
                headers=_headers(),
                body=_body("b"),
            )
            return a, b
        finally:
            await bridge.close()
            await client.aclose()

    a, b = _run(scenario())
    assert a.route == "FALLBACK"
    assert a.status == 200
    assert b.route == "FORWARD"
    assert b.status == 403
    assert bridge.store.counts() == {
        "pending": 2,
        "completed": 0,
    }


def test_store_failure_never_forwards_or_fakes_success_ack(tmp_path):
    calls = 0

    class BrokenStore:
        def persist_pending(self, _held):
            raise sqlite3.OperationalError("disk full")

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="success")

    mode = tmp_path / "mode.json"
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=1,
    )
    bridge = hb.WecomHoldingBridge(
        upstream_base="http://127.0.0.1:19090",
        store=BrokenStore(),
        mode_gate=hb.HoldingModeGate(mode),
        http_client=client,
    )

    async def scenario():
        await bridge.start()
        try:
            return await bridge.handle_post(
                raw_path=_raw_path(),
                headers=_headers(),
                body=_body(),
            )
        finally:
            await bridge.close()
            await client.aclose()

    result = _run(scenario())
    assert result.route == "PERSIST_FAILED"
    assert result.status == 503
    assert result.body != b"success"
    assert calls == 0


def test_process_failure_after_stage_remains_recoverable(tmp_path):
    db = tmp_path / "shared.sqlite3"
    mode = tmp_path / "mode.json"

    def interrupted(_request):
        raise RuntimeError("simulated process interruption")

    first_client = httpx.AsyncClient(
        transport=httpx.MockTransport(interrupted),
        timeout=1,
    )
    first = hb.WecomHoldingBridge(
        upstream_base="http://127.0.0.1:19090",
        store=hb.HoldingStore(db),
        mode_gate=hb.HoldingModeGate(mode),
        http_client=first_client,
    )

    async def interrupted_ingress():
        try:
            await first.handle_post(
                raw_path=_raw_path(),
                headers=_headers(),
                body=_body(),
            )
        finally:
            await first_client.aclose()

    try:
        _run(interrupted_ingress())
    except RuntimeError as exc:
        assert str(exc) == "simulated process interruption"
    else:
        raise AssertionError("simulated process failure did not occur")

    assert hb.HoldingStore(db).counts() == {
        "pending": 1,
        "completed": 0,
    }

    calls = 0

    def recovered(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="success")

    second_client = httpx.AsyncClient(
        transport=httpx.MockTransport(recovered),
        timeout=1,
    )
    second = hb.WecomHoldingBridge(
        upstream_base="http://127.0.0.1:19090",
        store=hb.HoldingStore(db),
        mode_gate=hb.HoldingModeGate(mode),
        http_client=second_client,
    )

    async def replay():
        result = await second.replay_once()
        await second_client.aclose()
        return result

    outcome = _run(replay())
    assert outcome == {
        "attempted": 1,
        "completed": 1,
        "failed": 0,
    }
    assert calls == 1


def test_crash_recovery_replay_is_idempotent_at_transport_layer(
    tmp_path,
):
    def unavailable(request):
        raise httpx.ConnectError(
            "down",
            request=request,
        )

    first_client = httpx.AsyncClient(
        transport=httpx.MockTransport(unavailable),
        timeout=1,
    )
    db = tmp_path / "shared.sqlite3"
    mode = tmp_path / "mode.json"
    first = hb.WecomHoldingBridge(
        upstream_base="http://127.0.0.1:19090",
        store=hb.HoldingStore(db),
        mode_gate=hb.HoldingModeGate(mode),
        http_client=first_client,
    )

    async def ingress():
        await first.start()
        try:
            return await first.handle_post(
                raw_path=_raw_path(),
                headers=_headers(),
                body=_body(),
            )
        finally:
            await first.close()
            await first_client.aclose()

    result = _run(ingress())
    assert result.route == "FALLBACK"
    assert hb.HoldingStore(db).counts()["pending"] == 1

    calls = 0

    def recovered_handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="success")

    second_client = httpx.AsyncClient(
        transport=httpx.MockTransport(recovered_handler),
        timeout=1,
    )
    second = hb.WecomHoldingBridge(
        upstream_base="http://127.0.0.1:19090",
        store=hb.HoldingStore(db),
        mode_gate=hb.HoldingModeGate(mode),
        http_client=second_client,
    )

    async def replay():
        outcome = await second.replay_once()
        repeated = await second.replay_once()
        duplicate = await second.handle_post(
            raw_path=_raw_path(),
            headers=_headers(),
            body=_body(),
        )
        await second_client.aclose()
        return outcome, repeated, duplicate

    outcome, repeated, duplicate = _run(replay())
    assert outcome == {
        "attempted": 1,
        "completed": 1,
        "failed": 0,
    }
    assert repeated == {
        "attempted": 0,
        "completed": 0,
        "failed": 0,
    }
    assert second.store.counts() == {
        "pending": 0,
        "completed": 1,
    }
    assert duplicate.route == "DUPLICATE"
    assert calls == 1


def test_replay_failure_stays_pending(tmp_path):
    store = hb.HoldingStore(
        tmp_path / "db.sqlite3"
    )
    held = hb.make_held_request(
        method="POST",
        raw_path=_raw_path(),
        headers=_headers(),
        body=_body(),
    )
    staged = store.persist_pending(
        held,
        now=1,
    )
    assert staged["created"] is True

    with store._connect() as connection:
        connection.execute(
            "UPDATE held_callbacks SET next_attempt_at = 0"
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                500,
                text="bad",
            )
        ),
        timeout=1,
    )
    bridge = hb.WecomHoldingBridge(
        upstream_base="http://127.0.0.1:19090",
        store=store,
        mode_gate=hb.HoldingModeGate(
            tmp_path / "mode"
        ),
        http_client=client,
    )

    async def scenario():
        result = await bridge.replay_once()
        await client.aclose()
        return result

    outcome = _run(scenario())
    assert outcome == {
        "attempted": 1,
        "completed": 0,
        "failed": 1,
    }
    assert store.counts()["pending"] == 1


def test_gateway_processed_but_response_lost_keeps_business_effect_once(
    tmp_path,
):
    from plugins.platforms.wecom.inbound_receipts import (
        WecomInboundReceiptStore,
    )

    gateway_receipts = WecomInboundReceiptStore(
        tmp_path / "gateway-receipts.sqlite3"
    )
    business_runs = 0
    transport_calls = 0

    def handler(request):
        nonlocal business_runs, transport_calls
        transport_calls += 1
        claim = gateway_receipts.claim(
            app_name="default",
            message_id="business-msg-1",
        )
        if claim["accepted"]:
            business_runs += 1
        if transport_calls == 1:
            raise httpx.ReadTimeout(
                "response lost",
                request=request,
            )
        return httpx.Response(
            200,
            text="success",
        )

    bridge, client, _ = _bridge(
        tmp_path,
        handler,
    )

    async def scenario():
        await bridge.start()
        try:
            first = await bridge.handle_post(
                raw_path=_raw_path(),
                headers=_headers(),
                body=_body(),
            )
            with bridge.store._connect() as connection:
                connection.execute(
                    "UPDATE held_callbacks SET next_attempt_at = 0 "
                    "WHERE state = 'pending'"
                )
            replay = await bridge.replay_once()
            return first, replay
        finally:
            await bridge.close()
            await client.aclose()

    first, replay = _run(scenario())
    assert first.route == "FALLBACK"
    assert replay["completed"] == 1
    assert transport_calls == 2
    assert business_runs == 1


def test_backlog_survives_hold_restart_and_switch_to_forward(
    tmp_path,
):
    calls = []

    def handler(request):
        calls.append(request.url.raw_path)
        return httpx.Response(
            200,
            text="success",
        )

    db = tmp_path / "backlog.sqlite3"
    mode = tmp_path / "mode.json"
    mode.write_text(
        json.dumps(
            {
                "schema_version": hb.MODE_SCHEMA_VERSION,
                "state": hb.HOLD_STATE,
            }
        ),
        encoding="utf-8",
    )

    client1 = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=1,
    )
    first = hb.WecomHoldingBridge(
        upstream_base="http://127.0.0.1:19090",
        store=hb.HoldingStore(db),
        mode_gate=hb.HoldingModeGate(mode),
        http_client=client1,
    )

    async def intake():
        await first.start()
        try:
            for tag in ("1", "2", "3"):
                result = await first.handle_post(
                    raw_path=_raw_path(tag),
                    headers=_headers(),
                    body=_body(tag),
                )
                assert result.route == "HOLD"
        finally:
            await first.close()
            await client1.aclose()

    _run(intake())
    assert hb.HoldingStore(db).counts()["pending"] == 3
    assert calls == []

    mode.unlink()
    client2 = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=1,
    )
    second = hb.WecomHoldingBridge(
        upstream_base="http://127.0.0.1:19090",
        store=hb.HoldingStore(db),
        mode_gate=hb.HoldingModeGate(mode),
        http_client=client2,
    )

    async def replay_all():
        result = await second.replay_once()
        await client2.aclose()
        return result

    outcome = _run(replay_all())
    assert outcome == {
        "attempted": 3,
        "completed": 3,
        "failed": 0,
    }
    assert second.store.counts() == {
        "pending": 0,
        "completed": 3,
    }
    assert len(calls) == 3


def test_corrupt_mode_file_fails_closed_to_hold(tmp_path):
    mode = tmp_path / "mode.json"
    mode.write_text(
        "{broken",
        encoding="utf-8",
    )
    snapshot = hb.HoldingModeGate(mode).snapshot()
    assert snapshot["mode"] == "HOLD"
    assert snapshot["active"] is True
    assert snapshot["valid"] is False


def test_missing_wecom_transport_fields_are_rejected_without_ack(
    tmp_path,
):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text="success",
            )
        ),
        timeout=1,
    )
    bridge = hb.WecomHoldingBridge(
        upstream_base="http://127.0.0.1:19090",
        store=hb.HoldingStore(
            tmp_path / "db.sqlite3"
        ),
        mode_gate=hb.HoldingModeGate(
            tmp_path / "mode"
        ),
        http_client=client,
    )

    async def scenario():
        result = await bridge.handle_post(
            raw_path="/wecom/callback?timestamp=1",
            headers=_headers(),
            body=_body(),
        )
        await client.aclose()
        return result

    result = _run(scenario())
    assert result.route == "REJECT"
    assert result.status == 400
    assert result.body != b"success"
    assert bridge.store.counts() == {
        "pending": 0,
        "completed": 0,
    }
