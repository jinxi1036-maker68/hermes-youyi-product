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

## 最新只读事实：Nginx bootstrap switchover audit PASS

PR #17 audit 回传：`5842404675`。

已确认：

- aa-nginx `1.22.1`，正式 reload 为 `aa_nginx -s reload`，支持 graceful worker replacement；
- 80/443 都有 exact `location = /wecom/callback`；
- 两个 callback location 当前都走共享 `hermes_hub -> 127.0.0.1:19090`；
- `hermes_hub` 同时被 callback、health、`/api/v1` 使用，因此 **不得修改共享 upstream member**；
- callback 可改为独立 loopback target 后 graceful reload，且可对称回切；
- callback 是普通短 HTTP GET/POST，请求 URI/query/body 可透明转发，上游 response status/body 原样返回；
- Nginx 没有 durable buffer、失败 ACK、queue 或 replay；19090 不可用时预期是普通 upstream 502；
- production SHA 仍为 `588ea6eecb1833159e886181f3259be6e0befe37`，当前链路健康。

## 当前执行中

Holding Bridge V1 代码层已 PASS。

最终代码状态：

- PR #19：Holding Bridge V1 核心实现；
- PR #20：关闭 direct FORWARD / background replay concurrency race；
- current main：`02c88bff5790110f6866b01031a7c9c75e0ded58`；
- main CI run：`36221515003`；
- Bridge + safe-drain regression：**24/24 PASS**；
- runtime control scripts py_compile PASS。

已证明的代码语义：

- GET `/wecom/callback` 只透明转发、不入队；
- POST 在任何 downstream attempt 前 durable stage；
- FORWARD 正常透传；
- HOLD 在 durable commit 后 ACK，且不下发；
- FALLBACK 在 transport timeout/error 或 transient upstream failure 时保留 durable pending 后 ACK；
- durable store 失败不转发、不伪造成功 ACK；
- replay 仅在 downstream 2xx 后 completed；
- process restart 恢复 unfinished direct/replaying rows；
- transport fingerprint duplicate control；
- Gateway 已处理但 response lost 时，由 Gateway `WecomInboundReceiptStore` 的业务 message-id 去重保证业务效果一次；
- direct FORWARD in-flight 时拥有 durable row，background replay 不可并发 claim；强制 overlap regression 已通过；
- completed 后清理 raw path/header/body，只保留 transport tombstone。

## Server isolated verification｜PASS

2026-09-26，Owner 手动转交 ChatGPT 准备的 Codex brief 后，Codex 完成服务器隔离验证；结果由 Owner 原样回传，并由 ChatGPT 记录到 PR #20 comment `5843978547`。

已确认：

- `production_changed=false`；
- MAIN_SHA 仍为 `02c88bff5790110f6866b01031a7c9c75e0ded58`；
- PRODUCTION_SHA 仍为 `588ea6eecb1833159e886181f3259be6e0befe37`；
- 当前 Nginx → Cloud Hub → Gateway 链路健康；
- Python 3.11.13、aiohttp 3.14.3、httpx 0.28.1、SQLite 3.26.0 可用；
- service identity 对 Holding Bridge 状态目录写权限 PASS；
- loopback `19091` 空闲、可 bind，测试后已释放；
- Holding Bridge 专项测试 **16/16 PASS**；
- FORWARD / HOLD / FALLBACK / persist-before-ACK / durable-store-failure fail-closed / crash recovery / replay / duplicate control / direct-replay ownership / backlog restart recovery / corrupt-HOLD fail-closed 全部 PASS；
- 临时 Bridge `19091` → 临时 mock upstream `29192` 的真实 loopback 冒烟 PASS；
- 未修改 GitHub code、Nginx、production config、HERMES_HOME、selector、production data；未重启生产服务；未发送真实企业微信 callback。

结论：**PASS_SERVER_ISOLATED**。当前已没有服务器隔离层 blocker。

## 当前下一步

由 ChatGPT 设计下一道 **controlled production technical deployment/cutover gate**。

这一步必须先冻结：
- 生产前置条件；
- Bridge 正式安装/启动边界；
- callback-only Nginx 切换方式；
- rollback；
- 切换后健康与 backlog/receipt 验证；
- 哪些条件一旦失败立即回滚。

只有该技术门禁由 Codex 执行并经 ChatGPT 判定 PASS 后，才轮到 Owner 在企业微信进行真实 Query 验收。

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


## Production gate design review｜new blocker

在设计 controlled production cutover 时发现一个 ADR-009 级别风险：

