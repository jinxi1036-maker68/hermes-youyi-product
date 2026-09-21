# 未来初始化脚本规格 V0

目标：定义 `tenant_initializer` 脚本应该做什么。当前 V0 已实现本地干跑版，只用于生成 demo 租户目录和初始化报告；它不是生产安装器。

## 1. 脚本定位

脚本名称建议：

```text
tenant_initializer.py
```

职责：

- 读取 `tenant_profile.json`。
- 创建租户目录。
- 生成新机构配置、初始 Memory、机构 Skill 和空白账本。
- 输出初始化报告。
- V0 不启动真实服务；`--start-services` 会被拒绝。

## 2. 输入

必需：

```text
--tenant-profile /path/to/tenant_profile.json
--platform-root /opt/hermes-platform
```

可选：

```text
--tenant-id demo_tuoguan
--dry-run
--force
--start-services
--report-dir /path/to/reports
```

安全规则：

- 默认 dry-run 优先。
- 没有 `--force` 时，目标租户目录已存在则停止。
- 不读取 `/opt/hermes-youyi/data/tuoguan-data`。
- 不复制任何生产业务数据。
- V0 只建议输出到 `work/commercialization/demo_output/`。

## 3. 输出

生成：

- 租户目录结构。
- `config/tenant_profile.json`
- `config/runtime.env`
- `data/wecom_whitelist.json`
- `data/teacher_wecom_map.json`
- `data/staff.json`
- `data/institution_operating_model.json`
- `memory/MEMORY.md`
- `skills/institution-facts/SKILL.md`
- 空白 JSONL 账本。
- `reports/startup/initialization-report-{timestamp}.md`

## 4. 校验

脚本必须检查：

- `tenant_id` 只包含小写字母、数字、下划线和短横线。
- 机构名称不为空。
- 至少有一个老板。
- 老板有企业微信 user_id。
- 企业微信密钥使用引用，不直接写明文。
- `tenant_profile.json` 不包含示例机构标记。

禁止标记包括：

- `示例机构`
- `机构负责人`
- `示例老师`
- `owner_test`
- `example_institution`
- `九月份续费率`

这些词如果出现在“禁止携带说明”以外的输入字段中，脚本应停止。

## 5. 失败处理

- 任一核心文件生成失败，脚本返回失败。
- 不启动服务。
- 生成 `initialization_failed` 报告。
- 保留已生成文件用于排查。
- 不自动删除租户目录，避免误删。

## 6. 首次验收输出

脚本完成后输出：

- 目录清单。
- 生成文件清单。
- 空白账本清单。
- 禁止携带检查结果。
- 待人工补充信息。
- 下一步首次启动命令建议。

## 7. 后续扩展

后续可以加入：

- systemd 服务生成。
- H5 token 生成。
- 企业微信回调配置检查。
- demo 数据导入。
- 首次启动自动验收。
- 中央运维平台注册。
