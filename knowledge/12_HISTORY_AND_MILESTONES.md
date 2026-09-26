# 历史与关键里程碑

> 这里只保存高价值历史，不保存所有聊天过程。

## 早期产品原则形成

- 确立“小U = 数字员工”，不是聊天机器人或固定流程。
- 确立“模型是唯一业务大脑，程序是能力与安全边界”。
- 确立 Institution Workspace 是业务事实载体。
- Agenda 用于持续工作，不等于第二业务脑。

## 自主工作阶段

曾真实证明：
- due item 可无人触发被发现；
- Hermes 可选择工具；
- 受保护写入；
- Receipt completed / writeback_verified；
- 主动企业微信只发送一次；
- 未完成工作可写 successor attention 并在未来重新回来。

但也暴露：
- restricted tool surface 过窄会只 reschedule 不推进；
- 中间 tool output 不能被误当成终态；
- 未完成工作不能静默退出。

## 身份能力封板

2026-09-23：
- Identity + Session 正式 PASS / sealed；
- stable tag：`youyi-stable-PASS-identity-authority-2026-09-23`。

## Query 阶段

### “今天未完成任务”
确立：today = `due_at` 本地日期等于 today；无日期不等于今天。

### Model-first
普通业务查询不再依赖关键词隐藏 Router 决定工具。

### Query evidence boundary
确立：
- task inventory/count/date facts 必须由 task query 支撑；
- active work context 仅作 continuation evidence。

## Release / Runtime Topology

连续部署失败后，根因从单个权限 bug 收敛为：

**release code 与 persistent Hermes runtime state 混在一起。**

由此建立 Runtime Topology V1：
- release immutable；
- persistent Home 外置；
- service identity preflight；
- self-contained gate；
- Core compatibility gate；
- migration plan/seed/finalize/verify；
- rollback 保留旧 Home。

## Unix socket blocker

真实 migration plan 曾被 `gateway.loop-tick.<pid>.sock` 阻塞。
最终确立：
- Unix socket 是 ephemeral；
- 记录但不迁；
- 其它未知 special node 继续 fail-closed。

## Core compatibility blocker

候选曾被仓库本地 `hermes_constants` 测试 shim 覆盖。
修复：
- production overlay allowlist；
- 测试 shim 禁止覆盖 Core；
- Core compatibility gate；
- guarded optional import 不误判成 hard dependency。

## Safe drain

PR #17：
- callback 继续 durable claim + ACK；
- drain 停止新模型派发；
- processing race 被封；
- cross-process recover_pending 验证通过。

## 当前历史终点（2026-09-26）

首次生产切换仍被 bootstrap ingress 阻塞：
- 当前 Cloud Hub 不是已证明的 durable holding/replay/idempotency 层；
- 需要受 Git 管理的正式 bootstrap holding 方案。


## Nginx bootstrap callback-only switchover

2026-09-26 只读审计确认：
- 80/443 的 exact `/wecom/callback` 当前走共享 `hermes_hub -> 127.0.0.1:19090`；
- 共享 upstream 同时服务 health 和 `/api/v1`，不能整体替换；
- callback location 可单独指向新的 loopback upstream，并通过 aa-nginx graceful reload 平滑切换和对称回切；
- Nginx 没有 durable queue、ACK-on-failure 或 replay，19090 不可用时预期 502。

因此 bootstrap blocker 从“入口能否安全切换”收敛为“Git-governed Holding Bridge V1 是否满足 ADR-009 并通过故障矩阵”。


## Holding Bridge V1 code merge and concurrency review

2026-09-26：
- PR #19 将 Git-governed Holding Bridge V1 合并到 main；
- POST 使用 durable stage before downstream attempt，具备 FORWARD / HOLD / FALLBACK、replay、transport fingerprint dedupe、crash recovery 与 offline HOLD control；
- GitHub Actions 对 Bridge + safe-drain 回归 23/23 PASS；
- 最终代码审查随后发现：新 staged row 会立即进入 background replay 的可选集合，可能在 direct FORWARD 等待下游响应时被 replay 同时转发；
- 因此未提前判定代码 PASS，先关闭该并发窗口并增加 overlap regression。


## Holding Bridge V1 code PASS

2026-09-26：
- PR #19 合并 Git-governed Holding Bridge V1；
- 最终审查发现 direct FORWARD / background replay 竞争窗口；
- PR #20 引入 durable direct ownership + explicit replay release/recovery，关闭正常路径并发双发；
- 强制 overlap regression 通过；
- current main `02c88bff5790110f6866b01031a7c9c75e0ded58` 上 Bridge + safe-drain regression 24/24 PASS；
- 代码层判定 PASS，但 production 仍未改变；下一步是服务器隔离验证，而不是直接切流。
