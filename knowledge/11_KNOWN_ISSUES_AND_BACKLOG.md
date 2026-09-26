# 已知问题与 Backlog

> 这里只记录已知但**当前不应混入主线**的问题。

## 当前主线 blocker

### Bootstrap durable ingress holding
状态：BLOCKED / current

需要在旧 Gateway 不具备 safe-drain 的情况下，完成第一次安全切换。
必须证明：
- Gateway down 时仍可靠接收；
- 正确 ACK 上游；
- durable persist；
- 恢复后 replay；
- duplicate 可控。

## Query backlog

- `/new` 用户可见技术信息泄漏；
- Query 结果可能泄漏 tenant_id / task_id / internal status / raw JSON；
- 部分 query flow 会写 session focus，不是严格零写；
- Student authority 仍有 legacy 来源；
- 历史未完成事项等后续 Query 回归。

## 旧技术债

- create_task 既有 NameError（与当前主线分离）；
- 旧测试/helper 失效；
- 旧身份架构需要逐步退役；
- legacy data governance；
- aa-nginx / proxy 历史运行债务。

## 后续能力

- Direct Message
- Student writes
- People / Organization
- Student Service
- Tasks
- Agenda
- Learning
- H5
- Long-term stability
- Voice / Hardware

## 原则

发现 backlog 不等于立刻修。
只有它成为当前阶段 blocker 或进入对应阶段，才展开。
