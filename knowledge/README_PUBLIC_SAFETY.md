# Public Repository Safety

当前 repository visibility = **public**。

因此本 Knowledge Branch 只能承载 **public-safe Project Knowledge**，不能等价成“所有内部项目知识”。

## 永远禁止

- Secret / Token / API Key
- .env 内容
- 私钥 / 证书私钥
- 企业微信密钥
- 认证 header
- 真实学生/家长/员工敏感个人数据
- 电话 / 身份证 / 健康信息
- 用户聊天正文
- 生产数据库备份
- 私有业务原始数据
- 不应公开的商业策略细节或安全运维机密

## 可以记录

- commit / PR / tag
- 架构原则
- 经判断可公开的服务拓扑抽象
- 非敏感路径
- 权威和能力边界
- 阶段状态
- 测试制度
- 无个人身份的抽象业务语义
- Working Method

## 内容扫描

Validator 除敏感文件名外，还扫描高置信 credential 模式。

但自动扫描不是完整 DLP。写入前仍必须人工/模型判断内容是否适合公开。

## 当前结构性限制

Public Repo 无法满足“保存全部小U内部知识”的最终目标。

因此目前的 Project Knowledge 定义为：

> **完整的 public-safe 项目连续性知识**

而不是：
> 完整内部商业/人员/生产秘密库。

## Private Repo 迁移

未来具备独立 private Project Knowledge repo 后：

- 保留同样目录结构；
- 保留 `00_START_HERE.md`；
- 保留 `PROJECT_INDEX.json`；
- 保留 Working Method / Evidence / Method History；
- 当前 public branch 变为最小公开镜像或迁移指针；
- private repo 承载更完整的内部项目知识。

即使迁入 private repo，也继续禁止不必要的 Secret 和真实敏感个人数据。
