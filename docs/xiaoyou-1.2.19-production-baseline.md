# XiaoYou 1.2.19 production-source candidate

This branch reconstructs a reviewable XiaoYou product baseline from a running
Hermes 0.21 deployment without copying its operational state. It is a source
baseline, not a deployment snapshot and not a backup of an institution.

## Included by whitelist

- XiaoYou plugin and adapter source under `runtime/plugins`.
- The two locally installed XiaoYou skills under `runtime/skills`.
- The current skill bundle, corrected to list only those two actually enabled
  XiaoYou-local skills.
- Safe production validation recipes under `runtime/tests/production`.
- Runtime capability manifest, deployment templates, and architecture notes.

The imported plugin source deliberately excludes Python bytecode and patch
residue (`*.orig`, `*.rej`). Hermes Core itself is not vendored: this package
declares a Hermes 0.21 runtime contract instead.

The candidate does not carry production people, account identifiers, tenant
identifiers, or addresses in source.  Where an older source path had embedded
identity constants, the candidate resolves the same authority from trusted
server-owned identity facts (or fails closed for a retired compatibility path).
Neutral fixtures are used only in tests and illustrative material.  The
production deployment is unchanged; see
`docs/production-baseline-integrity-audit.md` for the per-category review.

## Explicitly excluded

No institution Workspace, database, ledger, receipt, durable outbox, Agenda
ticket, session, trace, log, cache, backup, validation clone, virtual
environment, live configuration, credential, or personal/business record is
part of this branch. These exclusions are enforced in `.gitignore`; review
before adding a new runtime path.

The inherited `work/` dossier is retained from the historical `main` branch.
It is not part of the current runtime and may contain dated proposals,
generated material, or tenant-specific examples.  Retention preserves review
history; it must not be read as evidence that an item is loaded by the current
production topology.

## Skill availability

The running deployment has many Hermes built-in skills. The only local
XiaoYou skills enabled from the product deployment are
`xiaoyou-core-contract` and `xiaoyou-observation-evidence`. Historical skill
names found in old bundle material are not evidence that those skills are
installed or usable. The bundle here lists only the actual local pair.

## Deployment model

The target layout is:

```
Hermes Core 0.21.x (external dependency)
        +
XiaoYou capability checkout (this repository)
        +
Institution Workspace (runtime-only, never committed)
```

`deploy/` contains templates only. Operators must create the runtime config,
environment file, and Workspace outside this repository, set secret values in
a protected secret store or systemd `EnvironmentFile`, and run the certification
recipes with a disposable fixture before any deployment.
