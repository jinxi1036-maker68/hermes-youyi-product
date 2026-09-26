# WeCom Bootstrap Holding Bridge V1

## Purpose

Solve the one-time bootstrap paradox before the legacy production Gateway has
steady-state safe-drain support.

The bridge is transport infrastructure only. It is not a business router, does
not decrypt business content, and does not make XiaoYou decisions.

## Frozen contract

### FORWARD

When Gateway callback service is healthy:

```text
aa-nginx exact /wecom/callback
  -> Holding Bridge loopback
  -> Gateway callback 127.0.0.1:8866
```

The response status/body from Gateway is relayed to WeCom.

The existing shared Cloud Hub at 19090 continues to serve its other Nginx
routes such as /health and /api/v1. The bridge does not modify the shared
`hermes_hub` upstream.

### HOLD

When maintenance HOLD is active:

1. validate the WeCom transport signature;
2. persist the encrypted callback and original callback URI/query in SQLite;
3. SQLite commit must succeed with FULL synchronous durability;
4. only after commit may the bridge return the plain `success` ACK;
5. do not forward new POST callbacks to Gateway.

GET callback verification is never spooled; it still requires live Gateway
crypto and returns 503 if Gateway is unavailable.

### FALLBACK

In FORWARD mode, if the Gateway request:
- raises a transport/timeout exception; or
- returns a retryable status (408/425/429/5xx),

the bridge must authenticate and durably persist the callback before ACK.

A 4xx validation response from Gateway is relayed and is not converted into a
fake success.

### REPLAY

When HOLD is removed:

- pending callbacks are replayed in durable order;
- a row is `completed` only after downstream Gateway returns 2xx;
- non-2xx/transport failures go back to pending with bounded backoff;
- a bridge crash while a row is `delivering` resets it to pending on restart.

## Idempotency model

### Transport layer

The bridge uses SHA-256 of the encrypted `Encrypt` envelope as the transport
fingerprint.

Exact upstream retries of the same encrypted callback therefore share one
durable row.

### Business layer

The bridge intentionally does not decrypt the callback and cannot own the
authoritative WeCom business MsgId.

Business-level duplicate execution is prevented by the existing Gateway
`WecomInboundReceiptStore`, which claims decrypted `message_id` durably and
returns `success` for duplicates without re-entering the model.

This combination provides:

```text
Bridge transport delivery: at least once
Gateway business execution: idempotent by decrypted MsgId
```

The critical "Gateway processed but bridge lost the HTTP response" case is
therefore safe: replay may occur, but Gateway's MsgId receipt prevents duplicate
business/model execution.

## Persistent state

Default root:

`/var/lib/hermes-youyi/ingress-holding`

Files:

- `queue.sqlite3` — encrypted callback spool;
- `hold.json` — operator HOLD state when present.

Required:
- root directory service-owned, direct directory, mode 0700;
- DB/mode file mode 0600;
- not inside a versioned release;
- not inside Hermes Home;
- never reverse-sync into an old release.

## Operator controls

Run as the `hermes-youyi` service identity:

```text
python scripts/xiaoyou_wecom_holding_bridge.py control hold
python scripts/xiaoyou_wecom_holding_bridge.py control wait-held
python scripts/xiaoyou_wecom_holding_bridge.py control forward
python scripts/xiaoyou_wecom_holding_bridge.py control wait-empty
python scripts/xiaoyou_wecom_holding_bridge.py control offline-status
```

`wait-held` is the maintenance fence:

- mode must be HOLD;
- direct/replay in-flight forwards must be zero.

Only after that condition is true is stopping the legacy Gateway eligible.

## First production takeover sequence

Do not execute until isolated fault-matrix verification PASS.

1. Reconfirm current production and Nginx callback locations.
2. Assemble exact verified release candidate.
3. Root narrowly pre-creates the ingress state root, service-owned 0700.
4. Start Holding Bridge in default FORWARD mode on a verified free loopback port.
5. Verify local bridge health/status.
6. Verify callback path can be proxied without changing shared `hermes_hub`.
7. Prepare callback-only Nginx change in BOTH 80 and 443 server blocks.
8. Validate Nginx configuration.
9. Graceful reload Nginx.
10. Verify normal callback traffic remains healthy through Bridge -> 8866.
11. Enter HOLD as service identity.
12. Wait until HOLD + inflight_forwards=0.
13. Only now may the Runtime Topology maintenance window stop legacy Gateway/Agenda.
14. Finalize Runtime Home migration and switch to the verified new release.
15. Start new Gateway and pass all technical gates while Bridge remains HOLD.
16. Remove HOLD (FORWARD).
17. Wait until pending/delivering queue reaches zero.
18. Verify no duplicate business execution and production health.
19. Continue Query technical gate and only then Owner real WeCom acceptance.

## Rollback

Before the new Gateway is stopped/switch fails:

- Bridge remains HOLD so new callbacks stay durably queued.
- Restore old Gateway/runtime selector.
- Verify old Gateway 8866 health.
- Set Bridge FORWARD.
- Wait queue empty.

If the Nginx callback-only switchover itself must be rolled back:

- restore ONLY the two exact callback locations to `proxy_pass http://hermes_hub`;
- validate configuration;
- graceful reload;
- leave shared `hermes_hub` definition untouched.

## Stop rules

Do not:
- ACK before durable commit in HOLD/FALLBACK;
- use an in-memory queue as durability;
- mark held callbacks complete before Gateway 2xx;
- modify the shared hermes_hub upstream;
- route /health or /api/v1 through the bridge;
- patch unmanaged /opt/hermes-cloud-hub/hub.py;
- stop legacy Gateway until Bridge HOLD is active and inflight is zero;
- claim business exactly-once from the bridge transport fingerprint alone.