- Holding Bridge 当前默认 DB / HOLD marker 位于 target persistent `HERMES_HOME/state`；
- 首次切换仍需在旧 Gateway 停止后执行 runtime-home `finalize`；
- 现有 finalize 会 prune target Home 中不属于旧 source Home inventory 的额外文件；
- 因此如果 Bridge 已经开始接收/holding callback，finalize 可能把 live holding DB / marker 当作 target-only state 清理；
- 这会直接破坏“persist-before-ACK / backlog survive cutover”的冻结契约。

当前结论：**不能直接进入生产切换。**

下一步由 ChatGPT 在 GitHub 修复：
1. 将 Holding Bridge durable state 与 Gateway persistent Hermes Home 解耦；
2. 保持 Bridge state 为版本中立、持久、service-identity 可写；
3. 增加组合回归，证明 runtime-home finalize 不会删除 Bridge backlog / HOLD control；
4. CI PASS 后再交 Codex 做服务器边界复验；
5. 复验 PASS 后重新冻结正式 production cutover gate。


## Holding state / Runtime Home isolation｜code PASS

PR #21 已修复 production-gate design review 发现的 state collision：

- Holding Bridge DB / HOLD marker 不再默认位于 Gateway `HERMES_HOME/state`；
- 正式边界改为版本中立的独立 sibling state root；
- production template 使用 `XIAOYOU_WECOM_HOLDING_STATE_ROOT=/var/lib/hermes-youyi/wecom-holding`；
- Gateway persistent Home 仍是 `/var/lib/hermes-youyi/hermes-home`；
- Holding Bridge systemd template 的 `ReadWritePaths` 只指向独立 Bridge state root，不再授予 Gateway Home 写权限；
- HOLD control script 与 Bridge 使用同一个 state-root 规则；
- 新增组合回归：Bridge pending backlog + HOLD marker 已存在时执行 runtime-home finalize，Bridge DB/marker 不被 prune，pending 仍保留；
- 将 runtime-home migration regression 纳入 Holding Bridge CI；
- GitHub Runner 旧 Unix socket fixture 的 AF_UNIX path-too-long 被识别为 test harness 问题并用短 `/tmp` fixture 修复，没有改变产品语义。

最终：
- PR #21 merged；
- main：`37a16b94ecb8ed62930ab8c353660780f8712359`；
- main Actions run：`36225126717`；
- **37/37 PASS**；
- evidence：`EV-RT-009 = PASS_CODE`；
- production 未改变。

### 当前唯一下一步

让 Codex 只做真实服务器边界复验：

1. 确认 live main 与 production SHA；
2. 确认独立 Bridge state root 实际位于 Gateway `HERMES_HOME` 外；
3. 确认真实 service identity 能创建/写 SQLite、WAL/SHM、HOLD marker；
4. 确认 Bridge systemd writable boundary 可只授权该独立目录；
5. 在隔离/临时数据下证明 runtime-home finalize 不会删除或改写该目录中的 pending backlog / HOLD marker；
6. 不改 Nginx、不切 callback、不停 Gateway、不切 Home/selector、不发送真实 callback。

复验 PASS 后，由 ChatGPT 再冻结 controlled production deployment/cutover gate。


## Server re-verification after PR #21｜BLOCKED on provisioning only

Codex 回传：

- `production_changed=false`
- MAIN_SHA = `37a16b94ecb8ed62930ab8c353660780f8712359`
- PRODUCTION_SHA = `588ea6eecb1833159e886181f3259be6e0befe37`
- STATE_ROOT_BOUNDARY = FAIL
- SERVICE_WRITE_BOUNDARY = FAIL
- FINALIZE_COMPOSITION = PASS
- TARGETED_TESTS = PASS
- blocker：`/var/lib/hermes-youyi/wecom-holding` 不存在，父目录 root:root 0755，服务用户无法自行创建该 state root。

判断：

- 这不是 ADR-009 / Holding Bridge 语义失败；
- PR #21 的“Bridge state 独立于 Gateway Home”方向正确；
- 当前唯一缺口是部署阶段没有一个正式的、Git-governed 的 state-root provisioning gate；
- 不允许让服务用户提升权限或自行创建 `/var/lib/hermes-youyi` 下的新目录；
- 正确边界是：部署期 root 仅创建/修正 dedicated Bridge state root 的 owner/group/mode；运行期 Bridge 继续以 service identity 写入该目录。

下一步：ChatGPT 直接在 GitHub 增加 fail-closed provisioning/verification 工具和测试；production 保持不变。
