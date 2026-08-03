# Hermes Git 工作流 V1

## 定位

Git 只管理 Hermes 产品代码、通用模板、测试和交付文档；不管理优益生产业务数据、聊天记录、学生老师资料、运行账本、密钥或服务器备份。

当前阶段只使用本地仓库，不推 GitHub。后续接 GitHub 时必须使用私有仓库，并再次做敏感文件扫描。

## Git 在哪里

- 当前本地仓库目录：`C:\Users\Administrator\Documents\Codex\2026-07-26\019ee2d8-1d8f-7b03-9f76-35a9819bb5f0`
- 隐藏版本库目录：`.git`
- 当前基线 tag：`youyi-prod-baseline-2026-08-03`
- `.git` 不需要手动打开；日常通过 Git 命令查看版本状态。

## 老板常用查看命令

- `git status`：看现在有没有未提交修改。
- `git log --oneline --decorate -5`：看最近 5 个版本。
- `git tag --list`：看稳定版本标签。
- `git diff`：看当前未提交代码改了什么。
- `git show --stat <commit或tag>`：看某个版本包含哪些文件变化。

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

## 本地历史 artifact 归档

- Git 基线建立前的临时补丁、检查脚本、dry-run 输出和手册渲染中间产物，统一归档到 `archives/local-artifacts/`。
- 归档目录被 `.gitignore` 忽略，不进入 Git。
- 归档只用于追溯历史，不作为当前产品母版。
- 2026-08-03 已完成一次归档：`archives/local-artifacts/2026-08-03-pre-git/MANIFEST.md`。

## 服务器备份治理

- `/opt/hermes-youyi-upgrade-0.19.0/backups` 是生产代码部署备份，不等于 Git。
- 近 14 天备份默认保留原样。
- 关键修复备份默认保留原样，例如小优命名、主动外发锚点、目标撤回、外部学习。
- 更早的普通代码备份可以先压缩归档，确认稳定后再人工决定是否删除原目录。
- `/opt/hermes-youyi/data/tuoguan-data` 是生产业务数据，不能因为有 Git 就删除。
- 2026-08-03 已完成一次服务器备份审计：
  - Markdown 报告：`/opt/hermes-youyi-upgrade-0.19.0/backups_archived/backup-audit-20260803092649.md`
  - JSON 报告：`/opt/hermes-youyi-upgrade-0.19.0/backups_archived/backup-audit-20260803092649.json`
  - 本次只压缩 1 个旧代码备份，未删除源目录，未触碰生产业务数据。

## 未来接 GitHub 私有仓库

1. 本地仓库稳定后创建私有 GitHub 仓库。
2. 推送前再次运行敏感扫描。
3. 设置 GitHub 仓库为 private。
4. 推送 tags，保留生产稳定点。
5. 后续可用 PR 审查每次修复，但生产数据仍走独立备份机制。
