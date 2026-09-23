# XiaoU AI Collaboration Command-Path Discovery V1

## Purpose

Determine the safest minimal mechanism for ChatGPT-issued GitHub commands to reach the XiaoU server-side Codex worker automatically.

This stage is discovery only. It must not install software, change credentials, register runners, modify systemd, change firewall/network settings, deploy code, restart services, or mutate production data.

## Fixed responsibilities

- ChatGPT owns architecture, route decisions, GitHub code, tests, review, candidate approval, merge/tag/seal.
- Codex is server execution only: inspection, verification, deployment, health checks, rollback, evidence return.
- Owner performs only real business acceptance and owner-only decisions.

Codex must not edit product source, create repair commits, push, merge, tag, or choose the project route.

## Decision question

Choose the safest viable server-side command receiver among:

A. GitHub self-hosted Actions runner with a dedicated least-privilege runner user and explicit job allowlist.
B. A minimal read-only/pull command poller that fetches signed/validated GitHub tasks and invokes a fixed Codex wrapper.
C. An existing authenticated non-interactive Codex/server execution entrypoint already present in production that can be safely bound to GitHub commands.

Do not choose based on convenience alone. Evaluate isolation, credential scope, blast radius, auditability, rollback, operational complexity, and compatibility with the current XiaoU deployment model.

## Required evidence

Read-only inspect and report:

1. Whether a GitHub Actions self-hosted runner is already installed/registered/running.
2. Whether Codex CLI or another non-interactive Codex execution entrypoint exists; version/path only, no secrets.
3. Whether the current server can reach GitHub API/Actions endpoints outbound.
4. Which Unix account currently owns/runs the XiaoU deployment and services, and whether a separate least-privilege runner/worker account already exists.
5. Current repository checkout/deployment layout relevant to verification/deployment, without exposing secrets.
6. Current service-control boundary: which services Codex currently inspects/restarts during approved deployments.
7. Whether current Codex authentication can be used non-interactively without exposing or copying long-lived credentials into GitHub.
8. Existing GitHub credentials/tokens on server: report only credential type/scope class if safely observable; never reveal values.
9. Whether server-side GitHub write capability is already limited to workflow dispatch/comment submission or is broader.
10. Any blockers for A, B, or C.

## Final classification

Return exactly one primary recommendation:

- A — self-hosted runner is safest/minimal;
- B — fixed pull worker/poller is safest/minimal;
- C — existing execution entrypoint can be reused safely;
- BLOCKED — more prerequisite work is required.

Also state why the other two are inferior in this production environment.

## Hard boundaries

No source changes.
No GitHub code writes.
No install/uninstall.
No credential creation/rotation.
No runner registration.
No service restart.
No deployment.
No configuration changes.
No production data writes.
No secrets or sensitive business data in the report.
