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

## 5. Bootstrap Holding 独立状态边界

第一次 Runtime Topology 切换期间，Holding Bridge 的 durability 不能属于 Gateway persistent Home。

固定边界：

```text
/var/lib/hermes-youyi/hermes-home     # Gateway persistent HERMES_HOME
/var/lib/hermes-youyi/wecom-holding  # Holding Bridge durable state
```

原因：
- first-cutover runtime-home `finalize` 会清理 target Home 中不属于 source inventory 的 target-only state；
- 如果 Bridge SQLite / HOLD marker 位于 `HERMES_HOME/state`，可能在保护 callback 的窗口被 finalize prune；
- 因此 Bridge DB、WAL/SHM、HOLD marker 必须位于 Gateway Home 外部；
- Bridge service writable boundary 只授权 dedicated state root；
- Gateway Home migration/finalize 不复制、不 prune、不 reverse-sync Bridge state。

PR #21 已用组合回归证明：target Home finalize 时，外部 Bridge pending backlog 与 HOLD marker 保持不变。

## 6. 回滚原则

首次 Runtime Topology 切换必须：
- 保留旧 production selector；
- 保留旧 canonical Home；
- 保留依赖的旧 releases；
- 新 Home 不反向覆盖旧 Home；
- 关键门禁失败立即恢复旧 selector + 旧 Home；
- 技术验收通过前不进行业务真人测试。

## 7. 发布判断必须看语义

不能把会自然变化的 runtime 文件 hash 当成业务污染。

需要看：
- 业务 JSON / config 是否意外变化
- ticket / work fact / reply / delivery 等语义是否异常
- import path 是否正确
- health / port / service 是否正确
- 权限和真实执行边界是否正确


## 8. Callback-only Nginx cutover gate

Private Bridge PASS 后，公网入口切换必须单独成门禁，不得和 Gateway/Home/selector 切换混在一次操作中。

前置条件：
- formal Bridge active + healthy；
- Bridge mode = FORWARD；
- pending backlog = 0；
- loopback listener = `127.0.0.1:19091`；
- current Gateway / Cloud Hub healthy；
- 当前 exact callback locations 与 rollback 备份已记录。

允许变化：
- 仅 80/443 的 exact `location = /wecom/callback` 指向 Bridge loopback；
- 使用 aa-nginx 正式 config test + graceful reload。

禁止：
- 修改共享 `hermes_hub -> 127.0.0.1:19090`；
- 修改 health / `/api/v1`；
- 停/重启 Gateway 或 Cloud Hub；
- finalize runtime Home；
- 切 HERMES_HOME / production selector；
- 发送真实企业微信 callback。

验证：
- reload 后配置实际生效；
- synthetic GET 通过 Nginx callback path 与 direct Bridge 的状态/响应语义一致；
- 无 Nginx upstream 502/503；
- Bridge 继续 active/healthy；
- Gateway/Cloud Hub 继续健康；
- pending backlog 保持 0（synthetic GET 不入队）。

失败时：
- 立即恢复 exact callback 配置；
- 再次 `aa_nginx -t`；
- graceful reload；
- 确认 public callback 回到旧链后 STOP。
