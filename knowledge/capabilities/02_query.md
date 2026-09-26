# Capability 02｜Query

## 能力命题

小U能够理解普通自然语言查询，选择正确权威数据源，在权限范围内返回真实、完整、可解释且不泄漏内部技术字段的结果。

## 状态

当前状态、版本和 blocker 只从 `PROJECT_INDEX.json` 读取。

## 已确立的语义

### Today
“今天任务”由 `due_at` 的本地日期决定；无日期任务不自动属于今天。

### Model-first
普通业务表达由模型理解，不通过隐藏关键词 Router 替模型决定业务意图。

### Evidence Boundary
任务的：
- inventory
- list
- count
- status
- date-scoped facts

必须来自权威 task query。

active work context 只可用于 continuation / current focus，不可证明任务库存或数量。

### Permission
Query permission 与写入 permission 必须分离；查询范围修复不能暗中扩大写权限。

## 最终固定真人验收

`今天还有哪些事情没处理完？`

验收：
- model-first；
- authoritative task query；
- date_scope=today；
- open/unclosed；
- scope 与当前身份一致；
- 不虚构“您名下”或全机构范围；
- 不泄漏内部技术字段。

## 当前前置条件

必须先完成安全生产 Runtime Topology cutover 并部署包含 Query 修复的新版本。

## 后续回归

最终固定句通过后逐个验证：
- 当前老师；
- 某老师近期工作；
- 某学生近期情况；
- 历史未完成事项；
- 结果技术字段；
- query focus 是否发生不必要写入。

## Separate Backlog

`/new` 技术信息泄漏是独立问题，不能为了 Query 封板把它与当前 bootstrap cutover 混改。
