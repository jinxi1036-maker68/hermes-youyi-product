# ADR-010｜Knowledge Harness V3：最小启动、渐进加载、Current/History 分离

- Status: Accepted
- Date: 2026-09-27
- Scope: Project Knowledge runtime architecture
- Owner approval: explicit

## Context

Knowledge V2 已建立 PROJECT_INDEX、Freshness、Evidence、ADR、Working Method 与 Validator，但真实运行出现两个问题：

1. `ACTIVE_WORK.md` 持续追加已完成生产过程，Current 退化为 History；
2. `PROJECT_INDEX` 已进入 Stage 3 时，CURRENT_STATE / ACTIVE_WORK 仍保留 Stage 2 叙述，说明手工投影会漂移。

## Decision

采用 KH-003：

1. 选择 task profile 前，Agent 只读 PROJECT_INDEX + CURRENT_WORK；
2. `KNOWLEDGE_HARNESS.json` 定义 load profiles、预算和生命周期；
3. `CURRENT_WORK.json` 只保存一个当前工作项；
4. work item 关闭即归档到 `archive/work_items/`；
5. CURRENT_STATE / CURRENT_WORK Markdown 由机器事实生成，禁止手工追加；
6. Validator 检查投影 exact match、work/index 一致、profile 引用和预算；
7. Evidence / History / ADR / Archive 可以增长，但默认不进 Bootstrap；
8. 稳定架构/方法不复制到 Current，按 profile 或 on-demand 读取。

## Principle

> 不是让整个知识库永远变小，而是让每次开工必须加载的集合永远保持小且无歧义。

## Consequences

- 历史增长不再线性增加启动 token；
- 当前状态漂移成为机器可检测错误；
- ACTIVE_WORK 只保留短兼容指针；
- Agent 不再自己猜“应该读哪些文档”，而由 Harness 路由。
