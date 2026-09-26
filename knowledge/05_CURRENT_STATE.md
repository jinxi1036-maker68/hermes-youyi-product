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

第一次切换的 bootstrap 悖论仍存在，但 Nginx 切换边界已经只读审计清楚：

- 新代码已经有 safe-drain；
- 旧 production Gateway 还没有 safe-drain；
- aa-nginx 的 `/wecom/callback` 可单独 graceful reload 到新的 loopback upstream，并可对称回切；
- 共享 `hermes_hub -> 127.0.0.1:19090` 同时承载 callback、health 和 `/api/v1`，不能通过修改共享 upstream 来做切换；
- Nginx 本身没有 durable queue、ACK-on-upstream-failure 或 replay；
- 现有 Cloud Hub 的 direct-forward 失败不会自动 durable hold，也没有可靠 replay / idempotency。

Holding Bridge V1 已通过 PR #19 合并到 main，主线 SHA 为 `67ea2abfc4225e51dcf2889241c24718feb048c2`，GitHub CI 对 Bridge 故障矩阵 + safe-drain 回归为 23/23 PASS。

但合并后的最终代码审查又发现一个未被测试覆盖的并发窗口：POST durable stage 后立即成为可 replay 的 `pending`；正常 direct FORWARD 在 await 下游响应期间，后台 replay loop 可能同时选中同一行再次转发。Gateway 的业务 message-id 去重能够降低业务副作用，但这仍违反“Gateway 正常时不制造重复 transport delivery”的冻结契约。

因此当前 blocker 已进一步收敛为：**关闭 direct FORWARD 与 background replay 的并发竞争窗口，并用强制 overlap 回归证明正常路径只发一次。**

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
