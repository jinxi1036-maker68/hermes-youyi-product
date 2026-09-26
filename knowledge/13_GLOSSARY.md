# 术语表

## Business Truth
当前可验证业务事实。不是模型推断。

## Institution Workspace
机构业务事实和能力所使用的业务工作空间。不能和 Hermes Home 混为一谈。

## Hermes Home
Hermes profile/runtime state，如 sessions、cron、state.db、logs 等。长期必须版本中立、脱离 release。

## Goal / Work State
当前目标、未完成工作、等待对象、下一关注点和完成证据。

## Agenda
让未完成工作能够持续恢复、复查和继续推进的运行机制。不是第二个业务脑。

## Knowledge
制度、标准、方法、经过治理的经验。

## Memory / History
过去发生的事实和上下文。

## Learning
从纠错和结果中提炼候选经验、验证并形成可复用改进的机制。

## Execution Receipt
证明某次动作真实执行到什么程度的证据。不能用来夸大真实结果。

## Writeback Verification
写入后重新读取确认真实状态，而不是仅凭调用成功。

## Model-first
模型理解业务意图并选择能力；系统不靠隐藏关键词 Router 替模型做业务判断。

## Query Evidence Boundary
不同查询事实必须由有权威性的工具/数据源支撑。例如任务 inventory 不能由 active context 推断。

## Runtime Topology V1
release code 与 persistent runtime state 分离的长期生产拓扑。

## Safe Drain
继续接收并持久化 callback、继续 ACK，但暂停新业务派发，等待 in-flight 清空后安全维护。

## Bootstrap Holding
在旧 Gateway 还没有 safe-drain 的第一次切换阶段，由 Gateway 外部组件临时承担 durable receive + ACK + persist + replay。

## PASS / FAIL / BLOCKED
- PASS：满足冻结标准并有足够证据。
- FAIL：候选明确违反标准。
- BLOCKED：不能安全继续，但未证明候选本身错误。
