# 最小升级与回滚流程 V0

目标：当必须把本地修复同步到生产时，只同步必要文件，并保留可追溯备份和回滚路径。

## 1. 升级前本地条件

必须满足：

- `git status --short --branch` 干净或只有本次明确变更。
- 本地 demo tenant 初始化和 acceptance 通过。
- 相关窄回归通过。
- `git diff --check` 通过。
- 敏感扫描确认无生产业务数据、聊天、学生老师资料、企业微信密钥进入 Git。

## 2. 升级前生产只读核对

```text
ssh hermes-aliyun "systemctl is-active hermes-youyi-019.service"
ssh hermes-aliyun "systemctl cat hermes-youyi-019.service"
ssh hermes-aliyun "journalctl -u hermes-youyi-019.service -n 120 --no-pager"
```

记录：

- 服务名：
- 生产目录：
- 实际 Python/site-packages 加载路径：
- 当前文件 hash：
- 最近日志是否有 import/traceback：

## 3. 备份

在版本中立生产目录创建备份目录：

```text
/opt/hermes-youyi-current/backups/deploy-<commit>-minimal-<timestamp>/
```

备份规则：

- 只备份本次要覆盖的文件。
- 缺失文件记录为 `missing_before_deploy`。
- 不备份业务数据到 Git。

## 4. 最小同步

正式发布优先使用版本化发布包和受控链接。兼容期最小同步只允许同步本次修复必需文件到：

- runtime 源码目录。
- 实际加载的 `.venv/lib/python3.11/site-packages/plugins/tuoguan_core`。

禁止：

- 不清理生产额外模块。
- 不迁移数据目录。
- 不修改 systemd unit。
- 不修改 timer。
- 不触发真实外发。

## 5. 重启前验证

```text
ssh hermes-aliyun "cd /opt/hermes-youyi-current && .venv/bin/python -m py_compile <synced_files>"
ssh hermes-aliyun "cd /opt/hermes-youyi-current && .venv/bin/python - <<'PY'\nimport plugins.tuoguan_core\nprint('import ok')\nPY"
```

## 6. 受控重启

只有本地测试、备份、同步后 import 验证都通过，才允许一次服务重启：

```text
ssh hermes-aliyun "sudo systemctl restart hermes-youyi-019.service"
```

重启后必须立即验证：

```text
ssh hermes-aliyun "systemctl is-active hermes-youyi-019.service"
ssh hermes-aliyun "journalctl -u hermes-youyi-019.service -n 200 --no-pager"
```

日志中不得出现明显 `ImportError`、`ModuleNotFoundError` 或连续 `Traceback`。

## 7. 回滚

如果重启后 P0 失败：

- 停止继续操作。
- 从本次备份目录恢复覆盖文件。
- 再执行一次受控重启。
- 核对服务 active 和日志。
- 记录失败版本、失败文件和失败原因。

## 8. 升级完成记录

- commit：
- 备份目录：
- 同步文件：
- py_compile：
- import：
- 重启结果：
- 日志结论：
- dry-run 结果：
- 是否发生真实外发：
