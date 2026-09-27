# Archived Work Item｜WI-2026-09-knowledge-harness-v3

- Status: completed
- Closed: 2026-09-27
- Outcome: Knowledge Harness V3 deployed and validated; WM-007 activated.
- PR: #26
- Project Knowledge commit: `5d73f81e13cbe9aa49f700572f8894fb4822a9fb`
- Evidence: `EV-KH-001`

## Objective

Refactor Project Knowledge into a compact, progressive-disclosure Knowledge Harness before Stage 3 implementation begins.

## Problem

PROJECT_INDEX is current, but ACTIVE_WORK accumulated closed Runtime Topology history and CURRENT_STATE retained stale Stage 2 prose. New agents can find truth only after reading too much contradictory context.

## Acceptance result

- PASS — Agent bootstrap requires only PROJECT_INDEX.json and CURRENT_WORK.json before selecting a task profile.
- PASS — ACTIVE_WORK no longer carries live state; the completed bootstrap work item is archived.
- PASS — CURRENT_WORK.json contains only the active work item and stays within budget.
- PASS — CURRENT_STATE.md and CURRENT_WORK.md are deterministic generated projections and validator detects drift.
- PASS — Load profiles keep History, Evidence and Archive out of default bootstrap context.
- PASS — Validator detects work/index mismatch, invalid profile references and context-budget violations.
- PASS — Authority, evidence, safety, concurrency and WM-006 role guarantees remain preserved under WM-007.

## Final measured bootstrap/current sizes

- `00_START_HERE.md`: ~1.6 KB
- `KNOWLEDGE_HARNESS.json`: ~4.4 KB
- `PROJECT_INDEX.json`: ~6.1 KB
- `CURRENT_WORK.json`: ~2.6 KB during Harness work
- `ACTIVE_WORK.md`: ~0.4 KB compatibility pointer

## Key decisions

- Current is not History.
- Closed work is archived, never appended to Current.
- Agent startup uses progressive disclosure.
- Human current-state Markdown is generated, not hand-maintained.
- History/Evidence/Archive can grow without becoming bootstrap context.
