# XiaoU AI Collaboration Command Bridge V1

## Objective

V1 removes the owner from the ChatGPT -> Codex relay for read-only server work.

The control path is:

ChatGPT -> owner-authored GitHub PR command comment -> outbound server poll -> validated command -> read-only Codex execution -> validated XIAOU_OPS_REPORT_V1 -> GitHub PR.

This version deliberately does **not** automate deployment.

## Authority split

- ChatGPT owns architecture, route decisions, source changes, regression design, candidate approval, review, merge/tag/seal.
- Codex is a server verification worker only. It may inspect and verify; it cannot modify source or decide the route.
- The owner is required only for real WeCom/business acceptance and owner-only business decisions.

## Why the server polls GitHub

The repository is public. A GitHub self-hosted runner attached directly to the production host would create an unnecessary inbound execution surface.

The V1 bridge therefore uses an outbound-only poll model:

1. the production-side worker fetches public GitHub PR metadata/comments;
2. it accepts only a machine-formatted command comment authored by the exact repository owner with GitHub `author_association=OWNER`;
3. it verifies the command is bound to the current open PR head;
4. it runs only the V1 read-only action allowlist.

No GitHub workflow can directly execute shell commands on the production host.

## Separate control and production checkouts

The collaboration worker must run from a dedicated **Ops Control checkout**, separate from the XiaoU production checkout.

- Ops Control checkout: contains this bridge and may follow the reviewed collaboration-infrastructure main branch.
- Production checkout: remains pinned to the actual deployed/accepted production SHA until an explicit deployment action is separately approved.

Updating the bridge must never implicitly update production.

## Command protocol

A valid command is one owner-authored PR comment containing:

`<!-- xiaou-ops-command:v1 -->`

followed by a fenced JSON object.

Required fields:

- `protocol = XIAOU_OPS_COMMAND_V1`
- `command_id`: immutable unique operation identifier
- `pr_number`
- `candidate_sha`: exact current PR head
- `action`: only `READ_ONLY_INSPECTION` or `VERIFY` in V1
- `allow_code_change = false`
- `allow_production_change = false`
- `goal`
- `evidence_requirements`
- `issued_at`
- `expires_at`

The command lifetime may not exceed 24 hours.

## Fail-closed checks

The worker refuses execution when:

- the GitHub author is not the exact configured repository owner;
- `author_association` is not `OWNER`;
- the target PR is closed;
- the candidate SHA differs from the current PR head;
- the command is expired or valid for more than 24 hours;
- the action is outside the V1 allowlist;
- code or production mutation is requested;
- sensitive material appears in the command;
- the local command state is corrupt;
- another worker instance already holds the execution lock.

## Codex isolation

The worker starts Codex using:

- `sandbox_mode="read-only"`;
- `approval_policy="never"`;
- a structured output schema;
- an isolated candidate clone at the exact candidate SHA.

Codex does not receive the GitHub Actions dispatch credential. The subprocess environment is explicitly reduced and excludes arbitrary production environment variables.

Codex must return `PASS`, `FAIL`, or `BLOCKED` plus factual evidence and anomalies.

## Report return

The deterministic worker, not Codex, constructs `XIAOU_OPS_REPORT_V1`.

The worker dispatches the existing `xiaoyou-ops-report-intake.yml` workflow using a separate fine-grained GitHub credential restricted to this repository and Actions dispatch.

The command is acknowledged only after the worker observes the validated report comment on the PR. A dispatch that fails intake therefore remains retryable.

## Server credential boundary

The server-side GitHub credential must:

- be restricted to this repository;
- allow only the minimum permission needed to dispatch Actions;
- have no Contents write;
- have no branch/tag/merge/release write;
- never be passed into Codex;
- live outside the repository and logs.

Reading the public PR command queue does not require a credential, but production should use a separate read-only `XIAOU_GITHUB_READ_TOKEN` to avoid unauthenticated API rate limits. This read token must also be withheld from Codex.

## Scheduling

The worker should run as a dedicated unprivileged service account from a systemd timer at approximately one-minute cadence.

The service account must not own the production source checkout and must not have Git push credentials.

V1 requires read access to the production checkout and the ability to run the already-installed Codex CLI. Set `CODEX_HOME` to a worker-owned path under the writable state directory when Codex needs writable session/cache state. No service restart/deployment privilege is granted.

## V1 acceptance

V1 passes only after all of the following occur without owner relay:

1. ChatGPT posts a valid command to a test PR.
2. The server timer discovers the command automatically.
3. Codex runs in read-only mode.
4. The server dispatches a valid report.
5. GitHub validates and posts the report to the same PR.
6. ChatGPT can read the report directly.
7. No code or production state changed.

Deployment automation is a separate later stage.
