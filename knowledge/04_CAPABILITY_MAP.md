# 正式产品能力地图

> 这是 **Hermes 正式产品验收路线**，不要和 Pi Lab 的 Experiment 010-018 混为一谈。

| Stage | 能力 | 状态 | 当前说明 |
|---|---|---|---|
| 1 | Identity + Session | **PASS / SEALED** | 身份权威、角色、session reset 后身份连续性已封板 |
| 2 | Query | **ACTIVE** | 功能接近完成；生产上线被 Runtime Topology 首次安全切换阻塞 |
| 3 | Direct Message / 主动外发 | OPEN | Stage 2 完成后进入 |
| 4 | Student Basic Writes | OPEN | 学生基础写入 |
| 5 | Student Archive/Delete Semantics | OPEN | 结束服务/归档/误录作废与物理删除语义 |
| 6 | People & Organization | OPEN | 人员、角色、状态、组织 |
| 7 | Student Service Relationships | OPEN | 学生主档与多服务关系、负责人 |
| 8 | Tasks | OPEN | 创建、分派、截止、等待、完成、取消 |
| 9 | Task Progress / Contact Owner | OPEN | 推进、联系负责人、结果闭环 |
| 10 | Agenda Autonomous Work | OPEN | 持续恢复和无人推进 |
| 11 | Cross-Service Concern | OPEN | 跨服务安全/关注事项 |
| 12 | Progressive Claims / Legacy Governance | OPEN | 旧数据、claims、权威迁移 |
| 13 | Permission Regression | OPEN | 权限总回归 |
| 14 | Final Response Truth / UX | OPEN | 用户可见回复真实性和技术字段隔离 |
| 15 | Learning | OPEN | 纠错、偏好、流程改进、经验验证 |
| 16 | H5 | OPEN | 展示/洞察层 |
| 17 | Long-term Stability | OPEN | 长稳运行 |
| 18 | Voice / Hardware | OPEN | 语音、实体设备、Robot Channel |

## Stage 1

- PASS / SEALED
- Stable tag：`youyi-stable-PASS-identity-authority-2026-09-23`

## Stage 2 已完成的重要修复

1. 今日未完成任务：today 按 `due_at` 本地日期；无日期不等于今天。
2. Model-first：普通业务查询不靠隐藏关键词 Router 决定工具。
3. Query evidence boundary：任务 inventory/list/count/status/date 必须由权威 task query 支撑。
4. Runtime/Release：persistent Home、自包含、Core compatibility、service identity、migration gates、safe-drain。

## Stage 2 最终真人测试

`今天还有哪些事情没处理完？`

必须证明：
- model-first；
- 权威 task query；
- date_scope=today；
- open/unclosed；
- scope 与身份一致；
- 不虚构“您名下”或全机构范围；
- 不泄漏技术字段。

只有生产安全切换成功后，才通知 Owner：**“现在轮到你实测了。”**
