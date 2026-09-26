# ADR-003｜Hermes Runtime Home 必须版本中立

- Status: Accepted
- Date: 2026-09-25
- Scope: Production runtime

## Context

生产连续部署失败后发现：
- release local Home 被 root preflight 污染；
- sessions / cron / state.db / logs 跨 release 指向历史目录；
- 代码和持久运行状态耦合。

## Decision

长期结构：
- release code：版本化、不可变；
- Hermes persistent Home：版本中立；
- config/environment：稳定目录；
- Workspace / Agenda：独立业务状态。

## Why

升级代码不应该搬动或重新拥有 persistent state；persistent state 也不能依赖某个旧 release 才能运行。

## Consequences

- `HERMES_HOME=/var/lib/hermes-youyi/hermes-home`
- candidate preflight 必须 service identity
- release self-contained gate
- persistent Home gate
- migration plan/seed/finalize/verify
- rollback 保留旧 Home，禁止 reverse-sync
