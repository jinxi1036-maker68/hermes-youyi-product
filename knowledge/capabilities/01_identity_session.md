# Capability 01｜Identity + Session

## 能力命题

同一个真实人员在 session reset、Gateway 重启、服务重启后仍能稳定解析为同一身份、角色、tenant 和权限；临时 session 不能成为人员身份权威。

## 状态

正式状态请读 `PROJECT_INDEX.json`。

该能力已封板，证据索引：`EV-ID-001`。

## 稳定语义

- WeCom userid 是稳定账号身份来源之一；
- 显示名不是身份权威；
- Person identity 与账号绑定分离；
- 人员身份、角色、在职状态从当前人员权威读取；
- 旧 staff / 历史映射只能辅助兼容，不能继续作为当前授权根；
- Session 是临时上下文，不是 Person；
- reset / restart 不得导致身份或权限漂移；
- 测试账号、历史账号、正式账号不能被错误合并；
- 内部 Model / Provider / Context / runtime metadata 不能泄漏给最终用户。

## Stable Tag

`youyi-stable-PASS-identity-authority-2026-09-23`

## Reopen 条件

只有出现以下任一真实证据才重新打开：
- 同一 WeCom 用户被解析成不同 Person；
- reset/restart 后角色或 tenant 改变；
- 旧映射重新进入授权根；
- 身份相关内部技术信息再次泄漏；
- 后续架构证明当前身份前提错误。

普通文案差异不重开本能力。
