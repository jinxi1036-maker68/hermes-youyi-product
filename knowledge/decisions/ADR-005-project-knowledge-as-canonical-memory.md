# ADR-005｜GitHub Project Knowledge 作为项目长期权威记忆

- Status: Accepted
- Date: 2026-09-26
- Scope: Project continuity

## Context

聊天窗口上下文有限。长期依赖“上一窗口总结给下一窗口”会导致：
- 信息丢失；
- 新窗口不知道旧决策原因；
- 当前状态和历史状态混在一起；
- 多份交接文档互相冲突；
- 项目越做越长后出现认知分裂。

## Decision

建立 GitHub Project Knowledge，使用：
- 唯一入口；
- machine-readable index；
- current state；
- stable architecture/principles；
- capability map；
- ADR；
- history；
- update protocol。

当前放在同仓库 `project-knowledge` 分支，避免纯文档更新改变生产 main SHA。

## Why

GitHub天然提供：
- 版本
- diff
- commit
- 历史
- 可追溯性
- 与代码和PR的关联

项目记忆必须外置为可验证资产，不能依赖某个模型会话“记得”。

## Consequences

以后新窗口先读知识库，不再要求用户重新讲项目。
每次重大状态变化后，ChatGPT应主动更新 Knowledge Branch。
