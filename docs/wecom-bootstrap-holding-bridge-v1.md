# WeCom Bootstrap Holding Bridge V1

## Purpose

This service is the Git-governed durable ingress boundary for the first
Runtime Topology cutover. It is not a business router and does not decrypt or
interpret WeCom messages.

The intended chain remains:

\`\`\`text
WeCom
 -> aa-nginx exact /wecom/callback location
 -> Holding Bridge
 -> Cloud Hub:127.0.0.1:19090
 -> Gateway:127.0.0.1:8866
\`\`\`

The shared Nginx \`hermes_hub\` upstream is not changed. Only the exact
callback locations may later be pointed at the bridge.

## POST delivery semantics

Every valid callback POST is durably staged in SQLite before any downstream
attempt. This closes the receive-to-persist crash window.

After durable staging:

- **FORWARD**: when HOLD is inactive, the bridge immediately forwards the
  original method/path/query/body to Cloud Hub. A downstream 2xx is returned
  transparently and only then is the transport receipt marked completed.
- **HOLD**: when the offline maintenance marker is active, the bridge does not
  forward. The already-durable callback receives HTTP 200 \`success\`.
- **FALLBACK**: if normal forwarding encounters a transport timeout/error or a
  transient upstream status, the already-durable callback remains pending and
  the bridge returns HTTP 200 \`success\`.
- **PERSIST_FAILED**: if the durable stage cannot commit, the bridge does not
  forward and does not manufacture a success ACK.

GET \`/wecom/callback\` is always transparent and never enters the durable
queue.

SQLite uses WAL plus \`synchronous=FULL\`. Pending rows survive bridge restart.
Replay runs only while HOLD is inactive. A row becomes \`completed\` only after
the downstream chain returns 2xx.

## Duplicate boundary

The bridge computes a transport fingerprint from:

- HTTP method;
- callback path;
- canonicalized query parameters;
- SHA-256 of the raw encrypted body.

This suppresses identical transport retries only. The bridge does not decrypt
the payload and does not implement business message-id semantics.

Business idempotency remains owned by Gateway
\`WecomInboundReceiptStore\`. Therefore the hard case where Gateway durably
accepts a message but the upstream response is lost is safe by composition:
the bridge may replay the transport request, while Gateway's message-id receipt
prevents a second business/model turn.

Completed transport rows retain only the fingerprint/status/timestamps; the
raw path, headers and encrypted body are cleared after confirmed downstream
success.

## HOLD control plane

There is no network endpoint that changes HOLD state.

Use \`scripts/xiaoyou_wecom_holding_mode.py\` under the service identity:

\`\`\`text
hold    atomically create the HOLD marker
status  read current state only
resume  remove the marker and allow forwarding/replay
\`\`\`

An unreadable or invalid marker fails closed to HOLD.

## Production boundary

This implementation does not modify Nginx or production.

Before any production change:

1. verify the chosen loopback port is unused;
2. install and start the Git-governed bridge without changing public routing;
3. prove local FORWARD, HOLD, FALLBACK, restart recovery and duplicate paths;
4. enter HOLD before the old Gateway cutover window;
5. change only the exact \`/wecom/callback\` Nginx locations and use the
   supported graceful reload;
6. never alter the shared \`hermes_hub -> 127.0.0.1:19090\` member;
7. retain symmetric callback-only rollback.
