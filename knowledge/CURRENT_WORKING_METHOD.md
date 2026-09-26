# 当前工作方式｜WM-006 Role-Locked Concurrency-Safe Governed Adaptive Method

> **机器权威：`knowledge/WORKING_METHOD.json`**  
> 本文件是当前工作方式的人类可读投影。

## WM-006 为什么升级

WM-005 已经规定了 Owner / ChatGPT / Codex 的基本分工，但实践中仍出现了两个角色漂移风险：

1. 把“写入 PR comment 的 Codex brief”误认为“Codex 已收到并执行”；
2. 在给 Codex 的任务中，把本应由 ChatGPT 负责的架构/代码责任交给 Codex。

Owner 已于 2026-09-26 明确确认固定工作链路，因此 WM-006 把角色边界升级为显式锁定合同。

## 不可漂移的三方职责

### ChatGPT｜技术负责人 + GitHub代码执行者

负责：
- 整体架构与阶段规划；
- GitHub 代码修改；
- 测试和验收方案设计；
- PR 审查与合并判断；
- Evidence 的 PASS / FAIL / BLOCKED 判断；
- 决定下一步技术动作；
- Project Knowledge 持续更新。

**代码或架构有问题时，由 ChatGPT 修 GitHub。**

### Codex｜服务器执行者 / 验证者

负责：
- 服务器只读检查；
- 在真实服务器环境验证 GitHub candidate；
- 部署已经批准的 GitHub 代码；
- 在明确授权范围内 restart / start / stop；
- rollback；
- 把真实服务器事实和验证结果回传。

Codex不得默认：
- 重新设计架构；
- 接管 GitHub 产品代码；
- 发现代码问题后自行把服务器临时补丁变成正式产品方案。

**Codex发现结构/代码 blocker：STOP → 报事实 → ChatGPT修GitHub → Codex复验。**

### Owner｜产品决策 + 最终真实验收

负责：
- 产品/经营决定；
- 必要的生产授权；
- Class B 工作方法变更批准；
- 在技术门禁通过后，于企业微信真实测试小U。

Owner不是默认技术传话人。只有 Codex 执行链路不可用时，Owner才手动粘贴 ChatGPT 已经准备好的 Codex 指令，并把结果带回。

## 固定执行链路

```text
1. ChatGPT 读 Knowledge + Freshness Gate
2. ChatGPT 规划 / 设计
3. ChatGPT 在 GitHub 实现 + 测试 + PR
4. ChatGPT 给出服务器验证/部署门禁
5. Codex 检查 / 验证 / 部署 / 回滚
6. Codex 回传事实
7. ChatGPT 判断 PASS / FAIL / BLOCKED
8. 技术门禁 PASS 后，Owner 在企业微信实测小U
9. ChatGPT 根据真人验收结果封板或继续修复
10. Knowledge Sync
```

## 关键防误判规则

- PR comment 中存在 Codex 指令 ≠ Codex 已经收到。
- Codex 没有执行回传 ≠ 验证已完成。
- Codex 发现代码问题 ≠ Codex 应该改代码。
- GitHub CI PASS ≠ 服务器验证 PASS。
- 服务器验证 PASS ≠ 企业微信真人验收 PASS。
- 只有对应证据层完成，才能把该层标记为 PASS。

## 其余 WM-005 规则继续有效

WM-006 不降低任何 WM-005 的并发、安全和证据要求：

- Knowledge 乐观并发 / CAS；
- 禁止 force push 解决知识冲突；
- Class B 方法变更需要 Owner 明确批准；
- Freshness Gate；
- 结构失败 STOP；
- 无新证据不盲目重复生产尝试；
- Project Knowledge 必须在重大状态变化后同步。

详见 `knowledge/WORKING_METHOD.json` 和 `knowledge/CONCURRENCY_AND_RECOVERY.md`。
