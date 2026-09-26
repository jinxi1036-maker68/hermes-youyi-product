# Public Repository Safety

当前 repository visibility = **public**。

因此本 Knowledge Branch 只允许写入 public-safe 项目知识。

## 永远禁止

- Secret / Token / API Key
- .env 内容
- 企业微信密钥
- 真实学生/家长/员工敏感个人数据
- 电话 / 身份证 / 健康信息
- 用户聊天正文
- 生产数据库备份
- 认证 header
- 私有业务原始数据

## 可以记录

- commit / PR / tag
- 架构原则
- 公共安全的服务拓扑
- 非敏感路径
- 权威和能力边界
- 阶段状态
- 测试制度
- 无个人身份的抽象业务语义

## Private Repo 迁移

如果未来 GitHub 权限支持建立独立 private project-knowledge repo：
- 保留同样目录结构；
- 保留 `00_START_HERE.md` 和 `PROJECT_INDEX.yaml`；
- 当前 public branch 保留只读迁移指针；
- private repo 可逐步承载更丰富但仍合规的商业知识。
