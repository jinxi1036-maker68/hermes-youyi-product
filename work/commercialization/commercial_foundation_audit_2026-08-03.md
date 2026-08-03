# Hermes 商业化地基与稳定性审计 V1

生成时间：2026-08-03

## 结论

当前适合继续做“优益样板稳定 + 商业化地基补强”，不适合直接复制优益生产目录给新客户，也不建议一次性重构为复杂 SaaS 多租户。

本轮已做 P0 小修：

- 新增轻量租户上下文：运行时默认 `youyi_tuoguan`，新租户可通过 `HERMES_TENANT_ID` 覆盖。
- 机构事实读取优先 `institution_operating_model.json`，兼容旧 `youyi_operating_model.json`。
- 系统写入审计、日报、外部学习、唤醒巡检、目标保存等新记录改为使用当前租户 ID。
- 新机构初始化包显式写出 `HERMES_TENANT_OPERATING_MODEL_FILE=institution_operating_model.json`。
- 首次启动验收报告新增 runtime context 和跨租户污染扫描结果。

## P0 审计项

| 项目 | 当前状态 | 判断 | 下一步 |
| --- | --- | --- | --- |
| 环境隔离 | demo 包生成独立 `config/`、`data/`、`memory/`、`skills/`、`reports/`、`backups/`、`logs/` | 基础通过 | 真实交付前补 systemd 模板和环境变量加载验收 |
| 数据隔离 | initializer 不读取生产数据，生成空白账本；acceptance 验证账本为空 | 基础通过 | 增加启动后 H5/企业微信数据目录实际读取核对 |
| Memory/Skills 隔离 | demo Memory 和 institution-facts Skill 从 tenant profile 生成 | 基础通过 | 后续拆通用产品 Skill 与机构事实 Skill |
| 权限隔离 | 验收检查老板、店长、老师 whitelist/staff 映射；H5 权限需人工启动后验收 | 部分通过 | 增加自动化 H5 角色访问测试 |
| 密钥隔离 | 明文 secret/token/EncodingAESKey 被 initializer 和 acceptance 拒绝 | 基础通过 | 真实部署时只允许 env/secret/vault 引用 |
| 优益资料剥离 | 生成结果会扫描优益、金总、李老师、旧目标、生产路径等标记 | 基础通过 | 扩充污染词为机构 profile 驱动 |
| 备份恢复 | 文档已有策略，demo 包有独立 `backups/` | 未完全验证 | 增加备份生成与恢复演练脚本 |
| 监控/CI | 本地缺 pytest 可执行环境；脚本干跑可通过 | 待补齐 | 固化项目测试环境或 CI runner |
| 客户交付文档 | 已有标准安装包 V0 文档 | 部分通过 | 补首次启动人工验收记录模板 |
| 生产加载版本核对 | 本地已确认 `main` 对齐 `origin/main`，tag 存在；未做生产只读 SSH 核对 | 待只读核对 | 如访问生产，仅查服务状态、加载路径和模块 hash，不重启 |

## 敏感与污染扫描

执行范围：Git 管理文件和本地产品代码；不扫描、不读取生产业务数据。

文件名级扫描：

```text
git ls-files | rg "venv|__pycache__|demo_output|notification_outbox|students.json|records.json|secret|token|credential"
```

结果：无命中。

内容级扫描结论：

- P0：未发现真实生产业务数据、真实学生/老师资料、真实企业微信密钥进入 Git。
- P1：`systemd/hermes-youyi-*` 仍保留优益生产路径和服务名，这是当前优益生产模板，商业化前需要做参数化服务模板。
- P1：`scripts/hermes_resource_audit.py` 仍默认优益生产数据目录；它是只读生产审计工具，未来应增加租户参数。
- P1：测试夹具和优益回归测试仍含 `youyi_tuoguan`、金总/李老师等样板语境；允许保留，但需要新增通用租户回归测试。
- P2：H5 token、dashboard secret 等代码变量属于令牌逻辑，不是实际凭证；继续禁止生成物进入 Git。

## 稳定性回归重点

- 主动外发锚定：老板追问“这里边/刚才/这份报告”等，必须优先召回最近 3 小时已发送给老板的主动消息；材料只辅助模型判断，不接管意图。
- Attention thread：未解决主动问题作为高优先级材料，不是 Router；模型仍需判断老板本轮是否相关。
- 写入守卫：写入必须有主链路、operation、ledger、audit、幂等和写后反查；结果未知不能说成成功。
- 外部学习：有来源才生成候选；搜索失败只能记录失败和不确定项，不能编造趋势。
- 多 Agent：只做参谋，不能外发或写业务；主小优必须记录采纳、部分采纳、拒绝、延后或需更多证据。

## 执行边界

- 本轮不重启生产服务，不迁移数据，不清理生产目录，不做真实外发。
- Git 只管理产品代码、通用模板、脚本和商业化文档。
- 生产业务数据、聊天记录、学生老师资料、运行账本、企业微信密钥继续禁止纳入 Git。
