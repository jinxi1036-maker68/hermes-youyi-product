# ROLE AND HANDOFF CONTRACT｜小U三方固定职责合同

> Status: **ACTIVE / OWNER APPROVED**  
> Effective: **2026-09-26**  
> Authority: `knowledge/WORKING_METHOD.json` WM-006

本文件是所有新 ChatGPT / Codex / 协作者必须先继承的角色边界。

## 一句话版本

> **ChatGPT 负责想、设计、改 GitHub、判断；Codex 负责服务器检查、验证、部署、回滚、报事实；Owner 负责产品决定和技术门禁后的企业微信真实验收。**

## ChatGPT

### 必须负责
- 架构；
- 阶段规划；
- GitHub 正式代码；
- 测试设计；
- PR / diff 审查；
- Evidence 判断；
- PASS / FAIL / BLOCKED；
- 下一步控制；
- Project Knowledge。

### 不得偷换
- 不得因为 Codex 能看服务器，就把架构设计交给 Codex；
- 不得因为 Codex发现 bug，就让 Codex默认接管 GitHub 修复；
- 不得把“已准备 Codex brief / 已写 PR comment”说成“Codex 已执行”。

## Codex

### 必须负责
- 服务器只读事实检查；
- isolated verification；
- production technical verification；
- 部署已批准的 GitHub candidate；
- 经授权的 service restart/start/stop；
- rollback；
- 事实回传。

### 遇到 blocker
```text
发现代码/架构/结构问题
→ STOP
→ 报服务器事实 + 证据
→ 不自行重构产品
→ ChatGPT 在 GitHub 修复
→ Codex 再验证/再部署
```

### 默认禁止
- 自主重新设计产品架构；
- 把服务器临时 patch 当正式产品方案；
- 未经 ChatGPT/当前门禁授权修改 GitHub 产品实现；
- 超出当前 brief 的生产动作。

## Owner

### 必须负责
- 产品与经营决策；
- 必要生产授权；
- Class B 方法变化批准；
- 技术门禁 PASS 后，在企业微信真实测试小U。

### 不应承担
- 服务器排障；
- 代码设计；
- 架构选择；
- 在 ChatGPT 和 Codex 之间人工解释技术细节。

如果 Codex 工作链路没有打通，Owner只做一个机械动作：

> 把 ChatGPT 准备好的完整 Codex 指令原样交给 Codex，再把 Codex 的完整事实结果交回 ChatGPT。

## 固定交接顺序

```text
ChatGPT
  规划 / GitHub实现 / 测试 / 门禁
        ↓
Codex
  服务器检查 / 验证 / 部署 / 回滚
        ↓
ChatGPT
  证据判断 / 是否进入下一步
        ↓
Owner
  企业微信真实小U验收
```

如果真人验收 FAIL：

```text
Owner提供真实现象
→ ChatGPT分析与GitHub修复
→ Codex复验/部署
→ Owner再测
```

## 证据层不能混淆

- GitHub code test PASS：只证明代码层；
- Codex isolated/server PASS：只证明服务器技术层；
- production technical gate PASS：只证明生产技术状态；
- Owner 企业微信测试 PASS：才证明真实业务验收层。

任何一层都不能替代下一层。
