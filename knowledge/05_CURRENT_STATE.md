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
- production-gate 设计审查随后发现 Bridge state 若位于 target `HERMES_HOME/state`，会与 runtime-home finalize 的 prune 语义冲突；
- PR #21 已将 Bridge durability 独立到 Gateway Home 之外的版本中立 state root，并增加 finalize 组合回归；
- 当前 main：`37a16b94ecb8ed62930ab8c353660780f8712359`；
- main GitHub Actions：Holding Bridge + safe-drain + runtime-home migration regression **37/37 PASS**。

原 main `02c88...` 的服务器隔离验证已经 PASS，但 PR #21 改变了 Holding Bridge 的 durable state boundary，因此旧服务器验证不能自动覆盖这项新边界。

Holding Bridge 的服务器边界已经连续通过：
- dedicated state root provisioning PASS；
- exact-main stage-only PASS；
- active Gateway links unchanged PASS；
- formal private Bridge unit / health PASS；
- FORWARD / HOLD / FALLBACK / restart recovery PASS；
- legacy Hermes plugin shadow 已由 exact-file module load 修复并在真实服务器复验 PASS。

当前 public callback 仍然没有切流，Gateway production SHA 仍是旧稳定版本。

因此下一道独立门禁是 **callback-only Nginx cutover**：只把 80/443 的 exact `/wecom/callback` 从旧 `hermes_hub -> 19090` 路径切到 `127.0.0.1:19091` Holding Bridge，并保留对称 rollback。该门禁绝不同时停 Gateway、finalize Home 或切 selector。

callback-only gate PASS 后，才进入 Gateway/Home/selector 的正式生产切换；在后者技术 PASS 前，不进行 Owner 企业微信真人验收。

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
