# 当前工作方式｜WM-005 Concurrency-Safe Governed Adaptive Method

> **机器权威：`knowledge/WORKING_METHOD.json`**  
> 本文件是当前工作方式的人类可读投影。

## 这版比 WM-004 多解决了什么

WM-004 让工作方式可以学习和版本化。

最终对抗式审查发现，还必须防三件事：

1. 多个聊天窗口同时更新 Project Knowledge，互相覆盖；
2. AI 以“优化效率”为名，自行降低关键安全、证据或授权标准；
3. Public Knowledge 把 credential 直接写进 Markdown/JSON，而不是敏感文件。

WM-005 专门封这三类风险。

## 当前默认工作节奏

1. **Orient**  
   PROJECT_INDEX → Freshness → CURRENT_STATE → ACTIVE_WORK → WORKING_METHOD。

2. **定义唯一当前问题**  
   写清命题、blocker、non-goals、证据和 STOP 条件。

3. **分清责任**  
   Owner 管产品/经营与最终业务验收；ChatGPT 管架构和 GitHub；Codex 管服务器事实、验证、部署、回滚。

4. **先查再改**  
   区分产品问题、代码问题、权威问题、Runtime问题、流程问题。

5. **最小通用修复**  
   不为单个测试写死；不跨 Stage；不顺手混历史债。

6. **候选与基线比较**  
   必要时做 A/B；只把 candidate-only failure 算候选回归。

7. **服务器执行与验证**  
   Codex拿背景、目标、必须结果、硬边界、证据和STOP条件，不自行设计GitHub方案。

8. **结构失败就停**  
   不做无新证据的重复生产重试。

9. **技术门禁后才真人验收**  
   确实需要 Owner 时才明确说“现在轮到你实测了”。

10. **Project Knowledge Sync**  
    重大状态变化后同步项目知识。

## Knowledge 写入的并发规则

材料性更新前记录 `project-knowledge` HEAD。

真正写入前再读一次：

- HEAD没变：以原HEAD为parent，fast-forward，`force=false`；
- HEAD变了：STOP，读取另一窗口的变化，语义合并，再提交；
- **永远不 force push 来解决知识冲突。**

详见 `CONCURRENCY_AND_RECOVERY.md`。

## 工作方法更新的治理

### Class A｜操作优化
如果不改变：
- 角色边界；
- 安全保证；
- Evidence阈值；
- production授权；
- 权限/权威；
- Stage封板；

ChatGPT可以基于证据做有限验证后升级并记录。

### Class B｜重大规则变化
只要涉及上述关键边界，**必须获得 Owner 明确批准**后才能成为永久当前方法。

如果发现新风险，可以临时采用更严格 Stop Rule，但永久规则仍要完成正式方法治理。

## Method Learning Loop

```text
发现流程摩擦/重复失败
→ 判断是不是工作方法问题
→ Class A / Class B 分类
→ 候选方法
→ 有边界验证
→ 比较速度/正确性/安全/返工/用户负担
→ Class B 取得 Owner 批准
→ ACCEPT / REJECT / REVISE
→ ACCEPT 后升级 WM 版本
→ 保留旧方法与替换原因
→ Knowledge Sync
```

## 优化目标

不是流程越来越厚，而是：

> **更少返工、更少重复解释、更少用户传话、更少盲试，同时保持或提高正确性、证据质量和生产安全。**
