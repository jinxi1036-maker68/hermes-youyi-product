# WM-006｜Role-Locked Concurrency-Safe Governed Adaptive Method

- Status: Current
- Effective: 2026-09-26
- Supersedes: WM-005
- Change class: Class B
- Owner approval: Explicitly confirmed in conversation on 2026-09-26

## 触发原因

WM-005 已写明三方职责，但实际执行仍出现角色漂移：

1. 把 PR comment 中已经准备好的 Codex 指令误表述成 Codex 已收到/执行；
2. 给 Codex 的 brief 一度把“重新设计/修代码”混入服务器验证职责；
3. Owner再次明确：ChatGPT负责规划和GitHub修改，Codex负责检查/验证/部署，Owner负责企业微信真人测试。

## WM-006 新增

### 1. 角色锁定合同
新增 `ROLE_AND_HANDOFF_CONTRACT.md`，把角色边界提升为新窗口启动必读。

### 2. Blocker 回流规则
Codex发现代码/架构 blocker 时：
- STOP；
- 回传事实；
- ChatGPT改GitHub；
- Codex复验。

### 3. 执行状态真实性
准备好的 brief、PR comment、command id 只能证明“任务已准备”，不能证明 Codex 已接收、执行或 PASS。

### 4. Owner 真人验收顺序
Owner只在 ChatGPT确认 production technical gate PASS 后进行企业微信真实小U验收。

## 保留
WM-005 的乐观并发、Freshness Gate、安全、证据分层、Class A/B治理、Stop Rule 全部继续有效。
