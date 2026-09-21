# Production-to-candidate integrity audit

Date: 2026-09-21

This is a source-baseline audit.  The running server was read only throughout;
the candidate was built in an isolated checkout.

## Active-source comparison

The audit compared the production plugin source directly with
`runtime/plugins`, excluding bytecode and ignored runtime residue.

| result | count |
| --- | ---: |
| production Python files | 137 |
| matching candidate files | 137 |
| byte-identical files | 98 |
| intentionally different files | 39 |
| production files absent from candidate | 0 |
| candidate-only historical files | 3 |

The candidate-only files are the historical `router.py`, `semantic_router.py`,
and `dashboard_refresh_runner.py` retained from `main`. They are not loaded by
the verified 1.2.19 production topology and remain for review only.

### Identity-sensitive differences

Production contained person, account, and tenant literals in a small number of
source paths.  They have not been substituted with fake identities.  Instead,
the candidate uses server-owned, trusted facts:

- `employee_identity.py` resolves an owner from `super_users`, then a canonical
  active `staff.json` boss record or explicit trusted role.  An alias map alone
  cannot create owner authority.
- `autonomous_employee_loop.py` and `digital_employee_state.py` use that same
  resolver rather than duplicate alias-based fallbacks.
- `p4_8_account_admin.py` remains owner-only, but obtains that owner from the
  trusted resolver.
- `runtime_foundation.py` obtains protected owner display aliases only after
  resolving that owner id; a non-owner reply can therefore repair a mistaken
  direct salutation without carrying a source-code owner name.
- `acceptance_v1.py` derives optional acceptance targets from trusted roles and
  explicit configuration, not display names.
- `tenant_context.py` reads the configured tenant and has only a generic
  development fallback. It also reads optional institution display labels from
  the existing operating-model fact for directory normalisation. A deployed
  instance must set `HERMES_TENANT_ID`.
- `staff_directory.py`, `dashboard_builder.py`, and `payroll.py` use those
  server-owned display labels when they must reject a tenant brand as a person
  name. They no longer carry a particular institution name in their matching
  rules.
- `tool_service.py` converts a retired institutional-rollout sandbox from a
  two-person hard-coded list to an explicit server-owned compatibility policy.
  It is fail-closed until that policy is intentionally supplied; it is not part
  of the current Work Runtime release path.
- `runtime.py` keeps the retired summer reminder recipient as the symbolic
  reference `owner`; it is not an active 1.2.19 scheduler path.

Focused regression coverage proves that a trusted owner retains its authority,
a canonical active boss is an acceptable legacy fallback, an arbitrary alias
is not, the tenant's own operating-model label can normalise directory input,
and the retired rollout path requires an explicit policy. A separate Runtime
Contract regression proves that a model envelope cannot rebind the actor or
tenant of an already authenticated turn.

### Text-only and historical-material differences

The remaining source differences replace tenant-bound display text, examples,
scenario cards, comments, or neutral test labels. Where such text is shown to
the model it now refers to `本机构` or a role such as `相关老师`; it is not a
substitute business identity. These strings do not participate in identity,
recipient, role, permission, account selection, or Tool dispatch. They are
confined to the following files:

`platforms/wecom/callback_adapter.py`, `tuoguan_core/__init__.py`,
`config_changes.py`, `core_understanding.py`, `daily_reporter.py`,
`dashboard_http.py`, `external_learning_runner.py`,
`governance_claims_v1.py`, `gray_review_v1.py`, `gray_scenario_cards.py`,
`growth_plan_exporter.py`, `identity.py`, `learning_loop.py`,
`operational_facts.py`, `operations_daily_report.py`, `permission_guard.py`,
`research.py`, `social_market_research.py`, `staff_config.py`,
`student_daily_records.py`, `summer_course_coverage.py`,
`summer_enrollment.py`, `system_self_knowledge.py`, `task_query_gray.py`,
`tasks.py`, and `tools.py`.

`self_evolution.py` and the owner-question helper in
`autonomous_employee_loop.py` also remove stale literal-name matching. Their
current formal inputs carry trusted roles and identifiers, so role-based facts
remain available while arbitrary names cannot alter a decision.

There is no unresolved production-source omission. The only deliberately
non-identical semantic boundary is the retired rollout compatibility path,
which needs an explicit server-side policy if a future deployment chooses to
revive it. Historical P4/gray scenario cards remain source history rather than
current Runtime Contract inputs; their illustrative people and students have
neutral fixture labels.

## Provider template semantics (read-only production check)

