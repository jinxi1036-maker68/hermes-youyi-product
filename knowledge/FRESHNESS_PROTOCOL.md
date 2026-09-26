# Freshness Gate｜知识新鲜度协议

## 为什么需要

知识库记录的是“最后一次确认的事实”，不是永恒事实。

一个新窗口即使把知识库全部读完，也不能直接假设：
- GitHub main 没变；
- 生产没变；
- 当前 blocker 还没被其他窗口解决。

因此开工前必须做 Freshness Gate。

## Gate A｜GitHub main

任何涉及代码的工作：

1. 读 `PROJECT_INDEX.json.current.main_sha`；
2. 读取 GitHub 当前 `main` HEAD；
3. 比较。

### 相同
可以继续。

### 不同
停止实现，先查：
- main 新增了什么；
- 是否改变当前阶段/blocker；
- 更新 PROJECT_INDEX / CURRENT_STATE / ACTIVE_WORK；
- 再继续。

## Gate B｜Production

任何可能影响生产的工作：

- 不依赖“昨天刚查过”；
- 在同一轮执行前做必要的只读生产确认；
- 确认 production selector/SHA、关键服务和当前拓扑仍符合知识记录。

如果实时事实与知识冲突：
**实时事实优先，先同步知识库。**

## Gate C｜Active Work

确认：
- PROJECT_INDEX.active_work_item_id
- ACTIVE_WORK 中的 work_item_id

一致。

若不一致，不能开工。

## Gate D｜Evidence

任何准备声明 PASS / SEALED 的能力：
- 必须在 Evidence Index 中有可追溯证据；
- 正式业务能力仍需满足对应真人验收要求。

## 原则

Freshness Gate 不是让每个普通问题都查一遍服务器。

它只在“事实变化会改变本次判断”时执行相应层级：
- 文档讨论：通常只需读 Knowledge；
- 代码修改：至少检查 main；
- 生产动作：main + production；
- 封板：再加 Evidence。
