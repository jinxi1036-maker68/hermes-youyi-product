# ADR-002｜老板明确下令与AI自主行动采用不同安全语义

- Status: Accepted
- Date: 2026-09
- Scope: Authorization / Product behavior

## Context

过去曾把“发送消息”等动作强制要求预先存在 task/business anchor，导致老板明确指令也被阻挡，产品不可用。

## Decision

### 人明确下令
只要：
- 请求者有权限；
- 对象合法；
- 风险可接受；

就应直接执行。

### AI自主行动
必须要求：
- 更完整事实；
- 明确责任关系；
- Agenda / 工作状态；
- 风险控制；
- 必要升级。

## Why

“用户明确授权”本身就是重要业务事实。不能因为防止AI自主越权，就把人类明确指令也强制塞进固定流程。

## Consequences

安全系统要判断权限和风险，不应强迫所有业务必须先造任务。
