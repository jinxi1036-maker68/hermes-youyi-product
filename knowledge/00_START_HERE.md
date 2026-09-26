# 小U项目知识库｜唯一入口

> **用途**：这是小U/Hermes项目的长期项目记忆入口。任何新的 ChatGPT/Codex/协作者在开始工作前，先读这里。  
> **知识库分支**：`project-knowledge`  
> **代码主分支**：`main`  
> **最后更新时间**：2026-09-26

## 1. 一句话定义

小U不是聊天机器人、固定工作流、经营分析器或传统 SaaS。小U的目标是成为进入托管机构长期工作的 **AI 数字员工**：能认识机构、主动补事实、接住老板目标、帮助店长和老师完成工作、持续推进未完成事项、从错误和结果中学习，并最终创造可验证的经营价值。

## 2. 新窗口固定读取顺序

开始任何工作前，按以下顺序读取：

1. `knowledge/PROJECT_INDEX.yaml` —— 30 秒定位当前项目坐标。
2. `knowledge/05_CURRENT_STATE.md` —— 当前生产、main、阶段、阻塞、下一步。
3. `knowledge/04_CAPABILITY_MAP.md` —— 哪些能力已封板、正在做、尚未开始。
4. `knowledge/02_ARCHITECTURE_AND_BOUNDARIES.md` —— 架构不可破坏的原则。
5. `knowledge/03_OPERATING_MODEL_AND_ROLES.md` —— 用户 / ChatGPT / Codex 固定分工。
6. 仅在需要时继续读 Roadmap、ADR、历史和专项文档。

**不要一上来把整个仓库、所有历史和所有文档全部塞进上下文。** 先读索引和当前状态，再按任务检索，保持上下文预算。

## 3. 当前项目坐标（2026-09-26）

- 正式产品仓库：`jinxi1036-maker68/hermes-youyi-product`
- GitHub 仓库当前为 **public**。
- 代码 main：`3e9473d8083533387a9422a68f289793bb6d6585`
- 当前生产：`588ea6eecb1833159e886181f3259be6e0befe37`
- 正式能力阶段：
  - Stage 1 Identity + Session：**PASS / sealed**
  - Stage 2 Query：**ACTIVE，功能接近完成；上线被 Runtime Topology 首次切换安全问题阻塞**
- 当前核心阻塞：
  - 第一次把生产从 legacy release-bound Hermes Home 切到外置 persistent Home 前，缺少一个 **独立于旧 Gateway 的 durable ingress bootstrap holding layer**。
- safe-drain 已完成并合并，但旧生产 588 本身没有该能力，因此不能靠“先裸重启一次”安装 drain。
- Query 真人企业微信最终复测尚未执行；只有 Runtime Topology 安全切换成功后才进入。

详见 `05_CURRENT_STATE.md`。

## 4. 项目知识的权威顺序

发生冲突时按以下优先级判断：

1. **用户最新明确决定**
2. `PROJECT_INDEX.yaml` + `05_CURRENT_STATE.md`（当前项目状态）
3. GitHub `main`（代码当前事实）
4. 生产服务器实际运行状态（部署事实；可能落后于 main）
5. 已封板 ADR / stable tag / PASS 证据
6. 旧交接文档、旧聊天总结、历史实验记录

注意：
- **代码真相 ≠ 生产真相**。main 中存在的能力不代表已经部署。
- **历史文档 ≠ 当前状态**。旧 SHA、旧阶段、旧下一步必须以 CURRENT_STATE 为准。
- **模型记忆 ≠ 项目权威**。模型记得的内容只能辅助检索，不得覆盖 GitHub 当前知识。

## 5. 固定新窗口启动语

用户以后只需要对新窗口说：

> **读取 GitHub 仓库 `jinxi1036-maker68/hermes-youyi-product` 的 `project-knowledge` 分支，从 `knowledge/00_START_HERE.md` 开始接手小U项目。先读 PROJECT_INDEX 和 CURRENT_STATE，再按当前任务继续；不要从零重新设计。**

## 6. 安全规则

因为当前仓库是 public，本知识库只保存 **public-safe 项目知识**：

允许：
- 架构原则
- 能力状态
- commit / PR / tag
- 非敏感运行路径和服务拓扑
- 设计决策
- 测试/封板制度
- 无个人身份的业务规则抽象

禁止：
- API Key / Secret / Token / .env 内容
- 真实学生、家长、老师的敏感身份信息
- 聊天正文
- 电话、身份证、健康隐私
- 生产数据库备份
- 任何凭证或受保护业务数据

如果未来具备单独 private repo，应整体迁移本知识库，并保留同样的入口和目录结构。

## 7. 知识库不是聊天日志

只记录会影响未来判断的长期事实：
- 阶段变化
- main / production 关键版本变化
- 架构与产品正式决策
- 当前 blocker / next action
- PASS / FAIL / BLOCKED 证据
- 长期要继承的经验

普通调试过程、临时命令、一次性日志不要全部写入知识库。

## 8. 维护义务

每次重大工作结束后，ChatGPT应主动判断是否需要更新：
- `PROJECT_INDEX.yaml`
- `05_CURRENT_STATE.md`
- `04_CAPABILITY_MAP.md`
- 对应 ADR / HISTORY

详细规则见 `14_UPDATE_PROTOCOL.md`。
