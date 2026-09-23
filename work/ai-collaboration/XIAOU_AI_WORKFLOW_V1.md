# XiaoU AI Collaboration Pipeline V1

## Purpose

This protocol removes the owner from low-value message relaying while preserving human control over real business acceptance.

The authority split is fixed:

- ChatGPT is the technical lead and code owner: architecture, route, GitHub code changes, regression design, diff review, candidate approval, merge/tag/seal decisions.
- Codex is the server execution worker only: read-only inspection, isolated verification, controlled deployment, health checks, evidence collection and rollback.
- The owner handles real WeCom/business acceptance and owner-only product decisions.

Codex must never modify product source, create repair commits, push code, merge, tag, or choose the project route.

## Source of truth

For each formal change, one GitHub pull request is the engineering workbench and evidence ledger.

The PR records:

1. exact candidate SHA;
2. ChatGPT-issued operation command;
3. Codex server evidence report;
4. ChatGPT review decision;
5. human acceptance only when required;
6. final merge/tag/seal evidence.

Chat messages are not the technical source of truth.

## V1 scope

V1 implements only the return path:

Codex/server -> validated GitHub PR report -> ChatGPT review trigger.

V1 does not automatically start Codex and does not authorize deployment.

## Report protocol

Server reports must use `XIAOU_OPS_REPORT_V1` and be submitted through the repository workflow.

Required fields:

- `protocol`: exactly `XIAOU_OPS_REPORT_V1`
- `command_id`: immutable operation identifier
- `pr_number`: target pull request
- `candidate_sha`: exact 40-character candidate commit
- `action`: `READ_ONLY_INSPECTION`, `VERIFY`, `DEPLOY`, or `ROLLBACK_VERIFY`
- `result`: `PASS`, `FAIL`, or `BLOCKED`
- `code_changed`: must always be `false`
- `production_changed`: whether production runtime state was intentionally changed
- `summary`: factual short summary
- `evidence`: structured evidence object
- `anomalies`: list of anomalies

Optional SHA fields:

- `production_before_sha`
- `production_after_sha`

For `READ_ONLY_INSPECTION` and `VERIFY`, `production_changed` must be `false`.

## Hard boundaries

A report is rejected when:

- `code_changed=true`;
- SHA values are malformed;
- report PR number does not match the workflow target;
- report candidate SHA is not the current PR head;
- a read-only/verify action claims production mutation;
- the protocol or enums are unknown.

The workflow only posts a validated report as a PR comment. It does not execute server commands, merge, deploy, tag, or modify business data.

## Security model

The server-side credential used to dispatch the report workflow should have the minimum repository permission needed to trigger Actions. It must not have Contents write, branch write, merge, tag, or release permissions.

The workflow's GitHub token is scoped only to read repository contents and write the resulting PR/issue comment.

No production secret, token, private key, full environment dump, student data, staff private data, or raw sensitive log should be included in a report.

## Human gate

Automation must stop at `READY_FOR_HUMAN_ACCEPTANCE` whenever real WeCom/business behavior must be judged.

Only the owner decides that acceptance.

## V1 acceptance test

The first end-to-end test is intentionally harmless:

1. ChatGPT creates a pipeline-test PR.
2. Codex performs one read-only server observation with no deployment and no source changes.
3. Codex submits one valid `XIAOU_OPS_REPORT_V1` report for the exact PR head.
4. GitHub validates and posts the report.
5. ChatGPT receives/reviews the PR event without the owner copying the report.

Passing this test proves only the result-return path.
