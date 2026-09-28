# 当前状态｜自动生成投影

> **GENERATED — DO NOT EDIT.** 动态事实唯一权威：`knowledge/PROJECT_INDEX.json`；当前工作权威：`knowledge/CURRENT_WORK.json`。

## Formal Stage

- Stage: 3 — Direct Message / Outbound
- Status: `in_progress`
- Sealed: `false`

## Current Work

- ID: `WI-2026-09-stage3-direct-message-outbound-v2`
- Kind: `capability_stage`
- Status: `stage3_predeploy_blocked_runtime_binding`
- Objective: Freeze and then implement XiaoU's first formal direct outbound capability so an authorized human can ask XiaoU to send a specific work message to a legitimate recipient, with trustworthy recipient resolution, permission checks, execution evidence and truthful delivery semantics.
- Blocker: Production preflight found an existing release self-containment defect: active cb843 resolves hermes_cli from sibling e9c942. This is a runtime/deployment blocker, not a Stage 3 capability failure. A standalone exact config rollback copy is also absent.
- Next: Run a bounded read-only root-cause diagnosis of the active cb843 Core/venv binding (pyvenv.cfg, console shebang, sys.path/.pth, installed distribution metadata and symlink/origin chain) and identify the smallest Git-governed repair plus exact config-backup gate. Do not deploy Stage 3 or change production.

## Runtime facts

- main: `c4e116c4bd1799dc5532e020c2e8ccc1b7b50223`
- production: `cb843ccf4545cb40195614f3087cb4efd75cdfa8`

## Sealed capabilities

- Stage 1 Identity + Session: PASS — evidence `EV-ID-001`
- Stage 2 Query: PASS — evidence `EV-Q-004`

## Context routing

- Harness: `KH-003`
- Profile: `capability_design`
- Work execution state: `runtime_binding_diagnosis`
