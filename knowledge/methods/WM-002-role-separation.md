# WM-002｜ChatGPT / Codex / Owner 职责分离

- Status: Retained as part of current method
- Period: 2026-09 起

## 背景

曾出现把 GitHub实现和架构设计交给 Codex、用户在 ChatGPT 与服务器执行者之间反复传话的问题。

## 改进

形成稳定分工：

- ChatGPT：技术负责人 + GitHub代码执行者；
- Codex：服务器执行/验证；
- Owner：经营/产品决策 + 最终业务验收。

## 收益

- 架构决策与代码实现不再漂移；
- Codex基于真实服务器环境验证，而不是替代产品设计；
- 用户传话负担下降；
- 每一层证据归属更清楚。

## 保留到当前版本的原则

职责可以随着工具能力继续优化，但未经正式方法更新，不允许新窗口自行改变三方边界。
