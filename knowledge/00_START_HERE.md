# 小U项目知识库｜唯一入口

> 本分支是小U项目的长期 Project Knowledge。  
> **不要依赖聊天记忆作为权威。不要从零重新设计。不要在新窗口自行改工作方式。**

## 固定启动顺序

任何新 ChatGPT / Codex / 协作者开始工作前：

1. 读取 **`knowledge/PROJECT_INDEX.json`** —— 当前动态事实唯一权威。
2. 执行 **Freshness Gate**：核对 GitHub `main` HEAD；涉及生产时核对最近生产事实是否仍新鲜。
3. 读取 **`knowledge/05_CURRENT_STATE.md`** —— 当前状态的人类可读投影。
4. 读取 **`knowledge/ACTIVE_WORK.md`** —— 接着上一窗口最后一项工作继续。
5. 读取 **`knowledge/WORKING_METHOD.json`** + **`knowledge/CURRENT_WORKING_METHOD.md`** —— 继承当前最新工作方式。
6. 读取 **`knowledge/04_CAPABILITY_MAP.md`** —— 产品能力坐标。
7. 按当前工作需要再读取 Architecture、Domain Model、ADR、Evidence、Runtime、Backlog、Method History。

不要一开始把整个知识库和全部历史塞进模型上下文。先定位，再按需检索。

## 权威顺序

发生冲突时：

1. 用户最新明确决定；
2. 实时可验证事实（GitHub main / 生产服务器）；
3. `PROJECT_INDEX.json`；
4. 当前 `WORKING_METHOD.json`（工作方式）；
5. 已封板证据、stable tag、ADR；
6. 其它知识文档；
7. 旧交接文档、聊天总结和模型记忆。

实时事实若与 PROJECT_INDEX 冲突，**先更新知识库，再继续实现**。

如果新窗口想改变工作节奏、分工、验收或部署方法：
- 不能直接凭习惯改变；
- 先判断是否属于“工作方法问题”；
- 按 `WORKING_METHOD.json.method_learning_loop` 提出、验证、接受/拒绝；
- 正式接受后升级 Working Method 版本并保留历史。

## 两条分支边界

- `main`：代码权威。
- `project-knowledge`：项目长期记忆与工作方法权威。

本知识分支当前工作树应为**纯知识树**，不得用这里的历史代码快照分析当前代码。

## 新窗口固定启动语

> 读取 GitHub 仓库 `jinxi1036-maker68/hermes-youyi-product` 的 `project-knowledge` 分支，从 `knowledge/00_START_HERE.md` 开始。先读取 PROJECT_INDEX.json，执行 Freshness Gate，再读取 CURRENT_STATE、ACTIVE_WORK 和当前 WORKING_METHOD，继续小U当前工作；代码只从 main 分支读取，不要从零重新设计，也不要自行改变当前工作方式。

## 安全

仓库当前是 public，因此本分支只允许 public-safe 项目知识。详见 `README_PUBLIC_SAFETY.md`。
