# 新机构初始化流程 V0

目标：定义未来开通一家新机构 Hermes 的标准步骤。当前只作为蓝图，不执行真实开通。

## 阶段 1：收集资料

输入：

- `tenant_profile.json`
- 企业微信密钥的安全引用。
- 老板、店长、老师企业微信 user_id。
- 初始学生和老师数据，可为空。
- 机构制度和当前经营目标，可为空但必须标记缺失。

验收：

- `tenant_id` 使用短 ASCII 标识，例如 `demo_tuoguan`。
- 机构名称、老板账号、企业微信配置最少必须存在。
- 老师/学生资料可以后续补齐，但不能用示例机构资料代替。

## 阶段 2：创建租户目录

创建：

- `config/`
- `data/`
- `skills/`
- `memory/`
- `reports/`
- `backups/`
- `logs/`
- `runtime/`

规则：

- 目录属于该租户专用。
- 不从示例机构生产目录复制任何数据文件。
- 目录创建失败时停止，不允许继续写半成品配置。

## 阶段 3：生成配置文件

从 `tenant_profile.json` 生成：

- `wecom_whitelist.json`
- `teacher_wecom_map.json`
- `institution_operating_model.json`
- `academic_term_state.json`
- `dashboard_tokens.json`
- `runtime.env`

规则：

- 企业微信 Secret、Token、EncodingAESKey 只写安全引用或环境文件，不进模板。
- 老板默认拥有全局查看和目标确认权限。
- 店长和老师权限按配置生成，不默认全局可见。
- 家长自动发送默认关闭。

## 阶段 4：初始化空白账本

必须空白初始化：

- `hermes_work_items.jsonl`
- `wakeup_requests.jsonl`
- `business_events.jsonl`
- `action_executions.jsonl`
- `attention_threads.jsonl`
- `relationship_touch_candidates.jsonl`
- `daily_report_runs.jsonl`
- `hermes_employee_scorecard.jsonl`
- `value_progress_ledger.jsonl`
- `industry_learning_candidates.jsonl`

规则：

- 空文件表示该机构从零开始运行。
- 不允许复制示例机构工作项、日报、提醒、聊天、价值账本。

## 阶段 5：生成机构 Memory 和 Skill

生成：

- `memory/MEMORY.md`
- `skills/institution-facts/SKILL.md`
- `skills/institution-facts/references/institution-profile.md`

内容只包含：

- 新机构名称。
- 老板确认的制度。
- 已知业务线。
- 当前已知目标。
- 明确缺失的机构事实。

禁止包含：

- 示例机构老师、学生、老板、目标。
- 示例机构测试故障和运行日志。
- 其他机构任何资料。

## 阶段 6：安装服务模板

未来脚本生成或启用：

- `hermes@{tenant_id}.service`
- `hermes-wakeup@{tenant_id}.timer`
- `hermes-morning-report@{tenant_id}.timer`
- `hermes-evening-report@{tenant_id}.timer`
- `hermes-storage-audit@{tenant_id}.timer`

当前阶段只设计，不实际安装。

## 阶段 7：首次启动和入职自查

首次启动后 Hermes 必须完成：

- 识别自己服务的新机构。
- 说明自己已知哪些机构事实。
- 列出缺失事实和应问谁。
- 不知道示例机构的学生、老师、目标和聊天历史。
- 生成第一份入职认知报告。

## 阶段 8：人工验收

人工用老板账号测试：

- “你现在服务的是哪家机构？”
- “你知道示例机构的示例老师吗？”
- “你现在了解我们机构哪些信息，还缺什么？”
- “你今天准备怎么开始工作？”

通过后才允许进入真实试点。

