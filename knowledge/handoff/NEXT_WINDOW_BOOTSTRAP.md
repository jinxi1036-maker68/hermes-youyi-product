# 新聊天窗口接手协议

## 用户只需发送

> 读取 GitHub 仓库 `jinxi1036-maker68/hermes-youyi-product` 的 `project-knowledge` 分支，从 `knowledge/00_START_HERE.md` 开始接手小U项目。先读 PROJECT_INDEX 和 CURRENT_STATE，再按当前任务继续；不要从零重新设计。

## 新窗口必须先完成的理解检查

在开始改代码前，应能回答：

1. 小U的最终产品定义是什么？
2. 模型和程序的职责边界是什么？
3. 当前正式 Stage 是什么？
4. 哪个 Stage 已封板？
5. main SHA 是什么？
6. production SHA 是什么？
7. 当前唯一 blocker 是什么？
8. 为什么不能直接停旧 Gateway？
9. ChatGPT / Codex / Owner 各负责什么？
10. Stage 2 最终真人测试是什么？

## 当前标准答案（2026-09-26）

1. 小U是托管机构长期工作的AI数字员工。
2. 模型负责业务判断；程序负责事实、权限、安全、执行、证据。
3. Stage 2 Query。
4. Stage 1 Identity + Session。
5. main = `3e9473d8083533387a9422a68f289793bb6d6585`
6. production = `588ea6eecb1833159e886181f3259be6e0befe37`
7. bootstrap durable ingress holding。
8. 旧 production 588 没有 safe-drain，现有 Cloud Hub 也不能可靠 hold+ACK+replay。
9. ChatGPT 改 GitHub；Codex 管服务器；Owner 做业务决策和最终验收。
10. `今天还有哪些事情没处理完？`

若未来 CURRENT_STATE 已更新，以 CURRENT_STATE 为准，不要死记本页旧答案。
