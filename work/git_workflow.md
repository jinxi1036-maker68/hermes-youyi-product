# Hermes Git 工作流 V1

## 定位

Git 只管理 Hermes 产品代码、通用模板、测试和交付文档；不管理优益生产业务数据、聊天记录、学生老师资料、运行账本、密钥或服务器备份。

当前阶段只使用本地仓库，不推 GitHub。后续接 GitHub 时必须使用私有仓库，并再次做敏感文件扫描。

## 日常修改规则

- 每个独立修复或功能做一个 commit。
- commit message 使用简短英文动词开头，例如：
  - `fix: anchor owner replies to recent proactive reports`
  - `feat: add tenant initializer dry run`
  - `docs: document Git workflow`
- 提交前必须运行：
  - `git status --short`
  - `git diff --cached --stat`
  - `git ls-files | rg "venv|__pycache__|demo_output|notification_outbox|students.json|records.json|secret|token|credential"`
- 若扫描结果命中模板文件，需要人工确认它只是空模板或安全引用。

## 生产部署规则

- 部署前先看 `git status --short`，确认本轮修改范围。
- 部署生产前继续保留服务器备份目录，Git 不替代生产数据备份。
- 每次生产稳定后打 tag，例如：
  - `youyi-prod-2026-08-03-stable`
  - `youyi-prod-before-external-learning-v1`

## 永远不进 Git 的内容

- `.venv/`、缓存、编译产物。
- `.env*`、Secret、Token、Credential、EncodingAESKey。
- `/opt/hermes-youyi/data/tuoguan-data` 中的任何生产数据。
- 学生、老师、家长真实资料。
- 企业微信真实密钥和真实访问 token。
- 聊天记录、reply ledger、action ledger、主动提醒线程、日报报告。
- 生成的 H5 token、看板缓存、审计报告和备份压缩包。

## 未来接 GitHub 私有仓库

1. 本地仓库稳定后创建私有 GitHub 仓库。
2. 推送前再次运行敏感扫描。
3. 设置 GitHub 仓库为 private。
4. 推送 tags，保留生产稳定点。
5. 后续可用 PR 审查每次修复，但生产数据仍走独立备份机制。

