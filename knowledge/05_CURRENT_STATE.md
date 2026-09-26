# 当前状态｜PROJECT_INDEX 的人类可读投影

> **动态事实唯一权威：`knowledge/PROJECT_INDEX.json`。**  
> 本文件用于解释，不得作为独立事实源；发现不一致时以 PROJECT_INDEX + 实时核验为准并立即同步修正。

## 当前阶段解释

当前主线仍处于正式产品的 **Query 阶段**。Query 业务逻辑已接近最终真人验收，但生产仍运行旧稳定版本，因此“main 已实现”不能等价成“production 已部署”。

## 当前生产切换解释

Runtime Topology V1 已完成大量基础能力，包括：
- persistent Hermes Home；
- migration plan / seed / verify；
- service-identity preflight；
- self-contained gate；
- Core compatibility gate；
- production overlay policy；
- ephemeral Unix socket 迁移语义；
- WeCom callback safe-drain。

persistent target Home 已 seed，但尚未 finalize 和切 production runtime binding。

## 当前阻塞的本质

第一次切换的入口与代码语义已经收敛：

- aa-nginx 的 exact `/wecom/callback` 可以单独 graceful reload 到新的 loopback target，并可对称回切；
- 共享 `hermes_hub -> 127.0.0.1:19090` 不能修改；
- Holding Bridge V1 已通过 PR #19 合并；
- 最终代码审查发现 direct FORWARD / background replay 竞争窗口；
- PR #20 已用 durable direct ownership + replay release/recovery 关闭该窗口；
- 当前 main：`02c88bff5790110f6866b01031a7c9c75e0ded58`；
- main GitHub Actions：Bridge + safe-drain regression **24/24 PASS**，包含强制 overlap 测试。

服务器隔离验证现已 PASS：真实服务器运行环境可承载 Holding Bridge，service identity、SQLite durable state、loopback 端口与隔离故障矩阵均已证明，且 production 未发生任何变化。

因此当前已从“server-isolated verification”推进到下一层：**由 ChatGPT 设计并冻结 controlled production technical deployment/cutover gate，再由 Codex 按门禁执行正式部署与技术验证。** 在该门禁 PASS 前，不进行 Owner 企业微信真人验收。

## Stage 2 最终验收

只有生产安全切到包含 Query 修复的新版本后，才由 Owner 在企业微信执行固定真人测试：

`今天还有哪些事情没处理完？`

通过后才封板 Stage 2 并进入 Stage 3。

## 当前不要并行混入

- `/new` 技术信息泄漏；
- Student authority 全迁移；
- Query focus 严格零写；
- create_task 既有 NameError；
- H5；
- Learning；
- Voice / Hardware；
- 其它不影响当前首次安全切换的历史技术债。

精确 SHA、时间、blocker id、next action 请读取 PROJECT_INDEX.json。
