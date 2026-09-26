# ADR-009｜Bootstrap Holding 三段式可靠交付契约

- Status: Accepted
- Date: 2026-09-26
- Scope: WeCom ingress / first Runtime Topology cutover
- Owner decision: Confirmed

## Context

当前 production Gateway 仍是旧版本，没有 safe-drain。

现有 callback 链：

```text
WeCom
 -> aa-nginx
 -> Cloud Hub
 -> Gateway:8866
```

Cloud Hub 当前只能完成正常直转发。它的 fallback SQLite queue 不会在 direct-forward 失败时自动接住 callback，也没有可靠 replay 和消息幂等，因此不能证明首次生产切换期间不丢消息。

## Decision

正式 bootstrap holding layer 必须满足三段式契约。

### 1. Gateway 正常：透明转发

- 正常 callback 透明送达 `8866`；
- 不改变业务含义；
- 不产生重复业务执行。

### 2. Gateway drain / paused / unavailable：persist before ACK

- callback 先 durable persist；
- durable commit 成功后才能向 WeCom ACK success；
- persist 失败不得伪造成功；
- holding state 必须跨自身进程重启存在。

### 3. Gateway 恢复：idempotent replay, complete after confirmed success

- 使用稳定消息唯一标识做幂等 replay；
- replay 可以安全重试；
- Gateway 未确认成功时保持 pending；
- Gateway 确认成功后才标记 completed；
- dispatch / queued / attempted 均不等于 completed。

## Why

首次 Runtime Topology cutover 的真正风险不是“Gateway能不能重新启动”，而是：

> 在旧 Gateway 暂停到新 Gateway真正ready之间，企业微信 callback是否能被可靠接住且最终只产生一次业务效果。

因此该层的正确抽象不是 reverse proxy，而是一个最小的 **durable ingress delivery boundary**。

## Required Failure Semantics

实现必须证明至少：

1. Gateway 在线；
2. Gateway 预先不可用；
3. 转发时 transport timeout/failure；
4. persist 后 holding process crash/restart；
5. Gateway恢复后 replay；
6. duplicate replay；
7. Gateway已执行但确认响应丢失；
8. durable store写失败；
9. backlog跨切换保持完整。

## Rejected Semantics

不接受：
- 只依赖 502/503 和 WeCom上游重试；
- ACK before durable persist；
- 内存队列；
- replay without stable idempotency key；
- dispatch即completed；
- 仅happy-path测试。

## Consequences

- 后续 Nginx / bridge / holding 实现必须服从该契约；
- 具体技术实现可以变化，但上述语义不得为了方便部署而削弱；
- 如果实现需要改变该契约，属于 Working Method/安全边界以外的重大架构变更，必须重新获得 Owner明确决定。
