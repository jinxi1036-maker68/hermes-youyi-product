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

第一次切换存在 bootstrap 悖论：

- 新代码已经有 safe-drain；
- 旧 production Gateway 还没有 safe-drain；
- 不能为了“先装 drain”而裸重启旧 Gateway；
- 现有 Cloud Hub 虽有 fallback SQLite queue，但 direct-forward 失败不会自动 durable hold，也没有可靠 replay / idempotency，因此不能作为已证明的安全 bootstrap layer。

因此当前需要的是一个由事实驱动、受 Git 治理、可以证明 **receive + ACK + persist + replay + duplicate control** 的首次切换方案。

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
