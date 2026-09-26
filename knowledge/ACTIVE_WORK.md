# ACTIVE WORK｜当前唯一工作项

work_item_id: **WI-2026-09-bootstrap-durable-ingress**

> work item id、blocker status 和 next action 的机器权威在 `PROJECT_INDEX.json`。  
> 本文件保存“为什么、查到什么、否掉什么、接下来具体怎么继续”。

## 目标

在**不丢企业微信 callback、不制造重复业务执行、不裸停旧 Gateway**的前提下，完成第一次 Runtime Topology 生产切换。

## 为什么现在卡在这里

新 main 已经具备 steady-state safe-drain，但当前 production 仍运行旧版本，旧 Gateway 没有 drain 能力。

因此存在 bootstrap 悖论：

1. 要安全停 Gateway，最好先有 drain；
2. 要让旧 Gateway 获得 drain，通常又需要先重启/切版本；
3. 直接重启恰恰是当前不能证明安全的动作。

## 已经证明的事实

### Safe-drain 本身
PR #17 隔离验证 PASS：
- drain 时 callback 继续 durable claim；
- 继续 ACK；
- 不派发新 model/business turn；
- processing 状态在任务调度前写入；
- processing_count=0 才能判断 quiesced；
- backlog 可由新进程 recover_pending；
- corrupt drain fail-closed。

### 当前生产入口
真实链路：

```text
public HTTPS
 -> aa-nginx:443
 -> hermes-cloud-hub:127.0.0.1:19090
 -> Gateway WeCom callback:127.0.0.1:8866
```

### Cloud Hub
只读审计已证明：
- 是服务器独立本地 Python 脚本；
- 不在当前 Git repo；
- active direct-forward 失败时不会自动进入 fallback SQLite queue；
- 没有可靠 replay-to-Gateway；
- 没有 message-id/idempotency/dedupe；
- 因此不能把现有 Cloud Hub 直接当作安全 bootstrap holding。

## 已明确否定

1. **裸重启 588 Gateway 来“先装 drain”**  
   原因：没有独立 holding 时无法证明 callback 不丢。

2. **直接使用现有 Cloud Hub fallback queue**  
   原因：direct-forward transport failure 不进入该队列，且缺 replay / idempotency。

3. **直接在服务器 /opt/hermes-cloud-hub/hub.py 上临时打补丁**  
   原因：游离代码、无 Git 治理、变更归属不清，不符合正式产品发布制度。

## Bootstrap Holding 冻结验收契约

这是 Owner 已确认的正式契约。后续设计、实现、Codex验证和生产切换不得降低为“普通反向代理 + 尽量重试”。

### 1. Gateway 正常

- callback 透明转发至 Gateway `8866`；
- 不改变正常业务语义；
- 不制造重复处理；
- holding 层不能成为第二业务脑。

### 2. Gateway drain / 暂停 / 不可用

- callback 必须**先可靠持久化**；
- 只有 durable commit 成功后，才能向企业微信返回成功 ACK；
- 如果持久化失败，不得伪造成功 ACK；
- Gateway 不可用期间不得因为直转发失败而丢弃 callback；
- holding 状态必须可跨进程/服务重启恢复。

### 3. Gateway 恢复

- 按 callback 的**稳定消息唯一标识**进行幂等 replay；
- 同一业务消息不得因为重试产生重复模型/业务执行；
- replay 失败必须保持待处理状态，可继续安全重试；
- **只有 Gateway 已确认该消息处理成功后**，holding 层才允许将该消息标记 completed；
- “已经发起重投递”不等于“完成”。

### 必须证明的故障语义

正式 PASS 至少要覆盖：

- Gateway 在线正常转发；
- Gateway 在 callback 到达前不可用；
- Gateway 在转发过程中失败/超时；
- durable persist 成功后 holding 自身重启；
- Gateway 恢复后的 replay；
- replay 重复触发；
- Gateway 已处理但上游响应丢失/超时导致的重复重投递；
- durable store 写失败；
- backlog 在切换前后不丢、不静默跳过。

### 明确不接受

- 失败后只返回 502/503 依赖上游“也许会重试”；
- 先 ACK 再异步落盘；
- 只做内存队列；
- replay 没有稳定 message id / idempotency；
- 一旦发给 Gateway 就立即标记完成；
- 只验证 happy path，不验证 crash / timeout / duplicate path。

## 当前执行中

已向 Codex 发出只读审计：

- command: `XIAOU_NGINX_BOOTSTRAP_SWITCHOVER_AUDIT_V1`
- PR #17 comment: `5842332487`
- 目标：确认 aa-nginx 当前 callback location、真实 upstream、graceful reload / local upstream swap / rollback 是否可行。
- 本轮严格只读：不改配置、不 reload、不 restart、不创建 bridge。

**当前状态：等待审计结果。**

审计回来后：
- 若本地 upstream graceful swap 可行，由 ChatGPT 基于事实直接设计 Git-governed holding bridge；
- 若存在结构 blocker，先处理该 blocker，不猜配置、不进入生产变更。

## 当前下一步

恢复主线后：

1. **先做 aa-nginx callback location 的只读审计**：
   - 当前 /wecom/callback location；
   - upstream 指向；
   - 是否支持在 Gateway 在线期间通过 graceful reload 无中断切换到受控 bridge；
   - 不 reload、不改配置。

2. 基于真实 Nginx 事实，由 ChatGPT 在 GitHub 设计一个受治理的 bootstrap holding layer。

3. 该 layer 必须先证明：
   - durable receive；
   - ACK；
   - persist；
   - replay；
   - duplicate control；
   - Gateway 正常时透明转发；
   - Gateway 切换期间不丢 callback。

4. 只有 bootstrap layer PASS 后，才重新进入 Runtime Topology production finalize/cutover。

## Stop Rules

在 bootstrap holding 未证明之前：
- 不停止旧 Gateway；
- 不 finalize persistent Home；
- 不切 HERMES_HOME；
- 不切 production selector；
- 不做 Owner Query 真人验收；
- 不修改游离 Cloud Hub 生产脚本。

## 完成条件

该 work item 只有在“首次安全切换能力已证明并可执行”后才可关闭。关闭时必须同步：
- PROJECT_INDEX
- CURRENT_STATE
- EVIDENCE_INDEX
- HISTORY / ADR（如形成长期架构决策）
