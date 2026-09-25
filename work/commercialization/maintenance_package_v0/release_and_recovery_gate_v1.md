# 小优版本化发布与恢复门禁 V1

## 目标

每次上线都能回答五个问题：上线的是哪个提交、包含哪些文件、生产实际从哪里加载、失败后如何恢复、第二家机构是否会被示例机构数据污染。

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

## 5. 非示例机构租户门禁

```text
python scripts/xiaoyou_non_youyi_tenant_gate.py
```

门禁使用临时 demo 租户验证身份、人员目录、任务写后反查、主动授权、日报、健康查询、Memory、12 领域工具和看板，并扫描示例机构人员、路径与租户标记。任何污染命中都禁止发布。

## 6. Runtime Topology V1

生产长期结构固定为：

```text
<release-root>/<commit>/                  代码、依赖、不可变发布物
/var/lib/hermes-youyi/hermes-home/        Hermes 持久运行状态
/etc/hermes-youyi/                        稳定环境文件和部署配置
<institution-workspace>/                  机构业务 Workspace
<agenda-root>/                            Agenda / 业务运行数据
```

硬规则：

- `HERMES_HOME` 不得位于任何版本化 release 内，也不得通过 symlink 指向旧 release；
- release 中的 console、Python/venv、`hermes_cli`、小优插件和 WeCom 插件必须全部解析到本次 release；
- `sessions/`、`cron/`、`state.db`、logs 等 Hermes profile 状态跨 release 持久化；
- Institution Workspace 与 Agenda/业务数据保持独立，本迁移不得顺手搬入 Hermes Home；
- secret 继续由受保护的 EnvironmentFile/secret store 提供，不复制到发布物。

### 6.1 一次性旧 Home 迁移

只在从旧 release-bound Home 切换到 Runtime Topology V1 时执行一次。

先只读规划：

```text
python scripts/xiaoyou_runtime_home_migration.py plan \
  --source-home <current-real-hermes-home>
```

任何未明确排除、且指向 source Home 外部的 symlink 都会 fail closed。Workspace/Agenda 必须继续独立。

种子复制必须由 Gateway 服务身份执行：

```text
sudo -u hermes-youyi python scripts/xiaoyou_runtime_home_migration.py seed \
  --release-root <candidate-release> \
  --source-home <current-real-hermes-home> \
  --target-home /var/lib/hermes-youyi/hermes-home
```

维护窗口中 Gateway 完全静止后，执行最终同步：

```text
sudo -u hermes-youyi python scripts/xiaoyou_runtime_home_migration.py finalize \
  --release-root <candidate-release> \
  --source-home <current-real-hermes-home> \
  --target-home /var/lib/hermes-youyi/hermes-home \
  --gateway-stopped-confirmed
```

`state.db` 使用 SQLite backup 建立一致副本，不直接复制 WAL/SHM。最终同步前不得仅凭文件 hash 判断 SQLite 业务语义。

迁移期间，Unix domain socket（例如 Gateway loop-tick socket）视为进程级临时端点：inventory 必须记录但不得复制，目标 Home 中不应保留该 socket，由新进程启动后自行重建。除明确识别的 Unix socket 外，其它未知特殊节点继续 fail-closed，不能静默跳过。

迁移后必须 verify。旧 Home 保持原样作为回滚源；首次新拓扑失败时只切回旧 selector + 旧 `HERMES_HOME`，禁止把新 Home 状态反向覆盖旧 Home。

### 6.2 外置 Runtime Home 门禁

```text
python scripts/xiaoyou_runtime_topology.py \
  --release-root <candidate-release> \
  --runtime-home /var/lib/hermes-youyi/hermes-home \
  --config-root /etc/hermes-youyi \
  --workspace-root <institution-workspace> \
  --agenda-root <agenda-root>

python scripts/xiaoyou_release_home_gate.py \
  --release-root <candidate-release> \
  --runtime-home /var/lib/hermes-youyi/hermes-home
```

Runtime Home 必须是直接目录、由 Gateway 服务身份持有、模式为 `0700`，并且关键可变状态可被服务身份读写。Runtime Home 顶层出现跨 release symlink 时门禁失败。

