# 明确拒绝过的方案

> 目的：防止未来新窗口把已经证明有问题的方向重新包装一遍再走一次。

| 方案 | 状态 | 为什么拒绝 |
|---|---|---|
| 关键词业务 Router 代替模型理解 | Rejected | 程序会逐渐成为第二业务大脑，失去数字员工泛化能力 |
| 每个业务场景单独写固定流程 | Rejected | 产品退化为自动化 SaaS，而不是统一大脑+能力接口 |
| 新机构第一天巨大表单 | Rejected | 不像新员工入职；应先最低可工作地图，再边工作边补 |
| 所有低风险日常动作重新让老板逐项确认 | Rejected | 过度安全导致不可用，老板重新变项目经理 |
| 老板明确发消息也必须先造 task/business anchor | Rejected | 人类明确授权本身就是业务授权事实 |
| “无记录=现实没发生” | Rejected | 违反 Unknown 和证据原则 |
| release-local Hermes Home | Rejected | 代码与持久运行状态耦合，升级/回滚脆弱 |
| 通过旧 release symlink 持久化 runtime state | Rejected | 新 release 继续依赖历史版本，无法真正自包含 |
| root preflight 随意创建 runtime state | Rejected | 会制造 service identity 权限污染 |
| recursive chmod/chown 掩盖发布权限问题 | Rejected | 扩大风险且破坏最小权限 |
| runtime SQLite/WAL/heartbeat 字节变化=业务污染 | Rejected | runtime 自然变化会误判部署失败，应检查业务语义 |
| 小U测试 shim 覆盖 Hermes Core | Rejected | 会破坏正式 Core API 合同 |
| 把所有知识一次性塞进模型上下文 | Rejected | 知识越多越慢且噪声越大；应检索少量相关内容 |
| 一开始上 GraphRAG | Deferred/Rejected for V1 | 当前复杂度不足以证明收益，先 metadata+hybrid retrieval+rerank |
| 裸重启旧 588 来“先安装 safe-drain” | Rejected | bootstrap 阶段没有独立 holding，无法证明 callback 不丢 |
| 直接拿现有 Cloud Hub fallback queue 当 bootstrap | Rejected | direct-forward 失败不进入 queue，且缺 replay/idempotency |
| 直接在服务器游离 Cloud Hub 脚本临时打补丁 | Rejected | 无 Git 治理、变更归属不清、不可作为正式产品发布方式 |