The production configuration was read without exposing credentials or endpoint
secrets. Its non-secret model semantics are represented by
`deploy/config/config.yaml.example`: the primary model is
`agnes-3.0-flash`, the configured context length is 524288, and
`fallback_providers` is an empty list. There is no configured fallback model
or provider policy. This is a deployment fact only; it does not certify
Provider stability.

## Sensitive-material audit

The candidate was scanned without emitting values. The 379 tracked text files
contained no private-key marker and no known production person, account, or
tenant literal. The 12 retained binary historical artifacts were also scanned
for raw private-key, known-identity, phone, and email patterns; none matched.
The one retained DOCX was additionally checked through its XML payload with
the same clean result.

Heuristic matches were reviewed by path and type rather than copied into this
report: ten are configuration-field/placeholder, generated-token, test, or
validator code; three phone-shaped values are synthetic test fixtures; and one
email-shaped value is a generic proxy test. No credential, production contact,
or production Workspace data is included. This scan does not turn historical
screenshots into current runtime assets; those binaries remain retained
historical material pending any future archival review.

## Historical assets

The historical `work/` dossier and `systemd/` units deleted by the first
baseline import have been restored byte-for-byte from the prior `main` commit.
They are retained as history, not as deployment inputs. Current deployment
templates live under `deploy/`; a later retirement review must be a separate
change.

## Test inventory and release-gate classification

The complete candidate test collection contained 559 tests:

| class | count | treatment |
| --- | ---: | --- |
| current 1.2.19 production-contract tests | 9 | release gate; all passed |
| current disposable usability convergence recipe | 1 recipe | passed in a synthetic Workspace |
| direct Reply Truth / durable recovery regression | 1 recipe | passed against Hermes 0.21 |
| historical/compatibility tests passed | 354 | retained as historical evidence |
| historical or unsupported-environment failures | 89 | retained and classified below; not a 1.2.19 release gate |
| explicitly skipped fixture-dependent tests | 107 | require their named external fixture or historical setup |

The 89 retained failures break down as follows:

- 60 old assertion contracts: archived fixed-tool-surface, legacy direct
  router/facade, pre-Work-Runtime task, old learning, old supervision, and
  0.19/0.20 autonomous-loop expectations. Their modules begin with
  `test_youyi_` or the old `test_xiaoyou_reliability_gate_v1` family and test
  superseded contracts rather than the 1.2.19 Runtime Contract.
- 11 tests expect private or removed Hermes 0.20 hook fields. Hermes 0.21 and
  the public Runtime Contract intentionally do not provide those hooks.
- 10 WeCom tests were run using the lightweight test interpreter, which lacks
  its optional cryptography dependency. They are environment-inconclusive,
  not feature assertions.
- 7 tests import retired runtime-stability helpers that are not part of the
  1.2.19 source topology.
- 1 test requires a removed historical memory fixture.

### Tool retry-budget failure, precisely

`runtime/tests/plugins/test_xiaoyou_latency_reliability_v1.py::test_turn_tool_budget_allows_two_contract_corrections_then_stops`
expects the old comparison that blocks immediately after three completed
correctable failures. The current production
`runtime_performance.py` deliberately permits those three failed calls and one
further corrected call, then blocks a later retry. Its comment records why:
the former comparison could block the correction itself after a model first
omitted a required field and then removed unsupported fields.

This is XiaoYou production behavior, not a Hermes 0.21 default. It does not
turn a failed tool result into success, change idempotency, or bypass reply
truth; it only permits one final distinct correction within the existing
per-turn call budget. The historical test remains unchanged as evidence of the
old contract and is not a current release gate.

## Current release gate commands

Run these in a disposable environment:

1. `python -m compileall runtime/plugins/tuoguan_core runtime/tests/production`
2. `pytest runtime/tests/production -q` (nine collected contract tests; the
   separately invoked disposable-fixture recipe is skipped until its explicit
   environment is supplied)
3. create a synthetic fixture with
   `scripts/create_disposable_production_usability_fixture.py`, then run
   `runtime/tests/production/test_production_usability_convergence.py` against
   a Hermes 0.21 checkout
4. run `runtime/tests/production/reply_truth_runtime_regression.py` against a
   Hermes 0.21 checkout

The release gate covers trusted identity, immutable trusted-turn binding,
tenant scoping, Permission,
Agenda/Work Runtime progression, Receipt/writeback truth, Reply Truth,
Durable Outbox behavior, callback reset-envelope detection, and complete
student-list pagination. It deliberately does not claim that historical,
private-hook tests certify the current public Runtime Contract.
