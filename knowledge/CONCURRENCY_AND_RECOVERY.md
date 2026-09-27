# Project Knowledge 并发与恢复协议

## 威胁模型

多个窗口可能同时写 Knowledge。真正风险不是 Git 文本冲突，而是后写窗口覆盖已经改变的 Stage、Current Work、Method 或 Evidence。

## Compare-And-Swap

1. 材料性更新开始时记录 `K0 = project-knowledge HEAD`。
2. 基于 K0 准备 PROJECT_INDEX / CURRENT_WORK / Evidence / Method / ADR / Archive。
3. 提交前读取 `K1`。
4. K1 == K0：parent=K0，fast-forward，`force=false`。
5. K1 != K0：STOP，读取中间变化，语义合并，重新 render + validate。

**禁止 force push。**

## Current Work 并发规则

如果另一窗口改变 CURRENT_WORK：
- 不能只做文本合并；
- 必须判断旧 work item 是否已关闭、Stage 是否改变、next action 是否失效；
- 关闭的 work item 进入 archive，不得与新 Current 拼接。

## 恢复

误改时读 Git history，找到最后一致 commit，通过新的修复 commit恢复，不优先重写历史。

## 平台残余风险

project-knowledge 尚无平台级 branch protection。当前防线：
- fast-forward only；
- `force=false`；
- generated projections；
- Knowledge Validator；
- Freshness Gate。
