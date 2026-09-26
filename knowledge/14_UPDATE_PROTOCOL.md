# 知识库更新协议

## 目的

让未来任何聊天窗口都能接上最新工作，不再依赖“上一窗口临时总结”。

## Canonical Branch

`project-knowledge`

**不要在当前发布窗口把知识更新直接合并 main。**
原因：main SHA 参与候选构建和生产发布，纯文档更新不应制造新的 production target SHA。

未来如建立独立 private knowledge repo，可整体迁移。

## 每次重大工作后必须判断是否更新

发生以下任一事件，应更新知识库：

1. Stage PASS / FAIL / BLOCKED 改变；
2. main 关键 SHA 改变；
3. production SHA 改变；
4. 当前 blocker 改变；
5. next action 改变；
6. 新正式架构决策；
7. stable tag / 封板；
8. 原先权威被证明错误；
9. 新的长期工作原则；
10. 形成会影响未来实现的 Knowledge/Learning 结论。

## 每次更新至少同步

### PROJECT_INDEX.yaml
机器快速入口：
- updated_at
- main_sha
- production_sha
- current_stage
- current_blocker
- next_action

### 05_CURRENT_STATE.md
人类快速入口：
- 当前真实状态
- 已完成
- 未完成
- blocker
- next action

### 其它文件
按需：
- Capability Map
- ADR
- History
- Backlog
- Runtime
- Architecture

## 不应该写入

- 每次命令
- 临时 debug
- 全量日志
- 失败尝试的所有细节
- 低价值聊天内容

历史细节如果未来不会影响决策，不进入核心知识库。

## ADR 规则

出现以下情况必须写 ADR：
- 改变长期架构边界；
- 改变权威来源；
- 改变安全语义；
- 改变模型/程序职责；
- 选择一个长期方案并明确拒绝另一个方案。

ADR 最少包含：
- Context
- Decision
- Why
- Consequences
- Status
- Date

## 当前状态文件禁止保存旧状态

`05_CURRENT_STATE.md` 永远只描述“现在”。

旧状态移动到：
- HISTORY
- ADR
- Git commit history

## 新窗口接手检查

新窗口完成读取后，应能准确回答：

1. 小U是什么？
2. 当前正式 Stage 是什么？
3. 生产和 main 各是什么 SHA？
4. 当前唯一 blocker 是什么？
5. 为什么不能直接进入下一阶段？
6. 下一步该做什么？
7. 谁负责改代码，谁负责服务器，谁负责最终验收？
8. 哪些问题现在禁止混入？

任何一题答不上来，说明知识读取还不够，不应开始改代码。
