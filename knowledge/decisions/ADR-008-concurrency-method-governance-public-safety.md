# ADR-008｜并发安全、方法变更治理与Public知识防泄漏

- Status: Accepted
- Date: 2026-09-26
- Scope: Project Knowledge / Working Method governance

## Context

最终对抗式审查发现：

1. 多窗口并发可能导致 Knowledge 后写覆盖前写；
2. “工作方法可学习”如果没有治理，未来AI可能自行降低关键安全/证据标准；
3. public branch 的敏感文件名检查不足以阻止 credential 被直接写入 Markdown/JSON；
4. 项目知识说明本身也会老化，需要 validator + 更新协议共同约束。

## Decision

- Project Knowledge 使用 optimistic concurrency / fast-forward-only 写入协议；
- 永远禁止通过 force push 解决并发知识冲突；
- Working Method变更分 Class A / Class B；
- Class B 关键规则变化需要 Owner 明确批准；
- Validator增加高置信 credential 内容扫描；
- Public Knowledge明确只是 public-safe连续性知识，不承诺保存全部内部秘密；
- 当前分支未受平台级保护被记录为残余风险。

## Consequences

多个未来窗口可以并行，但更新项目权威前必须先处理并发。

Working Method可以持续进化，但关键边界不能被AI静默自我修改。
