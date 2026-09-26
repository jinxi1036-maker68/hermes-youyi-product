# 小U项目知识库｜唯一入口

> 本分支是小U项目的长期 Project Knowledge。  
> **不要依赖聊天记忆作为权威。不要从零重新设计。不要在新窗口自行改变当前工作方式。**

## 固定启动顺序

任何新 ChatGPT / Codex / 协作者开始工作前：

1. 读取 **`knowledge/PROJECT_INDEX.json`** —— 当前动态事实唯一权威。
2. 执行 **Freshness Gate** —— 代码工作核对 live `main`；生产工作再核对实际 production。
3. 读取 **`knowledge/ROLE_AND_HANDOFF_CONTRACT.md`** —— 先锁定 Owner / ChatGPT / Codex 的不可漂移职责边界。
4. 读取 **`knowledge/05_CURRENT_STATE.md`** —— 当前状态解释。
5. 读取 **`knowledge/ACTIVE_WORK.md`** —— 接着上一窗口最后一项工作。
6. 读取 **`knowledge/WORKING_METHOD.json`** + **`knowledge/CURRENT_WORKING_METHOD.md`** —— 继承当前工作方式。
7. 读取 **`knowledge/04_CAPABILITY_MAP.md`** —— 正式能力路线。
8. 按当前任务再读取 Architecture、Domain Model、Evidence、ADR、Runtime、Rejected Approaches、Method History。

不要把整个知识库一次性塞入上下文。先定位，再按需检索。

## 权威顺序

1. 用户最新明确决定；
2. 实时可验证事实（GitHub main / 生产服务器）；
3. `PROJECT_INDEX.json`；
4. `WORKING_METHOD.json`（当前工作方式）；
5. 已封板 Evidence / stable tag / ADR；
6. 其它知识文档；
7. 旧交接文档、聊天总结和模型记忆。

实时事实与 Project Knowledge 冲突时，**先刷新 Knowledge，再继续实现**。

## 两条分支边界

- `main`：当前代码权威；
- `project-knowledge`：项目长期记忆与工作方法权威。

本知识分支当前工作树只保存知识。分析当前代码必须读取 `main`。

## 更新知识前

材料性 Knowledge 写入必须先读：

`knowledge/CONCURRENCY_AND_RECOVERY.md`

多个聊天窗口并行时，**禁止 force push 覆盖其它窗口的知识变化**。

## 固定角色链路｜不得静默漂移

任何新窗口都必须遵守：

```text
ChatGPT：规划 / 架构 / GitHub代码 / 测试设计 / PR与证据判断 / Knowledge
  ↓
Codex：服务器检查 / 验证 / 部署 / 经授权的重启与回滚 / 事实回传
  ↓
ChatGPT：判断 PASS / FAIL / BLOCKED，决定下一步
  ↓
Owner：技术门禁通过后，在企业微信真实测试小U
```

Codex发现代码或架构问题时，**STOP并报告事实**；由ChatGPT在GitHub修复，再交Codex复验。  
PR comment、任务文档或准备好的 brief **不等于 Codex 已收到或已执行**。

详见：`knowledge/ROLE_AND_HANDOFF_CONTRACT.md`。

## 工作方式也不能静默改变

如果当前方法出现问题：
- 按 Method Learning Loop 形成候选变更；
- Class A 可由 ChatGPT 在有证据的有限验证后升级；
- Class B（角色、安全、Evidence、生产授权、权限/权威、Stage封板）必须 Owner 明确批准。

## 新窗口固定启动语

> 读取 GitHub 仓库 `jinxi1036-maker68/hermes-youyi-product` 的 `project-knowledge` 分支，从 `knowledge/00_START_HERE.md` 开始。先读取 PROJECT_INDEX.json，执行 Freshness Gate，再读取 CURRENT_STATE、ACTIVE_WORK 和当前 WORKING_METHOD，继续小U当前工作；代码只从 main 分支读取，不要从零重新设计，也不要自行改变当前工作方式。

## 安全

当前仓库是 public。本分支只能保存 public-safe 项目连续性知识，不能保存全部内部敏感知识。详见 `README_PUBLIC_SAFETY.md`。
