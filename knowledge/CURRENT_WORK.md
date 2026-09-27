# CURRENT WORK｜自动生成投影

> **GENERATED — DO NOT EDIT.** 机器权威：`knowledge/CURRENT_WORK.json`。

- work_item_id: `WI-2026-09-response-latency-ux-architecture`
- kind: `runtime_experience_architecture`
- status: `latency_v1_phase2_context_compaction`
- context_profile: `capability_design`
- formal_stage_context: Stage 3 Direct Message / Outbound / paused_before_implementation

## Objective

Redesign XiaoU for low true latency and low perceived latency: simple turns should finish quickly; long work should acknowledge quickly, continue safely in the background, and deliver a truthful final result later.

## Current blocker

Current production has latency telemetry but no current baseline decomposition by turn class, and the user-visible response policy does not separate first-visible response from final task completion.

## Next action

Build and isolate-test a reversible Hermes-native long-session compaction candidate for Agnes 3.0. Target the observed >=24k latency cliff while preserving model-led reasoning, recent verbatim turns, semantic summary/recovery, trusted identity/authority context and full historical recoverability.

## Acceptance criteria

- Separate TTFV (time to first visible value) from TTC (time to complete).
- Identify which latency components are evidenced in XiaoU today and which still require production measurement.
- Define adaptive fast/normal/work lanes without weakening authority or write confirmation boundaries.
- Define a truthful early-response/background-continuation policy that avoids spam on fast turns.
- Define latency observability by turn class using privacy-safe traces.
- Define phased changes with measurable p50/p95 targets and rollback/evaluation criteria.
- Preserve the single-business-brain invariant: the model remains responsible for business understanding, judgment, planning, action/tool selection, and user-facing business expression.
- Latency optimization must reduce orchestration overhead, redundant model/tool rounds, waiting and delivery silence without lowering reasoning quality or removing evidence required for correctness.
- Any concurrency layer may only schedule model-selected operations whose tool metadata proves they are independent and read-only; it must never infer business intent or choose actions for the model.
- Any proposed model downgrade, reasoning-budget reduction, context reduction or deterministic business reply path requires separate evidence that result quality and authority semantics are unchanged.
- All latency optimizations must be deployable behind a reversible release boundary and be testable before becoming the new production baseline.
- Owner must be able to compare speed and answer quality before accepting the optimized runtime.
- Rollback must restore the pre-optimization production behavior without losing business data, session truth or durable work state.

## Stop rules

- Do not start Stage 3 capability implementation during this latency architecture work.
- Do not assume knowledge/context size is the main cause without trace evidence.
- Do not make writes or risky actions asynchronous past their confirmation/authority boundary.
- Do not add generic progress spam to every turn.
- Do not introduce a keyword/business router, deterministic business decision tree, or rule-based replacement for model intent/action selection in the name of latency.
- Do not globally reduce reasoning effort, switch to a weaker model, cap necessary tool use, or truncate required context solely to hit a latency target.
- Do not bypass the model for final business expression merely because a tool returned authoritative data; transport/system receipts are the only exception.
- Do not let early progress messages make business claims that the model/tool evidence has not yet established.
- Do not make irreversible production data migrations solely for latency optimization.
- Do not retire the current stable runtime until Owner acceptance of the optimized behavior.

## Context routing

Profile: `capability_design`

Must read:
- `knowledge/08_RELEASE_AND_RUNTIME.md`

On demand:
- `knowledge/EVIDENCE_INDEX.json`
- `knowledge/12_HISTORY_AND_MILESTONES.md`
- `knowledge/WORKING_METHOD.json`
- `knowledge/09_TESTING_SEALING_AND_EVIDENCE.md`
