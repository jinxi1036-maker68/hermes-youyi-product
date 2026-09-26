# Project Knowledge V2 更新协议

## 1. 动态事实只有一个权威

**`knowledge/PROJECT_INDEX.json` 是唯一动态当前事实源。**

以下文件不得独立发明当前 SHA、Stage、blocker：
- START_HERE
- NEXT_WINDOW_BOOTSTRAP
- Roadmap
- Architecture

CURRENT_STATE 是解释性投影；如冲突，以 PROJECT_INDEX + 实时事实为准。

## 2. ACTIVE_WORK 是最后一公里交接

当当前工作发生以下变化时必须更新：
- work item 变化；
- 第一真实 blocker 变化；
- 已否定方案变化；
- next single action 变化；
- stop rule 变化。

## 3. Evidence Index

任何重要 PASS / FAIL / BLOCKED 结论，应该留下：
- area
- judgment
- PR / commit / tag（如有）
- verification comment（如有）
- 简明证据说明

正式 sealed capability 必须有 Evidence id。

## 4. Freshness Gate

开始代码工作：
- 比对 live main HEAD 和 PROJECT_INDEX。

开始生产工作：
- 再做同轮只读 production verification。

发现冲突：
- 先更新 Knowledge，再实现。

## 5. ADR

以下变化必须新建/更新 ADR：
- 长期架构边界；
- 权威来源；
- 安全语义；
- 模型/程序职责；
- 长期方案选择和明确拒绝方案。

ADR 最少包含 Context / Decision / Why / Consequences / Status / Date。

## 6. Capability Archive

能力开始时建立 capability file。
封板时必须补：
- stable semantics；
- evidence；
- regression；
- reopen conditions。

## 7. Definition of Done

重大工作真正完成必须满足：

```text
代码/设计完成
+ 必要隔离验证
+ 必要生产验证
+ 必要真人验收
+ Project Knowledge Sync
= Done
```

只 merge 代码但项目状态已变化、知识库未同步，视为**尚未完整收尾**。

## 8. 什么时候必须同步 Knowledge

- Stage PASS / FAIL / BLOCKED 改变；
- main 关键 SHA 改变；
- production SHA 改变；
- current blocker 改变；
- next action 改变；
- 新架构决策；
- stable tag / seal；
- 旧权威被证明错误；
- 新长期产品原则；
- Knowledge / Learning 长期结论形成。

## 9. 什么不要写

- 临时命令；
- 全量日志；
- 每次 debug；
- 低价值聊天流水；
- 未来不会影响判断的尝试细节。

## 10. 分支边界

`project-knowledge` 当前工作树必须保持纯知识。

代码永远从 `main` 读取。

纯文档更新不要合并到当前发布 main，避免无意义改变 production target SHA。

未来可迁移到独立 private Project Knowledge repo。
