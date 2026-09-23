# XiaoU AI Collaboration Ops Worker V2

## Decision

V2 uses a fixed pull worker, not a GitHub self-hosted runner.

The repository is public. The production server must not execute arbitrary GitHub Actions workflow code. The server therefore initiates outbound HTTPS reads to GitHub, accepts only narrowly validated XiaoU operation commands, invokes a fixed Codex executor, validates the returned report, and dispatches that report through the existing `XIAOU_OPS_REPORT_V1` intake workflow.

## Fixed authority split

- ChatGPT owns architecture, route decisions, GitHub source, regression design, diff review, candidate approval, merge/tag/seal.
- Codex is a server execution worker only: inspection, verification, later controlled deployment/rollback, and factual evidence return.
- Owner handles only real business acceptance and owner-only product decisions.

Codex never edits product source, creates repair commits, pushes, merges, tags, or decides the project route.

## V2 scope

V2 enables only:

- `READ_ONLY_INSPECTION`
- `VERIFY`

V2 explicitly does **not** enable:

- deployment;
- rollback;
- service restart;
- business data mutation;
- configuration mutation;
- arbitrary shell commands from GitHub.

Deployment/rollback can be added only after this command path is sealed.

## Command path

```text
ChatGPT
  -> trusted PR comment: XIAOU_OPS_COMMAND_V1
  -> public GitHub API (outbound-only pull)
  -> xiaou-ops-worker
  -> command validation + trusted issuer + PR/head binding + replay protection
  -> fixed Codex wrapper
  -> Codex read-only/verify execution
  -> XIAOU_OPS_REPORT_V1 validation
  -> GitHub workflow_dispatch
  -> existing intake workflow
  -> validated PR report comment
  -> ChatGPT review
```

The production server does not accept inbound GitHub webhooks and does not run a GitHub self-hosted runner.

## Trust boundary

A command is executable only when all of the following are true:

1. the comment author login exactly matches the configured trusted issuer;
2. the comment belongs to the configured repository;
3. the target pull request is open;
4. the PR author login matches the trusted issuer;
5. the PR head repository is the configured repository, not a fork;
6. the PR base branch is `main`;
7. the command candidate SHA exactly matches the current PR head SHA;
8. the protocol and all fields validate;
9. the action is on the V2 allowlist;
10. the command is inside its validity window;
11. the command ID and GitHub comment ID have not already been processed.

Unknown fields fail closed.

## Command protocol

`XIAOU_OPS_COMMAND_V1` fields:

- `protocol`: exactly `XIAOU_OPS_COMMAND_V1`
- `command_id`: immutable unique operation identifier
- `pr_number`: positive integer
- `candidate_sha`: exact 40-character lowercase/uppercase hex SHA
- `action`: `READ_ONLY_INSPECTION` or `VERIFY`
- `issued_at`: timezone-aware ISO-8601 timestamp
- `expires_at`: timezone-aware ISO-8601 timestamp, at most 12 hours after issue
- `production_change_authorized`: must be `false`
- `objective`: factual goal, not shell code
- `evidence_requirements`: bounded list of required factual evidence

No shell/script/service/env/credential fields are accepted.

## GitHub read access

The repository is public, so V2 command discovery uses unauthenticated public GitHub API reads by default.

This intentionally means the worker needs no GitHub credential merely to discover commands.

The polling cadence should stay below unauthenticated API limits. Recommended initial cadence: once every 180 seconds.

## GitHub write access

The worker needs only one GitHub write capability: dispatch the existing report-intake workflow.

Use a dedicated fine-grained GitHub credential with the smallest scope that can invoke Actions for this repository. Do not grant Contents write, branch write, merge, tag, release, issue-comment, or administration permissions to the worker credential.

The worker does not post PR comments directly. GitHub Actions posts the validated report using the workflow token.

The GitHub dispatch credential must never be passed into the Codex executor process.

## Unix identities

Production setup should use two separate identities:

### xiaou-ops-worker

Owns:

- polling loop;
- replay/audit state;
- report validation;
- GitHub workflow dispatch credential.

Does not own XiaoU production source or business data.

### xiaou-codex

Runs the fixed Codex executor.

May receive read access necessary for approved inspection/verification, plus a dedicated scratch workspace.

Must not read the worker's GitHub dispatch credential.

V2 grants no sudo/service-control capability to `xiaou-codex`.

## Codex execution

Use a root-owned fixed wrapper path configured into the worker service.

The worker never executes a command string supplied by GitHub and never uses `shell=True`.

The wrapper receives only the already validated JSON command.

The Codex process should run with:

- non-interactive `codex exec`;
- the most restrictive usable sandbox, targeting read-only inspection;
- no `--full-auto` for V2 read-only tasks;
- no access to GitHub dispatch credentials;
- no write access to production source/config/data;
- a bounded timeout;
- a dedicated scratch/Codex home.

The wrapper must return one `XIAOU_OPS_REPORT_V1` JSON object on stdout.

The worker validates that report again before dispatch.

## Codex authentication

Authentication is a server prerequisite, not repository source.

Preferred order:

1. short-lived/workload identity when available;
2. a dedicated Codex access token for automation;
3. a dedicated API/Codex credential supported by the installed CLI.

Do not put Codex credentials in GitHub, repository files, command comments, worker logs, or operation reports.

If persisted CLI auth is used, keep the credential store outside the inspected workspace and deny model-controlled filesystem access to it.

## Replay and idempotency

The worker keeps local SQLite state under its private state directory.

At minimum it records:

- GitHub comment ID;
- command ID;
- PR number;
- candidate SHA;
- received timestamp;
- execution status;
- report result.

A command/comment pair executes at most once.

A failed command is not silently retried with the same command ID. ChatGPT issues a new command ID after reviewing the failure.

## Failure behavior

Fail closed when:

- GitHub cannot be verified;
- the trusted issuer check fails;
- PR/head binding fails;
- the command is expired;
- action is unknown;
- command/report has unknown fields;
- the executor is missing;
- Codex exits non-zero;
- report validation fails;
- report PR/SHA/action does not match the command;
- GitHub report dispatch fails.

Failure never falls back to arbitrary shell or a broader privilege path.

## Secret handling

The worker and report validators reject secret-like report material.

Never return:

- GitHub tokens;
- Codex/OpenAI credentials;
- cookies;
- private keys;
- complete environment dumps;
- student/private staff data;
- raw sensitive logs.

## V2 acceptance sequence

1. Repository unit tests for command validation/replay/trust boundaries pass.
2. Server prerequisite setup is reviewed before installation.
3. Install worker identities, Codex CLI, fixed wrapper, state directory, and minimal secrets.
4. Start worker with only `READ_ONLY_INSPECTION` and `VERIFY`.
5. ChatGPT writes a valid command to a harmless test PR.
6. No owner message is sent to Codex.
7. Worker discovers the command automatically.
8. Codex executes automatically.
9. Validated report returns automatically to the PR.
10. ChatGPT reads and reviews it.

Only after this passes is the second bridge sealed.
