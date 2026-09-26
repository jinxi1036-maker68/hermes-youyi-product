# 小U核心领域模型

> 这是业务世界观，不是数据库 Schema。具体存储可以变化，但概念不能随实现漂移。

## Institution

小U服务的机构主体。

当前正式产品现实优先按一个机构/一个门店理解，不主动重新引入多校区复杂度。

## Person

真实人员身份。角色可能包括：
- Owner
- Manager
- Teacher

账号绑定（如 WeCom userid）是 Person 的身份绑定，不等于 Person 本身。

## Student

学生主档。一个学生原则上只有一份主档。

## Service

学生参加的服务，例如：
- 午托
- 晚托
- 周末班
- 暑假班

## Service Enrollment / Relationship

Student 与 Service 的关系。

一个 Student 可以同时拥有多个 Service Enrollment。

**退出某一服务 ≠ 删除 Student。**

## Service Owner

某个具体 Service Enrollment 的负责人。

同一 Student 的不同服务可以由不同老师负责。

## Service Record

绑定到具体服务关系的一手记录。

原作者、原负责人和历史不能因换负责人被覆盖。

## Concern

跨服务需要共享的安全/关注事项。

Concern 不是把原始 Service Record 全量复制给所有人，而是独立、受权限控制的关注对象。

## Task

可被创建、分派、等待、完成、取消、逾期、交接的工作对象。

Task 的 inventory/status/date 事实必须由任务权威提供。

## Communication

找对人、发送、记录投递事实的能力。

Communication 传输模型决定的内容，不替模型决定业务内容。

## Goal

老板或机构希望达成的经营目标。

Goal 不等于 Task；Goal 可以被模型拆解成多项工作。

## Work State

小U当前工作的持续状态：
- 正在做什么；
- 缺什么事实；
- 等谁；
- 何时重新关注；
- 完成证据是什么。

## Agenda

恢复和重新呈现 Work State 的持续运行机制。

Agenda 不替模型做业务判断。

## Knowledge

机构制度、标准、方法和经过治理的经验。

## Memory / History

过去发生过什么，以及相关上下文。

## Learning

从错误、纠正和结果中形成候选经验并验证改进的机制。

## Execution Receipt

证明一次真实执行到达什么阶段的证据。

## 关键关系

```text
Institution
 ├─ Person
 ├─ Student
 │   └─ Service Enrollment
 │       ├─ Service Owner
 │       └─ Service Record
 ├─ Goal
 │   └─ Work State
 │       └─ Task / Communication / Agenda
 ├─ Concern
 ├─ Knowledge
 └─ Learning / History / Execution Evidence
```
