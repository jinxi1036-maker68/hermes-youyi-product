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
