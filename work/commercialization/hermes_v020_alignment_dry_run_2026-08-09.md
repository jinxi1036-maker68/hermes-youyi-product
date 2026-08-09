# Hermes v0.20 对齐 Dry Run 报告｜2026-08-09

本报告只做本地与资料层面的升级评估，不执行生产升级，不重启优益服务，不迁移数据，不触发真实外发。

当前本机 Hermes CLI：

- 版本：`Hermes Agent v0.20.0 (2026.8.3)`
- 安装目录：`C:\Users\Administrator\AppData\Local\hermes\hermes-agent`
- Python：`3.11.15`
- OpenAI SDK：`2.24.0`

## 一句话结论

Hermes v0.20 的方向和小优高度一致，但不能直接把生产能力全打开。最值得吸收的是三类：工具失败自恢复、可信来源引用、上下文压缩；A2A 和 webhook 先作为顾问团与监控能力评估，不进入生产外发链路。

## 官方变化摘要

来源：

- Hermes v0.20.0 release：<https://github.com/NousResearch/hermes-agent/releases>
- Toolsets Reference：<https://hermes-agent.nousresearch.com/docs/reference/toolsets-reference>
- Skills System：<https://hermes-agent.nousresearch.com/docs/user-guide/features/skills>
- MCP：<https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp>
- Configuration / Web Search：<https://hermes-agent.nousresearch.com/docs/user-guide/configuration>
- Security：<https://hermes-agent.nousresearch.com/docs/user-guide/security>

v0.20 官方发布说明显示，本次核心变化包括：

- 工具自恢复：工具调用失败后可重试、降级或给出更可行动的失败信息。
- 可信引用：研究类输出强调可验证来源、引用和事实核验。
- A2A：支持 Agent-to-Agent 协议，适合多 Agent 系统互通。
- Webhook：可向外部系统推送签名事件。
- 上下文压缩：更温和地处理长会话，保留近期消息和重要上下文。
- Toolsets：工具按场景成组启用，适合给不同平台和任务设置能力边界。
- Skills：技能是按需加载的知识文档，适合承载员工手册和稳定工作方法。
- MCP：支持远程/本地工具接入，并可按服务过滤工具。
- Web Search：支持 Firecrawl、SearXNG、Parallel、Tavily、Exa 等后端。
- Security：包含密钥过滤、凭据透传边界、网站访问策略、SSRF 防护等安全能力。

## 和小优现状的对齐

### 1. Toolsets 对齐小优权限边界

小优生产会话应该继续只暴露托管业务工具、人员目录、只读健康查询、只读公开资料搜索。不能因为 v0.20 工具更强，就把 terminal、file write、browser write、任意 MCP 写工具交给生产模型。

建议：

- 生产默认 toolset：`tuoguan_business_read`、`tuoguan_business_write_guarded`、`web_readonly`。
- 禁止默认启用：terminal、file write、browser write、通用 MCP 写工具、自动 webhook 写业务。
- 每次新增工具必须说明：读写类型、风险级别、是否需要写后反查、是否可能真实外发。

### 2. Skills 对齐员工手册

Hermes 的 Skills 很适合小优，但小优的 Skill 不应写成功能菜单，而应写成“员工怎么工作”：

- 小优身份与边界。
- 托管机构业务常识。
- 主动工作原则。
- 问题自救原则。
- 个人工作方式适应原则。
- 夜间复盘与自我进化原则。
- 事实、证据、来源、写后反查原则。

当前我们已经建立了主动性、适应性、问题解决、自我进化契约。下一步应把多次验证有效的经验沉淀成“手册候选更新包”，经人工确认后再进入正式 Skill。

### 3. 工具自恢复对齐“先自救再求助”

v0.20 的工具自恢复方向，应吸收到小优的业务工具层，但要分级：

- 只读工具可以有限重试：人员目录、机构地图、健康查询、外部学习来源查询。
- 幂等写工具可以在明确 idempotency key 下恢复：偏好账本、事实缺口候选、自我进化候选。
- 外发、任务提醒、日报发送不能自动盲重试，必须保持 `pending -> sending(lease) -> sent/failed/result_unknown`。

当前已完成的对应基础：

- `update_json` 锁内读改写。
- outbox lease 状态。
- 工具失败候选进入自我进化账本。
- 没有真实工具调用或写后反查，不允许说“我查了/已保存”。

下一步适配建议：

- 建一个小优业务工具 retry policy：只读 1-2 次、幂等写按 key 恢复、外发不自动重发。
- 每次工具失败必须能进入健康查询或自我进化候选。

### 4. 可信引用对齐外部学习与业务事实诚实

v0.20 的 grounded citations 很适合小优的外部学习和“我查了”的诚实问题。

建议：

