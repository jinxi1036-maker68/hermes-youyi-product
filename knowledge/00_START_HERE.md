# 小U项目知识库｜唯一入口

> 本分支是小U项目的长期 Project Knowledge。  
> **不要依赖聊天记忆作为权威。不要从零重新设计。**

## 固定启动顺序

任何新 ChatGPT / Codex / 协作者开始工作前：

1. 读取 **`knowledge/PROJECT_INDEX.json`** —— 当前动态事实唯一权威。
2. 执行 **Freshness Gate**：核对 GitHub `main` HEAD；涉及生产时核对最近生产事实是否仍新鲜。
3. 读取 **`knowledge/05_CURRENT_STATE.md`** —— 当前状态的人类可读投影。
4. 读取 **`knowledge/ACTIVE_WORK.md`** —— 接着上一窗口最后一项工作继续。
5. 读取 **`knowledge/04_CAPABILITY_MAP.md`** —— 产品能力坐标。
6. 按当前工作需要再读取 Architecture、Domain Model、ADR、Evidence、Runtime、Backlog。

不要一开始把整个知识库和全部历史塞进模型上下文。先定位，再按需检索。

## 权威顺序

发生冲突时：

1. 用户最新明确决定；
2. 实时可验证事实（GitHub main / 生产服务器）；
3. `PROJECT_INDEX.json`；
4. 已封板证据、stable tag、ADR；
5. 其它知识文档；
6. 旧交接文档、聊天总结和模型记忆。

实时事实若与 PROJECT_INDEX 冲突，**先更新知识库，再继续实现**。

## 两条分支边界

- `main`：代码权威。
- `project-knowledge`：项目长期记忆权威。

本知识分支当前工作树应为**纯知识树**，不得用这里的历史代码快照分析当前代码。

## 新窗口固定启动语

> 读取 GitHub 仓库 `jinxi1036-maker68/hermes-youyi-product` 的 `project-knowledge` 分支，从 `knowledge/00_START_HERE.md` 开始。先读取 PROJECT_INDEX.json，执行 Freshness Gate，再读取 CURRENT_STATE 和 ACTIVE_WORK，继续小U当前工作；代码只从 main 分支读取，不要从零重新设计。

## 安全

仓库当前是 public，因此本分支只允许 public-safe 项目知识。详见 `README_PUBLIC_SAFETY.md`。
