# Hermes v0.20 小优服务器影子验证报告

日期：2026-08-11  
结论：**影子验证通过，具备申请生产升级确认的条件；生产尚未升级。**

## 一、老板先看这里

- Hermes v0.20 已在服务器的独立目录中安装和验证，不是本地模拟。
- 生产仍运行 Hermes v0.19，服务、定时器、企业微信入口和业务数据均未切换。
- 影子环境使用真实模型完成了 3 次连续调用，并完成 1 次自主唤醒闭环。
- 早报、晚报只做 dry-run，没有进入生产 outbox，也没有真实外发。
- 小优插件在 v0.20 下成功注册 98 个托管业务工具和必要生命周期钩子。
- 终端、文件写、浏览器写、Session Search、定时任务创建等高风险能力保持关闭。
- 全量小优回归为 `174 passed`；demo 租户验收为 `STATUS:PASS`。
- 回滚演练通过，v0.19 目标可恢复，演练耗时低于 10 分钟。

当前只差老板明确批准生产切换。批准前不得运行生产升级、不得改 systemd、不得重启生产服务。

## 二、验证基线

| 项目 | 固定版本 |
|---|---|
| Hermes 官方标签 | `v2026.8.3` |
| Hermes 版本 | `0.20.0` |
| Hermes 固定提交 | `3c27eb6234bf91b8ceee9e9071591b31e9b148cb` |
| 小优验证起点 | `153a83be3bd95ba01baa4f11a3e7941f0a9df055` |
| 小优最终兼容提交 | `1545811572befc40242f720ec206dbf3b20b4086` |
| Python | `3.11.13` |
| 生产当前版本 | `hermes-agent 0.19.0` |

本轮新增兼容提交：

- `9dcbf6f`：隔离社交市场影子后端，测试失败时不逃逸到真实浏览器。
- `9a693c3`：增加 Hermes v0.20 插件清单、回复守卫和审计钩子兼容。
- `1545811`：增强自主模型失败语义，并消除自主循环中的优益老板账号硬编码。

## 三、影子环境

影子根目录：`/opt/hermes-youyi-shadow-0.20.0`

- 官方源码：`hermes-agent/`
- 独立 Python 环境：`.venv/`
- 独立 Hermes Home：`home-shadow/`
- 合成业务数据：`data-shadow/`
- 小优源码：`xiaoyou-src/`
- v0.20 与小优合并运行层：`runtime-merged/`
- 报告：`reports/`
- 安装包和依赖锁：`artifacts/`

影子环境未启动企业微信监听、未占用生产端口、未创建 systemd unit 或 timer。它只获得模型供应商凭据，没有企业微信、Firecrawl 或发送凭据。

## 四、关键验证结果

### 1. 版本与依赖

- 官方标签解析提交与固定提交完全一致。
- v0.20 以官方支持的 editable 方式安装成功。
- 依赖冻结文件已生成：`artifacts/pip-freeze-final.txt`。
- 依赖锁 SHA256：`a3a0204f202ba44ab7ae936322f3cefbf6b8797022a0ed1396826ee0679a2322`。

### 2. 插件、Skills 与工具边界

- `tuoguan_core`：enabled，无加载错误。
- 注册托管业务工具：98 个。
- 注册钩子：`pre_llm_call`、`post_tool_call`、`transform_llm_output`、`post_llm_call`。
- `wecom` 与 `wecom_callback` 平台适配器均可发现，但没有启动监听。
- `xiaoyou-core` Bundle 与 8 项员工手册 Skills 可发现并启用。
- `USER.md` 只保留中性规则，没有老板、老师混合偏好。
- Session Search、terminal、file、browser、code execution、delegation、cronjob 均关闭。

### 3. 对话与可靠性回归

- 全量回归：`174 passed in 33.14s`。
- 覆盖身份隔离、重复 MsgId、短回复锚定、任务上下文、个人工作方式、日报、自主工作、外部学习和租户验收。
- 新增故障测试覆盖：连续 429、模型超时、截断 JSON。
- 失败时返回失败或降级，不生成假成果，不进入外发链路。

### 4. 真实模型与自主唤醒

- 真实模型连续 3 次调用全部成功。
- 自主唤醒真实运行一次，正确识别 demo 机构缺少机构名称、人员、学生、目标和制度事实。
- 没有把空数据编造成业务进展，没有创建任务，没有写生产数据，没有产生 outbox 消息。
- 首轮发现自主查询硬编码 `JinWenJie` 会污染新租户；修复后重新运行，污染扫描通过，demo acceptance 恢复 PASS。

### 5. 早晚报模拟

- 早报和晚报均能针对合成老板 `ShadowBoss` 正常生成。
- 运行模式为 dry-run，没有 drain outbox，没有真实发送。
- 生产 `notification_outbox.json` 与 `daily_report_runs.jsonl` 在测试前后 SHA256 完全一致。

### 6. 回滚演练

- 演练范围只在影子目录，不修改生产软链接或 systemd。
- Home 备份、模拟破坏、恢复和哈希反查通过。
- 测试指向可从 v0.20 切回 `/opt/hermes-youyi-upgrade-0.19.0`。
- v0.19 包版本核验为 `0.19.0`，演练低于 10 分钟。

## 五、安装包与哈希

| 文件 | SHA256 |
|---|---|
| `artifacts/hermes-agent-v0.20.0-3c27eb6.tar` | `1e9319c58a7f5e95808546af1091d58472be7437adc63fae0cbb53316e2711aa` |
| `artifacts/xiaoyou-v020-shadow-final-1545811.tar` | `1cfa75cc51c2478d0c6a3c4414a0a0ed5883366fd19b6810d51d666208550f64` |
| `artifacts/pip-freeze-final.txt` | `a3a0204f202ba44ab7ae936322f3cefbf6b8797022a0ed1396826ee0679a2322` |

## 六、生产只读复核

- `hermes-youyi-019.service`：`active/running`。
- 实际加载目录：`/opt/hermes-youyi-upgrade-0.19.0`。
- 影子验证后没有影子 gateway、企业微信监听、端口或 systemd 服务残留。
- 最近日志未发现 `Traceback`、`ImportError`、`ModuleNotFoundError`、`ERROR`、`Exception`、`unauthorized`、`denied`、`forbidden`。
- 生产 outbox 与日报账本测试前后哈希一致。

## 七、已知风险

- v0.20 尚未在真实企业微信入口处理生产消息；该验证只能在获得批准后的低使用窗口完成。
- 生产升级会改变 Hermes Home、插件加载和 systemd 指向，必须完整备份并保持 v0.19 Home 不变。
- v0.20 当前来自官方固定标签源码，不是 PyPI 稳定包；后续升级必须继续锁定提交和安装包哈希。
- 生产切换后仍需观察至少 3 个自然自主唤醒周期和下一次早报或晚报，期间只使用金总和李老师测试号。

## 八、门禁决定

影子环境的硬检查全部通过，当前状态为：**可以申请生产升级确认，但不能自动升级。**

获得老板明确确认后，按单独生产计划在北京时间 22:10 执行：完整备份、构建 v0.20 独立生产目录、建立版本中立路径、停止写入型 timer、切换、一次受控重启、金总与李老师测试、观察自然周期。出现启动失败、重复回复、跨人记忆、任务覆盖、未经授权外发、日报失效或自主循环假成功时，立即切回 v0.19。
