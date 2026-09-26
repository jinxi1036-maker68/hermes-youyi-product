# Project Knowledge 并发与恢复协议

## 威胁模型

未来可能同时存在：
- 两个 ChatGPT 窗口；
- 一个 ChatGPT + Codex结果回传窗口；
- 一个长期窗口和一个临时审计窗口。

如果它们都认为自己掌握“最新知识”，最危险的不是 Git 冲突本身，而是**后写窗口覆盖了前一个窗口刚形成的新事实**。

## 写入协议｜Compare-And-Swap 思维

### 1. 读
开始材料性 Knowledge 更新时，记录：

`K0 = project-knowledge HEAD`

### 2. 准备
基于 K0 整理要改的：
- PROJECT_INDEX
- ACTIVE_WORK
- Evidence
- Working Method
- ADR / History

### 3. 写前再读
提交前再次获取：

`K1 = project-knowledge HEAD`

### 4. 判断

#### K1 == K0
允许：
- commit parent = K0；
- update ref；
- **force=false**。

#### K1 != K0
必须：
- STOP 当前写入；
- 读取 K0..K1 的变化；
- 判断另一窗口是否改变 current state / active work / method / evidence；
- 语义合并；
- 重新执行 validator / freshness；
- 以 K1 之后的新 commit fast-forward。

**禁止 force push。**

## 为什么不能“自动取最后一个版本”

因为并发变化可能意味着：
- blocker 已经解决；
- production 已经切换；
- Stage 已封板；
- Working Method 已升级。

这不是文本冲突，而是项目事实改变。

## 恢复

若 project-knowledge 被误改：

1. 不依赖聊天记忆重建；
2. 查看 Git commit history；
3. 找最后一个已知一致的知识 commit；
4. 比较后续变化；
5. 通过新的修复 commit恢复，不优先重写历史。

## 平台残余风险

当前 `project-knowledge` 分支没有平台级 branch protection。

因此本协议是当前主要防线。

未来如果具备合适GitHub管理能力：
- 开启防 force push；
- 限制 branch deletion；
- 保留管理员恢复路径；
- 可进一步要求 Knowledge Validator 状态检查。
