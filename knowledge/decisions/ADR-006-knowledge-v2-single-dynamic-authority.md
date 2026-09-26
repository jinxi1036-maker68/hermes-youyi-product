# ADR-006｜Project Knowledge V2：纯知识树 + 单一动态权威

- Status: Accepted
- Date: 2026-09-26
- Scope: Project knowledge architecture

## Context

V1 已解决“没有长期项目记忆”的问题，但存在三个长期风险：

1. knowledge branch 从 main 分出，当前树仍携带旧代码快照；
2. main SHA / production SHA / Stage / blocker 在多个 Markdown 重复；
3. 新窗口知道“项目是什么”，但不一定知道上一窗口最后一步和原始证据。

## Decision

V2：

1. `project-knowledge` 当前工作树只保留 README + knowledge，不承载代码；
2. `PROJECT_INDEX.json` 成为动态当前事实唯一权威；
3. 增加 `ACTIVE_WORK.md` 保存最后一公里工作上下文；
4. 增加 `EVIDENCE_INDEX.json` 保存可追溯证据；
5. 新窗口开工前执行 Freshness Gate；
6. 增加 Domain Model、Rejected Approaches、Capability Archive；
7. 增加 Schema/Validator；
8. Knowledge Sync 纳入 Definition of Done。

## Why

项目连续性的目标不是“资料多”，而是：
- 找得到；
- 不冲突；
- 够新鲜；
- 有证据；
- 能直接续做；
- 越积累越不乱。

## Consequences

- 代码分析永远从 main；
- 动态事实只改 PROJECT_INDEX，再同步投影；
- knowledge branch 的旧代码只能存在于 Git 历史，不存在于当前工作树；
- 以后新窗口不依赖上一窗口手工总结。
