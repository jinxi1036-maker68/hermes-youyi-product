# Release / Runtime / 生产切换知识

## 1. 长期拓扑

目标：

```text
Versioned Release Code
        +
Persistent Hermes Home
        +
Stable Config/Environment
        +
Institution Workspace
        +
Agenda Runtime/Business State
```

代码 release 与整个 Hermes persistent runtime state 必须解耦。

## 2. Runtime Topology V1

目标路径：

- release：版本化、不可变
- persistent Home：`/var/lib/hermes-youyi/hermes-home`
- stable config：`/etc/hermes-youyi/`
- Workspace：独立
- Agenda：独立

硬规则：
- `HERMES_HOME` 不得在 release 内；
- 不得通过 symlink 指向旧 release；
- candidate Python / console / hermes_cli / plugins 必须来自 candidate；
- sessions / cron / state.db / logs 跨 release 持久化；
- Workspace / Agenda 不迁入 Hermes Home；
- secrets 不进入 release。

## 3. 已解决的发布问题

### Candidate Home permission
曾出现 root preflight 污染 service runtime state。现要求触碰 Home 的检查以服务身份执行。

### Shared logs
曾出现 `agent.log` owner 不可写。修复原则是最小权限修复，不递归 chown。

### Cross-release Home
旧 production Home 内大量路径指向更老 release，证明 release code 与 persistent state 混在一起。Runtime Topology V1 为长期修复。

### Unix socket
`state/gateway.loop-tick.<pid>.sock` 属于 ephemeral process socket：
- inventory 记录
- 不复制
- 新进程重建
- 其它未知 special nodes 继续 fail-closed

### Hermes Core shadow
小U仓库的测试 shim 曾覆盖正式 `hermes_constants`。现规则：
- production overlay 只能使用 allowlisted XiaoU payload；
- 测试 shim 禁止覆盖 Core；
- Core compatibility gate 区分 hard required 与 guarded optional imports。

## 4. Safe Drain

PR #17 已建立 steady-state WeCom callback safe-drain：

Drain active 时：
- callback 继续可靠持久 claim；
- 继续 ACK；
- 不派发新的 model/business turn；
- processing 在 task schedule 前落盘；
- processing_count=0 稳定后才可 quiesced；
- backlog 由新进程 recover_pending 恢复；
- corrupt drain fail-closed。

但旧 production 588 不含 drain，因此第一次启用仍需要独立 bootstrap holding。

## 5. 回滚原则

首次 Runtime Topology 切换必须：
- 保留旧 production selector；
- 保留旧 canonical Home；
- 保留依赖的旧 releases；
- 新 Home 不反向覆盖旧 Home；
- 关键门禁失败立即恢复旧 selector + 旧 Home；
- 技术验收通过前不进行业务真人测试。

## 6. 发布判断必须看语义

不能把会自然变化的 runtime 文件 hash 当成业务污染。

需要看：
- 业务 JSON / config 是否意外变化
- ticket / work fact / reply / delivery 等语义是否异常
- import path 是否正确
- health / port / service 是否正确
- 权限和真实执行边界是否正确
