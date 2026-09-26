# 项目路线图

## A. 正式 Hermes 产品能力路线

按顺序：
1. Identity + Session
2. Query
3. Direct Message
4. Student Basic Writes
5. Student Archive/Delete
6. People & Organization
7. Student Service Relationships
8. Tasks
9. Task Progress / Contact Owner
10. Agenda Autonomous Work
11. Cross-Service Concern
12. Progressive Claims / Legacy Governance
13. Permission Regression
14. Final Response Truth / UX
15. Learning
16. H5
17. Long-term Stability
18. Voice / Hardware

原则：
- 一次只推进一个正式能力；
- 上一阶段未封板，不把下一阶段真实执行塞进当前阶段；
- 可以识别未来问题，但不顺手重构。

## B. Pi Lab / Digital Employee Lifecycle 实验路线

这是实验路线，不等于正式生产能力序列，也不能直接把 Pi 实验代码覆盖 Hermes。

历史实验：
- 001-003：身份边界、独立分析、受控写入；
- 004-006：自主行动、可追溯决策、Review/Gate/Execute；
- 007：单次经营决策与统一执行，PASS WITH LATENCY LIMITATION；
- 008：Typed Operational Actions，FAIL，不进入稳定基线；
- 009：Minimal Thinking 性能实验，战略取消。

Lifecycle：
- 010 入职陌生机构
- 011 主动补事实
- 012 接住老板目标
- 013 老师协作
- 014 持续运行
- 015 自我纠错
- 016 经营价值
- 017 跨业务泛化
- 018 新租户复制

Pi Lab 的作用：
- 验证“数字员工应该怎样工作”的原则；
- 不直接代替正式 Hermes 产品实现；
- 实验结论需要重新映射到正式架构。

## C. Knowledge / Learning 长期路线

未来分两层：

### Project Knowledge
给开发小U的人和AI用：
- 项目是什么
- 做到哪里
- 为什么这么设计
- 当前 blocker
- 下一步

**本知识库就是 Project Knowledge。**

### Institution Knowledge
未来给运行中的小U用：
- 机构制度
- 服务标准
- 培训材料
- 方法
- 经过验证的经验

原则：
- Knowledge 不等于业务数据库；
- Knowledge 不等于 Memory；
- Learning 负责把纠错与结果沉淀为候选经验，经过验证后进入 Knowledge；
- 单次检索必须有上下文预算，知识规模增长不能线性拖慢用户响应。
