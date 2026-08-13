# 小优版本化发布与恢复门禁 V1

## 目标

每次上线都能回答五个问题：上线的是哪个提交、包含哪些文件、生产实际从哪里加载、失败后如何恢复、第二家机构是否会被优益数据污染。

## 1. 构建单一发布物

在干净提交上运行：

```text
python scripts/xiaoyou_release_package.py --output-dir dist --hermes-version 0.20.0 --model agnes-2.5-flash
```

发布物包含小优业务插件、企业微信适配器、运行脚本和 systemd 模板，不包含业务数据、聊天、密钥、日志或备份。`release_manifest.json` 记录提交、Hermes 版本、模型和每个文件的 SHA256。

## 2. 安装前只读计划

```text
python scripts/xiaoyou_release_installer.py --release-root <解压后的发布目录> --base /opt/hermes-youyi-current
```

默认不安装。只要生产存在发布物没有收录的历史模块，计划就失败，必须先分类和确认，不能静默删除。通过后才能在独立上线窗口显式增加 `--apply`。

## 3. SQLite 运行时门禁

```text
/path/to/candidate/python scripts/xiaoyou_sqlite_runtime_check.py --minimum 3.51.3 --strict
```

检查只操作临时数据库，覆盖事务提交、回滚、备份、恢复和完整性检查。候选 Python 未通过时不得替换生产运行时，也不得碰生产 Hermes 数据库。

## 4. 恢复演练

```text
python scripts/xiaoyou_backup_restore_drill.py --source <临时租户目录> --work-dir <隔离目录>
```

恢复后逐文件比较 SHA256。未配置异机加密存储时，报告必须明确 `offsite_backup.enabled=false`，异机任务保持 disabled；不能把本地备份误报为异机灾备。

## 5. 非优益租户门禁

```text
python scripts/xiaoyou_non_youyi_tenant_gate.py
```

门禁使用临时 demo 租户验证身份、人员目录、任务写后反查、主动授权、日报、健康查询、Memory、12 领域工具和看板，并扫描优益人员、路径与租户标记。任何污染命中都禁止发布。

## 6. 生产发布顺序

1. 固定回归与非优益门禁通过。
2. 构建并校验发布物，保存 archive SHA256。
3. 只读比较生产模块和发布清单。
4. 备份代码、Home、业务数据、systemd 和哈希清单。
5. 影子环境执行导入、真实模型和无外发回放。
6. 低峰窗口最小切换，一次受控重启。
7. 核对实际 import 路径、Hermes 版本、模型、日志、outbox 和 timer。
8. 出现重复回复、跨人记忆、未经授权外发、任务覆盖或日报失效时立即回滚。

## 边界

- 不运行 `hermes update`，不跟随 `main`。
- 不删除生产历史，不用旧文件覆盖完整业务账本。
- 不因发布自动扩大主动联系人范围。
- 模型继续负责判断和行动选择，发布系统只保证代码、证据和边界一致。
