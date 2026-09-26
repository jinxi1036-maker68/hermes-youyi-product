# Project Knowledge 更新协议

## 1. 动态事实唯一权威

`knowledge/PROJECT_INDEX.json` 是当前 Stage、关键 SHA、blocker、next action 的机器权威。

CURRENT_STATE 是解释性投影；START_HERE、Roadmap、handoff 不得独立维护动态答案。

## 2. 当前工作方式唯一权威

`knowledge/WORKING_METHOD.json` 是当前 Working Method 权威。

`CURRENT_WORKING_METHOD.md` 是人类可读投影。

新窗口不得静默改变工作节奏、职责、Evidence或生产纪律。

## 3. 并发写入

材料性更新必须使用 optimistic concurrency：

1. 记录开始时 `project-knowledge` HEAD = K0；
2. 准备更新；
3. 写前再读 HEAD = K1；
4. K1 == K0：parent=K0，fast-forward，`force=false`；
5. K1 != K0：STOP，读取并语义合并另一窗口变化，再重新校验；
6. 永远禁止用 force push 解决知识冲突。

详见 `CONCURRENCY_AND_RECOVERY.md`。

## 4. ACTIVE_WORK

以下变化必须同步：
- work item；
- 第一真实 blocker；
- 已否定方案；
- next single action；
- stop rules。

## 5. Evidence

重要 PASS / FAIL / BLOCKED 应记录：
- area；
- judgment；
- PR/commit/tag（如有）；
- verification comment（如有）；
- 简明证据。

sealed capability 必须有 Evidence id。

## 6. Working Method Learning

出现重复失败、协作摩擦、无价值等待、新工具能力或项目阶段变化时，可以提出方法优化。

### Class A
不改变角色、安全、Evidence阈值、生产授权、权限/权威、Stage封板。  
可由 ChatGPT 在证据支持下升级并记录。

### Class B
改变上述任一关键边界。  
**必须 Owner 明确批准后才能永久激活。**

临时更严格 Stop Rule 可以先用于防风险，但不能自动变成永久制度。

## 7. Freshness

代码工作：核对 live main。

生产工作：同轮做必要只读 production verification。

实时事实冲突时，先刷新 Project Knowledge。

## 8. ADR

以下长期变化写 ADR：
- 架构边界；
- 权威来源；
- 安全语义；
- 模型/程序职责；
- 长期方案选择；
- Knowledge / Working Method治理变化。

## 9. Capability Archive

能力开始建立 capability file；封板补齐：
- stable semantics；
- Evidence；
- regression；
- reopen conditions。

## 10. Definition of Done

```text
代码/设计完成
+ 必要隔离验证
+ 必要生产验证
+ 必要真人验收
+ Project Knowledge Sync
+ 如方法发生变化则 Working Method Sync
= Done
```

## 11. 必须同步的事件

- Stage状态改变；
- main关键SHA改变；
- production改变；
- blocker / next action改变；
- 架构/权威/安全决策；
- stable tag / seal；
- 旧权威被证明错误；
- 长期产品原则形成；
- Working Method升级、回滚或替换。

## 12. 不进入核心知识

- 临时命令；
- 全量日志；
- 每次debug；
- 低价值聊天流水；
- 不影响未来判断的尝试细节。

## 13. 分支边界

Project Knowledge 当前树保持纯知识；代码只从 main 读取。

未来可迁移到独立 private Project Knowledge repo。
