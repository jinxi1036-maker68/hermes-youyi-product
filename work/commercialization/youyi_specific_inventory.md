# 示例机构专属内容清单 V1

目标：识别当前 Hermes 中哪些材料是示例机构样板资产，未来不能直接复制给其他机构。

## 1. 明确示例机构专属的产品资料

- `youyi-digital-employee` Skill 名称和描述。
- 示例机构托管机构身份、业务事实和运营模型。
- 示例机构员工手册中已经写入的示例机构具体事实。
- `youyi_operating_model.json` 相关事实。
- 当前 Memory 中的示例机构长期事实和老板偏好。

处理方式：未来商业化时需要拆成“通用托管数字员工手册”和“某机构专属事实包”。

## 2. 明确示例机构专属的生产数据

- 示例机构学生名单、历史学生口径、服务关系。
- 示例机构老师、店长、老板企业微信映射。
- 示例机构当前和历史目标，例如 2026 年 9 月续费更稳。
- 示例机构家校沟通、学生记录、任务、工资、绩效候选。
- 示例机构日报、晚报、唤醒报告、价值账本。
- 示例机构主动提醒、回复锚定和关系经营候选。

处理方式：只能留在示例机构租户数据目录，不能进入模板或新机构初始包。

## 3. 当前已知写死点

这些是后续商业化前需要替换成 tenant 配置的风险点。

- 多处状态对象中写死 `tenant_id: example_institution`。
- 机构认知审计读取 `youyi_operating_model.json`。
- Skill 名称和标签使用 `youyi`。
- 系统服务名使用 `hermes-youyi-*`。
- 数据目录使用 `/opt/hermes-youyi/data/tuoguan-data`。
- 生产入口使用 `/opt/hermes-youyi-upgrade-0.19.0`。
- 测试中大量使用 `owner_test`、`机构负责人`、`示例老师`、`九月份续费率更稳` 等示例机构样例。

处理方式：短期不改生产；后续做标准安装包时通过配置、模板和测试夹具替换。

### 3.1 当前代码和配置证据

| 类型 | 当前证据 | 商业化处理 |
| --- | --- | --- |
| tenant_id 写死 | `runtime/plugins/tuoguan_core/digital_employee_state.py` 中多处写入 `example_institution` | 改为从租户配置读取。 |
| 机构事实文件 | `runtime/plugins/tuoguan_core/digital_employee_state.py` 读取 `youyi_operating_model.json` | 改为 `institution_operating_model.json` 或由 tenant profile 指定。 |
| 写入保护文件 | `runtime/plugins/tuoguan_core/write_guard.py` 包含 `youyi_operating_model.json` | 保护规则保留，文件名配置化。 |
| 生产路径 | `scripts/hermes_resource_audit.py` 默认 `/opt/hermes-youyi/data/tuoguan-data` | 后续改为参数化租户数据目录。 |
| 自主循环配置 | `runtime/plugins/tuoguan_core/autonomous_employee_loop.py` 读取 `/opt/hermes-youyi-upgrade-0.19.0/home-proddata/config.yaml` | 改为租户运行配置。 |
| systemd 服务 | `systemd/hermes-youyi-daily-*.service` 和 `hermes-youyi-autonomous-wakeup.*` | 改为 `hermes@tenant_id` 模板。 |
| 技能包名称 | `work/youyi-digital-employee/`、`work/handbook_v2_artifacts/skills/*` | 拆成通用产品 Skill 和机构事实 Skill。 |
| 测试夹具 | 多个 `runtime/tests/plugins/test_youyi_*.py` 使用机构负责人、示例老师、九月续费目标 | 保留示例机构回归测试，同时新增通用租户测试。 |

## 4. 可转成产品通用经验的示例机构内容

这些来自示例机构实践，但可以抽象成通用能力。

- 老板希望 Hermes 主动、有员工感、有目标推进感。
- 老师侧需要情绪价值、减负和绩效树，而不是单纯催任务。
- 店长侧需要现场助手，而不是监督感。
- 新学期服务关系不能用旧名单硬推，需要确认窗口。
- 自主工作要区分真实完成、等待、缺事实、建议动作。
- 系统只守权限和高风险边界，不替模型做业务决策。

处理方式：写入通用产品手册和测试，不携带示例机构真实资料。

## 5. 禁止进入商业模板的示例机构内容

- 示例机构真实学生和家长信息。
- 示例机构老师的真实聊天和回复。
- 示例机构老板私密经营偏好。
- 示例机构企业微信密钥和身份映射。
- 示例机构业务错误、测试故障和临时绕路日志。
- 示例机构历史 H5 token、链接、访问凭证。

处理方式：只允许出现在示例机构生产数据和审计备份中，不进入 `tenant_profile.template.json`。
