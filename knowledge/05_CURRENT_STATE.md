# 当前状态｜自动生成投影

> **GENERATED — DO NOT EDIT.** 动态事实唯一权威：`knowledge/PROJECT_INDEX.json`；当前工作权威：`knowledge/CURRENT_WORK.json`。

## Formal Stage

- Stage: 3 — Direct Message / Outbound
- Status: `in_progress`
- Sealed: `false`

## Current Work

- ID: `WI-2026-09-stage3-direct-message-outbound-v2`
- Kind: `capability_stage`
- Status: `stage3_runtime_repair_candidate_isolated_verification`
- Objective: Freeze and then implement XiaoU's first formal direct outbound capability so an authorized human can ask XiaoU to send a specific work message to a legitimate recipient, with trustworthy recipient resolution, permission checks, execution evidence and truthful delivery semantics.
- Blocker: Root cause is confirmed outside Stage 3 business logic: cb843 inherited stale editable Hermes Core metadata pointing at e9c942, and current production lacks one exact protected rollback snapshot for all Gateway config inputs + unit. PR #32 is the Git-governed repair candidate.
- Next: Verify PR #32 candidate 300a9b8b87f363e43c66fa828ffa259183ec4109 in isolation on the server. Prove a newly assembled full candidate has no sibling-release Core/metadata path, the current cb843 defect is detected, protected exact config/unit snapshot semantics pass, and production remains unchanged. Do not merge or deploy.

## Runtime facts

- main: `c4e116c4bd1799dc5532e020c2e8ccc1b7b50223`
- production: `cb843ccf4545cb40195614f3087cb4efd75cdfa8`

## Sealed capabilities

- Stage 1 Identity + Session: PASS — evidence `EV-ID-001`
- Stage 2 Query: PASS — evidence `EV-Q-004`

## Context routing

- Harness: `KH-003`
- Profile: `capability_design`
- Work execution state: `runtime_repair_candidate_verification`
