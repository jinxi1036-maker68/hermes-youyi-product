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

## Durable state boundary

Holding Bridge durability is deliberately separate from Gateway
`HERMES_HOME`.

For the first Runtime Topology cutover the Gateway persistent Home still needs
a final migration/finalize after the old Gateway has stopped. That finalize
prunes target-only Gateway Home files. Therefore a live holding SQLite database
or HOLD marker under `HERMES_HOME/state` could be deleted during the exact
window it is protecting.

The production template uses a version-neutral sibling state root:

```text
/var/lib/hermes-youyi/hermes-home     # Gateway persistent Home
/var/lib/hermes-youyi/wecom-holding  # Holding Bridge durable state
```

`XIAOYOU_WECOM_HOLDING_STATE_ROOT` is authoritative when configured. If it is
not configured but `HERMES_HOME` is available, the Bridge derives the sibling
`wecom-holding` directory rather than writing inside Gateway Home.

The Bridge service must have write access only to its dedicated state root.
Runtime Home migration/finalize must never own, copy, prune, or reverse-sync
Bridge backlog/control state.

### Provisioning gate

The service identity must **not** create privileged parent directories under
`/var/lib`. Provisioning is a separate deployment-time root action.

Use the Git-governed gate in read-only mode first:

```text
python scripts/xiaoyou_wecom_holding_state_root.py \
  --state-root /var/lib/hermes-youyi/wecom-holding \
  --gateway-home /var/lib/hermes-youyi/hermes-home
```

If the only blocker is that the dedicated state root is absent or has incorrect
root-directory ownership/mode, the deployment executor may run the same command
with `--apply` as root. The gate:

- creates only the exact state-root directory; it never creates parents;
- requires the state root to be disjoint from Gateway `HERMES_HOME`;
- rejects symlinked parents/state roots;
- sets only the exact root directory owner/group and mode `0700`;
- never recursively chowns/chmods existing Bridge state;
- verifies the service identity can traverse the parent path and read/write the
  dedicated root;
- fails if existing child state has unexpected ownership, access or symlinks.

After provisioning, rerun without `--apply` and require `ok=true` before
starting the Bridge.

### Stage-only candidate boundary

The first cutover has an ordering requirement: the Holding Bridge must be
running before any active Gateway code links are switched.

Therefore the candidate release must first be **staged without activation**:

```text
python scripts/xiaoyou_release_installer.py \
  --release-root <verified-release-root> \
  --base /opt/hermes-youyi-current \
  --stage-only \
  --apply
```

For production packaging, do not rely on the historical CLI defaults for
Hermes version/model metadata. Resolve the live production values read-only and
pass them explicitly when building the exact-main release.

Stage-only:
- verifies the release manifest and file hashes;
- copies the release into the canonical versioned `xiaoyou-releases` tree;
- never changes active runtime/plugin/script links;
- is idempotent when the exact verified release is already present;
- rejects symlinked or invalid existing canonical release paths.

The Bridge systemd template deliberately separates:
- `XIAOYOU_RELEASE_PAYLOAD_ROOT`: exact staged candidate payload;
- `HERMES_PYTHON`: already-verified existing Python interpreter;
- `XIAOYOU_SERVICE_USER/GROUP`: live service identity.

The Bridge entrypoint does **not** import through the global
`plugins.platforms.wecom` package name. It loads the candidate
`holding_bridge.py` directly from the staged payload path and verifies the
loaded module origin. This prevents an older installed Hermes `plugins`
package from shadowing the staged Bridge implementation.

This lets the Bridge run exact candidate code before Gateway activation without
copying scripts into the active production tree or switching current Gateway
code links.

## HOLD control plane

There is no network endpoint that changes HOLD state.

Use `scripts/xiaoyou_wecom_holding_mode.py` under the service identity and
point it at the same dedicated state root as the Bridge:

```text
hold    atomically create the HOLD marker
status  read current state only
resume  remove the marker and allow forwarding/replay
```

For production control, prefer explicit
`--state-root /var/lib/hermes-youyi/wecom-holding` (or the matching
environment setting) so control and service cannot drift onto Gateway Home.

An unreadable or invalid marker fails closed to HOLD.

## Production boundary

This implementation does not modify Nginx or production.

Before any production change:

1. verify the chosen loopback port is unused;
2. verify the dedicated Bridge state root is outside Gateway `HERMES_HOME`;
3. run the state-root provisioning gate read-only, provision with explicit
   root `--apply` only if required, then require read-only verification PASS;
4. build/verify the exact candidate release and stage it with `--stage-only`;
5. prove stage-only did not change current Gateway active links;
6. render/install the Bridge unit using the staged candidate payload plus the
   already-verified existing Python interpreter;
7. start the Bridge without changing public routing and prove local
   FORWARD/HOLD/FALLBACK/restart/duplicate paths;
8. only then change the exact `/wecom/callback` Nginx locations using the
   supported graceful reload;
9. never alter the shared `hermes_hub -> 127.0.0.1:19090` member;
10. retain symmetric callback-only rollback.
