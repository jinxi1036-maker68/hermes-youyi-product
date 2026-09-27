# WM-007｜Harness-Routed Role-Locked Governed Adaptive Method

- Status: Active
- Effective: 2026-09-27
- Change class: Class A operational
- Parent: WM-006
- Harness: KH-003

## Why

WM-006 的角色、安全、Evidence 和生产纪律有效，但启动步骤要求预读 CURRENT_STATE、ACTIVE_WORK、完整 Working Method。随着知识增长，这会绕过 progressive disclosure，并把过期投影带入上下文。

KH-003 已把 PROJECT_INDEX / CURRENT_WORK 设为机器当前事实，并用 task profile 路由后续知识，因此工作方式必须与 Harness 对齐。

## Change

开工顺序改为：

```text
00_START_HERE
 -> KNOWLEDGE_HARNESS
 -> PROJECT_INDEX
 -> CURRENT_WORK
 -> task-appropriate Freshness Gate
 -> CURRENT_WORK.context_profile
 -> required + must_read
 -> on_demand only when needed
```

Current State / Current Work Markdown 是人类投影，不再是 Agent 开工必读。
History / Evidence / Archive 不默认加载。

Knowledge Sync 改为：
- 关闭 work item 先归档；
- 替换 CURRENT_WORK；
- render projections；
- Validator PASS；
- 再声明同步完成。

## Preserved from WM-006

不改变：
- Owner / ChatGPT / Codex 角色边界；
- Evidence 分层；
- 生产授权与 rollback；
- Stage sealing；
- Freshness；
- optimistic concurrency；
- Class B 变更仍需 Owner 明确批准。

因此 WM-007 是效率/一致性升级，不降低任何安全门槛。
