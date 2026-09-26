# 已知问题与 Backlog 目录

> **哪个问题当前正在做、哪个是 current blocker，只能从 `PROJECT_INDEX.json` 读取。**  
> 本文件只是问题目录，不保存动态优先级。

## Release / Runtime 类

- bootstrap durable ingress holding；
- legacy release-bound Home 历史债务；
- aa-nginx / proxy / Cloud Hub 历史运行治理；
- 旧 release / rollback 依赖退役时机。

## Query 类

- `/new` 用户可见技术信息泄漏；
- Query 结果可能泄漏 tenant_id / task_id / internal status / raw JSON；
- 部分 query flow 会写 session focus，不是严格零写；
- Student authority 仍有 legacy 来源；
- 历史未完成事项等后续 Query 回归。

## 业务能力类

- create_task 既有 NameError；
- 学生主档与服务关系进一步权威化；
- 旧人员/身份兼容数据退役；
- progressive claims / legacy governance。

## 后续正式能力

正式顺序见 PROJECT_INDEX roadmap / 04_CAPABILITY_MAP：
- Direct Message；
- Student Writes；
- Student Archive/Delete；
- People / Organization；
- Student Service；
- Tasks；
- Agenda；
- Learning；
- H5；
- Long-term Stability；
- Voice / Hardware。

## 使用原则

发现 backlog 不等于立刻修。

只有当：
- PROJECT_INDEX 将它设为 active work / blocker；
- 或正式能力路线进入对应 Stage；

才展开实现。
