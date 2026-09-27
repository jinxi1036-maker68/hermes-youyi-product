# 小U Project Knowledge｜唯一入口

> 聊天记忆不是权威，历史文档也不是当前状态。

## Agent Bootstrap

新 ChatGPT / Codex / 协作者只按此顺序：
1. 读 `knowledge/KNOWLEDGE_HARNESS.json`；
2. 读 `knowledge/PROJECT_INDEX.json`；
3. 读 `knowledge/CURRENT_WORK.json`；
4. 按任务执行必要 Freshness Gate；
5. 按 CURRENT_WORK 的 `context_profile` 加载 profile.required + `must_read`，`on_demand` 只在真正需要时读取。

**不要一次性加载整个知识库。**

## 默认不读
`05_CURRENT_STATE.md`、`CURRENT_WORK.md` 是人类投影；`ACTIVE_WORK.md` 是兼容指针；Evidence、History、Archive、Method History 均按需读取。

## 权威顺序
Owner 最新明确决定 → 实时事实 → PROJECT_INDEX → CURRENT_WORK → Working Method / sealed Evidence / ADR → 其它稳定知识 → Archive/History/旧聊天。

## Current / History
Current 只保存现在的一件工作；关闭后归档到 `knowledge/archive/work_items/`。当前 Markdown 由机器事实生成，禁止手工追加。

## 分支
- `main`：代码权威；
- `project-knowledge`：Project Knowledge / Harness 权威。

材料性写入遵守并发协议，禁止 force push。

## 新窗口启动语
> 读取 `jinxi1036-maker68/hermes-youyi-product` 的 `project-knowledge`，从 `knowledge/00_START_HERE.md` 开始，按 Knowledge Harness 路由：先读 PROJECT_INDEX 和 CURRENT_WORK，做必要 Freshness Gate，再按 CURRENT_WORK.context_profile 渐进加载；代码只从 main 读取，不要整库加载。
