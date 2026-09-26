# Project Knowledge V2 更新协议

## 1. 动态事实只有一个权威

**`knowledge/PROJECT_INDEX.json` 是唯一动态当前事实源。**

以下文件不得独立发明当前 SHA、Stage、blocker：
- START_HERE
- NEXT_WINDOW_BOOTSTRAP
- Roadmap
- Architecture

CURRENT_STATE 是解释性投影；如冲突，以 PROJECT_INDEX + 实时事实为准。

## 2. 工作方式也有唯一当前版本

**`knowledge/WORKING_METHOD.json` 是当前工作方式的机器权威。**

`CURRENT_WORKING_METHOD.md` 是人类可读投影。

新窗口：
- 必须继承当前 method_id；
- 不得凭习惯悄悄改变工作节奏、角色分工、验收或部署纪律。

## 3. Working Method 如何进化

当出现以下情况时，应判断是否需要“方法升级”：
- 同一类错误反复发生；
- 上下文/交接反复丢失；
- Owner / ChatGPT / Codex职责混乱；
- 部署流程产生重复假 blocker；
- 出现新的工具能力，可以显著更安全或更快；
- 当前流程造成大量无价值等待、传话或重复劳动；
- 项目进入新的生命周期阶段，旧方法不再适合。

正式方法更新流程：

```text
发现流程问题
→ 区分产品问题 vs 工作方法问题
→ 提出候选工作方法
→ 有边界地试用/验证
→ 比较正确性、速度、安全、返工、用户负担
→ ACCEPT / REJECT / REVISE
→ ACCEPT 后升级 method_id
→ 新增 methods/ 历史记录
→ 更新 WORKING_METHOD / CURRENT_WORKING_METHOD
→ 更新 PROJECT_INDEX
→ Validator
```

### 禁止
- 因一次结果不方便就改规则；
- 为了快而直接降低安全/证据标准；
- 一次成功临时操作马上升级成永久制度；
- 删除旧方法历史；
- 新窗口静默改变分工或流程。

## 4. ACTIVE_WORK 是最后一公里交接

当当前工作发生以下变化时必须更新：
- work item 变化；
- 第一真实 blocker 变化；
- 已否定方案变化；
- next single action 变化；
- stop rule 变化。

## 5. Evidence Index

任何重要 PASS / FAIL / BLOCKED 结论，应该留下：
- area；
- judgment；
- PR / commit / tag（如有）；
- verification comment（如有）；
- 简明证据说明。

正式 sealed capability 必须有 Evidence id。

工作方法正式升级时：
- 至少保留 Method History；
- 若由明确事故/验证触发，可关联 Evidence / ADR。

## 6. Freshness Gate

开始代码工作：
- 比对 live main HEAD 和 PROJECT_INDEX。

开始生产工作：
- 再做同轮只读 production verification。

发现冲突：
- 先更新 Knowledge，再实现。

## 7. ADR

以下变化必须新建/更新 ADR：
- 长期架构边界；
- 权威来源；
- 安全语义；
- 模型/程序职责；
- 长期方案选择和明确拒绝方案；
- Project Knowledge / Working Method 的长期治理方式变化。

ADR 最少包含 Context / Decision / Why / Consequences / Status / Date。

## 8. Capability Archive

能力开始时建立 capability file。
封板时必须补：
- stable semantics；
- evidence；
- regression；
- reopen conditions。

## 9. Definition of Done

重大工作真正完成必须满足：

```text
代码/设计完成
+ 必要隔离验证
+ 必要生产验证
+ 必要真人验收
+ Project Knowledge Sync
+ 如工作方式发生变化则 Working Method Sync
= Done
```

只 merge 代码但项目状态或方法已经变化、知识库未同步，视为**尚未完整收尾**。

## 10. 什么时候必须同步 Knowledge

- Stage PASS / FAIL / BLOCKED 改变；
- main 关键 SHA 改变；
- production SHA 改变；
- current blocker 改变；
- next action 改变；
- 新架构决策；
- stable tag / seal；
- 旧权威被证明错误；
- 新长期产品原则；
- Knowledge / Learning 长期结论形成；
- **当前工作方式被正式升级、降级、回滚或替换。**

## 11. 什么不要写

- 临时命令；
- 全量日志；
- 每次 debug；
- 低价值聊天流水；
- 未来不会影响判断的尝试细节。

## 12. 分支边界

`project-knowledge` 当前工作树必须保持纯知识。

代码永远从 `main` 读取。

纯知识更新不要合并到当前发布 main，避免无意义改变 production target SHA。

未来可迁移到独立 private Project Knowledge repo。
