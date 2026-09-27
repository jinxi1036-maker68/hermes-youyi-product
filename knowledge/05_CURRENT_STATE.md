# 当前状态｜自动生成投影

> **GENERATED — DO NOT EDIT.** 动态事实唯一权威：`knowledge/PROJECT_INDEX.json`；当前工作权威：`knowledge/CURRENT_WORK.json`。

## Formal Stage

- Stage: 3 — Direct Message / Outbound
- Status: `ready_to_start`
- Sealed: `false`

## Current Work

- ID: `WI-2026-09-response-latency-ux-architecture`
- Kind: `runtime_experience_architecture`
- Status: `context_compaction_perf_evidence_blocked`
- Objective: Redesign XiaoU for low true latency and low perceived latency: simple turns should finish quickly; long work should acknowledge quickly, continue safely in the background, and deliver a truthful final result later.
- Blocker: Current production has latency telemetry but no current baseline decomposition by turn class, and the user-visible response policy does not separate first-visible response from final task completion.
- Next: Keep PR #28 open and unmerged. Run a production-adjacent, no-traffic synthetic A/B on the exact Agnes 3.0 route: same synthetic long-session semantics before and after Hermes-native compaction, capture prompt tokens, TTFT/API duration/complete time if available, and prove no production/public routing changes.

## Runtime facts

- main: `adb8b031a89f61950d71df005c176b07b9a5e3c1`
- production: `e9c942454bf0325a90a9df55485ae52b5247ce4e`

## Sealed capabilities

- Stage 1 Identity + Session: PASS — evidence `EV-ID-001`
- Stage 2 Query: PASS — evidence `EV-Q-004`

## Context routing

- Harness: `KH-003`
- Profile: `capability_design`
- Work execution state: `paused_before_implementation`
