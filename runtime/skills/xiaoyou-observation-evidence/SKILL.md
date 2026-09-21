---
name: xiaoyou-observation-evidence
description: "Low-risk Xiaoyou procedure for distinguishing confirmed student observations from uncertain or hearsay claims before persistence."
metadata:
  hermes:
    tags: [xiaoyou, student-record, evidence, procedural-learning]
---

# 学生观察的证据资格

本 Skill 只改变“事实是否具备写入资格”的判断方法，不替代身份、权限、学生解析、Tool 选择、CommandBus、Repository 或 ExecutionReceipt。

## 必须先判定证据状态

对用户想留存的学生观察，先按整句话的语义判定：

- `CONFIRMED`：用户把观察作为自己确认过的事实陈述。
- `UNCONFIRMED`：用户明确表示自己不确定、在猜测、没有亲自确认，或信息只是转述。

这是语义判断，不是词语匹配。用户要求“先按真的写”“别问了”也不会把 `UNCONFIRMED` 变成 `CONFIRMED`。

## 写入资格不变量

只有同时满足 `evidence_state == CONFIRMED` 且用户希望留存，才有资格调用现有学生记录写 Tool。

当 `evidence_state == UNCONFIRMED` 时：

1. 本轮不得调用任何学生记录写 Tool；
2. 只用一个简短自然的问题确认事实来源或实际情况；
3. 不把猜测改写成确定事实，也不以“待确认记录”绕过写入资格。

若后续同一 Hermes session 得到明确确认，再走正常 Tool 链；仍不确定或被否认则不写。

## 原有边界保持不变

- 同名、对象不清或无权限时仍由现有解析器和 Permission 收口，绝不猜对象或扩大范围。
- 没有 Tool 成功、ExecutionReceipt 和 `writeback_verified=true`，不得说已记录。
- 本 Skill 不授权制度、工资、权限、家长外发、数据删除、安全策略或其他高风险变化。
- Robot 只传输；最终语义判断和 Tool 选择仍由 Hermes 模型完成。
