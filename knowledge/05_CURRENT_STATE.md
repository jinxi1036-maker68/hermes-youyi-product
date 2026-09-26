# 当前真实状态

> **更新时间：2026-09-26**  
> 这是一份“现在是什么”的文件，不保存长篇历史。历史请看 HISTORY / ADR。

## 一句话状态

**Stage 1 已封板；Stage 2 Query 功能基本就绪，但生产仍停在旧稳定版本。当前不是 Query 逻辑阻塞，而是首次 Runtime Topology 切换前缺少一个独立的 durable ingress bootstrap holding layer。**

## 版本

- Repository：`jinxi1036-maker68/hermes-youyi-product`
- Code main：`3e9473d8083533387a9422a68f289793bb6d6585`
- Production：`588ea6eecb1833159e886181f3259be6e0befe37`
- Knowledge branch：`project-knowledge`

**main 比 production 新。不要把 main 已有能力误写成生产已部署。**

## 已封板

### Stage 1 Identity + Session
- PASS / SEALED
- stable tag：`youyi-stable-PASS-identity-authority-2026-09-23`

## 当前阶段

### Stage 2 Query
状态：**ACTIVE / near-complete**

业务逻辑主问题已经修到接近最终验收，但尚未进入 Owner 最终企业微信真人复测。

## Runtime Topology 当前状态

目标结构：

```text
/opt/.../releases/<commit>/       immutable code release
/var/lib/hermes-youyi/hermes-home persistent Hermes runtime state
/etc/hermes-youyi/                stable environment/config
Institution Workspace             business truth
Agenda root                       autonomous work/runtime business state
```

已经完成：
- Runtime Topology V1 代码
- explicit persistent Home
- migration plan / seed / verify
- service-identity preflight
- persistent Home gate
- self-contained gate
- Hermes Core compatibility gate
- release overlay policy
- Unix socket migration handling
- WeCom callback safe-drain

Persistent target Home 已创建并 seed：
`/var/lib/hermes-youyi/hermes-home`

仍未完成：
- finalize
- production HERMES_HOME switch
- production selector/systemd switch
- Query fix 实际部署
- Owner 真人 Query 验收

## 旧生产当前仍在运行

Production stable：
`588ea6eecb1833159e886181f3259be6e0befe37`

Legacy canonical Hermes Home：
`/opt/hermes-youyi-021-1.2.19-reply-recovery/home`

旧 Home / 旧 release 必须继续保留作回滚源；禁止把新 Home 反向同步覆盖旧 Home。

## 当前唯一生产切换核心阻塞

### Bootstrap durable ingress holding

真实 callback 链：

```text
public HTTPS
  -> aa-nginx:443
  -> hermes-cloud-hub:127.0.0.1:19090
  -> Gateway WeCom callback:127.0.0.1:8866
```

已确认：
- PR #17 safe-drain 本身 PASS；
- 但当前 production 588 不含 safe-drain；
- 不能为了“先安装 drain”裸重启旧 Gateway。

Cloud Hub 审计：
- 独立本地 Python 脚本；
- 不属于当前 Git repository；
- 有 fallback SQLite queue，但当前 direct-forward 到 8866 失败时不会自动进入 queue；
- 没有可靠 replay 到 Gateway；
- 没有 message-id / idempotency / dedupe 保障；
- 因此 **不是现成 safe bootstrap layer**。

下一步原则：
- 不直接在服务器游离脚本上随手打补丁；
- 先确定 bootstrap holding 应在哪个受 Git 管理的组件中实现；
- 必须保证 Gateway 暂停期间：可靠接收、持久保存、正确 ACK、恢复后 replay、重复处理可控。

## Query 最终验收条件

Runtime Topology 安全切换并部署新 main 后：

Owner 在企业微信发送：

`今天还有哪些事情没处理完？`

PASS 后：
- Stage 2 封板；
- 进入 Stage 3 Direct Message / 主动外发。

## 当前禁止并行混入

- /new 技术泄漏修复
- Student authority 全迁移
- query focus 严格零写
- create_task 旧 NameError
- H5
- Learning
- Voice/Hardware
- 其它不影响当前切换的历史技术债
