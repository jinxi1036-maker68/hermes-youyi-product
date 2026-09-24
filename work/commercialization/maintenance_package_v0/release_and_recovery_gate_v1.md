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

## 6. 生产发布顺序

1. 固定回归与非示例机构门禁通过。
2. 构建并校验发布物，保存 archive SHA256。
3. 只读比较生产模块和发布清单。
4. 备份代码、Home、业务数据、systemd 和哈希清单。
5. 影子环境执行导入、真实模型和无外发回放。
6. 对生产 Agenda 运行库建立只读语义快照：

   ```text
   python scripts/xiaoyou_agenda_semantic_gate.py snapshot \
     --database <workspace>/data/agenda_work_runtime.sqlite \
     --output /tmp/agenda-before.json
   ```

7. 候选 release 组装完成后、切换生产前，必须执行 Runtime Home 权限门禁：

   ```text
   python scripts/xiaoyou_release_home_gate.py --release-root <candidate-release>
   ```

   门禁要求：
   - release root 必须可被 Gateway 运行用户 traverse；
   - `home/` 必须由实际 Gateway 运行账号持有，当前生产为 `hermes-youyi:hermes-youyi`；
   - `home/` 模式必须为 `0700`；
   - `home/` 不能是 symlink；
   - 门禁只检查目录元数据，不读取 `.env` 或 secret 内容；
   - 不通过时禁止切换生产。

   若候选组装过程把 `home/` 错建为 root 所有，只允许在候选目录上执行窄修复：

   ```text
   python scripts/xiaoyou_release_home_gate.py --release-root <candidate-release> --repair
   ```

   repair 只允许修候选 `home/` 目录本身的 owner/group/mode，不递归修改 Home 内容，不复制 secret，不放宽为 world-readable。修复后必须再次无 `--repair` 运行门禁并通过。

8. 所有会接触候选 `HOME/HERMES_HOME` 的 import、Plugin Doctor、Hermes 启动探针或其它运行预检，必须通过服务身份预检执行器运行：

   ```text
   python scripts/xiaoyou_candidate_preflight.py \
     --release-root <candidate-release> \
     --cwd <candidate-release>/hermes-agent \
     -- <candidate-preflight-command>
   ```

   执行器必须把 `HOME` 与 `HERMES_HOME` 固定到候选 `home/`，并确保实际预检进程以 Gateway 服务身份运行；当前生产服务身份为 `hermes-youyi:hermes-youyi`。禁止以 root 身份直接运行会初始化候选 Home 的 import/Doctor/启动预检。

9. 所有候选运行预检完成后，必须再次运行 Runtime Home 门禁。该门禁除 `home/` 本身外，还检查已生成的可变运行状态 `sessions/` 与 `cron/` 及其现有内容：
   - 必须由 Gateway 服务身份持有；
   - 必须对 Gateway 服务身份保持所需读写/遍历权限；
   - 不读取文件内容；
   - 不允许 symlink 逃逸。
   
   若此时发现 `sessions/`、`cron/` 或其文件被 root/其它身份创建，禁止递归 chown/chmod 后继续发布；必须丢弃被污染的候选 Runtime Home、重新组装候选，并使用正确服务身份重新执行预检。

10. 只有 Runtime Home 门禁、服务身份预检和预检后 Runtime Home 复检全部 PASS，才允许低峰窗口最小切换和一次受控重启。
11. 核对实际 import 路径、Hermes 版本、模型、日志、outbox 和 timer。
12. 用同一生产库执行部署后语义比较：

   ```text
   python scripts/xiaoyou_agenda_semantic_gate.py compare \
     --before /tmp/agenda-before.json \
     --database <workspace>/data/agenda_work_runtime.sqlite \
     --after-output /tmp/agenda-after.json
   ```

   `agenda_runtime_status` 和 `agenda_runtime_daily_status` 的正常心跳/统计变化允许通过。
   其它用户表默认按业务/工作语义状态处理；ticket、work fact、payload、binding、wake batch、
   receipt、reply/delivery、lease 或未知未来非心跳表发生变化时门禁失败并进入人工核验或回滚。

13. 出现重复回复、跨人记忆、未经授权外发、任务覆盖、日报失效或未经解释的 Agenda 语义状态变化时立即回滚。

> SQLite 主库、`-wal`、`-shm` 的 mtime、大小或 SHA256 变化不能单独作为“业务数据被候选修改”的失败条件。
> WAL checkpoint 和 Agenda 心跳在稳定版本正常运行期间即可改变这些物理文件。文件级哈希仍可留作取证信息，但发布判定必须使用逻辑语义状态。
>
> 这条例外只适用于 Agenda SQLite 运行库及其 WAL/SHM。生产配置文件和普通业务 JSON 的既有哈希不变门禁继续严格执行，不能因为引入语义门禁而放宽。

## 边界

- 不运行 `hermes update`，不跟随 `main`。
- 不删除生产历史，不用旧文件覆盖完整业务账本。
- 不因发布自动扩大主动联系人范围。
- 模型继续负责判断和行动选择，发布系统只保证代码、证据和边界一致。