`--repair` 只允许修 Runtime Home 顶层目录自身 owner/group/mode；不得递归修 `sessions/cron/state.db/logs` 来掩盖污染。

### 6.3 服务身份预检

所有会触碰 Hermes Home 的 import、Plugin Doctor、启动探针，都必须通过：

```text
python scripts/xiaoyou_candidate_preflight.py \
  --release-root <candidate-release> \
  --runtime-home /var/lib/hermes-youyi/hermes-home \
  --cwd <candidate-release>/hermes-agent \
  -- <candidate-preflight-command>
```

预检必须以 Gateway 服务身份执行。预检完成后再次运行 Runtime Home Gate。

### 6.4 Hermes Core 兼容与生产 Overlay 门禁

候选必须从干净 Hermes Core runtime 组装，只允许叠加 release manifest 中明确允许的小优生产 payload。仓库内用于本地测试的兼容 shim（例如 `runtime/hermes_constants.py`、最小 gateway stubs）禁止覆盖 Hermes Core 正式模块。

在 Plugin Doctor 之前必须执行：

```text
python scripts/xiaoyou_core_compat_gate.py \
  --release-root <candidate-release> \
  --runtime-home /var/lib/hermes-youyi/hermes-home \
  --python <candidate-release>/.venv/bin/python
```

门禁使用候选自己的 Python 扫描 `hermes_cli` 对 `hermes_constants` 的实际导入，并要求：
- `hermes_constants` 来源位于候选 release 内；
- 不得检测到 `XIAOYOU_TEST_SHIM`；
- `hermes_cli` 实际导入的 Core API 必须全部存在；
- 扫描失败或缺失 API 时 fail closed。

### 6.5 Release 自包含门禁

```text
python scripts/xiaoyou_release_self_contained_gate.py \
  --release-root <candidate-release> \
  --runtime-home /var/lib/hermes-youyi/hermes-home \
  --python <candidate-release>/.venv/bin/python \
  --console <candidate-release>/.venv/bin/hermes
```

候选 Python、console、`hermes_cli`、小优插件、WeCom 插件或 `sys.path` 一旦解析到同级旧 release，发布失败。

## 7. 生产发布顺序

1. 固定回归与非示例机构门禁通过。
2. 构建并校验发布物，保存 archive SHA256。
3. 只读比较生产模块和发布清单。
4. 备份当前 selector、systemd 有效配置、Runtime Home 元数据和业务数据恢复点；不把 secret/业务数据写入 Git。
5. 对生产 Agenda 运行库建立只读语义快照。
6. Runtime Topology Gate PASS。
7. 外置 Runtime Home Gate PASS。
8. Core Compatibility Gate PASS，证明候选未被小优测试 shim 覆盖且 Hermes Core API 完整。
9. 通过服务身份执行 candidate import / Plugin Doctor / 无外发预检。
10. 预检后再次执行 Runtime Home Gate，必须 PASS。
11. Release Self-contained Gate PASS，证明不存在旧 release 代码解析。
12. 只有 6—11 全部 PASS 才允许低峰窗口最小切换和一次受控 Gateway 重启。
13. 核对实际 import 路径、Hermes 版本、19092/8866、日志、outbox 和 timer。
14. 用同一生产 Agenda 库执行部署后语义比较。
15. 出现重复回复、跨人记忆、未经授权外发、任务覆盖、日报失效、Core API 不兼容、旧 release 代码泄漏或未经解释的 Agenda 语义变化时立即回滚。

> SQLite 主库、`-wal`、`-shm` 的 mtime、大小或 SHA256 变化不能单独作为“业务数据被候选修改”的失败条件。
> WAL checkpoint 和 Agenda 心跳在稳定版本正常运行期间即可改变这些物理文件。文件级哈希仍可留作取证信息，但发布判定必须使用逻辑语义状态。
>
> 这条例外只适用于 Agenda SQLite 运行库及其 WAL/SHM。生产配置文件和普通业务 JSON 的既有哈希不变门禁继续严格执行。

## 边界

- 不运行 `hermes update`，不跟随 `main`。
- 不删除生产历史，不用旧文件覆盖完整业务账本。
- 不因发布自动扩大主动联系人范围。
- 模型继续负责判断和行动选择，发布系统只保证代码、证据和边界一致。
