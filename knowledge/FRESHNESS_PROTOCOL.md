# Freshness Gate｜知识新鲜度协议

## 原则

Knowledge 记录最后一次确认的事实，不代表外部世界停止变化。

## Gate A｜GitHub main

涉及代码时：
1. 读 `PROJECT_INDEX.current.main_sha`；
2. 读 live main HEAD；
3. 不同则先检查变化并同步 Knowledge，再实现。

## Gate B｜Production

涉及生产判断或动作时，同一轮先做必要只读确认：
- production SHA / selector；
- 关键服务；
- 与当前任务有关的拓扑或状态。

实时事实冲突时，实时事实优先。

## Gate C｜Current Work

必须确认：
- `PROJECT_INDEX.current.active_work_item_id`
- `CURRENT_WORK.json.work_item_id`

完全一致。不一致代表 Harness 漂移，不能继续。

## Gate D｜Evidence

准备声明 PASS / SEALED 时，必须读取对应 Evidence；正式能力仍需满足 capability 规定的真人验收层。

## 最小化

- 文档讨论：通常无需 production；
- 代码修改：至少 main；
- 生产动作：main + production；
- 封板：再加 Evidence。

不要把 Freshness 变成每个普通问题都全量查服务器。
