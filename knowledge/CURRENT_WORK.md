# CURRENT WORK｜自动生成投影

> **GENERATED — DO NOT EDIT.** 机器权威：`knowledge/CURRENT_WORK.json`。

- work_item_id: `WI-2026-09-stage3-direct-message-outbound-v2`
- kind: `capability_stage`
- status: `stage3_contract_design`
- context_profile: `capability_design`
- formal_stage_context: Stage 3 Direct Message / Outbound / contract_freeze

## Objective

Freeze and then implement XiaoU's first formal direct outbound capability so an authorized human can ask XiaoU to send a specific work message to a legitimate recipient, with trustworthy recipient resolution, permission checks, execution evidence and truthful delivery semantics.

## Current blocker

Stage 3 capability boundary is not yet Owner-frozen. The initial draft deliberately does not assume parent messaging, bulk/group messaging, or autonomous Agenda outreach.

## Next action

Confirm the Stage 3 first-scope boundary with the Owner. Proposed first slice: explicit human-commanded 1:1 outbound to current authorized internal staff only; no parent/external recipient, no bulk/group send, and no autonomous initiation in Stage 3.

## Acceptance criteria

- Model remains the only business brain: it understands the human request and chooses whether to use the outbound capability; runtime does not keyword-route business intent.
- Human explicit command and future AI-autonomous outreach remain distinct authorization semantics; Stage 3 does not silently import Agenda autonomy.
- Recipient must resolve to exactly one current legitimate recipient in the same tenant; ambiguous or unknown recipients remain Unknown and require clarification.
- Runtime enforces sender authority, tenant isolation, recipient eligibility, channel binding and message-send safety.
- The model owns the requested business message content; runtime does not invent or rewrite the business meaning.
- Outbound execution is idempotent for the authenticated turn and must not duplicate-send on retry/recovery.
- Truth states remain distinct: prepared/queued/API accepted/sent/received/completed are not conflated.
- Final response may say a message was sent only when delivery evidence supports the exact allowed claim.
- Failure leaves a truthful, recoverable state and never reports success without evidence.
- Stage 3 real acceptance uses a minimal real WeCom 1:1 send test with a known authorized internal recipient after technical gates pass.

## Stop rules

- Do not implement parent or external-customer messaging until its authority and privacy contract is separately approved.
- Do not implement group broadcast or bulk messaging in the first Stage 3 slice.
- Do not implement autonomous Agenda outreach or task follow-up; those belong to later roadmap stages.
- Do not mix Student Basic Writes, People & Organization, Tasks or Task Progress into Stage 3.
- Do not let recipient names from natural language bypass authoritative person resolution.
- Do not report API accepted as recipient-read or business-completed.
- Do not create a deterministic business router or canned message decision tree.

## Context routing

Profile: `capability_design`

Must read:
- `knowledge/01_PRODUCT_NORTH_STAR.md`
- `knowledge/02_ARCHITECTURE_AND_BOUNDARIES.md`
- `knowledge/07_AUTHORITY_DATA_AND_TRUTH.md`
- `knowledge/decisions/ADR-002-human-command-vs-ai-autonomy.md`
- `knowledge/09_TESTING_SEALING_AND_EVIDENCE.md`

On demand:
- `knowledge/08_RELEASE_AND_RUNTIME.md`
- `knowledge/EVIDENCE_INDEX.json`
- `knowledge/12_HISTORY_AND_MILESTONES.md`
