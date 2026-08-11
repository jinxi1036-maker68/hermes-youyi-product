# Hermes v0.20 与小优兼容评估

日期：2026-08-11  
范围：只读官方资料、本地临时源码 dry-run；未升级生产、未改生产配置、未重启生产服务。

## 结论

Hermes v0.20 与小优的方向高度一致，但不能直接在优益生产执行 `hermes update`。应先在独立临时环境验证企业微信回调插件、会话上下文、Skills、Memory、Cron、Toolsets 和生产数据目录，再另行决定升级。

当前可直接吸收而无需等待生产升级的能力：Skill Bundle 结构、渐进式手册加载、个人记忆隔离原则、Cron preflight 思路、按平台最小 Toolsets、安全审批和会话检索边界。小优本轮已经建立 `xiaoyou-core` bundle 及常驻核心契约，但仍由项目自己的权限和写入守卫负责生产安全。

## 官方能力与小优对应

| Hermes v0.20 官方能力 | 小优采用方式 | 当前决定 |
|---|---|---|
| Skills 渐进式加载 | 核心契约每轮固定注入，详细员工手册按问题需要加载 | 采用 |
| Skill Bundles | `xiaoyou-core.yaml` 组合身份、业务、主动取数、目标、记忆和学生服务手册 | 采用 |
| Persistent Memory | 原生 `USER.md` 只保留机构中立规则；每个人的工作方式进入按企业微信身份隔离账本 | 采用自有隔离层 |
| Background self-improvement | 只形成进化候选、应用和验证证据；不允许自动改制度、权限、工资、家长外发或正式手册 | 限制采用 |
| Session Search | 只可作为按身份授权的历史证据来源，不能让老师检索老板或其他员工会话 | 升级前专项验证 |
| Toolsets | 企业微信生产只暴露托管业务工具和必要只读联网，不开放 terminal/file 写能力 | 采用最小权限 |
| Cron preflight | 定时工作运行前检查模型、Skills 和交付目标，失败显式记为 blocked/degraded | 采用思想，暂保留现有 systemd timer |
| Skill-backed Cron | 夜间复盘可加载核心手册组合，但不得真实外发 | 临时环境验证后再决定 |
| MCP | 只允许明确选择的只读工具，不能绕过托管权限 | 暂不扩大生产 MCP |
| Security | 保留白名单、审批、跨会话隔离、输入清洗；项目写守卫继续作为业务层第二道边界 | 采用 |

## 关键兼容风险

1. Hermes 原生 `USER.md` 是一个 Hermes profile 的单一用户画像，并在会话开始时冻结注入。优益同一个企业微信网关服务多人，因此不能把金总和李老师偏好同时留在全局 `USER.md`。本轮提供了备份后迁移到个人工作方式档案的脚本。
2. v0.20 的后台自我改进可以修改 Memory 或 Skills。小优不能让未经确认的经验直接改正式员工手册；生产应关闭相应写权限或启用 write approval，只保留候选审核闭环。
3. 官方强调一个 Hermes home 不应由多个 agent 进程共同写 Memory。升级前必须确认主网关、定时任务和子 Agent 不并发写同一原生 Memory 文件。
4. v0.20 对 Python 的要求是 `>=3.11,<3.14`；当前生产 Python 3.11 在版本范围内，但依赖锁、插件 API 和 site-packages 最小同步方式仍需临时安装验证。
5. Toolsets 是能力暴露边界。不能直接启用 `all`、`coding` 或包含 terminal/file 的组合；企业微信应继续只开放业务工具和必要只读 web/search。
6. Session Search 可以检索历史会话。没有按企业微信身份授权验证前，不进入老师/店长生产工具集。
7. 生产当前不是 Git 工作树，且自定义代码同时存在 runtime 和 site-packages。升级必须先建立完整镜像备份和可重复安装包，不能依赖在线更新后手工补文件。

## 升级门禁

- 在临时目录安装官方 v0.20，不复用生产 home、state.db、数据账本或密钥。
- 运行本仓库 `scripts/hermes_v020_compatibility_dry_run.py`，所有硬检查通过。
- 用脱敏复制配置验证插件注册、企业微信事件结构、`pre_llm_call`、`post_gateway_response`、工具注册和消息幂等。
- 验证 `xiaoyou-core` bundle 可发现，核心契约能加载，缺失 Skill 会被明确报告。
- 验证原生 Memory 不混入个人档案，后台自我改进不能绕过候选审核。
- 验证 Cron preflight 失败时不调用模型、不误报成功、不外发。
- 验证企业微信 Toolsets 不含 terminal/file/browser 写操作，Session Search 默认不向员工开放。
- 完整回归通过后，另行制定生产升级、备份、回滚和一次受控重启计划。

## 官方依据

- [Hermes v0.20 pyproject](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/pyproject.toml)
- [Skills System](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- [Persistent Memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
- [Toolsets Reference](https://hermes-agent.nousresearch.com/docs/reference/toolsets-reference)
- [Cron Jobs](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)
- [MCP](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp)
- [Security](https://hermes-agent.nousresearch.com/docs/user-guide/security)
