# 当前工作方式｜WM-007 Harness-Routed Role-Locked Governed Adaptive Method

> **机器权威：`knowledge/WORKING_METHOD.json`**

## 核心变化

WM-007 保留 WM-006 的所有角色、安全、Evidence 和生产纪律，只改变“Agent 如何进入项目和加载上下文”。

启动时不再预读多个大文件，而是：

```text
00_START_HERE
→ KNOWLEDGE_HARNESS
→ PROJECT_INDEX
→ CURRENT_WORK
→ 必要 Freshness Gate
→ context_profile.required + must_read
→ on_demand only when needed
```

## 角色边界

- ChatGPT：架构、规划、GitHub代码、测试、Evidence判断、Knowledge；
- Codex：服务器检查、验证、部署、经授权 restart/rollback；
- Owner：产品决策、必要生产授权、Class B 方法变更批准、最终真人验收。

Codex发现代码/架构 blocker 时仍按正常合同 STOP 并报事实，除非 Owner 对特定问题显式批准临时例外。

## Harness纪律

- Current 只保存现在；
- 关闭 work item 归档，不向 Current 追加历史；
- CURRENT_STATE / CURRENT_WORK Markdown 由机器事实生成；
- History / Evidence / Archive 默认不加载；
- Knowledge Sync 必须 render + validate；
- 实时事实仍高于知识记录。

## 方法治理

WM-007 是 Class A operational upgrade，不改变 Class B 边界。后续若要改变角色、安全、Evidence阈值、生产授权、权限或 Stage sealing，仍需 Owner 明确批准。