- 外部学习候选必须带来源 URL、标题、抓取时间、摘要和模型采纳状态。
- 无来源时只能记录 `source_failed`，不能生成趋势结论。
- 对业务内部事实不能用联网猜测，只能来自机构账本、人员目录、聊天证据、任务回执或老板/店长/老师确认。
- 老板问“小优你为什么这么判断”时，回答应能分清：内部事实证据、公开资料来源、模型推断。

当前已完成的对应基础：

- 外部学习失败只记录失败候选。
- 工具失败和未查证承诺会进入自我修正候选。
- 机构工作地图返回事实、缺口、证据、置信度和事实归属人。

### 5. A2A 对齐 multi-agent 顾问团

A2A 适合未来把“小优顾问团”标准化，但本轮不直接接生产：

- 机构理解顾问。
- 人员沟通习惯顾问。
- 工具失败顾问。
- 经营机会顾问。
- 员工手册一致性顾问。

边界必须固定：

- 顾问只建议，不直接写业务。
- 顾问不外发。
- 顾问输出不是事实，必须由小优主模型采纳或拒绝并记录原因。
- 顾问不能绕过托管工具权限和生产数据隔离。

### 6. Webhook 对齐监控，不对齐业务外发

Webhook 适合后期做运维和看板事件流，例如：

- 日报生成成功/失败。
- outbox delivery status 变化。
- 工具连续失败。
- 重复提醒异常。
- 夜间进化候选数量异常。

本阶段不建议让 webhook 写业务、发企业微信、触发任务或触达老师/家长。先做只读监控事件，再考虑签名验签和接收端。

### 7. 上下文压缩对齐长会话能力

小优最容易出问题的地方是“上下文断了”：老板短回复、老师回复、最近主动外发、任务锚点丢失。v0.20 的压缩能力值得吸收，但必须保护这些锚点：

- 最近 3 小时老板主动外发锚点。
- open attention thread。
- active task context。
- pending next task context。
- 写后反查回执。
- 最近自我错误修正。
- 当前人的工作方式档案。

建议新增压缩门禁测试：长会话压缩后，小优仍能接住“可以”“那就按这个来”“李老师回复了”等短回复。

## 不建议现在启用的能力

- 不在优益生产直接执行 `hermes update`。
- 不开启生产 terminal/file/browser 写能力。
- 不把 MCP 远程工具直接暴露给小优生产会话。
- 不让 A2A 顾问团直接写业务或外发。
- 不让 webhook 自动触发真实外发。
- 不对外发类工具做盲重试。
- 不把外部公开资料当成机构内部事实。

## 建议落地顺序

### P0：保持生产稳定

不升级优益生产 Hermes 主程序。当前只把小优业务代码按最小同步策略上线，生产升级另开计划。

### P1：本地沙箱对齐

在临时租户环境验证 v0.20：

- 读取当前 toolsets 配置，不改变生产配置。
- 确认 Skills 加载路径和小优手册路径。
- 验证 web search/extract 只读后端。
- 验证 MCP 工具过滤能力，但不接生产业务。
- 跑 demo tenant initializer 和 acceptance。

### P2：小优业务适配

优先做三项代码适配：

- 业务工具 retry policy：只读工具重试、幂等写恢复、外发不盲重发。
- grounded evidence adapter：外部学习和机构事实引用统一返回来源/证据字段。
- context compression guard：压缩后保留任务、主动外发、写后反查、自我修正、个人偏好。

### P3：生产上线前验收

仍走我们已有生产流程：

- 本地全量窄回归。
- 敏感扫描。
- 生产只读核对。
- 备份生产相关代码和数据。
- 最小同步。
- 一次受控重启。
- 查日志和下一次真实早晚报。

## 验收标准

完成 v0.20 对齐，不等于“升级生产”。真正进入生产前必须满足：

- 小优仍只使用授权业务工具和只读公开资料。
- 所有写入都有审计和写后反查。
- 所有外发仍有权限、频率、幂等和 delivery status。
- 日报、主动提问、任务跟进能读到个人工作方式和自我修正。
- 外部学习有来源，无来源不生成结论。
- 长上下文下短回复能接到正确任务或主动消息锚点。

## 当前判断

v0.20 值得吸收，但吸收方式不是“整体升级生产”，而是先把官方能力翻译成小优的岗位能力：

- 工具自恢复 = 小优先自救再求助。
- grounded citations = 小优说任何“查到/判断/趋势”都要有来源。
- A2A = 顾问团，不是第二个老板。
- webhooks = 运维健康事件，不是外发通道。
- compression = 让小优记住关键工作脉络，不是压掉任务上下文。
- Skills = 员工手册和工作方法，不是死板菜单。

这个方向符合当前项目目标：小优像真实数字员工一样持续工作、复盘、改进，但系统仍只守边界，最终判断和行动选择仍由模型完成。
