# WM-005｜Concurrency-Safe Governed Adaptive Method

- Status: Current
- Effective: 2026-09-26
- Supersedes: WM-004

## 对抗式审查发现

WM-004 已经解决“工作方法可以学习”，但还存在三个方法层风险：

1. 多个聊天窗口可能同时更新 Project Knowledge；
2. AI 可以理论上通过“方法优化”自行降低关键安全/证据标准；
3. 当前 Public Knowledge 缺少足够强的内容泄密防线。

## 新增原则

### 1. Knowledge 写入采用乐观并发控制

写入前记录 `project-knowledge` HEAD。

提交前再次读取 HEAD：

- 没变：以原 HEAD 为 parent，`force=false` fast-forward 更新；
- 变了：STOP，先读另一窗口改了什么，再语义合并；
- 永远不通过 force push 覆盖并发修改。

### 2. 工作方法变更分级

#### Class A｜操作优化
不改变：
- Owner / ChatGPT / Codex核心职责；
- 安全标准；
- Evidence阈值；
- production授权；
- 权限/权威；
- Stage封板；

可由 ChatGPT 在有证据的有限验证后升级并记录。

#### Class B｜重大方法变更
涉及上述任一关键边界，必须获得 Owner 明确批准后才能成为永久当前方法。

发现新风险时可以临时采用更严格 Stop Rule，但永久规则仍需完成正式治理。

### 3. Project Knowledge 写入前做内容安全检查

不只检查文件名，还要扫描高置信：
- 私钥块；
- 常见 GitHub/OpenAI/AWS credential 格式；
- password/secret/token/api_key 的长值赋值。

## 目的

让“工作方式会进化”不等于“AI可以任意改规则”。

同时让多个未来聊天窗口真正可以并行工作，而不会互相覆盖项目记忆。
