# CURRENT WORK｜自动生成投影

> **GENERATED — DO NOT EDIT.** 机器权威：`knowledge/CURRENT_WORK.json`。

- work_item_id: `WI-2026-09-response-latency-ux-architecture`
- kind: `runtime_experience_architecture`
- status: `research_in_progress`
- context_profile: `capability_design`
- formal_stage_context: Stage 3 Direct Message / Outbound / paused_before_implementation

## Objective

Redesign XiaoU for low true latency and low perceived latency: simple turns should finish quickly; long work should acknowledge quickly, continue safely in the background, and deliver a truthful final result later.

## Current blocker

Current production has latency telemetry but no current baseline decomposition by turn class, and the user-visible response policy does not separate first-visible response from final task completion.

## Next action

Finish first-principles latency diagnosis and freeze an optimization architecture and measurement plan before code changes.

## Acceptance criteria

- Separate TTFV (time to first visible value) from TTC (time to complete).
- Identify which latency components are evidenced in XiaoU today and which still require production measurement.
- Define adaptive fast/normal/work lanes without weakening authority or write confirmation boundaries.
- Define a truthful early-response/background-continuation policy that avoids spam on fast turns.
- Define latency observability by turn class using privacy-safe traces.
- Define phased changes with measurable p50/p95 targets and rollback/evaluation criteria.

## Stop rules

- Do not start Stage 3 capability implementation during this latency architecture work.
- Do not assume knowledge/context size is the main cause without trace evidence.
- Do not make writes or risky actions asynchronous past their confirmation/authority boundary.
- Do not add generic progress spam to every turn.

## Context routing

Profile: `capability_design`

Must read:
- `knowledge/08_RELEASE_AND_RUNTIME.md`

On demand:
- `knowledge/EVIDENCE_INDEX.json`
- `knowledge/12_HISTORY_AND_MILESTONES.md`
- `knowledge/WORKING_METHOD.json`
- `knowledge/09_TESTING_SEALING_AND_EVIDENCE.md`
