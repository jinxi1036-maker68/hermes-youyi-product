# 当前状态｜自动生成投影

> **GENERATED — DO NOT EDIT.** 动态事实唯一权威：`knowledge/PROJECT_INDEX.json`；当前工作权威：`knowledge/CURRENT_WORK.json`。

## Formal Stage

- Stage: 3 — Direct Message / Outbound
- Status: `ready_to_start`
- Sealed: `false`

## Current Work

- ID: `WI-2026-09-response-latency-ux-architecture`
- Kind: `runtime_experience_architecture`
- Status: `latency_v1_owner_acceptance_window_active`
- Objective: Redesign XiaoU for low true latency and low perceived latency: simple turns should finish quickly; long work should acknowledge quickly, continue safely in the background, and deliver a truthful final result later.
- Blocker: Current production has latency telemetry but no current baseline decomposition by turn class, and the user-visible response policy does not separate first-visible response from final task completion.
- Next: Hold the system unchanged while Owner tests real WeCom behavior. Acceptance must cover perceived speed, simple turn quality, direct read, multi-read, long-session continuity, >8s progress receipt behavior, authority/Unknown correctness and no business-brain regression. On any Owner rejection, perform immediate rollback to the preserved stable release/config.

## Runtime facts

- main: `cb843ccf4545cb40195614f3087cb4efd75cdfa8`
- production: `cb843ccf4545cb40195614f3087cb4efd75cdfa8`

## Sealed capabilities

- Stage 1 Identity + Session: PASS — evidence `EV-ID-001`
- Stage 2 Query: PASS — evidence `EV-Q-004`

## Context routing

- Harness: `KH-003`
- Profile: `capability_design`
- Work execution state: `paused_before_implementation`
