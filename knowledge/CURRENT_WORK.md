# CURRENT WORK｜自动生成投影

> **GENERATED — DO NOT EDIT.** 机器权威：`knowledge/CURRENT_WORK.json`。

- work_item_id: `WI-2026-09-knowledge-harness-v3`
- kind: `knowledge_harness`
- status: `in_progress`
- context_profile: `knowledge_maintenance`
- formal_stage_context: Stage 3 Direct Message / Outbound / paused_before_implementation

## Objective

Refactor Project Knowledge into a compact, progressive-disclosure Knowledge Harness before Stage 3 implementation begins.

## Current blocker

Current knowledge structure permits projection drift and lets current-work documents grow as append-only history.

## Next action

Implement KH-003 files, generated projections, archive-on-close, task load profiles, and validator checks; validate the branch; then resume Stage 3 capability design.

## Acceptance criteria

- Agent bootstrap requires only PROJECT_INDEX.json and CURRENT_WORK.json before selecting a task profile.
- ACTIVE_WORK no longer carries live state; the completed bootstrap work item is archived.
- CURRENT_WORK.json contains only the active work item and stays within budget.
- CURRENT_STATE.md and CURRENT_WORK.md are deterministic generated projections and validator detects drift.
- Load profiles keep History, Evidence and Archive out of default bootstrap context.
- Validator detects work/index mismatch, invalid profile references and context-budget violations.
- Authority, evidence, safety, concurrency and WM-006 guarantees are not weakened.

## Stop rules

- Do not start Stage 3 product implementation until KH-003 consistency passes.
- Do not delete historical evidence; archive closed work instead.
- Do not make History/Evidence/Archive default bootstrap context.
- Do not weaken public-safety, Freshness, Evidence, role or rollback requirements.

## Context routing

Profile: `knowledge_maintenance`

Must read:
- `knowledge/decisions/ADR-005-project-knowledge-as-canonical-memory.md`
- `knowledge/decisions/ADR-006-knowledge-v2-single-dynamic-authority.md`

On demand:
- `knowledge/EVIDENCE_INDEX.json`
- `knowledge/12_HISTORY_AND_MILESTONES.md`
- `knowledge/WORKING_METHOD.json`
- `knowledge/ROLE_AND_HANDOFF_CONTRACT.md`
