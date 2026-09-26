# ADR-007｜Working Method 作为可学习、可版本化的项目资产

- Status: Accepted
- Date: 2026-09-26
- Scope: Project execution / collaboration

## Context

Project Knowledge 已经解决“新窗口不知道项目过去、现在、未来”的问题。

但仍存在另一种分裂：

新窗口可能知道当前项目状态，却：
- 改变 Owner / ChatGPT / Codex 分工；
- 改变部署节奏；
- 改变验收标准；
- 重新采用已经证明低效的工作方式；
- 忘记之前为什么从“不断重试”升级为“evidence-first + stop rules”。

这说明“工作方式”本身也是需要长期继承的项目知识。

## Decision

将 Working Method 提升为一等项目资产：

- `WORKING_METHOD.json`：当前工作方式机器权威；
- `CURRENT_WORKING_METHOD.md`：人类可读投影；
- `methods/WM-xxx-*.md`：历史版本与演化原因；
- PROJECT_INDEX 保存当前 method_id；
- 新窗口启动必须读取当前 Working Method；
- Validator 检查 method_id、历史文件和投影一致性；
- 正式方法升级必须经过 Method Learning Loop。

## Method Learning Loop

工作方式不是永久不变，也不能随意变化。

只有当真实工作证明当前方法存在问题时：
1. 观察流程问题；
2. 区分产品问题与方法问题；
3. 提出候选方法；
4. 有边界验证；
5. 比较正确性、速度、安全、返工、用户负担；
6. ACCEPT / REJECT / REVISE；
7. ACCEPT 后升级 method_id；
8. 保留旧版本和被替换原因。

## Why

目标不是建立越来越厚的流程制度，而是让项目本身具备“方法层学习”：

> 既继承经验，又能根据新问题持续优化做事方式。

## Consequences

- 新窗口不得静默改变项目工作方式；
- 方法优化必须留下版本和历史；
- 好的方法可以保留，坏的方法可以被淘汰；
- 未来方法升级可能是增加步骤，也可能是删除无价值步骤；
- 项目最终不仅拥有“越来越聪明的小U”，还拥有“越来越成熟的开发小U的方法”。
