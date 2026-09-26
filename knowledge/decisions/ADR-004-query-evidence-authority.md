# ADR-004｜Query 事实必须按证据权威分层

- Status: Accepted
- Date: 2026-09
- Scope: Query

## Context

用户问“今天还有哪些事情没处理完？”时，系统曾使用 active work context 返回 0，并推断“今天全部完成”，还使用了没有证据的“您名下”。

问题不是一句话措辞，而是 **证据边界错误**。

## Decision

对于任务：
- inventory
- list
- count
- status
- date-scoped facts

必须由权威 task query 支撑。

active work context 只作为 continuation / current-focus evidence，不能证明任务库存、数量或日期范围。

## Consequences

模型仍负责理解用户问题，但最终事实必须落在正确的数据权威上。
