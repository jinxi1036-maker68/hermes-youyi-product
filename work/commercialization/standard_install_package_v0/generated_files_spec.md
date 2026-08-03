# 生成文件规则 V0

目标：明确未来新机构安装包中哪些文件复制、哪些生成、哪些空白初始化、哪些禁止携带。

## 1. 从产品母版复制

| 材料 | 来源 | 目标 | 说明 |
| --- | --- | --- | --- |
| Hermes 运行代码 | 产品发布包 | `app/releases/{version}` | 不包含机构数据。 |
| 通用托管工具 | 产品发布包 | `app/current/runtime` | 写入守卫和权限规则通用。 |
| 通用数字员工手册 | 产品发布包 | `app/current/home-proddata/skills` | 不包含机构事实。 |
| H5 通用结构 | 产品发布包 | `app/current` | 展示数据来自租户目录。 |
| 测试套件 | 产品发布包 | `app/current/tests` | 新机构验收可复用。 |
| 存储治理和资源巡检 | 产品发布包 | `app/current/scripts` | 参数化数据目录。 |

## 2. 从 `tenant_profile.json` 生成

| 文件 | 生成来源 | 默认规则 |
| --- | --- | --- |
| `config/tenant_profile.json` | 用户填写 | 原样保存，密钥仅保存引用。 |
| `config/runtime.env` | tenant、operation_policy | 写 tenant_id、数据目录、时间策略。 |
| `config/runtime.secrets.env` | 安全密钥引用 | 未来由安全流程写入，不进仓库。 |
| `config/dashboard_tokens.json` | roles、dashboard_policy | 每个角色单独 token。 |
| `data/wecom_whitelist.json` | roles、wecom | 老板进入 super_users，老师进入 allowed_users。 |
| `data/teacher_wecom_map.json` | roles | 名称到 wecom_user_id 映射。 |
| `data/staff.json` | roles | 老板、店长、老师角色表。 |
| `data/institution_operating_model.json` | tenant、operation_policy、制度 | 新机构运营模型。 |
| `data/academic_term_state.json` | service_relations | 新学期确认窗口和名单可信度。 |
| `memory/MEMORY.md` | tenant、已确认制度 | 只写稳定机构事实。 |
| `skills/institution-facts/SKILL.md` | tenant | 新机构事实入口 Skill。 |

## 3. 可导入但必须标注来源

| 文件 | 来源 | 标注要求 |
| --- | --- | --- |
| `data/students.json` | 客户学生表 | 标注导入时间、学期、是否当前在读。 |
| `data/records.json` | 历史记录 | 标注历史数据，不能直接当作当前表现。 |
| `data/tasks.json` | 历史任务 | 默认 closed 或 historical，不能自动执行。 |
| `data/parent_communications.json` | 历史沟通 | 标注历史来源和可信度。 |
| `data/payroll_rules.json` | 客户制度 | 需要老板确认后才可启用。 |

## 4. 必须空白初始化

这些文件必须从空白开始，不得复制优益历史。

- `data/hermes_work_items.jsonl`
- `data/wakeup_requests.jsonl`
- `data/business_events.jsonl`
- `data/action_executions.jsonl`
- `data/attention_threads.jsonl`
- `data/relationship_touch_candidates.jsonl`
- `data/daily_report_runs.jsonl`
- `data/hermes_employee_scorecard.jsonl`
- `data/value_progress_ledger.jsonl`
- `data/institution_fact_gap_events.jsonl`
- `data/industry_learning_candidates.jsonl`
- `data/external_research_runs.jsonl`
- `data/market_research_candidates.jsonl`
- `data/competitor_profiles.jsonl`
- `data/weekly_market_report_runs.jsonl`
- `data/agent_delegations.jsonl`
- `data/agent_delegation_results.jsonl`

## 5. 禁止生成或复制

- 优益学生、老师、家长资料。
- 优益 Memory。
- 优益企业微信密钥、Token、EncodingAESKey。
- 优益聊天记录、原始消息、reply ledger。
- 优益目标、工作项、日报、唤醒报告。
- 优益 H5 token。
- 调试失败和临时修复日志。

## 6. 失败处理原则

- 生成任何核心配置失败时，必须停止初始化。
- 已创建目录但配置失败时，保留目录并标记 `initialization_failed`，不得启动服务。
- 不允许在缺企业微信密钥时伪造可发送状态。
- 不允许用 demo 或优益数据填补客户缺失资料。
